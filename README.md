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
- **Live Dashboard** — Dark-theme glassmorphism UI with status timelines, latency charts, auto-refresh, per-scenario failure causes, links to a self-contained per-run full report (themed to match, works offline, opens on the first failed scenario, linkable per scenario), and a live "test run in progress" indicator

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
| `serve.py` (dashboard) | `8080` | Serves static HTML/JS/CSS dashboard with security headers, backed by symlinked JSON files; supports `HEAD`, `ETag`/`Last-Modified` revalidation (`304`) and gzip, and uses relative paths so it works behind a sub-path reverse proxy |
| Cron | — | Schedules Rally test runs and lightweight API health checks |

The entrypoint starts as root (with every capability except `SETUID`/`SETGID` dropped, plus `no-new-privileges`) only to write `/rally/rally_env`, the cron table and the dashboard symlinks; everything else runs as the unprivileged `rally` user, including the boot-time seeding of `/results` and `/rally/logs` (`scripts/init_state.sh`) and log rotation. Code and config (`/scripts`, `/exporter`, `/dashboard`, `/rally`) are root-owned and read-only to `rally`; only `/results`, `/rally/data`, `/rally/logs` and rally's home (`/home/rally`) are writable by it.

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
  - "0.0.0.0:${EXPORTER_HOST_PORT:-${EXPORTER_PORT:-9101}}:9101"
```

To publish on different host ports, set `EXPORTER_HOST_PORT` / `DASHBOARD_HOST_PORT` in your shell or in `docker/.env` (Compose reads that file for `${...}` interpolation; the repository-root `.env` is only the container's `env_file`, so a port set there never reaches the `ports:` mapping). The older `EXPORTER_PORT` / `DASHBOARD_PORT` names are still honored there as host-port fallbacks (`${EXPORTER_HOST_PORT:-${EXPORTER_PORT:-9101}}`); nested defaults need Docker Compose v2 (v2.18 or later recommended), not the legacy `docker-compose` v1 or `docker stack deploy`. The in-container ports stay 9101/8080: the compose file pins them under `environment:`, which the healthcheck and port mappings depend on.

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

The container exits at startup (naming the missing variables, never their values) when any of these four is empty, and logs a warning while `OS_AUTH_URL` or `OS_PASSWORD` still holds the `env.sample` placeholder.

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `OS_USER_DOMAIN_NAME` | `Default` | User domain |
| `OS_PROJECT_DOMAIN_NAME` | `Default` | Project domain |
| `OS_REGION_NAME` | `RegionOne` | Region |
| `RALLY_SERVICES` | `keystone,nova,neutron,glance,cinder,swift` | Comma-separated set of services to monitor. Trim to match your cloud (e.g. drop `swift` on a deployment without object storage). Parsed defensively (trimmed, lowercased, deduped, order-preserving); names must match `^[a-z0-9_-]+$` (lowercase letters, digits, `_`, `-`) — any token failing the allowlist is dropped, and if every token is dropped the default set is used. Unknown but valid names are skipped with a log line. `keystone` is always health-checked (the session authenticates against it) |
| `RALLY_SCHEDULE_INTERVAL` | `240` | Requested cron interval for full Rally test runs, in minutes; decimal integer `1..1440`. Values at least 60 minutes that are not whole hours round up to the next hour for cron |
| `HEALTH_CHECK_INTERVAL` | `15` | Requested cron interval for lightweight API health checks, in minutes; decimal integer `1..1440` with the same cron rounding policy |
| `HEALTH_CHECK_TIMEOUT` | `300` | Seconds before a hung health check is killed and an all-down result published; also the floor of the exporter's `health.json` staleness threshold. Decimal integer `1..3600` |
| `HEALTH_LATENCY_WARN_MS` | `5000` | Latency in ms above which a reachable service is reported `degraded`. Degraded counts as up for uptime/`rally_api_up`; the slowness shows in `rally_api_latency_milliseconds` |
| `RALLY_RESULTS_RETENTION_DAYS` | `7` | Days to keep results before pruning; decimal integer `0..36500` (larger values are clamped to `36500` with a warning) |
| `PROVENANCE_RETENTION_DAYS` | `90` | Days to keep `rally_project_ids.log` (the RGW auto-purge authorization ledger). Deliberately decoupled from `RALLY_RESULTS_RETENTION_DAYS`; decimal integer `0..36500`, where `0` is raised to `1` with a warning (it would erase the ledger every run) |
| `UPTIME_WINDOW_DAYS` | `30` | Window in days for the dashboard uptime percentages; decimal integer `1..36500` (`0` is raised to `1` and larger values clamped, with a warning) |
| `RALLY_NOVA_FLAVOR` | `m1.tiny` | Flavor name for Nova scenarios |
| `RALLY_NOVA_IMAGE` | `cirros-0.6.2-x86_64-disk` | Image name for Nova scenarios |
| `RALLY_NEUTRON_NETWORK_CIDR` | `10.99.0.0/24` | CIDR for Neutron test networks |
| `RGW_ADMIN_URL` | — | RGW admin API endpoint (enables RGW orphan detection/purge) |
| `RGW_ACCESS_KEY` | — | S3 access key for RGW admin user (`buckets=*;users=*` caps) |
| `RGW_SECRET_KEY` | — | S3 secret key for RGW admin user |
| `RGW_REGION` | — | Explicit SigV4 region for RGW (omit if not required) |
| `RGW_PURGE_TIMEOUT` | `600` | Seconds allowed for one orphan-user delete with `purge-data` (it removes every bucket and object of that user in a single request) |
| `NOTIFY_WEBHOOK_URL` | — | Webhook URL for smoke-status transition notifications (enables the feature). Treated as a secret — may embed a token and lands in `/rally/rally_env` (mode 0640) |
| `NOTIFY_FORMAT` | `generic` | Webhook payload shape: `generic` (raw JSON), `slack` (`{"text": …}`), or `discord` (`{"content": …}`) |
| `NOTIFY_DASHBOARD_URL` | — | Optional dashboard URL embedded in the notification payload / chat message |
| `EXPORTER_HOST_PORT` / `DASHBOARD_HOST_PORT` | `9101` / `8080` | Compose-only host ports for the `127.0.0.1` mappings (falling back to `EXPORTER_PORT` / `DASHBOARD_PORT` from the same place). Set in the shell or `docker/.env`, not the root `.env` (see Configure Prometheus) |
| `EXPORTER_PORT` / `DASHBOARD_PORT` | `9101` / `8080` | In-container listener ports, decimal integer `1..65535`. Pinned by the compose file; only meaningful when running the image without compose |

The entrypoint validates every numeric setting above. A malformed schedule interval, timeout or port (for example `5m`) stops the container at startup. The three day counts never do: an out-of-range day count is clamped and a malformed one (for example `30d`) falls back to its default, both with a warning, so a value that ran before validation existed cannot crash-loop the container. Scripts started directly (cron, `docker exec`) apply the same fallbacks to day counts and to the health-check interval and timeout rather than aborting mid-run.

Cron step fields reset at hour and day boundaries; these settings are schedule
requests, not guaranteed elapsed-time delays. A minute step that does not divide
60 has one shorter gap at the next hour, and an effective whole-hour step that
does not divide 24 has one shorter gap at midnight.

## Scenarios

Each service has a dedicated YAML scenario in `rally/scenarios/`. The monitored set is configurable via `RALLY_SERVICES` (default: all six below); a configured name without a matching `rally/scenarios/<name>.yaml` is skipped with a `SKIP` log line rather than failing the run. A service whose scenario file exists but whose Rally task never starts (no task UUID) is reported **failed** with `error: "task_failed_to_start"`, not skipped; the dashboard modal and its "Full Rally report" show the tail of the task log as the reason.

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
| `rally_api_overall_up` | — | Overall API health, **fail-closed**: `up`/`degraded` → 1; everything else → 0 — `down`, `unknown`/missing overall (e.g. the seed `health.json` on a fresh volume, or a corrupt/missing file), and any unrecognized value. Semantics: `1` = the most recent health data reports overall reachability; `0` = down OR no valid signal. When the health pipeline breaks, the per-service `rally_api_up` series vanish (so they cannot fire `== 0`) and only this gauge can still signal the loss — see `RallyApiSignalLost`. A `health.json` older than three health-check intervals counts as stale: per-service series are dropped and this gauge reads 0 |
| `rally_api_last_check_timestamp` | — | Epoch time of the last health check the exporter read; kept while `health.json` is missing or corrupt, 0 until a first check is seen (so a fresh container does not alert); drives `RallyApiHealthStale` |
| `rally_api_stale_after_seconds` | — | Age after which `health.json` counts as stale: three health-check intervals (45 min at the default), never less than one interval plus `HEALTH_CHECK_TIMEOUT` + 60 s |
| `rally_schedule_interval_seconds` | — | Longest gap cron leaves between Rally runs (`RALLY_SCHEDULE_INTERVAL` after the whole-hour round-up) |
| `rally_last_run_end_timestamp` | — | When the last run finished: its start plus `rally_run_duration_seconds` (just its start for a run aborted during setup); `RallyStaleResults` measures from here |
| `rally_run_setup_ok` | — | 0 when the last run aborted during setup, before any test (its summary carries a top-level `error`: `deployment_setup_failed` or `task_args_write_failed`), else 1. Such a run also clears the per-service series and sets `rally_overall_success` and `rally_run_duration_seconds` to 0. Only meaningful while `rally_data_valid` is 1 |
| `rally_announcement_active` | `type` | Count of currently-active announcements per type; all three labels (`incident`, `maintenance`, `scheduled`) are always emitted (0 when none) |
| `rally_maintenance_mode` | — | 1 when any `maintenance`-type announcement is active, else 0 (drives the optional alert inhibition below) |
| `rally_cleanup_scan_ok` | — | 1 when every OpenStack orphan listing in the last `cleanup_monitor.sh` scan succeeded, 0 when any list call failed or returned unparseable output (`openstack_scan_status`/`openstack_scan_errors` in `cleanup_metrics.json`). A failed listing reads as 0 orphans for its type, so the orphan gauges cannot be trusted while this is 0; drives `RallyCleanupScanDegraded`. Resource types of services outside `RALLY_SERVICES` (keystone is always scanned), or with no endpoint in the service catalog, are skipped, not errors |

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
| `RallyCleanupScanDegraded` | warning | An OpenStack orphan listing failed in the last cleanup scan for >15 min — the orphan counts for that type read as 0 and cannot be trusted |
| `RallyTestFailure` | warning | A scenario failed |
| `RallyServiceDown` | critical | Entire service is failing |
| `RallySLABreach` | warning | SLA criteria not met |
| `RallyApiDown` | critical | A service's lightweight API health check has reported down for >20 min |
| `RallyApiSignalLost` | warning | `rally_api_overall_up == 0` for >60 min — a sustained overall outage OR a broken/stale health pipeline (corrupt, missing, or stale `health.json`, whose absent per-service series cannot fire `RallyApiDown`) |
| `RallyApiHealthStale` | warning | `health.json` older than `rally_api_stale_after_seconds` (45 min at the default) for >15 min — the checks stopped, not necessarily the cloud |
| `RallyStaleResults` | warning | No run finished for over two schedule intervals plus the last run's duration (8 h + run time at the default `RALLY_SCHEDULE_INTERVAL=240`) |
| `RallyRunSetupFailed` | critical | The last run aborted during setup, before any test — `error` in `latest_summary.json` says which step (`deployment_setup_failed`: Keystone auth or `OS_AUTH_URL`; `task_args_write_failed`: `/results` not writable) |
| `RallyOverallFailure` | critical | One or more services failing (an aborted run is left to `RallyRunSetupFailed`) |

`RallyApiDown` fires on `rally_api_up == 0` with `for: 20m` (two consecutive 15-minute health checks), making it the fastest available outage signal — far ahead of the ~4-hour Rally cadence. A `degraded` (slow-but-reachable) service keeps `rally_api_up == 1`, so alert on `rally_api_latency_milliseconds` if you want a latency-based warning. `RallyApiSignalLost` (warning, `for: 60m`) is the fail-closed companion: `rally_api_overall_up` is `0` on a real overall outage AND when the health pipeline itself breaks (corrupt or missing `health.json`). In the broken-pipeline case the per-service `rally_api_up` series are absent and cannot fire `RallyApiDown`, so this catch-all is the only signal that survives a blind exporter — `RallyApiDown` stays the page-worthy critical, `RallyApiSignalLost` stays a warning. The exporter also treats a `health.json` older than three check intervals as stale (the checker uses a 30 s per-request API timeout, `health_check.sh` kills a run after `HEALTH_CHECK_TIMEOUT` (300 s), and cron is a supervised process), so a stalled pipeline reaches this alert too; `RallyApiHealthStale` names that case specifically.

Every alert carries a `runbook` annotation. Purge runbooks always preview with a dry run (`docker exec -u rally rally-monitor /scripts/purge_orphans.sh`) before `--confirm`.

### Maintenance inhibition (opt-in)

`RallyTestFailure`, `RallyServiceDown`, and `RallySLABreach` ship with commented-out maintenance-inhibition variants. Uncomment the `… unless on(job, instance) rally_maintenance_mode == 1` form of each `expr` to suppress these flappy alerts while an operator has posted a `maintenance`-type announcement, so a single `announce.sh post --type maintenance` silences both the dashboard banner and the alerts during a planned window. `rally_maintenance_mode` carries only the scrape labels (`job`, `instance`), so the `unless` matches `on(job, instance)` — the same reason the labeled test alerts guard with `and on(job, instance) rally_data_valid == 1` (a bare `and` compares every label and never matches a `service`-labeled series).

The rules have promtool unit tests in `prometheus/rally_alerts_test.yml`: `promtool test rules prometheus/rally_alerts_test.yml` (the pytest suite runs them when `promtool` is on `PATH`).

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

- `run_tests.sh` automatically purges **Rally-owned** RGW orphans after each test run via `auto_purge_rgw()` (in `rgw_helpers.sh`). Project IDs must appear in the provenance ledger (`/results/rally_project_ids.log`), which a per-task tracker (`scripts/rally_project_tracker.py`, one openstacksdk session polling every 5 s) fills with every `c_rally_*` project a task creates. If the tracker cannot take its pre-task snapshot it records nothing for that task (fail-closed: its orphans stay unknown-owner). Non-Rally orphans are never touched.
- Deletion is one `DELETE /admin/user` per orphan; for Rally-owned users it sets `purge-data=true` so RGW removes the user's buckets from its own (tenant-correct) bucket listing. Buckets are never deleted by bare name: implicit-tenant buckets live in tenant `<project_id>`, and before Ceph Squid (19) the Remove Bucket op ignores the tenant and would target a same-named bucket in the global tenant. Empty unknown-owner users (manual purge only) are deleted *without* `purge-data`, so RGW refuses if one gained a bucket since the dry listing. The manual purge re-inventories each user's buckets just before deleting it from the stats-free bucket listing (the stats listing can silently omit buckets on Ceph Reef/Quincy; if the two disagree the user is skipped), so its counts, threshold check, and audit log (`rgw_buckets_purged`) reflect what was actually removed; an already-absent user (404) counts as nothing deleted, and after a failed purge-data request the user is re-checked and the actual outcome (deleted, partly purged, or `rgw_users_indeterminate`) is recorded. After a request timeout RGW may still be deleting, so the purge keeps re-checking with backoff for about 5 minutes and counts the user as deleted only if it disappears; otherwise it is recorded as indeterminate
- The monitor's OpenStack credentials must be cloud-admin (see every project in every domain): orphan detection treats a project missing from the list as deleted. Malformed Keystone or OpenStack listings fail the whole scan rather than being partially trusted
- Orphan detection only trusts a complete Keystone project list: the bulk list is read from the raw `GET /v3/projects` response (`scripts/keystone_projects.py`), and if Keystone's `list_limit` truncated it, the scan falls back to one authoritative existence check per project (all over one session) instead of treating unlisted projects as deleted
- `cleanup_monitor.sh` then queries the RGW admin API for implicit-tenant users, cross-references against Keystone, and reports post-purge orphan counts in `cleanup_metrics.json` with a scan health status (`ok`/`skipped`/`error`)
- `purge_orphans.sh` is available for manual bulk cleanup of all resource types (including RGW) when needed. Unlike the automatic purge, it also reaps **empty unknown-owner** RGW orphans (Keystone project confirmed gone + zero buckets/objects) — data-safe because empty implicit-tenant users self-heal — which clears pre-provenance-ledger orphans the ledger gate cannot vouch for. Unknown-owner users that still hold data are always skipped
- All operations are **fail-closed**: scan errors or inconclusive Keystone lookups block destructive purge and surface as degraded scan status rather than false zeros

If RGW credentials are not set, all RGW features are silently skipped — existing functionality is unaffected.

## Operator Announcements

The dashboard renders a single banner above the 7-day timeline that operators can publish to via `docker exec`. Three types are supported:

| Type          | Color    | Lifecycle |
|---------------|----------|-----------|
| `incident`    | red      | Active until cleared manually, or auto-cleared after the next all-green Rally run |
| `maintenance` | amber    | Active until `--expires-at`, or until cleared manually |
| `scheduled`   | blue     | Hidden until `--effective-from`, then visible until `--expires-at` |

All examples below run as the `rally` user (started as root, the script re-executes itself as `rally`). `announce.sh --help` (or `<subcommand> --help`) lists the per-type flag rules and an example.

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

# Clear an announcement explicitly (idempotent — exits 0 even if the id is gone,
# but reports "cleared <id>" or "no announcement with id <id>" on stderr)
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
- `NOTIFY_WEBHOOK_URL` may embed a token. Like `OS_PASSWORD` it lands in `/rally/rally_env` (mode 0640) so cron jobs can read it — treat it as a secret. The URL is never written to the logs and never placed on curl's command line (it is passed through a `-K` config on a file descriptor, so it does not show in `ps`); the RGW admin key pair is handled the same way.

## Useful Commands

Run scripts with `-u rally`. A script started as root (a `docker exec` without `-u rally`) re-executes itself as `rally` with a notice, so it can no longer leave root-owned lock or state files that break later cron runs. Leftovers from root runs of older images are repaired at boot where possible (unwritable lock and state files are replaced with rally-owned copies). Root-owned run directories cannot be fixed from inside the container (`CAP_CHOWN` is dropped); the boot log and every prune name them, and one host-side command fixes them:

```bash
docker volume ls | grep rally-results   # find your volume name
docker run --rm -v <volume>:/results busybox chown -R 1500:0 /results
```

```bash
# Trigger a manual test run
docker exec -u rally rally-monitor /scripts/run_tests.sh

# Run a lightweight health check (read-only, non-destructive)
docker exec -u rally rally-monitor /scripts/health_check.sh

# Run orphan detection manually (read-only, updates Prometheus metrics)
docker exec -u rally rally-monitor /scripts/cleanup_monitor.sh

# Dry-run purge: list all orphaned resources without deleting anything
docker exec -u rally rally-monitor /scripts/purge_orphans.sh

# Purge orphaned resources (permanently deletes s_rally_* and c_rally_* resources).
# Refuses (exit 4) while a Rally test run holds the run lock, since a live run's
# resources look exactly like orphans; a scheduled run that fires during the
# purge is skipped (logged as SKIPPED). A dry run only warns, from
# run_state.json, and never touches the lock. Exit 3 (dry run
# too) means a list call failed and the listing is incomplete.
docker exec -u rally rally-monitor /scripts/purge_orphans.sh --confirm

# View live logs
docker logs -f rally-monitor
docker exec rally-monitor tail -f /rally/logs/rally-tests.log
docker exec rally-monitor tail -f /rally/logs/health-check.log
```

> **Orphan prefixes:** `s_rally_*` resources are created by scenario plugins and cleaned up during the test. `c_rally_*` resources are created by context plugins (projects, users, networks) and cleaned up after the task completes. Both are detected by `cleanup_monitor.sh` and removable via `purge_orphans.sh`, but reported at different severities — see the alert table above.

## Continuous Integration

`.github/workflows/build-push.yml` runs two gate jobs before the build. The `test` job runs `shellcheck` and the full pytest suite (`exporter/`, `dashboard/`, `scripts/`) on Python 3.13 — matching the `python:3.13-slim` runtime — with a pinned, checksum-verified `promtool` installed so the alert-rule unit tests run too. The `audit` job runs `pip-audit` (itself installed from a hashed lock, `.github/ci/requirements-audit.txt`) against the image's hashed lockfile and fails on any known vulnerability; it also runs weekly on a schedule, which only audits and never builds or pushes. The test dependencies are installed constrained to the image lock, so the suites run against the image's transitive versions. The `build-push` job declares `needs: [test, audit]`, so a failure in either blocks the image build and publication entirely. Images are pushed to GitHub Container Registry (`ghcr.io/<owner>/<repo>`) on push to `main`, on `v*` tags, and via manual dispatch; pull requests build only (no push). Every action is pinned to a full commit SHA (the release tag is in a trailing comment).

Dependencies are pinned for reproducible images. The image's direct pins (the exporter's `flask`, `prometheus-client` and `gunicorn`, and the OpenStack toolchain `rally-openstack`, `rally`, `python-openstackclient`) live only in `docker/requirements.in`, and `docker/requirements.txt` is the pip-compile lockfile generated from it: every transitive package is pinned and SHA-256-hashed, and the Dockerfile installs it with `--require-hashes` from wheels only (`murano-pkg-check`, which has no Python 3 wheel, is built from its hashed sdist with build dependencies taken from the same lock), so the image needs no C toolchain. After changing a direct pin, regenerate the lock with the command documented in `docker/requirements.in`; `exporter/test_requirements_lock.py` fails if the two disagree. `exporter/requirements.txt` names the exporter packages unpinned and constrains them with `-c ../docker/requirements.txt`, so a local `pip install -r requirements.txt` gets exactly the image's versions (in pip's `--require-hashes` mode, because the lock carries hashes). The base image is pinned by digest. `.github/dependabot.yml` opens weekly PRs (pip for `/docker`, which should regenerate the lock in the same PR because the pins live in an `.in` file — the only case where Dependabot re-runs pip-compile — though that is not yet confirmed, and `exporter/test_requirements_lock.py` fails any PR that leaves the pins and the lock out of step; `pytest` in `/exporter`; `pip-audit` in `/.github/ci`; the docker base image; and GitHub Actions), each running through the gates above, and all but the base image waiting out a 14-day release cooldown (base-image digests get no advisory-based security updates, so they are not delayed), so version drift is always a reviewed, tested change rather than something that happens silently on a rebuild. The vendored Chart.js and Inter font (SHA-256-pinned in the Dockerfile) and the CI `promtool` version are outside Dependabot's reach and are bumped by hand.

To run the suite locally (`python -m pytest exporter/ dashboard/ scripts/`), **bash >= 4 must be on `PATH`** — the script-suite tests resolve bash via `shutil.which("bash")` and the scripts use `mapfile` (a bash 4 builtin). macOS ships bash 3.2 at `/bin/bash`, so `brew install bash` and ensure it precedes `/bin/bash` on `PATH`.

## Project Structure

```
openstack-rally-monitor/
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── requirements.in       (direct pins)
│   └── requirements.txt      (hashed lockfile, generated)
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
│   ├── run_lib.sh
│   ├── health_check.sh
│   ├── cleanup_monitor.sh
│   ├── purge_orphans.sh
│   ├── rgw_helpers.sh
│   ├── render_report.py
│   ├── backfill_reports.sh
│   └── patch_rally.py
├── exporter/
│   ├── rally_exporter.py
│   ├── health_metrics.py
│   ├── announcement_metrics.py
│   ├── metrics_common.py
│   ├── requirements.txt
│   ├── requirements-test.txt
│   ├── test_*.py, conftest.py, exporter_testlib.py
├── prometheus/
│   ├── prometheus.yml
│   ├── rally_alerts.yml
│   └── rally_alerts_test.yml
├── dashboard/
│   ├── index.html
│   ├── style.css
│   ├── app.js
│   ├── report.css
│   ├── report.js
│   ├── serve.py
│   ├── themes/
│   │   └── default/
│   ├── test_serve.py
│   └── test_serve_http.py
├── docs/
│   └── CUSTOMIZING.md
└── env.sample
```

## License

MIT
