# Changelog

## [1.1.0] - 2026-02-28

### Summary

Reliability, security, and observability improvements across the container, exporter, and dashboard. No breaking changes to the Rally scenario configuration or Prometheus scrape target.

### Added

- **Prometheus exporter** — production gunicorn serving (replaces Flask dev server)
- **Prometheus exporter** — `/ready` readiness endpoint (returns 503 until first run completes)
- **Prometheus exporter** — `rally_run_duration_seconds` gauge (total wall time of each full run)
- **Prometheus exporter** — `rally_exporter_errors_total` counter (parse/read errors per file)
- **Prometheus exporter** — UTC-aware timestamp parsing; stale metric label cleanup on each scrape
- **Dashboard** — `dashboard/serve.py` static file server with security headers (CSP, X-Frame-Options, etc.)
- **Dashboard** — self-hosted Inter variable font and Chart.js (no runtime CDN dependency); SHA-256 verified at build time
- **Grafana** — `grafana/rally-dashboard.json` — importable Grafana 10+ dashboard with 8 panels
- **Docker** — `rally` non-root system user; gunicorn and dashboard server run as `rally`
- **Docker** — `docker/logrotate.conf` with 10 MB / 3-rotation / copytruncate policy
- **Docker** — resource limits (`mem_limit: 2g`, `cpus: 2.0`) and capability hardening (`cap_drop: ALL`, `cap_add: SETUID SETGID`)
- **Scripts** — cron schedule validation with floor/ceiling division and bounds checking
- **Scripts** — atomic file writes (`tmp` + `mv`) and `flock` lockfiles across all scripts

### Changed

- Log files moved: `/var/log/rally-tests.log` → `/rally/logs/rally-tests.log`, `/var/log/health-check.log` → `/rally/logs/health-check.log`
- Environment file moved: `/etc/rally_env` → `/rally/rally_env`
- Cron job user field changed from `root` to `rally`
- `docker-compose.yml`: removed deprecated `version:` field

### Migration Guide (existing deployments)

#### 1. Transfer /results volume ownership (required)

The `rally` non-root user now owns `/results`. Existing volumes are root-owned and must be migrated once before upgrading.

Run this **before** rebuilding the image, using a temporary busybox container with the correct volume name.

The volume name is prefixed by your Docker Compose project name (the directory containing `docker-compose.yml`). With the default layout the volume is named `docker_rally-results`:

```bash
# Find the exact volume name if unsure
docker volume ls | grep rally-results

# Run the migration (replace docker_rally-results with your actual volume name)
docker run --rm -v docker_rally-results:/results busybox chown -R 997:0 /results
```

Note: `docker exec ... chown` will not work because `cap_drop: ALL` removes `CAP_CHOWN` from the running container. The busybox container above runs with default Docker capabilities (which include `CAP_CHOWN`).

UID 997 is the `rally` system user. Verify with:
```bash
docker run --rm <new-image> id rally
```

#### 2. Rebuild and restart

```bash
cd docker
docker compose down
docker compose up -d --build
```

#### 3. Verify ownership

```bash
docker exec rally-monitor ls -la /results
# Expected: drwxr-x--- rally root ...

docker exec rally-monitor ls -la /rally/logs
# Expected: -rw-rw---- root root rally-tests.log
```

#### 4. Import Grafana dashboard (optional)

In Grafana: Dashboards → Import → Upload JSON file → select `grafana/rally-dashboard.json`. Set the Prometheus datasource when prompted.

---

## [1.0.0] - 2026-02-27

Initial release. Containerized Rally OpenStack test runner with:

- Rally scenarios for keystone, nova, neutron, glance, cinder, swift
- Prometheus exporter on `:9101/metrics`
- Dark-theme HTML dashboard on `:8080`
- Cron-scheduled test runs and API health checks
- Orphan detection for `s_rally_*` and `c_rally_*` resources
- Prometheus alert rules in `prometheus/rally_alerts.yml`
