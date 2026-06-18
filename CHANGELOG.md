# Changelog

## [Unreleased]

### Added

- **Config** — `RALLY_SERVICES` environment variable (default `keystone,nova,neutron,glance,cinder,swift`) makes the monitored service set configurable across `run_tests.sh`, `api_health_check.py`, and the `entrypoint.sh` seed (pending cards are generated from the set, not a hardcoded six). All three parse it with identical normalization (trim, lowercase, drop empty segments, dedupe, preserve operator order), cross-referenced as keep-in-sync. Unknown names are tolerated: a smoke run logs `SKIP` for a service without a `rally/scenarios/<name>.yaml`, and the health check logs a warning and omits a name without a registered checker. `keystone` is always health-checked regardless of the configured set — the openstacksdk session authenticates against it, so its token issuance *is* a keystone availability check
- **Scripts** — `scripts/notify.sh` fires an outbound webhook on smoke-status **transitions only** (`passed`↔`failed`, compared against `/results/.last_notified_status`); identical statuses are a no-op so there is no per-run spam. Off by default (silent no-op unless `NOTIFY_WEBHOOK_URL` is set); `NOTIFY_FORMAT` selects the payload shape (`generic` raw JSON, `slack` `{"text":…}`, `discord` `{"content":…}`); `NOTIFY_DASHBOARD_URL` is optionally embedded. A missing state file baselines to `passed`, so a first-ever failed run notifies while a first-ever green run stays quiet. The state file is updated atomically **only on a successful send** (curl exit 0), so a failed POST leaves the prior baseline intact and the next run retries. Invoked from `run_tests.sh::main` on **both** the normal and `deployment_setup_failed` paths via the `RUN_SMOKE_STATUS` global — notify.sh is intentionally predicate-free and never re-derives the all-green predicate that lives in `record_smoke_result`/`announce.sh`
- **Scripts** — `scripts/api_health_check.py`: a single authenticated openstacksdk `Connection` replaces the six per-service `openstack <cmd>` CLI invocations the health check used to spawn. It authenticates once (timing `conn.authorize()` as the keystone check), then issues one cheap read-only GET per dependent service reusing the cached token (~6× fewer Keystone tokens per cycle). `health_check.sh` is now a thin cron wrapper: it holds the `flock`, runs the checker, writes `health.json` atomically, appends history via `jq -f`, and publishes a synthetic all-down document if the checker itself fails so monitoring never goes blind. Emits the same `health.json` shape on stdout
- **Scripts** — third health-check status `degraded`: a service that answers but exceeds `HEALTH_LATENCY_WARN_MS` (default 5000) is `degraded` rather than `up`/`down`. Degraded **counts as up** for availability — `health_history_filter.jq` counts any check whose `overall != "down"`, and the exporter's `rally_api_up` stays `1` — because the service is reachable, just slow. The slowness is visible in `rally_api_latency_milliseconds` and rendered as amber cells/header/live-chips on the dashboard (keep-in-sync between the jq filter and `rally_exporter.py::_apply_health_metrics`)
- **Scripts** — `run_tests.sh` extracts `first_error` per scenario in `build_summary` (the message element of the first failed iteration, capped at 300 chars; Rally iteration errors are `[type, message, traceback]`, so it takes `.error[1]` and falls back to `.error[0]` defensively) and writes a `run_state.json` (`{state: running|idle, …}`) — `running` after the run flock is held, `idle` via an `EXIT` trap that fires on every failure path. `entrypoint.sh` resets `run_state.json` to `idle` on every boot, so a stale `running` only arises from an abrupt crash
- **Dashboard** — failure causes surface in the UI: the service modal renders each scenario's `first_error` and a per-service "Full Rally report" link that opens `runs/<TIMESTAMP>/<service>.html`. A header chip shows a pulsing "Test run in progress · started …" while `run_state.json` reports `running`, distrusting a `running` state older than 24h (fail-safe against a stale chip). The latency timeline, header status, and live chips gain `degraded` (amber) rendering
- **Exporter** — `rally_api_up{service}` (degraded counts as up — reachability, not speed), `rally_api_latency_milliseconds{service}`, and unlabeled `rally_api_overall_up` (`up`/`degraded` → 1; everything else → 0, **fail-closed** — see the Fixed entry below) read from `health.json` with clear-before-set on every scrape so a service dropping out of a later snapshot does not leave a stale series. `avg_over_time(rally_api_up[30d])` is the endorsed Prometheus-side uptime derivation (uptime stays dashboard-only by design)
- **Exporter** — `rally_announcement_active{type}` (all three enum labels — `incident`/`maintenance`/`scheduled` — emitted every scrape, 0 when none) and unlabeled `rally_maintenance_mode` read from `announcement-state.json`. A missing state file yields zeros, **not** an exporter error. Active-ness is kept in sync with `dashboard/app.js::isAnnouncementActive` (the same convention as the `ALL_GREEN_PREDICATE` duplication across `announce.sh`/`run_tests.sh`); label cardinality is enum-only, so body/id text never becomes a label
- **Alerts** — new `rally_api` group with `RallyApiDown` (critical, `for: 20m` — two consecutive 15-minute checks, the fastest available outage signal) and `RallyApiSignalLost` (warning, `rally_api_overall_up == 0`, `for: 60m`; see the Fixed entry below). Commented opt-in maintenance-inhibition variants (`… unless on() rally_maintenance_mode == 1`) added to `RallyTestFailure`, `RallyServiceDown`, and `RallySLABreach`, so one operator `announce.sh post --type maintenance` can silence both the dashboard banner and the flappy alerts during planned windows
- **Grafana** — `grafana/rally-dashboard.json` gains "API Availability (per service)" and "API Latency" panels (10 panels total)
- **Config** — `HEALTH_LATENCY_WARN_MS` (default `5000`), `PROVENANCE_RETENTION_DAYS` (default `90`), `NOTIFY_WEBHOOK_URL`, `NOTIFY_FORMAT` (default `generic`), and `NOTIFY_DASHBOARD_URL` environment variables, exported to cron via the `entrypoint.sh` whitelist and documented in `env.sample`/README
- **CI** — a `test` job in `.github/workflows/build-push.yml` runs `python -m pytest exporter/ dashboard/ scripts/` on Python 3.13 (matching the `python:3.13-slim` runtime) before the image build; `build-push` declares `needs: test`, so a failing suite blocks publication. `.github/dependabot.yml` opens weekly PRs for pip (`/exporter`, `/docker`), the docker base image, and GitHub Actions, so the now-pinned dependencies stay current as reviewable, test-gated PRs instead of drifting on rebuilds
- **Build** — exact version pins for reproducible images: `docker/requirements-rally.txt` (`rally-openstack==3.0.0`, `rally==5.0.1`, `python-openstackclient==10.1.0`) isolates the OpenStack toolchain because `patch_rally.py` targets Rally module internals and the scenario YAMLs reference plugin names that upstream renames across releases — a silent bump could move the patched code or rename a plugin out from under a build; `exporter/requirements.txt` (`flask==3.1.3`, `prometheus-client==0.25.0`, `gunicorn==26.0.0`) and `exporter/requirements-test.txt` (`pytest==9.0.3`)
- **Full report** — the "Full Rally report" is now a self-contained, dashboard-themed page rendered by `scripts/render_report.py` from the persisted `rally task results` JSON (`<RUN_DIR>/<service>.json`), replacing `rally task report` (whose AngularJS page loaded d3/nvd3/Angular from `ajax.googleapis.com`/`cdnjs.cloudflare.com` — when those CDN scripts did not drive the page the nav went non-clickable with every group expanded, and it never matched the dark dashboard). `dashboard/report.js` + `report.css` render a working collapsible left nav, an overview table, and per-scenario detail (atomic-action Chart.js bar, SLA table, error tracebacks, input config) using the dashboard's own theme cascade — so a custom theme restyles reports for free. The HTML embeds its data in a non-executable `<script type="application/json">` block and references only same-origin assets (theme CSS, `report.css`, `report.js`, vendored Chart.js) by relative `../../` path, so it works offline/air-gapped and behind a sub-path proxy. Rendering is best-effort: a malformed/empty/partial results file degrades to a valid empty-state report so the dashboard link never 404s
- **Server** — `dashboard/serve.py` keeps the fourth allowlist branch for served per-run reports: `^runs/[0-9]{8}T[0-9]{6}Z/[a-z0-9_-]+\.html$` (anchored, no separators inside the captures) with re-checked `RESULTS_ROOT` containment on the resolved target, via the `/dashboard/runs -> /results` symlink. Because the report is now self-contained and same-origin, **every response — reports included — uses one strict CSP** (`script-src 'self'`, no `'unsafe-inline'` script, no external origins, no `sandbox`); the per-path relaxed `REPORT_SECURITY_HEADERS` and the per-instance header-swap/leak-reset logic were removed with the AngularJS report they served. `report.css`/`report.js` were added to `ALLOWED_STATIC`; `ALLOWED_JSON_SYMLINKS` gains `run_state.json`
- **Dashboard** — uptime percentage badges in the two timeline section headers: smoke-test uptime (Rally runs) and API uptime (health checks), each computed over `UPTIME_WINDOW_DAYS` (default 30 days). Badges stay hidden until the first uptime data is published, so pre-existing data files render unchanged
- **Scripts** — `scripts/run_tests.sh` maintains `/results/smoke_history.json`, a rolling per-run `{timestamp, status}` ledger pruned by `UPTIME_WINDOW_DAYS` and decoupled from `RALLY_RESULTS_RETENTION_DAYS`, so uptime can cover more days than the heavy per-run directories are retained. A run counts as passed only when it has services, no top-level error, and every service passed (a copy of the all-green predicate in `announce.sh`; cross-referenced in both files); deployment-setup failures are recorded as failed runs before the script exits, and `record_smoke_result` syncs the fresh uptime into an existing `results.json` so the dashboard stays accurate even on the failure path that exits before `publish_dashboard_files`
- **Scripts** — `scripts/health_check.sh` sizes the `health_history.json` rolling cap to cover `UPTIME_WINDOW_DAYS` at `HEALTH_CHECK_INTERVAL` (floored at the previous 672 entries) and embeds an `uptime` object (`window_days`, `checks_total`, `checks_up`, `percent`) computed over the window. The jq program lives in `scripts/health_history_filter.jq` (loaded via `jq -f`) so tests exercise the exact shipped filter
- **Dashboard** — the API latency chart plots at most the last 672 checks (`HEALTH_CHART_MAX_POINTS`, the previous 7-day history span) even though the stored history now covers the full uptime window, keeping chart rendering cost flat
- **Scripts** — `run_tests.sh` prunes `rally_project_ids.log` on its own `PROVENANCE_RETENTION_DAYS` window (default 90), decoupled from `RALLY_RESULTS_RETENTION_DAYS` (default 7). The ledger is the sole authorization basis for RGW auto-purge, so pruning it on the short run-retention window would degrade older orphans to "unknown owner" (never purged) and make `RallyRgwOrphanedUsers` fire forever — the same defect class as the `smoke_history.json` retention decoupling above
- **Config** — `UPTIME_WINDOW_DAYS` environment variable (default `30`), exported to cron via the `entrypoint.sh` whitelist and documented in `env.sample`/README
- **Tests** — `scripts/test_uptime_ledger.py` covers the smoke ledger (pass/fail classification including the `deployment_setup_failed` and skipped-service shapes, window pruning, configurable window, percent rounding, `results.json` uptime sync, resilience to an unreadable summary) and the health-history filter (append + uptime computation, window exclusion, storage cap, null percent on empty window)

- **CI** — `.github/workflows/build-push.yml` builds the container image and pushes it to GitHub Container Registry (`ghcr.io/<owner>/<repo>`) on push to `main`, on `v*` tags, on PRs to `main` (build-only, no push), and via manual dispatch. Uses the built-in `GITHUB_TOKEN` (`packages: write`) so no extra secrets are required; tags via `docker/metadata-action` (branch, PR ref, semver, SHA, `latest` on default branch) with GitHub Actions layer caching
- **Docker** — `docker/docker-compose.yml` now defaults to the CI-published image via `image: ${RALLY_MONITOR_IMAGE:-ghcr.io/ching-kuo/openstack-rally-monitor:latest}` while keeping the `build:` block, so `docker compose pull` runs the published image and `docker compose up --build` still builds locally under the same tag
- **Dashboard** — operator announcement banner above the 7-day timeline. Three types are supported with distinct lifecycles: `incident` (active until cleared, or auto-cleared after the next all-green Rally run), `maintenance` (active until `--expires-at`), and `scheduled` (hidden until `--effective-from`, then visible until `--expires-at`). The banner shows a visible `[TYPE]` label alongside type-colored borders so color is not the sole signal. A native `<details>` element exposes progress updates and other concurrent records; the open state survives the 5-minute auto-refresh when the same primary record is still active. Outer edges align with section content (`max-width: calc(1400px - 4rem)`) so the banner sits flush with the timeline cards below
- **CLI** — `scripts/announce.sh post | update | clear | list | auto-clear-if-all-green`. Bodies are plain text (max 500 chars); timestamps must be ISO 8601 UTC and are validated semantically before reaching `jq`. State is persisted atomically to `/results/announcement-state.json` via `tmpfile + mv`. ID format `<type>-<YYYYMMDDTHHmmssZ>-<8 hex chars>`. Invoke via `docker exec -u rally rally-monitor /scripts/announce.sh ...`
- **Scripts** — `scripts/run_tests.sh` invokes `announce.sh auto-clear-if-all-green` after `build_summary` and before `publish_dashboard_files`. The predicate `(.services | length) > 0 AND (.error // null) == null AND (.services | to_entries | all(.value.status == "passed"))` guards against the `deployment_setup_failed` shape — the empty-services check is critical because `jq all(...)` returns `true` on empty iteration, which would silently erase the very banner the operator posted to explain the failure
- **Scripts** — every mutating subcommand wraps its read-modify-write cycle in `flock` on `/results/.announce.lock`, preventing an operator post/update/clear from racing with a cron-driven `auto-clear-if-all-green`. Falls through to direct execution (single-operator behavior) when `flock` is unavailable
- **Server** — `dashboard/serve.py` `ALLOWED_JSON_SYMLINKS` extended with `announcement-state.json`. Same containment as the existing entries (`target.is_relative_to(RESULTS_ROOT)`); no new attack surface
- **Tests** — `scripts/test_announce.py` covers CLI behavior (35 cases including the empty-services guard, ISO 8601 semantic validation, ID-entropy under burst posts, atomicity, and a corrupt-state-file refusal). `dashboard/test_serve.py` extended with allowlist regression coverage for the new entry
- **Tests** — the suite grew across this branch (now 294 passed + 1 skipped) and is now gated by CI: `scripts/test_notify.py` (transition semantics, payload shapes, atomic state-on-success), `scripts/test_api_health_check.py` (single-session checker, degraded classification, `RALLY_SERVICES` parsing/subset/allowlist, keystone-always), `scripts/test_run_summary.py` (`first_error` contract, `run_state`, seed-from-set), `scripts/test_render_report.py` (payload normalization, percentile/atomic/error aggregation, data-block escaping + breakout resistance, truthy-non-dict shape resilience, empty-state fallback), `exporter/test_rally_exporter.py` (API/announcement gauges, fail-closed `rally_api_overall_up`), `dashboard/test_getrunstatus.py` and `dashboard/test_report_js.py` (Node contract tests for the dashboard/report pure helpers), and `dashboard/test_serve.py` (the `runs/` report branch with denial cases and a single-strict-CSP assertion shared by reports and dashboard assets)

### Changed

- **Scripts** — `run_tests.sh` tags each Rally task with `--tag run-<TIMESTAMP>-<service>`, and the UUID fallback path is now a tag-scoped `rally task list --tag … --uuids-only` instead of a bare `tail -1` over the global task list (verified against rally 5.0.1: `--tag` composes with `--uuids-only`). The old `tail -1` was misattribution-prone — concurrent or interleaved tasks could pin the wrong UUID
- **Scripts** — `scripts/api_health_check.py`'s single-session model means `latency_ms` now measures the API round-trip rather than ~1–3 s of CLI interpreter startup + token issuance. **Latency baselines step down at upgrade** — any latency alerts/thresholds tuned against the old CLI-based values should be re-baselined after deploying this version
- **Dashboard** — `serve.py` now uses `ThreadingHTTPServer`; the strict CSP drops `'unsafe-inline'` from `script-src` (the former `themes/custom <link onerror=…>` handlers were moved into `app.js::pruneFailedThemeLinks`). Health-chart and run-timeline service lists are derived from the data (`orderedServiceUnion` + `PREFERRED_SERVICE_ORDER`) and timeline headings are data-driven "Last N days" subheads instead of hardcoding "7-Day"/six services, so a trimmed `RALLY_SERVICES` or a shorter retention renders correctly. Uptime badges carry count tooltips (`runs_passed/runs_total` vs `checks_up/checks_total`), and failed run-timeline cells carry a `✕` glyph for color-blind accessibility (the narrow `.htl-cell` health cells are deliberately left without the glyph)
- **Docs** — README gains "Webhook Notifications" and (already shipped) "Operator Announcements" sections; CLAUDE.md gains "Webhook Notifications" and "Announcement System" architecture subsections. The "Announcement System" subsection explains the deliberate placement at `/results/announcement-state.json` (outside `/results/branding/` to avoid the documented read-only theme bind-mount collision)

### Removed

- **Dashboard** — dead `.card-icon` CSS rule (never emitted to the DOM; service cards inline the service emoji inside `.card-title`) and the unused internal `--shadow-sm` token. Both are internal, non-public-contract assets, so custom themes are unaffected
- **Tooling** — `.antigravityignore` (was byte-identical to `.claudeignore`)

### Fixed

- **Security** — `RALLY_SERVICES` now passes every normalized token through a `^[a-z0-9_-]+$` allowlist before it is accepted, in all three parsers (`run_tests.sh::parse_rally_services`, `api_health_check.py::parse_rally_services`, and the new `scripts/health_fallback_filter.jq`). Service names index `rally/scenarios/<name>.yaml` and `runs/<ts>/<name>.html`, so a token like `../etc` must never survive — path-traversal hardening. When every token is dropped the parser falls back to the default set (so an all-invalid value cannot collapse the monitored set to keystone-only)
- **Exporter** — `rally_api_overall_up` now **fails closed**: `up`/`degraded` → 1, everything else → 0 (explicit `down`, `unknown`/missing overall, a corrupt/missing `health.json`, and any unrecognized value). Previously `unknown`/missing left the gauge at its prior value, so a broken health pipeline could read as "all healthy" indefinitely. Because the per-service `rally_api_up` series are cleared-then-only-set for a valid file, a broken pipeline leaves no per-service series to fire `== 0` — this unlabeled gauge is the only surviving signal
- **Alerts** — new `RallyApiSignalLost` (warning, `rally_api_overall_up == 0`, `for: 60m`) in `prometheus/rally_alerts.yml` covers both a sustained overall API outage and a silently-broken health pipeline (absent per-service series cannot fire the per-service critical `RallyApiDown`)
- **Scripts / Dashboard** — the `deployment_setup_failed` path in `run_tests.sh` now calls `publish_dashboard_files` after `record_smoke_result`, so `results.json`/`history.json` reflect the failure instead of the last green run. `dashboard/app.js::getRunStatus` treats a summary with a non-empty `.error` OR empty services as **failed** (no longer "All Healthy"), and the timeline tooltip surfaces the error verbatim (`Error: <error>`) instead of the misleading "No service data"
- **Dashboard** — the run-duration chart derives its service list from the union of all retained runs (`orderedServiceUnion`, matching the health-latency chart) instead of only the oldest retained run, so a service added via `RALLY_SERVICES` appears immediately rather than waiting for older runs to age out of retention. Separately, `formatCoveredSpan` (the "Last N days" timeline subheads) switched to a single-pass min/max (no argument-spreading) to avoid a `RangeError` on large health histories, and now returns "" for a single data point
- **Scripts** — `health_check.sh` validates `api_health_check.py`'s output with `jq empty` before publishing, so stdout pollution (a stray dependency banner) can never replace `health.json` with an unparseable document. The all-down fallback document was extracted into the shipped `scripts/health_fallback_filter.jq` (invoked with `jq -n -f`, mirroring `health_history_filter.jq`) so tests exercise the exact published program, and it honors `RALLY_SERVICES` instead of a hardcoded six so a trimmed deployment never gains phantom down services
- **Tests** — the script-suite tests resolve bash via `shutil.which("bash")` instead of hardcoding `/bin/bash`, so the suite runs on macOS (whose `/bin/bash` is 3.2, lacking `mapfile`) when a bash >= 4 is installed (`brew install bash`) and precedes `/bin/bash` on `PATH`
- **Docs** — `CLAUDE.md` no longer claims the exporter serves `/api/results` and `/api/history`; the real HTTP endpoints are `/metrics`, `/health`, and `/ready`
- **Scripts** — `mirror_job_logs.sh` no longer writes to `/proc/1/fd/1`; cron jobs run as `rally` (UID 1500) and the cross-uid write to root-owned PID 1 stdout failed with `Permission denied`, aborting the pipeline before `tee` reached the log file. `entrypoint.sh` now spawns a `tail -F` of the job log files as a PID 1 child to forward output into `docker logs`
- **Scripts** — `purge_orphans.sh` no longer narrows listings to `OS_PROJECT_NAME`. Rally creates orphan resources inside `c_rally_*` context projects, not the service account's project, so the redundant `--project`/`--owner` filter masked every scenario-leaked server, volume, router, security group, network, and image. Detection now matches the always-on `cleanup_monitor.sh` (admin scope + name-prefix match across all projects)
- **Scripts** — `purge_orphans.sh` resets stuck volumes (`error_deleting` / `deleting` / `error`) to `error` via `openstack volume set --state` before issuing `openstack volume delete --force`. Cinder protects the `error_deleting` state from a plain delete, leaving volumes from failed scenario cleanups un-purgeable until manually intervened
- **Scripts** — `run_tests.sh` now folds `cleanup_metrics.json` into the run's `summary.json` (and `latest_summary.json`) after `check_cleanup` runs, so `history.json` carries per-run cleanup counts instead of zeros. Previously `build_summary` wrote the per-run summary before cleanup detection ran, so the dashboard timeline showed `cinder=0` for every historical entry regardless of actual orphan state

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
