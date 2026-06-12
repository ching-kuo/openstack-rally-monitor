---
date: 2026-06-12
topic: next-cycle-features-and-refactors
focus: open survey of new feature ideas and refactor/hardening opportunities across the whole codebase
mode: repo-grounded
---

# Ideation: Next-Cycle Features and Refactors

## Grounding Context

**Codebase shape.** Single container, three processes (`scripts/entrypoint.sh`): gunicorn exporter on `:9101`, `dashboard/serve.py` on `:8080`, cron-driven `run_tests.sh` (default 240 min) and `health_check.sh` (default 15 min). All state flows through `/results/` as atomic JSON file-drops; the dashboard is static JS polling symlinked JSON every 5 minutes. Recent cycles shipped: theme customization (1.3.0), operator announcements (`announce.sh`, unreleased), uptime badges with decoupled ledgers (unreleased), and CI image publish to ghcr.io (unreleased).

**Standing decisions honored here.** File-drop > HTTP-write (2026-05-15 ideation, settled). Uptime is dashboard-only; Prometheus users derive it via `avg_over_time()` (CLAUDE.md). `serve.py` stays deny-by-default. No dashboard build step.

**Observed gaps that anchor the ranking below.**
- `.github/workflows/build-push.yml` builds and pushes the image but never runs the six test suites (~1,944 lines: `test_rally_exporter.py` 432, `test_announce.py` 549, `test_serve.py` 342, `test_uptime_ledger.py` 273, `test_rgw_helpers.py` 292, `test_mirror_job_logs.py` 56).
- `exporter/rally_exporter.py` reads only `latest_summary.json` + `cleanup_metrics.json`; the 15-minute `health.json` / `health_history.json` signal never reaches Prometheus. Worst-case alerting latency for a full API outage is one Rally interval (4 h) via `RallyStaleResults`/`rally_service_status`, not one health interval (15 min).
- `run_tests.sh::prune_rally_project_ledger` prunes the RGW purge-authorization ledger by `RALLY_RESULTS_RETENTION_DAYS` (7 d) — the same window-coupling defect class already fixed for `smoke_history.json` (deliberately pruned by `UPTIME_WINDOW_DAYS` instead).
- Rally HTML reports (`rally task report --out <service>.html`) are generated into every run directory but are unreachable from the dashboard; failure *reasons* (`.result[].error`) are dropped by `build_summary`, so the dashboard shows failure counts without causes.
- `docker/Dockerfile` installs `rally-openstack` and `python-openstackclient` unpinned and `exporter/requirements.txt` uses `>=` ranges, while vendored frontend assets are SHA-256-pinned — the supply-chain posture is inconsistent.

## Ranked Ideas

### 1. CI test job — run the existing suites before building the image
**Description:** Add a `test` job to `.github/workflows/build-push.yml` (checkout, `actions/setup-python`, `pip install -r exporter/requirements-test.txt`, `pytest exporter/ dashboard/ scripts/`), and make `build-push` `needs: test`. The script-driven suites (`test_announce.py`, `test_uptime_ledger.py`, `test_mirror_job_logs.py`) shell out to bash + jq, both present on `ubuntu-latest`.
**Basis:** `direct:` six test files exist and pass locally by design (CHANGELOG 1.2.0 and Unreleased document them as deliverables) yet no workflow executes them; the only PR gate today is "the Docker build succeeds."
**Rationale:** Highest value-to-effort item in the repo. Every subsequent idea on this list becomes safer to ship once regressions in the exporter, allowlist, announcement CLI, and uptime ledgers fail CI instead of production.
**Downsides:** Adds ~1–2 min to CI. Python version matrix is a decision point (pin to the image's 3.13 to match runtime).
**Confidence:** 95%
**Complexity:** Low
**Status:** Unexplored

### 2. Export API health-check results as Prometheus metrics
**Description:** Extend `rally_exporter.py` with a third cached file read (`health.json`, reusing `_load_cached_json`) emitting `rally_api_up{service}` (1/0), `rally_api_latency_milliseconds{service}`, and `rally_api_overall_up`. Add `prometheus/rally_alerts.yml` rules: `RallyApiDown` (critical, `for: 20m` ≈ two missed checks) and optionally latency-based warnings. Update the Grafana dashboard with an API availability row.
**Basis:** `direct:` `health_check.sh` has produced this data every 15 minutes since 1.0.0; the exporter has never read it. The dashboard's "API health failure takes precedence" logic (`app.js::updateHeader`) exists precisely because this signal is the fastest outage indicator — but only browser viewers get it. `reasoned:` this does not violate the "uptime is dashboard-only" decision: that covers the derived percentage, not the raw up/down samples, and `avg_over_time(rally_api_up[30d])` then gives Prometheus users uptime for free, exactly the pattern CLAUDE.md endorses.
**Rationale:** Cuts worst-case alert latency for an API outage from ~4 h to ~15–30 min using data that already exists, via the exporter's established scrape-time read pattern.
**Downsides:** Six more labeled series (trivial cardinality). `health.json` and the summary update on different cadences — already handled by the precedent in `_apply_cleanup_metrics` (apply on every scrape, clear-before-set).
**Confidence:** 90%
**Complexity:** Low
**Status:** Unexplored

### 3. Maintenance mode: wire `announce.sh` into Prometheus alert inhibition
**Description:** The exporter reads `announcement-state.json` on scrape and emits `rally_announcement_active{type}` (count of currently-active records per type, computing active-ness with the same `effective_from`/`expires_at` semantics as `app.js::isAnnouncementActive`) plus a convenience `rally_maintenance_mode` (1 if any maintenance record is active). Ship commented `unless rally_maintenance_mode == 1` guards on the flappy rules (`RallyTestFailure`, `RallyServiceDown`, `RallySLABreach`) in `rally_alerts.yml` so operators opt in.
**Basis:** `direct:` 2026-05-15 ideation ranked the maintenance flag (#3, 80%) and announcement gauge (#7, 75%) — both "Unexplored" — and identified "I'm working, ignore the failures" as the killer use case. The state file, CLI, expiry semantics, and atomic locking shipped since then; only the exporter bridge is missing. `external:` Alertmanager silences are the canonical maintenance primitive; gauge-driven inhibition is the standard single-binary approximation.
**Rationale:** One operator action — `announce.sh post --type maintenance --expires-at …` — now informs both humans (banner) and machines (alert silence), with auto-expiry preventing the stale-silence failure mode. Completes already-validated design work at marginal cost.
**Downsides:** Two sources of truth for "active" (JS and Python) need a keep-in-sync cross-reference comment, the same convention used for `ALL_GREEN_PREDICATE`. Labels stay enum-only (3 types) to respect cardinality.
**Confidence:** 85%
**Complexity:** Low–Medium
**Status:** Unexplored

### 4. Decouple the RGW provenance ledger from results retention
**Description:** Prune `rally_project_ids.log` by a dedicated `PROVENANCE_RETENTION_DAYS` (default 90) instead of `RALLY_RESULTS_RETENTION_DAYS` (default 7), or skip pruning entries still referenced by the latest RGW orphan scan.
**Basis:** `direct:` the ledger is the *sole authorization basis* for `auto_purge_rgw` — `rgw_classify_owner` greps it, and unknown-owner orphans are never touched (fail-closed by design). An RGW orphan that survives past the 7-day prune (container downtime, RGW credentials configured after the fact, repeated scan errors that skip purge fail-closed) permanently degrades from `rally_owned` to `unknown_owner`: `RallyRgwOrphanedUsers` then fires forever until a human intervenes. This is the identical coupling defect the team already fixed for `smoke_history.json` ("pruned by `UPTIME_WINDOW_DAYS`, independent of run-directory retention" — CLAUDE.md).
**Rationale:** Small correctness fix that preserves the fail-closed posture while removing its main self-inflicted trigger. The ledger is a tiny append-only text file; retention cost is nil.
**Downsides:** One more env var (could instead hardcode a generous constant). Migration note needed: already-degraded orphans stay manual.
**Confidence:** 85%
**Complexity:** Low
**Status:** Unexplored

### 5. Surface failure causes: per-scenario error excerpt + served Rally HTML reports
**Description:** Two complementary halves. (a) `build_summary` extracts the first error message per failed scenario (`[.result[]? | select(.error | length > 0)][0].error[1]`) into a `first_error` field on `scenarios_detail`; the modal in `app.js` renders it (escaped, truncated ~200 chars) under the scenario row. (b) `serve.py` gains a fourth allowlist branch serving `runs/<YYYYMMDDTHHMMSSZ>/<service>.html` — strict regex on the timestamp segment, `.html` suffix only, containment in `RESULTS_ROOT` — plus an entrypoint symlink `/dashboard/runs -> /results`; the modal links each service to its full Rally report.
**Basis:** `direct:` `run_service_tests` already writes `<service>.html` via `rally task report` into every run directory; today the only consumer is `docker cp`. The dashboard shows `failed_iterations: 2` with no way to learn *why* without shelling into the container. `external:` every CI system (Jenkins, GitLab) treats "click through to the full report" as table stakes for test dashboards.
**Rationale:** Converts the dashboard from a status display into a diagnostic tool — the single biggest UX gap for the on-call person it exists to serve.
**Downsides:** Rally reports carry inline scripts; current CSP (`script-src 'unsafe-inline'`) permits them, but if idea 9's CSP tightening lands, reports need a per-path relaxed CSP or a sandboxed iframe — decide ordering. Allowlist widening must stay regex-strict to preserve the deny-by-default story. Reports vanish after retention pruning; modal link should 404 gracefully.
**Confidence:** 85% (a) / 75% (b)
**Complexity:** Low (a) / Medium (b)
**Status:** Unexplored

### 6. "Run in progress" indicator
**Description:** `run_tests.sh` writes `/results/run_state.json` — `{state: "running", started_at}` after acquiring the flock, `{state: "idle", finished_at}` on exit via an EXIT trap — published through the standard symlink + `ALLOWED_JSON_SYMLINKS` entry. `app.js` shows a small pulsing header chip ("Test run in progress · started 12:04") and suppresses staleness anxiety during the multi-minute runs.
**Basis:** `direct:` runs take many minutes (server boots, image uploads across 6 services); during a run the dashboard silently shows the previous result, and the initial boot run (`entrypoint.sh` line 249) looks like "Waiting for first run…" with no liveness signal at all. The flock already encodes this state — it's just invisible.
**Rationale:** Cheap, follows the established file-drop pattern exactly, and meaningfully improves the first-boot and incident-watching experience ("is it re-testing yet?").
**Downsides:** A SIGKILL'd run leaves a stale "running" file; mitigate by having the dashboard distrust `running` older than ~2× `RALLY_SCHEDULE_INTERVAL`, or by `entrypoint.sh` resetting it at boot.
**Confidence:** 80%
**Complexity:** Low
**Status:** Unexplored

### 7. Webhook notification on status transitions
**Description:** `scripts/notify.sh`, called from `run_tests.sh::main` after `record_smoke_result` (and from the deployment-failure path): when the all-green predicate's outcome *changes* relative to `/results/.last_notified_status`, POST a compact JSON payload (timestamp, overall status, failed services, dashboard URL) to `NOTIFY_WEBHOOK_URL` with `curl`. Optional `NOTIFY_FORMAT=generic|slack|discord` jq templates. Transition-only semantics (green→red, red→green) prevent spam; unset URL = no-op.
**Basis:** `direct:` the only push path today is Prometheus + Alertmanager, which the homelab/single-operator audience (per both prior ideation docs) frequently lacks; curl and jq are already in the image, so this is dependency-free. `external:` Gatus, Uptime Kuma, and Healthchecks.io all converge on webhook-on-transition as the minimum viable notification.
**Rationale:** Closes the "nobody was looking at the dashboard when it went red" gap for deployments without an alerting stack, without adding an SMTP/apprise dependency surface.
**Downsides:** Outbound HTTP from the test runner is new egress (document it; off by default). Webhook secrets land in `/rally/rally_env` — already 0640 and holding `OS_PASSWORD`, so no new exposure class, but add the var to the whitelist deliberately.
**Confidence:** 75%
**Complexity:** Low–Medium
**Status:** Unexplored

### 8. Pin the OpenStack toolchain
**Description:** Pin `rally-openstack`, `python-openstackclient` (and transitively `rally`) to exact versions in the Dockerfile or a constraints file; tighten `exporter/requirements.txt` from `>=` to `==`/`~=`; add Dependabot (or Renovate) config so bumps arrive as reviewable PRs that run idea 1's test job.
**Basis:** `direct:` `patch_rally.py` monkey-patches Rally internals at build time and scenario YAMLs reference plugin names — both are implicit contracts with whatever version pip resolves on build day. The repo SHA-256-pins vendored JS/fonts "to detect supply-chain tampering" yet leaves the multi-hundred-package OpenStack toolchain floating; two builds of the same commit can differ. `external:` upstream rally-openstack releases have historically renamed/removed scenarios.
**Rationale:** Makes the ghcr.io `latest` image reproducible per-commit and turns upstream breakage from a silent production surprise into a red PR.
**Downsides:** Someone must own periodic bumps (Dependabot reduces this to merge-or-not). A constraints file adds a little Dockerfile plumbing.
**Confidence:** 90%
**Complexity:** Low
**Status:** Unexplored

### 9. serve.py + dashboard hardening/polish batch
**Description:** Bundle of small, independent improvements: (a) swap `HTTPServer` for `ThreadingHTTPServer` — one stalled client currently blocks every other viewer's 5-minute refresh; (b) drop `'unsafe-inline'` from `script-src` (index.html has zero inline scripts; `style-src` keeps it for the inline style attributes `app.js` writes) — note the interaction with idea 5b; (c) derive `renderHealthChart`'s service list from `check.services` keys instead of the hardcoded six-name array; (d) stop hardcoding "7-Day Status Timeline" — label from data span or config; (e) add `runs_passed/runs_total` as a `title` tooltip on the uptime badges (data already in `results.json`); (f) add a non-color glyph to failed timeline cells, extending the project's own "color is not the sole signal" principle from the announcement banner to the timelines.
**Basis:** `direct:` each item is visible in the current source (`serve.py:143`, CSP block at `serve.py:40-45`, `app.js:785`, `index.html:53`, `app.js:470-479`, `app.js:376-404`). `external:` Python docs recommend `ThreadingHTTPServer` for anything user-facing; WCAG 1.4.1 (use of color) for (f).
**Rationale:** Six fixes, each ~5–20 lines, no architectural risk, collectively a meaningful robustness/a11y/correctness bump. Good "first PR after CI lands" material.
**Downsides:** (b) must be sequenced against idea 5b's report serving. None otherwise.
**Confidence:** 85%
**Complexity:** Low
**Status:** Unexplored

### 10. Health check refactor: one authenticated session, honest latency, degraded state
**Description:** Replace the six `openstack` CLI invocations in `health_check.sh` with one small Python checker on openstacksdk (already installed as an openstackclient dependency): authenticate once, time one read-only GET per service, emit the existing JSON shape. Add a third status `degraded` when latency exceeds `HEALTH_LATENCY_WARN_MS` (default ~5000): amber cell in the health timeline, counts as *up* for uptime math. Keep `health_check.sh` as the cron wrapper (lock, history append via the shipped jq filter).
**Basis:** `direct:` each `check` call spawns a fresh CLI interpreter and performs a full keystoneauth round-trip — `latency_ms` therefore measures ~1–3 s of client startup + token issuance rather than API latency, and the cycle issues ~6 tokens × 96 checks/day against Keystone. The latency chart's absolute values are dominated by this overhead today. `reasoned:` binary up/down hides brownouts — a 30 s `server list` renders identically to a 50 ms one.
**Rationale:** Truthful latency data makes the existing chart diagnostic instead of decorative, cuts Keystone load ~6×, and the degraded state catches the most common real-world failure mode (slow, not down).
**Downsides:** Latency baselines step down discontinuously (CHANGELOG note; uptime math unaffected since up/down semantics are preserved). New Python surface needs tests (idea 1 first). The dashboard's `htl-cell` and `updateHeader` need a third state.
**Confidence:** 80%
**Complexity:** Medium
**Status:** Unexplored

### 11. Configurable service set + optional scenario packs
**Description:** `RALLY_SERVICES` env (default `keystone,nova,neutron,glance,cinder,swift`) consumed by `run_tests.sh` (replacing the hardcoded `SERVICES` array) and a data-driven check table in `health_check.sh`; dashboard derives service lists from payload keys (the `SERVICE_ICONS` fallback `⚙️` already handles unknown names). Ship optional scenario YAMLs for additional services (octavia, heat, designate) that activate only when listed.
**Basis:** `direct:` the six-service set is hardcoded in three places (`run_tests.sh:21`, `health_check.sh:34-39`, `app.js:14-30,785`); clouds without Swift currently log a permanent failing column, and clouds with Octavia get no coverage. The exporter is already service-agnostic (labels come from data).
**Rationale:** Turns the tool from "monitors exactly these six services" into "monitors your cloud," which is the difference between a demo and an operations tool for heterogeneous deployments.
**Downsides:** Touches every layer (medium blast radius); `cleanup_monitor.sh`'s resource-type checks are service-set-independent but the summary's per-service mapping needs review. New scenario packs need real-cloud validation. Do after idea 1.
**Confidence:** 75%
**Complexity:** Medium
**Status:** Unexplored

### 12. `build_summary` contract tests + Rally task tagging
**Description:** (a) Fixture-driven pytest for `build_summary`'s jq parsing (sample Rally task-results JSON → expected summary), the same pattern `test_uptime_ledger.py` established for `record_smoke_result`. (b) Start tasks with `rally task start --tag run-${TIMESTAMP}-${service}` and resolve UUIDs by tag, replacing the `rally task list --uuids-only | tail -1` fallback that can misattribute a *previous* task's results to a service whose own task failed to start.
**Basis:** `direct:` the jq pipeline at `run_tests.sh:263-277` is the heart of pass/fail classification (feeding the dashboard, exporter, uptime ledger, and announcement auto-clear) and has zero test coverage; the tail-1 fallback at `run_tests.sh:209` is a latent misattribution bug visible by inspection.
**Rationale:** Locks the most consequential parsing contract in the repo and removes a correctness landmine in failure-path attribution — the path that matters most.
**Downsides:** Fixtures must track the Rally results schema (pinning, idea 8, makes that stable). Tag-based lookup needs verification against the pinned rally version's CLI.
**Confidence:** 80%
**Complexity:** Low–Medium
**Status:** Unexplored

## Recommended Combinations

- **Foundation first (one small PR each):** #1 CI tests → #8 pinning → #9 hardening batch. Everything afterward ships against a real safety net.
- **Observability cycle:** #2 API metrics + #3 maintenance gauges + #4 ledger decoupling — three exporter/script changes that close the monitoring gaps with no UI risk.
- **Operator-experience cycle:** #5 failure causes + #6 run-in-progress + #7 webhooks — the dashboard becomes diagnostic and proactive.
- **Coverage cycle (later):** #10 health-check refactor, then #11 configurable services, with #12's tests landing alongside.

## Rejection Summary

| Idea | Reason Rejected |
|------|-----------------|
| Parallelize the six per-service Rally runs | `track_rally_context_projects` attributes new `c_rally_*` projects to the currently running service by polling during execution — concurrent tasks would corrupt RGW provenance attribution; also quota contention between contexts. Sequential is load-bearing |
| Rewrite `run_tests.sh` in Python | bash + jq is the established, tested idiom; targeted hardening (#12) captures most of the value without a risky rewrite |
| Expose uptime percentages as Prometheus metrics | Explicitly decided dashboard-only (CLAUDE.md); `avg_over_time()` over #2's `rally_api_up` covers Prometheus users |
| HTTP write endpoint for announcements/config | Settled by 2026-05-15 ideation: file-drop + `docker exec` > HTTP-write; no new evidence |
| SQLite/DuckDB for run history | The atomic-JSON file-drop architecture is deliberate and load-bearing (symlinks, allowlist, jq tooling); a database adds failure modes without a query workload to justify it |
| Multi-cloud aggregation in one container | One container per cloud is the sane unit (separate credentials, volumes, blast radius); a fleet-index page over N `results.json` URLs is a separate, later idea |
| Email/SMTP or apprise notifications | Dependency and config sprawl; generic webhook (#7) reaches every modern notifier |
| Dynamic favicon on failure | Rejected in 2026-05-15 ideation (3.7) as micro-scope; unchanged |
| Rally SQLite task pruning | `rally db recreate` at every container start already bounds growth; cross-restart accumulation is negligible at 36 tasks/day |
