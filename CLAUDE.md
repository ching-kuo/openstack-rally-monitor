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

1. **Prometheus Exporter** (`exporter/rally_exporter.py`) — Flask app on `:9101` that reads JSON files from `/results/` (`latest_summary.json`, `cleanup_metrics.json`, `health.json`, `announcement-state.json`) and exposes them as Prometheus gauge metrics on `/metrics`. Also serves `/health` (liveness) and `/ready` (readiness).

2. **Dashboard** (`dashboard/`) — Static files served by Python's `ThreadingHTTPServer` on `:8080`. The dashboard is pure HTML/JS/CSS with no build step; it fetches JSON from symlinked files (`results.json`, `history.json`, `health.json`, `health_history.json`, `announcement-state.json`, `run_state.json`) in the same directory, plus self-contained per-run HTML reports (rendered by `render_report.py`, see Full Report) via the `runs/` symlink.

3. **Cron Jobs** — Two cron schedules:
   - Rally test runs (`RALLY_SCHEDULE_INTERVAL`, default 240 min) → `scripts/run_tests.sh`
   - API health checks (`HEALTH_CHECK_INTERVAL`, default 15 min) → `scripts/health_check.sh`

### Data Flow

```
run_tests.sh
  → write_run_state_running()          → /results/run_state.json (state: running; EXIT trap → idle)
  → rally task start <scenario>.yaml   (for each service in RALLY_SERVICES; --tag run-<ts>-<service>)
  → poll live c_rally_* projects       (append IDs to /results/rally_project_ids.log)
  → rally task results <uuid>          (JSON to /results/<timestamp>/<service>.json)
  → render_report.py <service>.json    (self-contained themed /results/<timestamp>/<service>.html)
  → build_summary()                    → /results/latest_summary.json (first_error per scenario)
  → record_smoke_result()              → /results/smoke_history.json (rolling uptime ledger)
  → notify.sh <RUN_SMOKE_STATUS>       (webhook on passed<->failed transition; no-op unless configured)
  → announce.sh auto-clear-if-all-green
  → auto_purge_rgw()                   (deletes rally-owned RGW orphans; no-op without RGW creds)
  → cleanup_monitor.sh                 → /results/cleanup_metrics.json
  → publish_dashboard_files()          → /results/results.json, history.json
  (deployment_setup_failed path also runs record_smoke_result + publish_dashboard_files + notify.sh
   before exiting, so the failed run shows as a failed timeline cell instead of the last green run)

health_check.sh                        (thin cron wrapper: flock, atomic write w/ `jq empty` validation,
                                        history append, all-down fallback via health_fallback_filter.jq)
  → python3 api_health_check.py        (ONE openstacksdk session: authorize()=keystone timing,
                                        then one read-only GET per service in RALLY_SERVICES;
                                        emits health.json shape on stdout, third status "degraded")
  → /results/health.json, health_history.json (rolling window sized to cover
    UPTIME_WINDOW_DAYS at HEALTH_CHECK_INTERVAL, min 672 entries; carries an
    embedded uptime object computed by scripts/health_history_filter.jq)

rally_exporter.py
  → reads latest_summary.json + cleanup_metrics.json + health.json + announcement-state.json each scrape
  → updates Prometheus Gauges in memory (RGW orphan/scan health, rally_api_* from health.json,
    rally_announcement_active/rally_maintenance_mode from announcement-state.json)
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
  - `run_state.json` — `{state: running|idle, started_at, ...}` written by `run_tests.sh` (running after the run flock is held, idle via an EXIT trap incl. failure paths); reset to idle on every boot by `entrypoint.sh`
  - `.last_notified_status` — `notify.sh` baseline (`passed`/`failed`); updated atomically only after a successful webhook send. Not symlinked into the dashboard (internal state)
  - `announcement-state.json` — operator announcement records (see Announcement System)
  - `<TIMESTAMP>/` — per-run directories with `<service>.json`, `<service>.html`, `run.log`
- `/rally/data/` — Docker volume `rally-data`; SQLite DB for Rally state
- `/dashboard/` — ephemeral; `results.json` etc. are symlinks → `/results/`; `/dashboard/runs -> /results` exposes per-run `<TIMESTAMP>/<service>.html` reports

### Theme Customization

Dashboard theming follows a narrow `themes/<name>/` contract without a build step. The shipped default theme is `/dashboard/themes/default/` and contains `tokens.css`, `logo.svg`, and `favicon.svg`. At container startup, `scripts/entrypoint.sh` ensures `/results/branding/` exists and recreates `/dashboard/themes/custom -> /results/branding`.

`dashboard/index.html` loads styles in this cascade order: `themes/default/tokens.css`, `style.css`, `themes/custom/tokens.css`, then `themes/custom/overrides.css`. Custom files are optional and partial; missing custom assets fall back to defaults. `dashboard/app.js` probes `themes/custom/logo.svg` and `themes/custom/favicon.svg` with GET at boot, then swaps the header logo and favicon only when those files exist.

`dashboard/serve.py` keeps a deny-by-default allowlist with four branches: `ALLOWED_STATIC` (own assets, including `report.css`/`report.js`), `ALLOWED_JSON_SYMLINKS` (JSON symlinks resolving into `/results/`), the theme branch, and the served-report branch. Theme paths must live under `themes/`, use one of `.css`, `.svg`, `.png`, or `.ico`, and resolve either under `/dashboard/themes/` or under the tighter `/results/branding/` custom-theme root. Do not widen this to all of `/results/`.

The fourth branch serves the per-run HTML reports: the regex `^runs/[0-9]{8}T[0-9]{6}Z/[a-z0-9_-]+\.html$` (anchored, no separators inside the captures) plus re-checked `RESULTS_ROOT` containment on the resolved target, reached via the `/dashboard/runs -> /results` symlink. **There is a single strict CSP for every response** (`SECURITY_HEADERS`: `script-src 'self'`, no `'unsafe-inline'` in `script-src`, no external origins, no `sandbox`). The reports earn the strict policy because they are now rendered by `scripts/render_report.py` (see Full Report) from same-origin assets only — no CDN, no inline executable script. The earlier per-path `REPORT_SECURITY_HEADERS` (relaxed `'unsafe-inline'` + `ajax.googleapis.com`/`cdnjs.cloudflare.com` + `sandbox`) and the per-instance header-swap/leak-reset logic were **removed** along with the rally-generated AngularJS report they existed to serve. `ALLOWED_JSON_SYMLINKS` includes `run_state.json` and `announcement-state.json`, both of which 404 (dangling symlink, `target.exists()` false) until first written.

### Full Report

The "Full Rally report" linked from each service card is a self-contained, dashboard-themed page generated by `scripts/render_report.py` (invoked from `run_tests.sh::run_service_tests`, replacing `rally task report`). For each service it reads the persisted `rally task results` JSON (`<RUN_DIR>/<service>.json`; the "old-json-results" shape: a list of workloads with `key.{name,description,kw}`, `result[].{duration,atomic_actions,error}`, `sla[]`, `hooks`, `load_duration`, `full_duration`), normalizes it into a small render-ready payload, and emits `<RUN_DIR>/<service>.html`. That HTML embeds the payload in a non-executable `<script type="application/json" id="rally-report-data">` block (exempt from `script-src`) and references the dashboard's own theme cascade plus `report.css`, `report.js`, and the vendored Chart.js — all by **relative** `../../` path so it works behind a sub-path reverse proxy. `dashboard/report.js` renders it client-side: a working collapsible left nav (our own click handlers — the rally/AngularJS nav's `ng-show`/`ng-hide` broke when the CDN scripts did not load), an overview table, and per-scenario detail (atomic-action duration bar chart, SLA table, error tracebacks, input config). Rendering is best-effort: `render_report.py` degrades a malformed/empty/partial results file (including truthy-but-non-dict `key`/`kw`/`runner` shapes) to a valid empty-state report and exits 0, so the dashboard link never 404s. Because every asset is same-origin and there is zero inline executable script, the report needs no CDN, no relaxed CSP, and no `sandbox`, and renders with no network access (air-gapped friendly). A custom theme restyles reports for free via the same token cascade.

`scripts/backfill_reports.sh` heals reports from *before* this migration. A pre-upgrade run directory still holds the old `rally task report` AngularJS HTML, which renders broken (raw `{{...}}` placeholders) under the new strict CSP because its CDN scripts are blocked. `entrypoint.sh` backgrounds the backfill as the rally user on every boot (after the dashboard symlinks, before starting services): it walks every `<RUN_DIR>` and re-renders `<service>.html` from the persisted `<service>.json` via `render_report.py`, so deploying the new image fixes historical reports immediately instead of waiting out `RALLY_RESULTS_RETENTION_DAYS`. It is idempotent (skips any report already carrying the `id="rally-report-data"` data-block marker, so steady-state cost is one `grep` per report) and best-effort (a malformed/missing JSON yields a valid empty-state report). Its run-directory guard (`^[0-9]{8}T[0-9]{6}Z$`) and service-token guard (`^[a-z0-9_-]+$`) mirror `serve.py`'s `RUN_REPORT_RE` and `parse_rally_services` — **KEEP IN SYNC** — so it never descends into `branding/` or emits a non-servable report. `entrypoint.sh` `%q`-quotes `RESULTS_DIR` into the `su -c` string so an operator-set value with shell metacharacters cannot break out.

Public theme tokens are documented in `docs/CUSTOMIZING.md`. Internal glass/radius/shadow tokens and decorative alpha literals are not stable API. SVGs loaded via `<img>` do not inherit page CSS variables, so a custom theme must supply its own `logo.svg` to recolor the brand mark.

### Announcement System

Operator-authored dashboard banners are managed by `scripts/announce.sh` — a bash + jq CLI invoked via `docker exec -u rally rally-monitor /scripts/announce.sh ...`. State lives at `/results/announcement-state.json` (top-level shape: `{"announcements": [...]}`), written atomically via `tmpfile + mv`. No HTTP write endpoint is introduced; authorization equals "you can `docker exec -u rally` on the host."

The state file lives directly under `/results/` rather than under `/results/branding/` to avoid a collision with the documented read-only theme bind-mount pattern (`./my-theme:/results/branding:ro` in `docs/CUSTOMIZING.md`), which would otherwise silently break every write.

`scripts/entrypoint.sh` creates `/dashboard/announcement-state.json -> /results/announcement-state.json` unconditionally at startup; the file appears lazily on the first `post`, and `serve.py`'s `target.exists()` check 404s on the dangling symlink until then. The dashboard fetches via the same GET surface as other JSON files: one entry in `ALLOWED_JSON_SYMLINKS`.

The CLI supports five subcommands: `post`, `update`, `clear`, `list`, and `auto-clear-if-all-green`. The last is invoked from `scripts/run_tests.sh::main` after `build_summary` and before `publish_dashboard_files`; it removes every `incident`-type record when the post-run summary is unambiguously all-green. The predicate is `(.services | length) > 0 AND (.error // null) == null AND (.services | to_entries | all(.value.status == "passed"))`. The empty-services and `.error` guards are critical — a naive `all(...)` returns `true` on the `{"services": {}, "error": "deployment_setup_failed"}` shape and would silently erase the incident banner during the exact failure mode it exists to communicate.

Mutating subcommands wrap their read-modify-write cycle in `flock` on `/results/.announce.lock`. Without this, an operator `post` running between a cron-driven `auto-clear-if-all-green`'s read and write would be silently overwritten. `list` is unlocked so it never blocks on a stuck mutator.

The dashboard renders one banner above the 7-day timeline. Body text is rendered via `textContent` (no Markdown, no HTML); a `<details>` element exposes progress updates and other concurrent records. Each banner carries a visible bracketed type label (`[INCIDENT]` / `[MAINTENANCE]` / `[SCHEDULED]`) so color is not the sole signal — color tokens reused from the public theme contract: `--color-failure` (incident), `--color-warning` (maintenance), `--color-brand-secondary` (scheduled). Auto-refresh preserves `<details>` open state when the same primary announcement is still active.

`rally_exporter.py::_apply_announcement_metrics` reads the same `announcement-state.json` on every scrape and emits `rally_announcement_active{type}` (all three enum labels always present, 0 when none) plus unlabeled `rally_maintenance_mode` (1 iff any `maintenance` record is active). A missing state file yields zeros, not an exporter error. The active-ness predicate (`_is_announcement_active`: unknown type → inactive; future `effective_from` → inactive; past/now `expires_at` → inactive; unparseable bounds ignored) is **KEEP IN SYNC** with `dashboard/app.js::isAnnouncementActive`; label cardinality is enum-only (`_ANNOUNCEMENT_TYPES`, mirroring `TYPE_LABELS` in app.js and `VALID_TYPES_RE` in announce.sh), so body/id text never becomes a label. `rally_maintenance_mode` drives the commented-out, opt-in maintenance-inhibition variants of `RallyTestFailure`/`RallyServiceDown`/`RallySLABreach` in `prometheus/rally_alerts.yml` (`… unless on() rally_maintenance_mode == 1`; `on()` matches the unlabeled gauge against each rule's label set), so one `announce.sh post --type maintenance` can silence both the banner and those alerts.

### Uptime Tracking

The dashboard shows two uptime badges (in the timeline section headers), both computed over `UPTIME_WINDOW_DAYS` (default 30):

- **Smoke-test uptime** — `run_tests.sh::record_smoke_result` appends `{timestamp, status}` to `/results/smoke_history.json` after every run (including the deployment-failure path) and recomputes the embedded `uptime` object. A run is `passed` only under the same all-green predicate `announce.sh` uses (non-empty services, no `.error`, all passed) — the predicate is duplicated in both scripts with keep-in-sync cross-references. The ledger is pruned by timestamp, so uptime coverage is independent of `RALLY_RESULTS_RETENTION_DAYS`. `publish_dashboard_files` embeds `uptime` into `results.json`; `record_smoke_result` also syncs it directly into an existing `results.json`. The deployment-failure path now calls `publish_dashboard_files` right after `record_smoke_result`, so `results.json`/`history.json` reflect the failure instead of the last green run — this is correct because `dashboard/app.js::getRunStatus` treats a summary with a non-empty `.error` OR empty services as **failed** (it no longer renders "All Healthy"). The failed run therefore appears as a failed timeline cell with the error surfaced in the tooltip (`Error: <error>`). The standalone `record_smoke_result` sync is retained as belt-and-suspenders for direct invocations and any future early-exit path that exits before a publish.
- **API uptime** — `health_check.sh` applies `scripts/health_history_filter.jq` (via `jq -f`, so tests exercise the shipped filter) to append the check, cap stored history (`UPTIME_WINDOW_DAYS * 24 * 60 / HEALTH_CHECK_INTERVAL`, floored at 672 so the 7-day health timeline never shrinks), and embed `uptime` into `health_history.json`. The count cap bounds storage; the timestamp cutoff defines the uptime window — they are intentionally separate. Uptime measures **reachability, not speed**: the filter counts a check as up whenever `.overall != "down"`, so a `degraded` check (every service answered but at least one exceeded `HEALTH_LATENCY_WARN_MS`) does not depress availability. This is **KEEP IN SYNC** with `rally_exporter.py::_apply_health_metrics`, which likewise treats `degraded`/`up` as `rally_api_up == 1`. (The unlabeled `rally_api_overall_up` gauge is intentionally stricter — it **fails closed**, mapping anything other than `up`/`degraded` to 0, including `unknown`/missing; see Prometheus Integration.)

`dashboard/app.js::renderUptimeBadge` hides a badge until its `uptime.percent` is a number, so first boots and pre-feature data files render unchanged. The latency chart plots at most `HEALTH_CHART_MAX_POINTS` (672) checks regardless of stored history size. Uptime is dashboard-only by design — Prometheus users can derive it with `avg_over_time()` on existing gauges.

### Orphan Detection

`scripts/cleanup_monitor.sh` runs after each Rally test suite. It queries each OpenStack service for resources prefixed with `s_rally` (scenario resources) or `c_rally` (context resources — projects, users, networks created by Rally contexts) and writes counts to `cleanup_metrics.json`. When `RGW_ADMIN_URL`, `RGW_ACCESS_KEY`, and `RGW_SECRET_KEY` are configured, it also queries the RGW admin REST API for orphaned implicit-tenant users and marks the scan as `ok`, `skipped`, or `error`. The exporter exposes these as OpenStack cleanup gauges plus `rally_rgw_*` metrics.

After the summary is built, `run_tests.sh` automatically purges rally-owned RGW orphans via `auto_purge_rgw()`, then refreshes `cleanup_metrics.json` so dashboard and Prometheus state reflect the post-purge state. This only runs when RGW admin credentials are configured. It is fail-closed: if the orphan scan had errors or Keystone lookups were inconclusive, purge is skipped entirely. Only users whose project IDs appear in `rally_project_ids.log` are eligible for deletion. Unknown-owner orphans are never touched. For non-RGW resources, `purge_orphans.sh` remains the manual cleanup tool.

`rally_project_ids.log` is the **sole authorization basis** for RGW auto-purge, so `run_tests.sh` prunes it on its own `PROVENANCE_RETENTION_DAYS` window (default 90), deliberately decoupled from `RALLY_RESULTS_RETENTION_DAYS` (default 7) — the same defect class as the `smoke_history.json` retention decoupling. Pruning the ledger on the short run-retention window would degrade older orphans to unknown-owner (never purged), making `RallyRgwOrphanedUsers` fire forever.

### Rally Scenarios

Located in `rally/scenarios/`. The monitored service set is configurable via `RALLY_SERVICES` (default `keystone,nova,neutron,glance,cinder,swift`), which drives `run_tests.sh`, `api_health_check.py`, and the `entrypoint.sh` seed (pending cards generated from the set). The normalization is **identical across three parsers** — split on commas, trim/strip whitespace, lowercase, drop empty segments, dedupe preserving first-seen order, then drop any token not matching the `^[a-z0-9_-]+$` allowlist — documented as **KEEP IN SYNC** between `run_tests.sh::parse_rally_services`, `api_health_check.py::parse_rally_services`, and `scripts/health_fallback_filter.jq` (the all-down health document `health_check.sh` publishes when the checker fails). The allowlist is **path-traversal hardening**: service names index `rally/scenarios/<name>.yaml` and `runs/<ts>/<name>.html`, so a token like `../etc` must never survive. When every configured token is dropped (all invalid, or unset/empty), all three fall back to the default set. Unknown but valid names are tolerated: a smoke run logs `SKIP` for a service with no `rally/scenarios/<name>.yaml`; the health check logs a warning and omits a name with no registered checker (dropped invalid tokens are warned to stderr). `keystone` is always health-checked regardless (the openstacksdk session authenticates against it, so its token issuance *is* a keystone availability check; new scenario packs like octavia/heat/designate were deliberately deferred pending real-cloud validation). Scenarios are parameterized via `--task-args-file` with `RALLY_NOVA_FLAVOR`, `RALLY_NOVA_IMAGE`, and `OS_AUTH_URL` from the environment.

### Key Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `OS_AUTH_URL` | — | Keystone endpoint |
| `OS_USERNAME` / `OS_PASSWORD` / `OS_PROJECT_NAME` | — | Credentials |
| `RALLY_SERVICES` | `keystone,nova,neutron,glance,cinder,swift` | Configurable monitored service set (normalized identically in `run_tests.sh`, `api_health_check.py`, and `health_fallback_filter.jq`; names must match `^[a-z0-9_-]+$`, invalid tokens dropped, all-invalid falls back to default; keystone always health-checked) |
| `RALLY_SCHEDULE_INTERVAL` | `240` | Minutes between full test runs |
| `HEALTH_CHECK_INTERVAL` | `15` | Minutes between lightweight health checks |
| `HEALTH_LATENCY_WARN_MS` | `5000` | Latency threshold above which a reachable service is reported `degraded` (counts as up for uptime/`rally_api_up`) |
| `RALLY_RESULTS_RETENTION_DAYS` | `7` | Days before old run directories are pruned |
| `PROVENANCE_RETENTION_DAYS` | `90` | Days before `rally_project_ids.log` entries are pruned; decoupled from `RALLY_RESULTS_RETENTION_DAYS` (it is the RGW auto-purge authorization ledger) |
| `UPTIME_WINDOW_DAYS` | `30` | Window in days for dashboard uptime percentages; sizes the smoke ledger and the health history cap |
| `RALLY_NOVA_FLAVOR` / `RALLY_NOVA_IMAGE` | `m1.tiny` / `cirros-...` | Nova scenario inputs |
| `RGW_ADMIN_URL` / `RGW_ACCESS_KEY` / `RGW_SECRET_KEY` | — | Optional RGW admin API config for orphan detection and purge |
| `RGW_REGION` | unset | Optional explicit SigV4 region for RGW admin requests |
| `NOTIFY_WEBHOOK_URL` | — | Webhook URL for smoke-status transition notifications (enables `notify.sh`); secret — lands in `/rally/rally_env` (0640), never logged |
| `NOTIFY_FORMAT` | `generic` | Webhook payload shape: `generic` / `slack` / `discord` |
| `NOTIFY_DASHBOARD_URL` | — | Optional dashboard URL embedded in the notification payload |
| `EXPORTER_PORT` / `DASHBOARD_PORT` | `9101` / `8080` | Exposed ports |
| `RALLY_DEBUG` | `false` | Set to `true` for verbose rally task logging |
| `RALLY_MONITOR_IMAGE` | `ghcr.io/ching-kuo/openstack-rally-monitor:latest` | Compose-only: image tag to pull/run |

### Cron Environment

Environment variables are exported to `/rally/rally_env` (mode 0640) at container startup and sourced by cron jobs. The file contains `OS_PASSWORD` and (when configured) `RGW_SECRET_KEY` — do not loosen its permissions.

### patch_rally.py

`scripts/patch_rally.py` is applied at Docker build time to patch Rally's internal password-generation policy. It runs once inside the image build (via `RUN python3 /scripts/patch_rally.py` in the Dockerfile) and does not need to be re-run manually.

### RGW Helpers

`scripts/rgw_helpers.sh` is a source-only bash library used by both `cleanup_monitor.sh` and `purge_orphans.sh`. It wraps the RGW admin REST API via `curl --aws-sigv4` (no Ceph CLI needed). Key functions: `rgw_list_implicit_users` (paginated user listing), `rgw_find_orphaned_users` (cross-references RGW against Keystone with bulk pre-fetch), `rgw_classify_owner` (checks Rally provenance ledger), `rgw_delete_bucket`/`rgw_delete_user` (idempotent deletion). All deletion is gated on Rally ownership — only project IDs recorded in `rally_project_ids.log` are eligible for purge.

### Webhook Notifications

`scripts/notify.sh` fires an outbound webhook on smoke-status **transitions only**, invoked from `run_tests.sh::main` right after `record_smoke_result` on **both** the normal and `deployment_setup_failed` early-exit paths (always `|| true`, so a notification failure never affects the run). It is intentionally **predicate-free**: the caller hands it the status it already computed via the `RUN_SMOKE_STATUS` global, so the all-green predicate is *not* re-derived a third time (it lives in `run_tests.sh::record_smoke_result` and `announce.sh`'s `ALL_GREEN_PREDICATE`). notify.sh only compares the given status against `${RESULTS_DIR}/.last_notified_status` and sends on a change.

Behavior: off (silent no-op) unless `NOTIFY_WEBHOOK_URL` is set; a missing state file baselines to `passed` (first-ever failed run notifies, first-ever green run stays quiet); the state file is updated atomically (`tmp + mv`) **only on a successful send** (`curl --fail` exit 0), so a failed POST leaves the baseline intact and the next run retries. `NOTIFY_FORMAT` picks `generic`/`slack`/`discord` payload shapes; `NOTIFY_DASHBOARD_URL` is optionally embedded. `NOTIFY_WEBHOOK_URL` may embed a token, lands in `/rally/rally_env` (0640) alongside `OS_PASSWORD`, and is **never** echoed into logs.

### Prometheus Integration

- Scrape target: `<host>:9101/metrics`
- API health gauges (from `health.json`): `rally_api_up{service}` (degraded counts as up — reachability, not speed; clear-before-set each scrape, so a service dropping out of a snapshot leaves no stale series and a corrupt/missing file leaves NO per-service series), `rally_api_latency_milliseconds{service}` (where degraded slowness is visible), and unlabeled `rally_api_overall_up` which **fails closed**: `up`/`degraded` → 1, everything else → 0 (explicit `down`, `unknown`/missing overall, corrupt/missing file, any unrecognized value). Because per-service series vanish when the pipeline breaks, this unlabeled gauge is the only one that can still signal a blind exporter — hence it must not retain a stale `1`
- Announcement gauges (from `announcement-state.json`): `rally_announcement_active{type}` (all three enum labels always emitted) and `rally_maintenance_mode`
- Alert rules: copy `prometheus/rally_alerts.yml` to your Prometheus rules directory and add it under `rule_files:` in `prometheus.yml`. The `rally_api` group adds `RallyApiDown` (critical, `rally_api_up == 0`, `for: 20m`) and `RallyApiSignalLost` (warning, `rally_api_overall_up == 0`, `for: 60m` — covers both a sustained overall outage and a broken/stale health pipeline whose absent per-service series cannot fire `RallyApiDown`); `RallyTestFailure`/`RallyServiceDown`/`RallySLABreach` carry commented opt-in `unless on() rally_maintenance_mode == 1` inhibition variants
- Uptime is dashboard-only by design; derive it Prometheus-side with `avg_over_time(rally_api_up[30d])`

### CI

`.github/workflows/build-push.yml` runs a `test` job — `python -m pytest exporter/ dashboard/ scripts/` on Python 3.13 (matching the `python:3.13-slim` runtime; the script suites shell out to bash + jq, both on `ubuntu-latest`) — before the `build-push` job, which declares `needs: test`, so a failing suite blocks the build. The build pushes the container image to GitHub Container Registry (`ghcr.io/<owner>/<repo>`) on push to `main`, on `v*` tags, on PRs to `main` (build-only, no push), and via manual dispatch. Auth uses the built-in `GITHUB_TOKEN` (`packages: write`), so no extra secrets are required. The build context is the repo root with `docker/Dockerfile`, matching `docker/docker-compose.yml`. Tags are derived by `docker/metadata-action` (branch, PR ref, semver, SHA, and `latest` on the default branch); layer caching uses the GitHub Actions cache backend.

Dependencies are pinned to exact versions for reproducible images: `docker/requirements-rally.txt` (`rally-openstack==3.0.0`, `rally==5.0.1`, `python-openstackclient==10.1.0`), `exporter/requirements.txt` (`flask`, `prometheus-client`, `gunicorn`), and `exporter/requirements-test.txt` (`pytest`). The OpenStack toolchain in particular **must** stay pinned because `patch_rally.py` targets Rally module internals and the scenario YAMLs reference plugin names upstream renames across releases — a silent bump could move the patched code or rename a plugin out from under a build. `.github/dependabot.yml` opens weekly bump PRs (pip for `/exporter` and `/docker`, the docker base image, and GitHub Actions), each gated by the test job, so version drift is always a reviewed, tested change rather than a transparent rebuild.

`docker/docker-compose.yml` defaults to this published image (`image: ${RALLY_MONITOR_IMAGE:-ghcr.io/ching-kuo/openstack-rally-monitor:latest}`) while retaining the `build:` block, so `docker compose pull` fetches upstream and `docker compose up --build` still builds locally under the same tag.

Running the suite locally (`python -m pytest exporter/ dashboard/ scripts/`) requires **bash >= 4 on `PATH`** — the script-suite tests resolve the interpreter via `shutil.which("bash")`, and bash 4 introduced `mapfile`, which the scripts use. macOS ships bash 3.2 at `/bin/bash`, so install a newer one (`brew install bash`) and ensure it precedes `/bin/bash` on `PATH`. The suite is 270 passed + 1 skipped.

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
