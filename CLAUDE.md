# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Rally OpenStack Monitor — a containerized tool that runs OpenStack cloud tests via Rally on a cron schedule, exposes results as Prometheus metrics, detects orphaned resources from failed cleanups, and serves a read-only dark-theme dashboard.

## Commands

### Build and Run

```bash
# Build and start the container
cd docker && docker-compose up -d --build

# Trigger a manual test run inside the running container
docker exec rally-monitor /scripts/run_tests.sh

# Run a lightweight health check (non-destructive read-only API calls)
docker exec rally-monitor /scripts/health_check.sh

# Run cleanup orphan detection manually
docker exec rally-monitor /scripts/cleanup_monitor.sh /results/latest_summary.json

# Purge orphaned Rally resources (dry-run — shows what would be deleted)
docker exec rally-monitor /scripts/purge_orphans.sh

# Purge orphaned Rally resources (actually deletes them)
docker exec rally-monitor /scripts/purge_orphans.sh --confirm

# View live logs
docker logs -f rally-monitor
docker exec rally-monitor tail -f /var/log/rally-tests.log
docker exec rally-monitor tail -f /var/log/health-check.log
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
  → rally task results <uuid>          (JSON to /results/<timestamp>/<service>.json)
  → build_summary()                    → /results/latest_summary.json
  → cleanup_monitor.sh                 → /results/cleanup_metrics.json
  → publish_dashboard_files()          → /results/results.json, history.json

health_check.sh
  → openstack <service> list (read-only)
  → /results/health.json, health_history.json (rolling 672-entry window)

rally_exporter.py
  → reads /results/latest_summary.json + cleanup_metrics.json on each /metrics scrape
  → updates Prometheus Gauges in memory
```

### File Layout (runtime volumes)

- `/results/` — Docker volume `rally-results`; persists across restarts
  - `latest_summary.json` — current run summary (seed file created on first boot)
  - `cleanup_metrics.json` — orphaned resource counts from `cleanup_monitor.sh`
  - `results.json` — combined summary + cleanup for dashboard
  - `history.json` — all retained per-run summaries for the timeline
  - `health.json` / `health_history.json` — API health check results
  - `<TIMESTAMP>/` — per-run directories with `<service>.json`, `<service>.html`, `run.log`
- `/rally/data/` — Docker volume `rally-data`; SQLite DB for Rally state
- `/dashboard/` — ephemeral; `results.json` etc. are symlinks → `/results/`

### Orphan Detection

`scripts/cleanup_monitor.sh` runs after each Rally test suite. It queries each OpenStack service for resources prefixed with `s_rally` (scenario resources) or `c_rally` (context resources — projects, users, networks created by Rally contexts) and writes counts to `cleanup_metrics.json`. The exporter exposes these as `rally_cleanup_failure` and `rally_orphaned_resources` Prometheus metrics.

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
| `EXPORTER_PORT` / `DASHBOARD_PORT` | `9101` / `8080` | Exposed ports |
| `RALLY_DEBUG` | `false` | Set to `true` for verbose rally task logging |

### Cron Environment

Environment variables are exported to `/etc/rally_env` (mode 0600) at container startup and sourced by cron jobs. The file contains `OS_PASSWORD` — do not loosen its permissions.

### patch_rally.py

`scripts/patch_rally.py` is applied at Docker build time to patch Rally's internal password-generation policy. It runs once inside the image build (via `RUN python3 /scripts/patch_rally.py` in the Dockerfile) and does not need to be re-run manually.

### Prometheus Integration

- Scrape target: `<host>:9101/metrics`
- Alert rules: copy `prometheus/rally_alerts.yml` to your Prometheus rules directory and add it under `rule_files:` in `prometheus.yml`
