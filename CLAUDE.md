# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Rally OpenStack Monitor — a containerized tool that runs OpenStack cloud tests via Rally on a cron schedule, exposes results as Prometheus metrics, detects orphaned resources from failed cleanups, and serves a read-only dark-theme dashboard.

## Commands

### Build and Run

```bash
# Build and start the container
cd docker && docker compose up -d --build

# Trigger a manual test run inside the running container
docker exec -u rally rally-monitor /scripts/run_tests.sh

# Run a lightweight health check (non-destructive read-only API calls)
docker exec -u rally rally-monitor /scripts/health_check.sh

# Run cleanup orphan detection manually
docker exec -u rally rally-monitor /scripts/cleanup_monitor.sh

# Purge orphaned Rally resources (dry-run — shows what would be deleted)
docker exec -u rally rally-monitor /scripts/purge_orphans.sh

# Purge orphaned Rally resources (actually deletes them)
docker exec -u rally rally-monitor /scripts/purge_orphans.sh --confirm

# View live logs
docker logs -f rally-monitor
docker exec rally-monitor tail -f /rally/logs/rally-tests.log
docker exec rally-monitor tail -f /rally/logs/health-check.log
```

### Exporter (local dev, outside Docker)

```bash
cd exporter
pip install -r requirements.txt
RESULTS_DIR=./results python rally_exporter.py
```

### Setup

```bash
cp env.sample .env
# Fill in OS_AUTH_URL, OS_USERNAME, OS_PASSWORD, OS_PROJECT_NAME
```

## Architecture

The entire system runs in a single Docker container (`rally-monitor`) with three concurrent processes managed by `scripts/entrypoint.sh`:

1. **Prometheus Exporter** (`exporter/rally_exporter.py`) — Flask app on `:9101` that reads JSON files from `/results/` and exposes them as Prometheus gauge metrics on `/metrics`. Also serves `/health` and `/api/results`, `/api/history`.

2. **Dashboard** (`dashboard/`) — Static files served by Python's `http.server` on `:8080`. The dashboard is pure HTML/JS/CSS with no build step; it fetches JSON from symlinked files (`results.json`, `history.json`, `health.json`, `health_history.json`) in the same directory.

3. **Cron Jobs** — Two cron schedules:
   - Rally test runs (`RALLY_SCHEDULE_INTERVAL`, default 240 min) → `scripts/run_tests.sh`
   - API health checks (`HEALTH_CHECK_INTERVAL`, default 15 min) → `scripts/health_check.sh`

### Data Flow

```
run_tests.sh
  → rally task start <scenario>.yaml   (for each of 6 services)
  → poll live c_rally_* projects       (append IDs to /results/rally_project_ids.log)
  → rally task results <uuid>          (JSON to /results/<timestamp>/<service>.json)
  → build_summary()                    → /results/latest_summary.json
  → auto_purge_rgw()                   (deletes rally-owned RGW orphans; no-op without RGW creds)
  → cleanup_monitor.sh                 → /results/cleanup_metrics.json
  → publish_dashboard_files()          → /results/results.json, history.json

health_check.sh
  → openstack <service> list (read-only)
  → /results/health.json, health_history.json (rolling 672-entry window)

rally_exporter.py
  → reads /results/latest_summary.json + cleanup_metrics.json on each /metrics scrape
  → updates Prometheus Gauges in memory (including RGW orphan/scan health)
```

### File Layout (runtime volumes)

- `/results/` — Docker volume `rally-results`; persists across restarts
  - `latest_summary.json` — current run summary (seed file created on first boot)
  - `cleanup_metrics.json` — orphaned resource counts from `cleanup_monitor.sh`
  - `rally_project_ids.log` — append-only ledger of Rally-created Keystone project IDs for RGW purge provenance
  - `results.json` — combined summary + cleanup for dashboard
  - `history.json` — all retained per-run summaries for the timeline
  - `health.json` / `health_history.json` — API health check results
  - `<TIMESTAMP>/` — per-run directories with `<service>.json`, `<service>.html`, `run.log`
- `/rally/data/` — Docker volume `rally-data`; SQLite DB for Rally state
- `/dashboard/` — ephemeral; `results.json` etc. are symlinks → `/results/`

### Theme Customization

Dashboard theming follows a narrow `themes/<name>/` contract without a build step. The shipped default theme is `/dashboard/themes/default/` and contains `tokens.css`, `logo.svg`, and `favicon.svg`. At container startup, `scripts/entrypoint.sh` ensures `/results/branding/` exists and recreates `/dashboard/themes/custom -> /results/branding`.

`dashboard/index.html` loads styles in this cascade order: `themes/default/tokens.css`, `style.css`, `themes/custom/tokens.css`, then `themes/custom/overrides.css`. Custom files are optional and partial; missing custom assets fall back to defaults. `dashboard/app.js` probes `themes/custom/logo.svg` and `themes/custom/favicon.svg` with GET at boot, then swaps the header logo and favicon only when those files exist.

`dashboard/serve.py` keeps a deny-by-default allowlist. Theme paths must live under `themes/`, use one of `.css`, `.svg`, `.png`, or `.ico`, and resolve either under `/dashboard/themes/` or under the tighter `/results/branding/` custom-theme root. Do not widen this to all of `/results/`.

Public theme tokens are documented in `docs/CUSTOMIZING.md`. Internal glass/radius/shadow tokens and decorative alpha literals are not stable API. SVGs loaded via `<img>` do not inherit page CSS variables, so a custom theme must supply its own `logo.svg` to recolor the brand mark.

### Announcement System

Operator-authored dashboard banners are managed by `scripts/announce.sh` — a bash + jq CLI invoked via `docker exec -u rally rally-monitor /scripts/announce.sh ...`. State lives at `/results/announcement-state.json` (top-level shape: `{"announcements": [...]}`), written atomically via `tmpfile + mv`. No HTTP write endpoint is introduced; authorization equals "you can `docker exec -u rally` on the host."

The state file lives directly under `/results/` rather than under `/results/branding/` to avoid a collision with the documented read-only theme bind-mount pattern (`./my-theme:/results/branding:ro` in `docs/CUSTOMIZING.md`), which would otherwise silently break every write.

`scripts/entrypoint.sh` creates `/dashboard/announcement-state.json -> /results/announcement-state.json` unconditionally at startup; the file appears lazily on the first `post`, and `serve.py`'s `target.exists()` check 404s on the dangling symlink until then. The dashboard fetches via the same GET surface as other JSON files: one entry in `ALLOWED_JSON_SYMLINKS`.

The CLI supports five subcommands: `post`, `update`, `clear`, `list`, and `auto-clear-if-all-green`. The last is invoked from `scripts/run_tests.sh::main` after `build_summary` and before `publish_dashboard_files`; it removes every `incident`-type record when the post-run summary is unambiguously all-green. The predicate is `(.services | length) > 0 AND (.error // null) == null AND (.services | to_entries | all(.value.status == "passed"))`. The empty-services and `.error` guards are critical — a naive `all(...)` returns `true` on the `{"services": {}, "error": "deployment_setup_failed"}` shape and would silently erase the incident banner during the exact failure mode it exists to communicate.

Mutating subcommands wrap their read-modify-write cycle in `flock` on `/results/.announce.lock`. Without this, an operator `post` running between a cron-driven `auto-clear-if-all-green`'s read and write would be silently overwritten. `list` is unlocked so it never blocks on a stuck mutator.

The dashboard renders one banner above the 7-day timeline. Body text is rendered via `textContent` (no Markdown, no HTML); a `<details>` element exposes progress updates and other concurrent records. Each banner carries a visible bracketed type label (`[INCIDENT]` / `[MAINTENANCE]` / `[SCHEDULED]`) so color is not the sole signal — color tokens reused from the public theme contract: `--color-failure` (incident), `--color-warning` (maintenance), `--color-brand-secondary` (scheduled). Auto-refresh preserves `<details>` open state when the same primary announcement is still active.

### Orphan Detection

`scripts/cleanup_monitor.sh` runs after each Rally test suite. It queries each OpenStack service for resources prefixed with `s_rally` (scenario resources) or `c_rally` (context resources — projects, users, networks created by Rally contexts) and writes counts to `cleanup_metrics.json`. When `RGW_ADMIN_URL`, `RGW_ACCESS_KEY`, and `RGW_SECRET_KEY` are configured, it also queries the RGW admin REST API for orphaned implicit-tenant users and marks the scan as `ok`, `skipped`, or `error`. The exporter exposes these as OpenStack cleanup gauges plus `rally_rgw_*` metrics.

After the summary is built, `run_tests.sh` automatically purges rally-owned RGW orphans via `auto_purge_rgw()`, then refreshes `cleanup_metrics.json` so dashboard and Prometheus state reflect the post-purge state. This only runs when RGW admin credentials are configured. It is fail-closed: if the orphan scan had errors or Keystone lookups were inconclusive, purge is skipped entirely. Only users whose project IDs appear in `rally_project_ids.log` are eligible for deletion. Unknown-owner orphans are never touched. For non-RGW resources, `purge_orphans.sh` remains the manual cleanup tool.

### Rally Scenarios

Located in `rally/scenarios/` (6 services: keystone, nova, neutron, glance, cinder, swift). Parameterized via `--task-args-file` with `RALLY_NOVA_FLAVOR`, `RALLY_NOVA_IMAGE`, and `OS_AUTH_URL` from the environment.

### Key Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `OS_AUTH_URL` | — | Keystone endpoint |
| `OS_USERNAME` / `OS_PASSWORD` / `OS_PROJECT_NAME` | — | Credentials |
| `RALLY_SCHEDULE_INTERVAL` | `240` | Minutes between full test runs |
| `HEALTH_CHECK_INTERVAL` | `15` | Minutes between lightweight health checks |
| `RALLY_RESULTS_RETENTION_DAYS` | `7` | Days before old run directories are pruned |
| `RALLY_NOVA_FLAVOR` / `RALLY_NOVA_IMAGE` | `m1.tiny` / `cirros-...` | Nova scenario inputs |
| `RGW_ADMIN_URL` / `RGW_ACCESS_KEY` / `RGW_SECRET_KEY` | — | Optional RGW admin API config for orphan detection and purge |
| `RGW_REGION` | unset | Optional explicit SigV4 region for RGW admin requests |
| `EXPORTER_PORT` / `DASHBOARD_PORT` | `9101` / `8080` | Exposed ports |
| `RALLY_DEBUG` | `false` | Set to `true` for verbose rally task logging |

### Cron Environment

Environment variables are exported to `/rally/rally_env` (mode 0640) at container startup and sourced by cron jobs. The file contains `OS_PASSWORD` and (when configured) `RGW_SECRET_KEY` — do not loosen its permissions.

### patch_rally.py

`scripts/patch_rally.py` is applied at Docker build time to patch Rally's internal password-generation policy. It runs once inside the image build (via `RUN python3 /scripts/patch_rally.py` in the Dockerfile) and does not need to be re-run manually.

### RGW Helpers

`scripts/rgw_helpers.sh` is a source-only bash library used by both `cleanup_monitor.sh` and `purge_orphans.sh`. It wraps the RGW admin REST API via `curl --aws-sigv4` (no Ceph CLI needed). Key functions: `rgw_list_implicit_users` (paginated user listing), `rgw_find_orphaned_users` (cross-references RGW against Keystone with bulk pre-fetch), `rgw_classify_owner` (checks Rally provenance ledger), `rgw_delete_bucket`/`rgw_delete_user` (idempotent deletion). All deletion is gated on Rally ownership — only project IDs recorded in `rally_project_ids.log` are eligible for purge.

### Prometheus Integration

- Scrape target: `<host>:9101/metrics`
- Alert rules: copy `prometheus/rally_alerts.yml` to your Prometheus rules directory and add it under `rule_files:` in `prometheus.yml`

<!-- code-review-graph MCP tools -->
## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
|------|----------|
| `detect_changes` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context` | Need source snippets for review — token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern="tests_for" to check coverage.
