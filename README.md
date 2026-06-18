# OpenStack Rally Monitor

Automated OpenStack cloud health testing using **Rally**, with a live dark-theme dashboard, Prometheus metrics, and Alertmanager integration for cleanup failure detection.

## Features

- **Configurable Service Set** — Keystone, Nova, Neutron, Glance, Cinder, and Swift by default; trim the monitored set per cloud with `RALLY_SERVICES`
- **Lightweight Health Checks** — Read-only API probes every 15 minutes between heavy test runs, via a single authenticated session (one token per cycle); a slow-but-reachable service is reported as `degraded`
- **Prometheus Metrics** — Full metrics exposure for test results, SLA compliance, orphaned resources, per-service API availability/latency, and maintenance state
- **Orphan Detection & Cleanup** — Detects resources left behind by failed Rally cleanups (both `s_rally_*` and `c_rally_*` prefixes) and provides a manual purge tool
- **RadosGW Orphan Management** — Optional: detects orphaned Ceph RGW implicit-tenant users and automatically purges rally-owned ones after each test run (requires RGW admin API credentials)
- **Webhook Notifications** — Optional outbound webhook on smoke-status transitions (`passed`↔`failed`), with generic / Slack / Discord payload formats
- **7-Day History** — Results retained with automatic pruning
- **Uptime Percentages** — API health and smoke-test uptime over a configurable window (default 30 days), shown as dashboard badges
- **Live Dashboard** — Dark-theme glassmorphism UI with status timelines, latency charts, auto-refresh, per-scenario failure causes, links to a self-contained per-run full report (themed to match, works offline), and a live "test run in progress" indicator

## Dashboard Customization

The dashboard can be reskinned with a small Horizon-style theme overlay. Place optional `tokens.css`, `overrides.css`, `logo.svg`, or `favicon.svg` files under `/results/branding/`, or bind mount a local theme directory:

```yaml
volumes:
  - rally-results:/results
  - ./my-theme:/results/branding:ro
```

See [docs/CUSTOMIZING.md](docs/CUSTOMIZING.md) for the stable token contract, fallback behavior, and examples.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                  Docker: rally-monitor                    │
│                                                          │
│  ┌──────────┐  ┌───────────────┐  ┌─────────────────┐  │
│  │  Cron     │→│  run_tests.sh │→│  Rally Task      │  │
│  │  Schedule │  │  orchestrator │  │  Execution       │  │
│  └──────────┘  └──────┬────────┘  └─────────────────┘  │
│                       │                                   │
│                       ▼                                   │
│              ┌────────────────┐    ┌─────────────────┐  │
│              │  /results/     │    │  cleanup_monitor │  │
│              │  JSON files    │←──│  .sh             │  │
│              └───────┬────────┘    └─────────────────┘  │
│                      │                                    │
│            ┌─────────┴─────────┐                         │
│            ▼                   ▼                         │
│  ┌──────────────────┐  ┌──────────────┐                 │
│  │  rally_exporter   │  │  Dashboard   │                 │
│  │  :9101/metrics    │  │  :8080       │                 │
│  └────────┬─────────┘  └──────────────┘                 │
│           │                                               │
└───────────┼───────────────────────────────────────────────┘
            │
            ▼
┌──────────────────┐    ┌──────────────────┐
│  Prometheus      │───→│  Alertmanager    │
│  (your existing) │    │  (your existing) │
└──────────────────┘    └──────────────────┘
```

Three processes run inside a single container, managed by `scripts/entrypoint.sh`:

| Process | Port | Description |
|---------|------|-------------|
| `rally_exporter.py` | `9101` | Flask app; reads JSON from `/results/` and exposes Prometheus metrics |
| `serve.py` (dashboard) | `8080` | Serves static HTML/JS/CSS dashboard with security headers, backed by symlinked JSON files |
| Cron | — | Schedules Rally test runs and lightweight API health checks |

## Quick Start

### 1. Clone and Configure

```bash
git clone https://github.com/ching-kuo/openstack-rally-monitor.git
cd openstack-rally-monitor
cp env.sample .env
# Edit .env with your OpenStack credentials and Rally settings
vim .env
```

### 2. Pull and Run

By default the compose file uses the pre-built image published to GitHub Container Registry by CI (`.github/workflows/build-push.yml`). Pull it and start:

```bash
cd docker
docker compose pull
docker compose up -d
```

Override the image with the `RALLY_MONITOR_IMAGE` environment variable to pin a specific tag. Available tags: `latest` (default branch), the branch name, version tags (`vX.Y.Z`), and the commit SHA:

```bash
RALLY_MONITOR_IMAGE=ghcr.io/ching-kuo/openstack-rally-monitor:v1.2.3 docker compose up -d
```

To build locally instead of pulling (the built image is tagged with the same name):

```bash
docker compose up -d --build
```

### 3. Access

| Service    | URL                           |
|------------|-------------------------------|
| Dashboard  | http://localhost:8080         |
| Metrics    | http://localhost:9101/metrics |
| Health     | http://localhost:9101/health  |

### 4. Configure Prometheus

Add to your `prometheus.yml`:

By default, ports are bound to `127.0.0.1` only. If Prometheus runs on the same host, use:

```yaml
scrape_configs:
  - job_name: "rally-openstack-monitor"
    scrape_interval: 60s
    static_configs:
      - targets: ["127.0.0.1:9101"]
```

If Prometheus runs on a **different host**, expose the port via a reverse proxy (nginx, Caddy) with authentication, or override the binding in `docker-compose.yml`:

```yaml
ports:
  - "0.0.0.0:${EXPORTER_PORT:-9101}:9101"
```

Copy `prometheus/rally_alerts.yml` to your Prometheus rules directory and include it under `rule_files:`.

## Configuration

All settings are controlled via environment variables in `.env`.

### Required

| Variable | Description |
|----------|-------------|
| `OS_AUTH_URL` | OpenStack Keystone endpoint (e.g. `https://openstack:5000/v3`) |
| `OS_USERNAME` | OpenStack username |
| `OS_PASSWORD` | OpenStack password |
| `OS_PROJECT_NAME` | OpenStack project |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `OS_USER_DOMAIN_NAME` | `Default` | User domain |
| `OS_PROJECT_DOMAIN_NAME` | `Default` | Project domain |
| `OS_REGION_NAME` | `RegionOne` | Region |
| `RALLY_SERVICES` | `keystone,nova,neutron,glance,cinder,swift` | Comma-separated set of services to monitor. Trim to match your cloud (e.g. drop `swift` on a deployment without object storage). Parsed defensively (trimmed, lowercased, deduped, order-preserving); names must match `^[a-z0-9_-]+$` (lowercase letters, digits, `_`, `-`) — any token failing the allowlist is dropped, and if every token is dropped the default set is used. Unknown but valid names are skipped with a log line. `keystone` is always health-checked (the session authenticates against it) |
| `RALLY_SCHEDULE_INTERVAL` | `240` | Minutes between full Rally test runs |
| `HEALTH_CHECK_INTERVAL` | `15` | Minutes between lightweight API health checks |
| `HEALTH_LATENCY_WARN_MS` | `5000` | Latency in ms above which a reachable service is reported `degraded`. Degraded counts as up for uptime/`rally_api_up`; the slowness shows in `rally_api_latency_milliseconds` |
| `RALLY_RESULTS_RETENTION_DAYS` | `7` | Days to keep results before pruning |
| `PROVENANCE_RETENTION_DAYS` | `90` | Days to keep `rally_project_ids.log` (the RGW auto-purge authorization ledger). Deliberately decoupled from `RALLY_RESULTS_RETENTION_DAYS` |
| `UPTIME_WINDOW_DAYS` | `30` | Window in days for the dashboard uptime percentages |
| `RALLY_NOVA_FLAVOR` | `m1.tiny` | Flavor name for Nova scenarios |
| `RALLY_NOVA_IMAGE` | `cirros-0.6.2-x86_64-disk` | Image name for Nova scenarios |
| `RALLY_NEUTRON_NETWORK_CIDR` | `10.99.0.0/24` | CIDR for Neutron test networks |
| `RGW_ADMIN_URL` | — | RGW admin API endpoint (enables RGW orphan detection/purge) |
| `RGW_ACCESS_KEY` | — | S3 access key for RGW admin user (`buckets=*;users=*` caps) |
| `RGW_SECRET_KEY` | — | S3 secret key for RGW admin user |
| `RGW_REGION` | — | Explicit SigV4 region for RGW (omit if not required) |
| `NOTIFY_WEBHOOK_URL` | — | Webhook URL for smoke-status transition notifications (enables the feature). Treated as a secret — may embed a token and lands in `/rally/rally_env` (mode 0640) |
| `NOTIFY_FORMAT` | `generic` | Webhook payload shape: `generic` (raw JSON), `slack` (`{"text": …}`), or `discord` (`{"content": …}`) |
| `NOTIFY_DASHBOARD_URL` | — | Optional dashboard URL embedded in the notification payload / chat message |
| `EXPORTER_PORT` | `9101` | Prometheus exporter port |
| `DASHBOARD_PORT` | `8080` | Dashboard port |

## Scenarios

Each service has a dedicated YAML scenario in `rally/scenarios/`. The monitored set is configurable via `RALLY_SERVICES` (default: all six below); a configured name without a matching `rally/scenarios/<name>.yaml` is skipped with a `SKIP` log line rather than failing the run.

| Service | Tests |
|---------|-------|
| **Keystone** | Create/delete users & projects; list services; multi-service auth validation |
| **Nova** | Boot/delete servers; list flavors & hypervisors; floating IP associate/dissociate |
| **Neutron** | Create/delete networks, subnets, ports, routers; security group management |
| **Glance** | Create/delete/list images |
| **Cinder** | Create/delete/list volumes; cloning; snapshots; QoS policy management |
| **Swift** | Container/object CRUD; object listing and download |

## Prometheus Metrics

All metrics are exposed on `:9101/metrics`. Alongside the existing `rally_task_*`, `rally_service_status`, `rally_*_orphaned_resources`, `rally_rgw_*`, and `rally_data_valid` gauges, the API health check and announcement state add:

| Metric | Labels | Meaning |
|--------|--------|---------|
| `rally_api_up` | `service` | API health check reachable (1) or down (0). `degraded` (slow but reachable) counts as **up** — this gauge measures reachability, not speed |
| `rally_api_latency_milliseconds` | `service` | Latency of the last health check. This is where a `degraded` service is visible — alert here for latency-based warnings |
| `rally_api_overall_up` | — | Overall API health, **fail-closed**: `up`/`degraded` → 1; everything else → 0 — `down`, `unknown`/missing overall (e.g. the seed `health.json` on a fresh volume, or a corrupt/missing file), and any unrecognized value. Semantics: `1` = the most recent health data reports overall reachability; `0` = down OR no valid signal. When the health pipeline breaks, the per-service `rally_api_up` series vanish (so they cannot fire `== 0`) and only this gauge can still signal the loss — see `RallyApiSignalLost` |
| `rally_announcement_active` | `type` | Count of currently-active announcements per type; all three labels (`incident`, `maintenance`, `scheduled`) are always emitted (0 when none) |
| `rally_maintenance_mode` | — | 1 when any `maintenance`-type announcement is active, else 0 (drives the optional alert inhibition below) |

Uptime is intentionally **dashboard-only** — derive it Prometheus-side with `avg_over_time(rally_api_up[30d])` rather than scraping a dedicated gauge.

## Alert Rules

Defined in `prometheus/rally_alerts.yml`.

| Alert | Severity | Condition |
|-------|----------|-----------|
| `RallyCleanupFailure` | critical | `s_rally_*` scenario orphans detected — cleanup failed mid-test |
| `RallyOrphanedResourcesHigh` | warning | `s_rally_*` orphan count >5 |
| `RallyContextCleanupWarning` | info | `c_rally_*` context orphans detected — teardown failed on passing run |
| `RallyRgwOrphanedUsers` | warning | Orphaned RGW implicit-tenant users detected |
| `RallyRgwOrphanedBuckets` | warning | Orphaned RGW bucket count >5 |
| `RallyRgwScanDegraded` | warning | RGW orphan scan is failing (API/auth issue) |
| `RallyTestFailure` | warning | A scenario failed |
| `RallyServiceDown` | critical | Entire service is failing |
| `RallySLABreach` | warning | SLA criteria not met |
| `RallyApiDown` | critical | A service's lightweight API health check has reported down for >20 min |
| `RallyApiSignalLost` | warning | `rally_api_overall_up == 0` for >60 min — a sustained overall outage OR a broken/stale health pipeline (corrupt or missing `health.json`, whose absent per-service series cannot fire `RallyApiDown`) |
| `RallyStaleResults` | warning | No new results in >2 hours |
| `RallyOverallFailure` | critical | One or more services failing |

`RallyApiDown` fires on `rally_api_up == 0` with `for: 20m` (two consecutive 15-minute health checks), making it the fastest available outage signal — far ahead of the ~4-hour Rally cadence. A `degraded` (slow-but-reachable) service keeps `rally_api_up == 1`, so alert on `rally_api_latency_milliseconds` if you want a latency-based warning. `RallyApiSignalLost` (warning, `for: 60m`) is the fail-closed companion: `rally_api_overall_up` is `0` on a real overall outage AND when the health pipeline itself breaks (corrupt or missing `health.json`). In the broken-pipeline case the per-service `rally_api_up` series are absent and cannot fire `RallyApiDown`, so this catch-all is the only signal that survives a blind exporter — `RallyApiDown` stays the page-worthy critical, `RallyApiSignalLost` stays a warning.

### Maintenance inhibition (opt-in)

`RallyTestFailure`, `RallyServiceDown`, and `RallySLABreach` ship with commented-out maintenance-inhibition variants. Uncomment the `… unless on() rally_maintenance_mode == 1` form of each `expr` to suppress these flappy alerts while an operator has posted a `maintenance`-type announcement, so a single `announce.sh post --type maintenance` silences both the dashboard banner and the alerts during a planned window. `rally_maintenance_mode` is unlabeled, so the `unless` needs `on()` to match the empty label set.

> **Health-check latency baseline caveat:** as of this version the health check uses a single authenticated openstacksdk session instead of six per-service CLI invocations, so `rally_api_latency_milliseconds` now measures the API round-trip rather than ~1–3 s of CLI startup + token issuance. Latency values **step down** at upgrade — re-baseline any latency alerts tuned against the previous CLI-based numbers.

### Orphan severity rationale

Rally records a task as **passed** based on scenario iteration success rate. Context teardown runs after that result is recorded, so its failures are invisible to the task status. This means two distinct failure modes:

| Prefix | Created by | Orphan cause | Severity |
|--------|-----------|--------------|----------|
| `s_rally_*` | Scenario plugins | Cleanup failed **during** the test | critical/warning |
| `c_rally_*` | Context plugins (users, projects, networks) | Teardown failed **after** a passing run | info |

## RadosGW Orphan Management (Optional)

When `rgw_keystone_implicit_tenants=true` is enabled in your Ceph cluster, each Keystone project automatically gets a corresponding RGW user (`<project_id>$<project_id>`). Rally creates and tears down Keystone projects for every test run, but does not clean up the RGW side — leaving orphaned users and buckets in Ceph.

To enable RGW orphan detection and cleanup, set these environment variables:

```bash
RGW_ADMIN_URL=https://your-radosgw.example.com/admin
RGW_ACCESS_KEY=<admin-s3-access-key>
RGW_SECRET_KEY=<admin-s3-secret-key>
# RGW_REGION=  # optional, only if your Ceph requires an explicit SigV4 region
```

The RGW admin user needs `buckets=*;users=*` capabilities:

```bash
radosgw-admin caps add --uid=<admin-uid> --caps="buckets=*;users=*"
```

**How it works:**

- `run_tests.sh` automatically purges **Rally-owned** RGW orphans after each test run via `auto_purge_rgw()`. Project IDs must appear in the provenance ledger (`/results/rally_project_ids.log`). Non-Rally orphans are never touched.
- `cleanup_monitor.sh` then queries the RGW admin API for implicit-tenant users, cross-references against Keystone, and reports post-purge orphan counts in `cleanup_metrics.json` with a scan health status (`ok`/`skipped`/`error`)
- `purge_orphans.sh` is available for manual bulk cleanup of all resource types (including RGW) when needed
- All operations are **fail-closed**: scan errors or inconclusive Keystone lookups block destructive purge and surface as degraded scan status rather than false zeros

If RGW credentials are not set, all RGW features are silently skipped — existing functionality is unaffected.

## Operator Announcements

The dashboard renders a single banner above the 7-day timeline that operators can publish to via `docker exec`. Three types are supported:

| Type          | Color    | Lifecycle |
|---------------|----------|-----------|
| `incident`    | red      | Active until cleared manually, or auto-cleared after the next all-green Rally run |
| `maintenance` | amber    | Active until `--expires-at`, or until cleared manually |
| `scheduled`   | blue     | Hidden until `--effective-from`, then visible until `--expires-at` |

All examples below run as the `rally` user. Using a different UID can leave the state file owned by root and break subsequent cron-driven auto-clears.

```bash
# Publish an incident (clears automatically when the next Rally run is all-green)
docker exec -u rally rally-monitor /scripts/announce.sh \
    post --type incident --body "RGW backend degraded — investigating"

# Publish a maintenance window (auto-expires at the given UTC time)
docker exec -u rally rally-monitor /scripts/announce.sh \
    post --type maintenance \
    --body "Lab maintenance — expect failures" \
    --expires-at 2026-05-16T18:00:00Z

# Publish a scheduled-future announcement (hidden until effective_from)
docker exec -u rally rally-monitor /scripts/announce.sh \
    post --type scheduled \
    --body "Compute upgrade window" \
    --effective-from 2026-05-17T10:00:00Z \
    --expires-at 2026-05-17T12:00:00Z

# Append a progress update to an existing incident or maintenance record
docker exec -u rally rally-monitor /scripts/announce.sh \
    update <id> --body "Restarted nginx, observing"

# List active announcements (returns the full state JSON)
docker exec -u rally rally-monitor /scripts/announce.sh list

# Clear an announcement explicitly (idempotent — succeeds even if the id is gone)
docker exec -u rally rally-monitor /scripts/announce.sh clear <id>
```

`post` prints the new announcement's ID to stdout so it can be captured for follow-up updates. Timestamps must be ISO 8601 UTC (`YYYY-MM-DDTHH:MM:SSZ`). Bodies are plain text (max 500 characters); HTML and Markdown are not rendered.

State lives at `/results/announcement-state.json`. The dashboard picks up changes on its existing 5-minute refresh cycle — no new HTTP endpoint is introduced.

## Webhook Notifications

Set `NOTIFY_WEBHOOK_URL` to fire an outbound webhook whenever the smoke-test status **transitions** (`passed`→`failed` or `failed`→`passed`). Transition-only semantics mean steady-state runs are silent — you are paged when the cloud breaks and again when it recovers, not on every run. The feature is **off by default**: with the URL unset the test runner makes no outbound HTTP calls.

Add the variables to your `.env` (consumed by `docker compose`):

```bash
# Slack incoming webhook example
NOTIFY_WEBHOOK_URL=https://hooks.slack.com/services/T000/B000/XXXXXXXX
NOTIFY_FORMAT=slack
NOTIFY_DASHBOARD_URL=https://rally.example.com
```

`NOTIFY_FORMAT` selects the payload shape:

| Format | Body |
|--------|------|
| `generic` (default) | Raw JSON: `{event, status, previous_status, timestamp, failed_services, error, dashboard_url?}` |
| `slack` | `{"text": "<human-readable line>"}` |
| `discord` | `{"content": "<human-readable line>"}` |

Notes:

- A missing notification state file (`/results/.last_notified_status`) baselines to `passed`, so a first-ever failed run notifies while a first-ever green run stays quiet.
- The state is updated **only on a successful send** (HTTP 2xx). A failed POST leaves the prior status intact, so the next run retries the transition.
- `NOTIFY_WEBHOOK_URL` may embed a token. Like `OS_PASSWORD` it lands in `/rally/rally_env` (mode 0640) so cron jobs can read it — treat it as a secret. The URL is never written to the logs.

## Useful Commands

```bash
# Trigger a manual test run
docker exec -u rally rally-monitor /scripts/run_tests.sh

# Run a lightweight health check (read-only, non-destructive)
docker exec -u rally rally-monitor /scripts/health_check.sh

# Run orphan detection manually (read-only, updates Prometheus metrics)
docker exec -u rally rally-monitor /scripts/cleanup_monitor.sh

# Dry-run purge: list all orphaned resources without deleting anything
docker exec -u rally rally-monitor /scripts/purge_orphans.sh

# Purge orphaned resources (permanently deletes s_rally_* and c_rally_* resources)
docker exec -u rally rally-monitor /scripts/purge_orphans.sh --confirm

# View live logs
docker logs -f rally-monitor
docker exec rally-monitor tail -f /rally/logs/rally-tests.log
docker exec rally-monitor tail -f /rally/logs/health-check.log
```

> **Orphan prefixes:** `s_rally_*` resources are created by scenario plugins and cleaned up during the test. `c_rally_*` resources are created by context plugins (projects, users, networks) and cleaned up after the task completes. Both are detected by `cleanup_monitor.sh` and removable via `purge_orphans.sh`, but reported at different severities — see the alert table above.

## Continuous Integration

`.github/workflows/build-push.yml` runs in two stages. A `test` job first runs the full pytest suite (`exporter/`, `dashboard/`, `scripts/`) on Python 3.13 — matching the `python:3.13-slim` runtime — and the `build-push` job declares `needs: test`, so a failing suite blocks the image build and publication entirely. Images are pushed to GitHub Container Registry (`ghcr.io/<owner>/<repo>`) on push to `main`, on `v*` tags, and via manual dispatch; pull requests build only (no push).

Dependencies are pinned to exact versions for reproducible images — `docker/requirements-rally.txt` (the OpenStack toolchain: `rally-openstack`, `rally`, `python-openstackclient`), `exporter/requirements.txt`, and `exporter/requirements-test.txt`. `.github/dependabot.yml` opens weekly PRs to bump them (pip for `/exporter` and `/docker`, the docker base image, and GitHub Actions), each of which runs through the test gate above — so version drift is always a reviewed, tested change rather than something that happens silently on a rebuild.

To run the suite locally (`python -m pytest exporter/ dashboard/ scripts/`), **bash >= 4 must be on `PATH`** — the script-suite tests resolve bash via `shutil.which("bash")` and the scripts use `mapfile` (a bash 4 builtin). macOS ships bash 3.2 at `/bin/bash`, so `brew install bash` and ensure it precedes `/bin/bash` on `PATH`.

## Project Structure

```
openstack-rally-monitor/
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
├── rally/
│   ├── deployment.yaml
│   └── scenarios/
│       ├── keystone.yaml
│       ├── nova.yaml
│       ├── neutron.yaml
│       ├── glance.yaml
│       ├── cinder.yaml
│       └── swift.yaml
├── scripts/
│   ├── entrypoint.sh
│   ├── run_tests.sh
│   ├── health_check.sh
│   ├── cleanup_monitor.sh
│   ├── purge_orphans.sh
│   ├── rgw_helpers.sh
│   ├── render_report.py
│   ├── backfill_reports.sh
│   └── patch_rally.py
├── exporter/
│   ├── rally_exporter.py
│   ├── requirements.txt
│   ├── requirements-test.txt
│   ├── test_rally_exporter.py
│   └── test_rgw_helpers.py
├── prometheus/
│   ├── prometheus.yml
│   └── rally_alerts.yml
├── dashboard/
│   ├── index.html
│   ├── style.css
│   ├── app.js
│   ├── report.css
│   ├── report.js
│   ├── serve.py
│   ├── themes/
│   │   └── default/
│   └── test_serve.py
├── docs/
│   └── CUSTOMIZING.md
└── env.sample
```

## License

MIT
