# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Rally OpenStack Monitor — a containerized tool that runs OpenStack cloud tests via Rally on a cron schedule, exposes results as Prometheus metrics, detects orphaned resources from failed cleanups, and serves a read-only dark-theme dashboard.

## Commands

### Build and Run

```bash
# Pull the CI-published image and start the container (default)
cd docker && docker compose pull && docker compose up -d

# Or build locally instead of pulling (tagged with the same image name)
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

1. **Prometheus Exporter** (`exporter/rally_exporter.py`) — Flask app on `:9101` that reads JSON files from `/results/` and exposes them as Prometheus gauge metrics on `/metrics`. Also serves `/health` (liveness) and `/ready` (readiness).

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
  → record_smoke_result()              → /results/smoke_history.json (rolling uptime ledger)
  → auto_purge_rgw()                   (deletes rally-owned RGW orphans; no-op without RGW creds)
  → cleanup_monitor.sh                 → /results/cleanup_metrics.json
  → publish_dashboard_files()          → /results/results.json, history.json

health_check.sh
  → openstack <service> list (read-only)
  → /results/health.json, health_history.json (rolling window sized to cover
    UPTIME_WINDOW_DAYS at HEALTH_CHECK_INTERVAL, min 672 entries; carries an
    embedded uptime object computed by scripts/health_history_filter.jq)

rally_exporter.py
  → reads /results/latest_summary.json + cleanup_metrics.json on each /metrics scrape
  → updates Prometheus Gauges in memory (including RGW orphan/scan health)
```

### File Layout (runtime volumes)

- `/results/` — Docker volume `rally-results`; persists across restarts
  - `latest_summary.json` — current run summary (seed file created on first boot)
  - `cleanup_metrics.json` — orphaned resource counts from `cleanup_monitor.sh`
  - `rally_project_ids.log` — append-only ledger of Rally-created Keystone project IDs for RGW purge provenance
  - `smoke_history.json` — rolling per-run `{timestamp, status}` uptime ledger with embedded uptime figures; pruned by `UPTIME_WINDOW_DAYS`, independent of run-directory retention
  - `results.json` — combined summary + cleanup + smoke uptime for dashboard
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

### Uptime Tracking

The dashboard shows two uptime badges (in the timeline section headers), both computed over `UPTIME_WINDOW_DAYS` (default 30):

- **Smoke-test uptime** — `run_tests.sh::record_smoke_result` appends `{timestamp, status}` to `/results/smoke_history.json` after every run (including the deployment-failure path) and recomputes the embedded `uptime` object. A run is `passed` only under the same all-green predicate `announce.sh` uses (non-empty services, no `.error`, all passed) — the predicate is duplicated in both scripts with keep-in-sync cross-references. The ledger is pruned by timestamp, so uptime coverage is independent of `RALLY_RESULTS_RETENTION_DAYS`. `publish_dashboard_files` embeds `uptime` into `results.json`; `record_smoke_result` also syncs it directly into an existing `results.json` so the deployment-failure path (which exits before publish) still updates the badge. The failure path deliberately does not run a full publish: the empty-services failure summary would render as "All Healthy" via `getRunStatus`.
- **API uptime** — `health_check.sh` applies `scripts/health_history_filter.jq` (via `jq -f`, so tests exercise the shipped filter) to append the check, cap stored history (`UPTIME_WINDOW_DAYS * 24 * 60 / HEALTH_CHECK_INTERVAL`, floored at 672 so the 7-day health timeline never shrinks), and embed `uptime` into `health_history.json`. The count cap bounds storage; the timestamp cutoff defines the uptime window — they are intentionally separate.

`dashboard/app.js::renderUptimeBadge` hides a badge until its `uptime.percent` is a number, so first boots and pre-feature data files render unchanged. The latency chart plots at most `HEALTH_CHART_MAX_POINTS` (672) checks regardless of stored history size. Uptime is dashboard-only by design — Prometheus users can derive it with `avg_over_time()` on existing gauges.

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
| `UPTIME_WINDOW_DAYS` | `30` | Window in days for dashboard uptime percentages; sizes the smoke ledger and the health history cap |
| `RALLY_NOVA_FLAVOR` / `RALLY_NOVA_IMAGE` | `m1.tiny` / `cirros-...` | Nova scenario inputs |
| `RGW_ADMIN_URL` / `RGW_ACCESS_KEY` / `RGW_SECRET_KEY` | — | Optional RGW admin API config for orphan detection and purge |
| `RGW_REGION` | unset | Optional explicit SigV4 region for RGW admin requests |
| `EXPORTER_PORT` / `DASHBOARD_PORT` | `9101` / `8080` | Exposed ports |
| `RALLY_DEBUG` | `false` | Set to `true` for verbose rally task logging |
| `RALLY_MONITOR_IMAGE` | `ghcr.io/ching-kuo/openstack-rally-monitor:latest` | Compose-only: image tag to pull/run |

### Cron Environment

Environment variables are exported to `/rally/rally_env` (mode 0640) at container startup and sourced by cron jobs. The file contains `OS_PASSWORD` and (when configured) `RGW_SECRET_KEY` — do not loosen its permissions.

### patch_rally.py

`scripts/patch_rally.py` is applied at Docker build time to patch Rally's internal password-generation policy. It runs once inside the image build (via `RUN python3 /scripts/patch_rally.py` in the Dockerfile) and does not need to be re-run manually.

### RGW Helpers

`scripts/rgw_helpers.sh` is a source-only bash library used by both `cleanup_monitor.sh` and `purge_orphans.sh`. It wraps the RGW admin REST API via `curl --aws-sigv4` (no Ceph CLI needed). Key functions: `rgw_list_implicit_users` (paginated user listing), `rgw_find_orphaned_users` (cross-references RGW against Keystone with bulk pre-fetch), `rgw_classify_owner` (checks Rally provenance ledger), `rgw_delete_bucket`/`rgw_delete_user` (idempotent deletion). All deletion is gated on Rally ownership — only project IDs recorded in `rally_project_ids.log` are eligible for purge.

### Prometheus Integration

- Scrape target: `<host>:9101/metrics`
- Alert rules: copy `prometheus/rally_alerts.yml` to your Prometheus rules directory and add it under `rule_files:` in `prometheus.yml`

### CI

`.github/workflows/build-push.yml` builds the container image and pushes it to GitHub Container Registry (`ghcr.io/<owner>/<repo>`). It runs on push to `main`, on `v*` tags, on PRs to `main` (build-only, no push), and via manual dispatch. Auth uses the built-in `GITHUB_TOKEN` (`packages: write`), so no extra secrets are required. The build context is the repo root with `docker/Dockerfile`, matching `docker/docker-compose.yml`. Tags are derived by `docker/metadata-action` (branch, PR ref, semver, SHA, and `latest` on the default branch); layer caching uses the GitHub Actions cache backend.

`docker/docker-compose.yml` defaults to this published image (`image: ${RALLY_MONITOR_IMAGE:-ghcr.io/ching-kuo/openstack-rally-monitor:latest}`) while retaining the `build:` block, so `docker compose pull` fetches upstream and `docker compose up --build` still builds locally under the same tag.

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
