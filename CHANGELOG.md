# Changelog

## [Unreleased]

### Added

- **Dashboard** — operator announcement banner above the 7-day timeline. Three types are supported with distinct lifecycles: `incident` (active until cleared, or auto-cleared after the next all-green Rally run), `maintenance` (active until `--expires-at`), and `scheduled` (hidden until `--effective-from`, then visible until `--expires-at`). The banner shows a visible `[TYPE]` label alongside type-colored borders so color is not the sole signal. A native `<details>` element exposes progress updates and other concurrent records; the open state survives the 5-minute auto-refresh when the same primary record is still active. Outer edges align with section content (`max-width: calc(1400px - 4rem)`) so the banner sits flush with the timeline cards below
- **CLI** — `scripts/announce.sh post | update | clear | list | auto-clear-if-all-green`. Bodies are plain text (max 500 chars); timestamps must be ISO 8601 UTC and are validated semantically before reaching `jq`. State is persisted atomically to `/results/announcement-state.json` via `tmpfile + mv`. ID format `<type>-<YYYYMMDDTHHmmssZ>-<8 hex chars>`. Invoke via `docker exec -u rally rally-monitor /scripts/announce.sh ...`
- **Scripts** — `scripts/run_tests.sh` invokes `announce.sh auto-clear-if-all-green` after `build_summary` and before `publish_dashboard_files`. The predicate `(.services | length) > 0 AND (.error // null) == null AND (.services | to_entries | all(.value.status == "passed"))` guards against the `deployment_setup_failed` shape — the empty-services check is critical because `jq all(...)` returns `true` on empty iteration, which would silently erase the very banner the operator posted to explain the failure
- **Scripts** — every mutating subcommand wraps its read-modify-write cycle in `flock` on `/results/.announce.lock`, preventing an operator post/update/clear from racing with a cron-driven `auto-clear-if-all-green`. Falls through to direct execution (single-operator behavior) when `flock` is unavailable
- **Server** — `dashboard/serve.py` `ALLOWED_JSON_SYMLINKS` extended with `announcement-state.json`. Same containment as the existing entries (`target.is_relative_to(RESULTS_ROOT)`); no new attack surface
- **Tests** — `scripts/test_announce.py` covers CLI behavior (35 cases including the empty-services guard, ISO 8601 semantic validation, ID-entropy under burst posts, atomicity, and a corrupt-state-file refusal). `dashboard/test_serve.py` extended with allowlist regression coverage for the new entry

### Changed

- **Docs** — README gains an "Operator Announcements" section; CLAUDE.md gains an "Announcement System" architecture subsection that explains the deliberate placement at `/results/announcement-state.json` (outside `/results/branding/` to avoid the documented read-only theme bind-mount collision)

### Fixed

- **Scripts** — `mirror_job_logs.sh` no longer writes to `/proc/1/fd/1`; cron jobs run as `rally` (UID 1500) and the cross-uid write to root-owned PID 1 stdout failed with `Permission denied`, aborting the pipeline before `tee` reached the log file. `entrypoint.sh` now spawns a `tail -F` of the job log files as a PID 1 child to forward output into `docker logs`
- **Scripts** — `purge_orphans.sh` no longer narrows listings to `OS_PROJECT_NAME`. Rally creates orphan resources inside `c_rally_*` context projects, not the service account's project, so the redundant `--project`/`--owner` filter masked every scenario-leaked server, volume, router, security group, network, and image. Detection now matches the always-on `cleanup_monitor.sh` (admin scope + name-prefix match across all projects)
- **Scripts** — `purge_orphans.sh` resets stuck volumes (`error_deleting` / `deleting` / `error`) to `error` via `openstack volume set --state` before issuing `openstack volume delete --force`. Cinder protects the `error_deleting` state from a plain delete, leaving volumes from failed scenario cleanups un-purgeable until manually intervened

---

## [1.3.0] - 2026-05-04

### Added

- **Dashboard** — theme customization via `themes/<name>/` directory contract. Default theme ships at `dashboard/themes/default/` with `tokens.css`, `logo.svg`, and `favicon.svg`. Operators can supply optional `tokens.css`, `overrides.css`, `logo.svg`, or `favicon.svg` under `/results/branding/` (symlinked at runtime to `/dashboard/themes/custom`). Missing custom files transparently fall through to defaults
- **Dashboard** — `dashboard/style.css` `:root` extracted to a public token contract (surfaces, text, status, brand, gradients, chart series, chart chrome). Internal radius/shadow/glass tokens remain unstable
- **Dashboard** — Chart.js datasets and chrome (axis labels, ticks, grid) now read colors from CSS custom properties at render time instead of hardcoded hex
- **Server** — `dashboard/serve.py` allowlist extended with a third branch for theme assets gated by prefix (`themes/`), suffix (`.css`/`.svg`/`.png`/`.ico`), and tightened containment (`SERVE_ROOT/themes/` or `${RESULTS_DIR}/branding/`); MIME type derived from the requested suffix so operator symlinks inside `branding/` cannot mask their type
- **Docs** — `docs/CUSTOMIZING.md` documents the public token contract, override workflows, fallback semantics, and what does and does not reskin

### Changed

- **Dashboard** — header logo replaced inline SVG with `<img src="themes/default/logo.svg">`; `<img>`-loaded SVG does not inherit page CSS variables, so custom themes must supply their own `logo.svg` to recolor the brand mark

---

## [1.2.3] - 2026-03-07

### Fixed

- **Docker** — `rally db recreate` moved from Dockerfile build step to `entrypoint.sh` startup; previously ran as root during the image build, creating `/tmp/rally.sqlite` owned by root and unwritable by the `rally` user at runtime
- **Scripts** — `run_tests.sh` lockfile path changed from `/tmp/rally-run.lock` to `/rally/logs/rally-run.lock`; `/tmp` is not writable by the `rally` user when `cap_drop: ALL` is set
- **Docs** — all `docker exec` commands now include `-u rally` flag; without it, commands run as root which — due to `cap_drop: ALL` removing `DAC_OVERRIDE` — cannot write to `/results` (owned by `rally`, mode 755)
- **Docs** — `docker-compose` command updated to `docker compose` throughout README and CLAUDE.md

---

## [1.2.2] - 2026-03-02

### Fixed

- **Dashboard** — timeline tooltip text appeared vertically stretched because the tooltip inherited its parent cell's `scaleY` hover transform; fixed by applying a counter-scale (`scaleY(1/1.3)` on hover, `scaleY(1/1.4)` on selected state) so tooltip text renders at normal proportions

---

## [1.2.1] - 2026-03-01

### Fixed

- **Dashboard** — health check timeline tooltip was clipped by `overflow: hidden` on the container; changed to `overflow: visible` so tooltips render above the bar correctly
- **Dashboard** — health check timeline now trims rendered cells to the maximum count that physically fits (2px cell + 2px gap per slot, minus 32px container padding); excess history is no longer rendered off-screen
- **Dashboard** — health check count badge shows `"N of total checks"` when the visible count is less than the full history, making the trim visible to the user

---

## [1.2.0] - 2026-03-01

### Summary

Security hardening, exporter performance, test suite, and container stability improvements. No breaking changes to Rally scenarios, Prometheus scrape target, or environment variables.

Existing deployments upgrading from v1.1.0: if your volume was created by v1.1.0 or earlier (before the `rally` UID was pinned), run the ownership migration below before rebuilding.

### Added

- **Prometheus exporter** — `rally_data_valid` gauge: `1` when valid result data is available, `0` otherwise (useful for alerting on stale/missing results)
- **Prometheus exporter** — mtime-based file caching: skip JSON re-reads when `latest_summary.json` and `cleanup_metrics.json` have not changed between scrapes (60-second max-age safety fallback)
- **Tests** — `exporter/test_rally_exporter.py`: unit + integration tests covering metric generation, caching, error paths, and timestamp parsing
- **Tests** — `dashboard/test_serve.py`: unit tests for the static file server (security headers, path traversal prevention, content-type mapping)
- **Scripts** — `purge_orphans.sh`: threshold guard — refuses to purge if orphan count exceeds a safety limit, preventing accidental mass deletion

### Changed

- **Docker** — `rally` user UID pinned to `1500` in the Dockerfile (`useradd -u 1500`); previously assigned dynamically by `useradd -r`, which caused ownership drift when upgrading between Python base image versions
- **Exporter** — cleanup metric service-mapping extracted into `_SVC_MAP`; cleanup and summary metrics now independently tracked and applied
- **Dashboard** — `escapeHtml()` utility applied to all `innerHTML` sinks (service names, scenario names, timestamps) to prevent XSS from crafted result files
- **Dashboard** — dead timer-display IIFE removed from `app.js`
- **Scripts** — `patch_rally.py` uses `sysconfig.get_paths()["purelib"]` for dynamic site-packages path discovery instead of a hardcoded glob
- **Scripts** — `purge_orphans.sh` refactored: `_list_orphans()` generic helper extracted; duplicate per-service loops replaced with a single parameterized call using bash namerefs
- **Scripts** — `run_tests.sh`: `task_args.json` written once in `main()` instead of once per service; removed useless `cat` pipes
- **Entrypoint** — ownership mismatch detection at startup: logs a warning with actionable remediation steps if `/results` is not owned by the `rally` user (e.g., after an upgrade with an existing volume); no exit — container starts regardless to allow inspection
- **Dockerfile** — `apt-get install`, `pip install`, and build-dep removal consolidated into a single `RUN` layer to reduce image layers and final size

### Fixed

- **Security** — cron environment export uses `printf %q` quoting to prevent shell injection if an `OS_*` variable contains special characters
- **Security** — `purge_orphans.sh` and `cleanup_monitor.sh`: path containment check prevents traversal outside `/results` when writing output files
- **Security** — `docker-compose.yml` ports bound to `127.0.0.1` by default, preventing unintended external exposure

### Migration Guide (upgrading from v1.1.0)

#### Check if volume ownership migration is needed

If you are upgrading from v1.1.0 and your volume was created before the UID was pinned (i.e., the `rally` user was assigned a dynamic UID such as 997, 999, or similar), you must migrate volume ownership once.

At startup, the container will log a warning if a mismatch is detected:

```
[entrypoint] WARNING: /results is owned by UID 997 but rally user is UID 1500
```

If you see this warning, run the migration below before or after the upgrade:

```bash
# Find the exact volume name (prefixed by your Compose project name)
docker volume ls | grep rally-results

# Migrate ownership (replace docker_rally-results with your actual volume name)
docker run --rm -v docker_rally-results:/results busybox chown -R 1500:0 /results
```

Note: `docker exec ... chown` will not work because `cap_drop: ALL` removes `CAP_CHOWN`. The busybox container above runs with default Docker capabilities (which include `CAP_CHOWN`).

#### Rebuild and restart

```bash
cd docker
docker compose down
docker compose up -d --build
```

---

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
docker run --rm -v docker_rally-results:/results busybox chown -R 1500:0 /results
```

Note: `docker exec ... chown` will not work because `cap_drop: ALL` removes `CAP_CHOWN` from the running container. The busybox container above runs with default Docker capabilities (which include `CAP_CHOWN`).

UID 1500 is the pinned `rally` user UID (fixed in the Dockerfile to avoid drift between base image rebuilds). Verify with:
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
