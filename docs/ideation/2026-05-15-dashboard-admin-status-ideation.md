---
date: 2026-05-15
topic: dashboard-admin-status
focus: feature to let admin post status on the dashboard, probably localhost with unauthenticated API; unsure if a full admin dashboard is overkill
mode: repo-grounded
---

# Ideation: Operator Status Posting on the Dashboard

## Grounding Context

**Codebase shape.** Single Docker container (rally-monitor) with three concurrent processes: cron (rally tests + health checks), `dashboard/serve.py` (Python `http.server` on `:8080`, deny-by-default allowlist, GET-only), `exporter/rally_exporter.py` (Flask on `:9101` via gunicorn — endpoints `/metrics`, `/health`, `/api/results`, `/api/history`). Dashboard is pure static HTML/JS/CSS; refreshes every 5 minutes (`REFRESH_INTERVAL = 300_000` in `app.js`). The existing operator-override convention bind-mounts `/results/branding/` and entrypoint symlinks it to `/dashboard/themes/custom`; the dashboard probes optional theme assets at boot with graceful fallback to defaults. The `/results/` Docker volume is shared and writable by cron jobs; cron environment is exported to `/rally/rally_env` (mode 0640) and holds `OS_PASSWORD` and `RGW_SECRET_KEY` — must not be widened.

**Past learnings.** Documented project preference (CLAUDE.md + theme-customization plan): file-drop > HTTP-write. `serve.py` is read-only and allowlist-only. POST belongs on `rally_exporter.py` if anywhere. The team has a documented preference for "partial overlay" operator config — optional files with graceful fallback.

**External prior art.** Gatus is the closest analog (YAML announcements file + reload, no HTTP write, single-container tool; fields `timestamp / type / message / archived`). Uptime Kuma has a full CRUD UI with SQLite — overkill for single-operator homelab. Grafana's banner is UI-only with RBAC — enterprise. Alertmanager silences are time-bounded annotations operators POST via curl. The /etc/motd analogy: file-drop banners go stale without an expiry mechanism; every mature implementation adds `ends_at` / `archived`. Localhost-only HTTP requires the canonical triad: 127.0.0.1 bind inside container + host port mapping `127.0.0.1:port:port` + Host header validation against DNS rebinding.

## Topic Axes

1. Authoring surface
2. Storage and lifecycle
3. Rendering and dismissibility
4. Security boundary
5. Schema and content

## Ranked Ideas

### 1. File-drop announcement with NOTAM-style schema
**Description:** Extend the existing `/results/branding/` operator-overlay convention with `announcement.json` (or YAML). Schema is strict: `severity` (info/advisory/warning/critical enum), required `expires_at` ISO timestamp, optional `effective_from`, plain-text `body` (recommend ≤280 chars, rendered via `textContent` — no Markdown parser, no DOMPurify dependency). Dashboard reads it on the existing 5-minute poll; entries past `expires_at` render as nothing. Add the path to `serve.py`'s allowlist as one narrow line.
**Axis:** Authoring + Storage and lifecycle + Schema
**Basis:** `direct:` `scripts/entrypoint.sh` already creates `/results/branding/` and symlinks it to `/dashboard/themes/custom`. CLAUDE.md: "File-drop > HTTP-write is the documented preference for this project." `external:` Aviation NOTAM (FAA/ICAO) — mandatory expiry is the canonical fix for stale-banner failure mode. Gatus YAML reload with `type`/`message`/`archived`.
**Rationale:** Reuses the operator's existing muscle memory (bind-mount file edits), zero new auth surface, eliminates the dominant failure mode (stale banners) at the schema level, and stays inside serve.py's hardened read surface.
**Downsides:** Operator needs shell/volume access to publish — no curl-from-anywhere story. Plain text means no inline links; either acceptable (paste the URL) or revisit if operators push back. JSON timestamp typing adds friction over a free-text file.
**Confidence:** 85%
**Complexity:** Low
**Status:** Explored (selected for brainstorm 2026-05-15, combined with #4)

### 2. Two-layer banner: auto-derived from health + operator overlay
**Description:** Dashboard derives a default banner from existing state (last-N-runs failure pattern, `health.json` degradation, RGW orphan-scan errors). Operator-written `announcement.json` overrides or stacks above the derived one. When the operator says nothing and reality is bad, the system speaks; when reality is fine, nothing shows; when the operator wants to clarify ("we know, planned migration"), their message wins.
**Axis:** Authoring + Storage and lifecycle
**Basis:** `direct:` `exporter/rally_exporter.py` already reads `latest_summary.json`, `cleanup_metrics.json`, and `health.json` — every signal needed exists. `reasoned:` The most common reason for an operator banner is "explain what the metrics already show" — having the system do the default explanation and the operator override only when needed inverts who's typing in the common case.
**Rationale:** Cuts publishing effort to near-zero in the routine case (failures speak for themselves), and pairs naturally with idea 1's file-drop for the override case. Banner presence becomes information ("something is off") even when nobody typed anything.
**Downsides:** Two-layer logic adds rendering complexity (which one wins, do they stack?). Derived messages need careful copy to avoid alarm-fatigue. Could leak underlying-state details an operator would have phrased more diplomatically.
**Confidence:** 70%
**Complexity:** Medium
**Status:** Unexplored

### 3. Maintenance-mode flag with Prometheus integration
**Description:** Drop `/results/branding/maintenance.flag` (any content; presence + mtime is what matters) to enter maintenance mode. Dashboard renders a yellow banner ("Maintenance in progress since HH:MM — expect transient failures"). Exporter emits `rally_maintenance_mode 1`. Alertmanager rules can `unless rally_maintenance_mode == 1` to silence noisy alerts during operator work. Delete the file to exit. Optional content: a free-text reason file alongside.
**Axis:** Security boundary + Storage and lifecycle + Schema
**Basis:** `external:` Hotel "Do Not Disturb" door hanger — one bit of operator-set state, externally readable. Alertmanager silences are the canonical maintenance primitive in the Prometheus ecosystem. `reasoned:` The #1 reason an operator wants a banner is "I'm working, ignore the failures" — that's also exactly when they want to silence alerts. One file-drop should do both jobs.
**Rationale:** Unifies the two operator workflows (tell humans + tell Prom) into one file-touch. Solves the most concrete real-world use case without inventing a general announcement framework. Trivially observable from outside the dashboard.
**Downsides:** Solves only the maintenance use case (not arbitrary announcements). If shipped alongside idea 1, two systems coexist; if shipped instead of idea 1, doesn't cover non-maintenance status posting. Touching a single file feels weirder than writing a structured record.
**Confidence:** 80%
**Complexity:** Low
**Status:** Unexplored

### 4. `rally-announce` CLI helper via `docker exec` (no HTTP write surface)
**Description:** Counter-proposal to the user's "localhost-only POST" lean. Add `scripts/announce.sh` that takes `--body`, `--severity`, `--until` flags and atomically writes `/results/branding/announcement.json` (tmpfile + rename for atomicity). Operators publish with `docker exec rally-monitor /scripts/announce.sh "Maintenance tonight 22:00 UTC" --until 2026-05-20T22:00Z`. Authorization = "you can run `docker exec` on this host" — already the operational trust boundary.
**Axis:** Security boundary + Authoring surface
**Basis:** `direct:` CLAUDE.md says `serve.py` is "read-only and append-allowlist-only. Adding POST belongs on `rally_exporter.py` if anywhere." Existing operational pattern includes `docker exec -u rally rally-monitor /scripts/run_tests.sh`. `external:` Alertmanager's `amtool` CLI is the canonical "no UI, just curl/exec" ergonomic.
**Rationale:** Same one-command UX as a POST endpoint with zero new HTTP attack surface, zero new code in the exporter, zero Host-header / CSRF / DNS-rebinding reasoning needed. The exporter holds OS_PASSWORD — keeping write paths off it limits blast radius.
**Downsides:** Operator must have host shell access (true in homelab; less convenient for multi-host or remote-only operators). No "post from my laptop without SSHing in" story. Slightly more typing than a hypothetical web form.
**Confidence:** 90%
**Complexity:** Low
**Status:** Explored (selected for brainstorm 2026-05-15, combined with #1)

### 5. Localhost-only POST on exporter — done correctly
**Description:** The user's stated hypothesis, with the security details that make it actually localhost-only. New Flask route `POST /api/announcement` on `rally_exporter.py`, **but**: (a) Flask binds to `127.0.0.1`, not `0.0.0.0`, (b) `docker-compose.yml` exposes the admin port as `127.0.0.1:9102:9102` (not `9102:9102`), (c) handler validates `Host: 127.0.0.1:9102` or `localhost:9102` to defeat DNS rebinding, (d) optional bearer token from `/rally/rally_env` for defense in depth. Writes to the same `/results/branding/announcement.json` consumed by idea 1.
**Axis:** Security boundary + Authoring surface
**Basis:** `external:` GitHub Security Engineering blog on localhost CSRF and DNS rebinding (canonical mitigation triad: 127.0.0.1 bind + host port restriction + Host header validation). `direct:` `rally_exporter.py` is already a Flask app, so adding a route is one decorator.
**Rationale:** Honors the user's stated preference for an HTTP-callable surface (curl from a script, possible SSH-tunneled access from a workstation) without inheriting the textbook footguns. Co-exists with idea 1 — the endpoint is just an alternative authoring surface for the same target file.
**Downsides:** Adds an HTTP write path with non-trivial security-review surface (CSRF reasoning, header parsing). Operator must remember to map the port correctly in docker-compose; a misconfigured `9102:9102` exposes write access to the LAN. Exporter process now both reads metrics AND mutates state — separation of concerns degrades.
**Confidence:** 60%
**Complexity:** Medium
**Status:** Unexplored

### 6. Per-service annotation pills on service cards
**Description:** Instead of (or in addition to) a global banner, each service row gets an optional small annotation: `/results/branding/service_notices.json` keyed by service name. "Cinder volume creation flaky since deploy X" appears as a pill on the Cinder card; "RGW backend in maintenance" on the RGW row. Same schema as idea 1 minus the global aspect.
**Axis:** Rendering and dismissibility
**Basis:** `external:` NYC MTA / London Underground service-advisory posters appear at the affected station, not the whole system map. Grafana panel annotations follow the same pattern. `reasoned:` Operators think in terms of "Cinder is flapping," not "the whole platform is degraded" — scoping the message matches their mental model and avoids global-banner-fatigue.
**Rationale:** Localizes visual disruption to where the affected metric is. Stays fresh because each note is owned by the chart it annotates. Complements idea 1 rather than replacing it (global banner for cross-cutting events; service notes for service-specific ones).
**Downsides:** Two surfaces (global + per-service) increases UI complexity and operator decision cost ("where does this go?"). Service cards must accommodate variable height. Doesn't help with cross-service announcements.
**Confidence:** 65%
**Complexity:** Medium
**Status:** Unexplored

### 7. Active announcement emits Prometheus gauge
**Description:** Exporter reads `/results/branding/announcement.json` (or a list) on each `/metrics` scrape and emits `rally_announcement_active{id="...", severity="...", title="..."}` gauges. When a banner is up, the metric appears; when it expires, it disappears. Grafana panels can render these as vertical annotations on every chart automatically; Alertmanager can route on severity (e.g., page on `critical` banners not posted within an SLA after a failure).
**Axis:** Schema and content + Rendering
**Basis:** `direct:` `exporter/rally_exporter.py` already does the scrape-time JSON-read pattern for `latest_summary.json` and `cleanup_metrics.json` — adding one more file follows the established convention. `external:` Grafana annotations are commonly populated from gauge metrics; this is the pattern most production Prometheus shops use.
**Rationale:** Makes banners observable in the rest of the monitoring stack — Grafana annotations, Alertmanager routing, log correlation — with no new infrastructure. Banner stops being a dashboard-only concept.
**Downsides:** High-cardinality labels (`title` is free text) can stress Prometheus storage if many banners are emitted with unique strings. Mitigation: only emit `id` and `severity` as labels, keep `title` as an `info`-pattern metric on `latest`.
**Confidence:** 75%
**Complexity:** Low
**Status:** Unexplored

## Recommended Combinations

- **Minimal-and-honest:** #1 + #4 — file-drop schema with mandatory expiry, published via the existing `docker exec` idiom. No new HTTP surface; matches documented project preference.
- **Pragmatic-with-leverage:** #1 + #3 + #7 — schema-driven announcements + one-bit maintenance flag for the killer use case + observability via /metrics. Three small primitives that each pull weight.
- **Your-original-hypothesis-done-right:** #1 + #5 (+ #7) — keeps the HTTP endpoint but inside a corrected security boundary.

## Rejection Summary

| # | Idea | Reason Rejected |
|---|------|-----------------|
| 1.3 | Decouple polling cadence (20-30s) | Premature optimization; 5-min cadence is acceptable for status |
| 1.4 | Dismissal nonce | Solves a problem that doesn't exist yet; content-hash keying covers main case |
| 1.5 | `?preview=1` staging path | Operator can preview by writing to alternate path and reading; below ambition floor |
| 1.6 | Per-run `note.md` | Different feature (run annotation, not status); belongs in separate brainstorm |
| 1.8 | Stable banner region | Implementation detail, not a design idea |
| 2.3 | CHANGELOG.md / git tag as banner | Conflates release notes with operational status |
| 2.4 | Cron-derived "next run in N min" | Feature creep; operators don't want scheduler chatter as banner |
| 2.5 | Reader-suggested via mailto | Solves the wrong problem (collect, not publish) |
| 2.6 | Non-dismissible auto-fade | Subsumed by idea 2's auto-derived layer |
| 2.7 | Banner-via-symlink | `mv` works the same; cute analog, not a real win |
| 3.1 | `note` field on latest_summary.json | Couples banner lifecycle to test-run cadence |
| 3.3 | Alertmanager annotations as source | Requires Alertmanager; subsumed by simpler shapes |
| 3.7 | Favicon + tab title swap | Useful micro-feature, but tiny scope; not a central design |
| 3.8 | Template-by-reference (incident ID) | Requires external incident system; doesn't fit homelab |
| 3.9 / 6.7 | Git-tracked notice / external CDN URL | Adds external availability dependency; CSP widening |
| 4.2 | Append-only events.jsonl | Speculative leverage; no concrete second consumer |
| 4.3 | Statuspage-subset schema | Bigger surface than warranted; premature standards adoption |
| 4.5 | File-per-record audit trail | Adds complexity; rarely revisited |
| 4.6 | Shared marked+DOMPurify renderer | Only useful if multiple Markdown surfaces exist; YAGNI |
| 4.8 | Alertmanager silence bridge | Significant complexity; covered more simply by idea 3 |
| 5.2 | Daily chalkboard wipe (midnight rotate) | Too aggressive; multi-day maintenance windows are real |
| 5.3 | Lighthouse vocabulary (fixed enum, no text) | Too restrictive long-term; severity enum (in idea 1) captures the win |
| 5.6 | Email-style `effective_from` + `effective_until` | Subsumed by idea 1's expiry schema |
| 5.8 | Wikipedia git-PR workflow | Too slow for time-pressured incidents |
| 6.2 | Ephemeral tmpfs (dies on restart) | Restart is too aggressive a lifecycle |
| 6.5 | GPG-signed banner | Wildly over-engineered for actual threat model |
| 6.6 | 100-operator append-only ledger | Overkill for single-operator homelab |
| 6.8 | Severity=critical full-page takeover | Interesting ceiling; not a v1 design driver |
