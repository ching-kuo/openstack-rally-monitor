---
date: 2026-05-16
status: active
type: feat
title: Dashboard Admin Status Posting
origin: docs/brainstorms/2026-05-16-dashboard-admin-status-requirements.md
---

# feat: Dashboard Admin Status Posting

## Summary

Add a `rally-announce` CLI (invoked via `docker exec`) that lets the operator publish, update, clear, and list three types of dashboard announcements — maintenance, incident, scheduled — backed by a JSON state file at `/results/announcement-state.json`. The dashboard fetches the state on its existing 5-minute refresh cycle and renders a single-slot banner with a `<details>` expand affordance for progress updates and other concurrent announcements. The Rally cron auto-clears incident-type announcements after every all-green test run. No new HTTP write surface is introduced; authorization equals "you can `docker exec -u rally` on the host" (see origin: `docs/brainstorms/2026-05-16-dashboard-admin-status-requirements.md`).

---

## Problem Frame

The Rally Monitor dashboard is read-only and has no in-band channel for operators to explain *why* the metrics look the way they do during planned maintenance, an in-flight incident, or a scheduled change. Viewers misread "things are broken" as a real regression, and operators end up coordinating context out-of-band on Slack/email. The brainstorm pressure-tested an unauthenticated localhost POST endpoint and rejected it: the project's documented preference is file-drop over HTTP-write (`CLAUDE.md` Theme Customization), and the exporter — the only Flask process — holds `OS_PASSWORD` in its environment, making it an undesirable place to add a write path.

---

## Scope Boundaries

### Active scope

- `rally-announce` CLI with `post | update | clear | list` operations plus an internal `auto-clear-if-all-green` subcommand invoked by the Rally cron.
- Three announcement types (`maintenance`, `incident`, `scheduled`) with the lifecycles defined in the origin doc (R1–R3, R6, R8).
- Dashboard render of a single banner above the 7-day timeline, with `<details>`-based expand for progress updates and other concurrent records (R4, R5, R15).
- `serve.py` allowlist extension and entrypoint symlink so the new state file is fetchable on the dashboard's existing GET surface (R12, R14).
- Auto-clear-on-pass integration into `scripts/run_tests.sh` post-run flow (R8).
- Documentation updates: `README.md` operator section, `CLAUDE.md` architecture section.

### Deferred to Follow-Up Work

- Forensic / historical view of expired or cleared announcements. Origin scope boundary; v1 is in-the-moment-only.
- Localhost-only HTTP POST endpoint as an alternative authoring surface (origin idea #5). Preserved in ideation doc `docs/ideation/2026-05-15-dashboard-admin-status-ideation.md` for possible later revisit.
- Prometheus gauge emission `rally_announcement_active` (origin idea #7). Separate track.
- Per-service annotation pills on individual service cards (origin idea #6). Separate track.

### Outside this product's identity

- Persistent caveats / standing context ("this lab is shared with other tenants"). Explicitly excluded from the use cases the operator named in the brainstorm.
- Markdown, HTML, or rich-content rendering in announcement bodies.
- GPG signing or multi-operator authentication.

---

## Key Technical Decisions

- **Implementation language for the CLI: bash + `jq`.** Matches the existing project idiom (`run_tests.sh`, `purge_orphans.sh`, `cleanup_monitor.sh`, `rgw_helpers.sh`). Atomic state writes use the `tmpfile + mv` pattern already established in `run_tests.sh` (e.g., `${SUMMARY_FILE}.tmp` → `${SUMMARY_FILE}`). Single-operator model means write-race is not a meaningful concern.
- **Single mutable state file at `/results/announcement-state.json`.** Top-level shape is `{"announcements": [...]}`. This sits **outside** `/results/branding/` deliberately: `docs/CUSTOMIZING.md` documents `./my-theme:/results/branding:ro` as a supported read-only bind-mount pattern for theme customization, which would silently fail every announcement write if state lived under `branding/`. Placing the file directly under `/results/` aligns with the existing `results.json` / `history.json` / `health.json` convention (already cron-writable by the `rally` user), keeps `serve.py`'s allowlist extension to one entry, and lets the dashboard fetch in a single GET. Trades: each `update` requires a read-modify-write cycle, accepted because the single-operator model makes concurrent writes a non-issue.
- **JSON over YAML.** Project already standardizes on JSON for all `/results/*.json` files; no YAML parser in the stack. JSON is also what the dashboard already consumes.
- **Top-level schema shape:** `{"announcements": [...]}` only. No `schema_version` field in v1 — there is no consumer that would branch on it, and speculative versioning hooks rot without a reader. If schema migration becomes needed later, the version field can be added at that point with a real consumer.
- **ID generation: `<type>-<YYYYMMDDTHHmmssZ>-<8 hex chars>`.** Human-readable, sortable, copy-pasteable into CLI commands. Example: `incident-20260516T143012Z-a7f3b2d1`. Generated server-side by the CLI; never operator-provided. The 8-hex (32-bit) random suffix gives ~4 billion values, eliminating collision risk under same-second burst posts.
- **ISO 8601 timestamp validation in the CLI.** `announce.sh` validates `--expires-at` and `--effective-from` against a strict ISO 8601 UTC pattern (`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`) before passing to `jq --arg`. Without this, malformed timestamps would pass `jq --arg`'s JSON-string safety and produce `NaN` in the dashboard's `Date.parse` time comparisons, so a "maintenance until 2026-13-45" banner would never expire client-side.
- **Body length cap: 500 chars per body and per update.** Inherited from the brainstorm's open question (R13 deferred). 500 leaves room for incident progress notes ("Restarting nginx" → "Logs show OOM, increasing memory" → "Service recovering, monitoring") without becoming an essay; CLI rejects overlong inputs with a clear error.
- **R13 (plain-text body) split between U1 and U3.** U1 owns *input-side* concerns: length cap, whitespace trim, ISO timestamp validation, type enum validation. U3 owns *output-side* safety: rendering via `textContent` so HTML/JS payloads land as literal text. Both halves are independently testable; neither implementer should assume the other side handles their concern.
- **Plain-text rendering via `textContent`.** No `marked`, no `DOMPurify`, no allowlisted Markdown subset. Eliminates the entire client-side sanitization surface.
- **Type → CSS token mapping + visible type label.** Each type gets both a color token AND a non-color signal — a visible bracketed type label (`[INCIDENT]` / `[MAINTENANCE]` / `[SCHEDULED]`) rendered as a prefix or pill alongside the body. Color alone would fail ~8% of male viewers (red-green CVD); the text label is a redundant channel that also doubles as the screen-reader semantic. Color tokens reused from the dashboard's public token contract (`docs/CUSTOMIZING.md`):
  - `incident` → `--color-failure`
  - `maintenance` → `--color-warning`
  - `scheduled` → `--color-brand-secondary` (cyan; semantically "informational")

  No new public tokens are introduced. Banner-internal layout tokens (padding, border-radius) are internal to the theme, not part of the public contract — this preserves `CLAUDE.md`'s "internal glass/radius/shadow tokens are not stable API" rule.
- **Expand affordance: native `<details>` element with accessibility annotations.** The banner container gets `role="region"` and `aria-label="Operator announcement"`. The `<summary>` text includes the visible count when secondary content exists (e.g., `Show 2 updates and 1 other notice`). No JS-driven focus management; the auto-refresh path does not steal focus from an expanded `<details>` element. Open/closed state is per-tab and per-session (no localStorage).
- **Auto-clear placement in `run_tests.sh`: before `publish_dashboard_files`.** The functional constraint is that the auto-clear must run after `build_summary` (it reads per-service outcomes) and before `publish_dashboard_files` (so the dashboard's next refresh sees a consistent state). Inserting the call between `build_summary` and `auto_purge_rgw` is one valid position; any position before `publish_dashboard_files` satisfies the constraint.
- **Auto-clear correctness predicate.** Auto-clear fires only when the post-run summary indicates an unambiguous all-green run: `(.services | length) > 0 AND (.error // null) == null AND (.services | to_entries | all(.value.status == "passed"))`. The empty-services check is required because `latest_summary.json` is written as `{"services": {}, "error": "deployment_setup_failed", ...}` when deployment setup fails (`scripts/run_tests.sh` lines 508-516), and `jq '[.services[]] | all(...)' ` returns `true` on an empty iteration — without the guard, the exact failure mode the incident banner exists to communicate would silently erase the banner. The `.error` null check is a belt-and-braces guard against future error-bearing summary shapes.
- **`.services` is a keyed object, not an array.** Throughout the plan, `.services` refers to an object `{"keystone": {...}, "nova": {...}, ...}` (as seeded at `entrypoint.sh` lines 80-87 and written by `build_summary` at `run_tests.sh` line 295). `jq` iterates object values via `.services | to_entries | .[].value` or `.services[]` (which yields values, not keys). Test fixtures must use the object shape.
- **Symlink lives at `/dashboard/announcement-state.json` → `/results/announcement-state.json`.** Created unconditionally at container start by `entrypoint.sh`. Dangling symlink (when the file does not exist yet) resolves to 404 via `serve.py`'s `target.exists()` check.
- **Operator examples use `docker exec -u rally`.** The cron path runs as the `rally` user; standardizing operator-facing examples on the same UID prevents file-ownership flips between `root` and `rally` across write paths (which can cause subsequent rally-as-cron writes to fail on a root-owned state file). All CLI examples in README and CLAUDE.md must use `-u rally`.

---

## Output Structure

```
scripts/
  announce.sh                          (new — CLI helper)
  test_announce.py                     (new — pytest tests with subprocess)
  entrypoint.sh                        (modified — symlink line)
  run_tests.sh                         (modified — auto-clear hook)
dashboard/
  app.js                               (modified — fetchAnnouncements, renderAnnouncements)
  index.html                           (modified — banner container above timeline)
  style.css                            (modified — banner + details styles)
  serve.py                             (modified — one allowlist entry)
  test_serve.py                        (modified — allowlist regression test)
README.md                              (modified — operator section)
CLAUDE.md                              (modified — architecture section)
CHANGELOG.md                           (modified — release entry)
```

The per-unit `**Files:**` sections are authoritative.

---

## Data Shape

`/results/announcement-state.json` — top-level object holding the active record set. No `schema_version` field in v1; if migration becomes necessary later, add the field when a real reader exists.

```json
{
  "announcements": [
    {
      "id": "incident-20260516T143012Z-a7f3b2d1",
      "type": "incident",
      "body": "RGW backend degraded — investigating",
      "created_at": "2026-05-16T14:30:12Z",
      "expires_at": null,
      "effective_from": null,
      "updates": [
        { "ts": "2026-05-16T14:45:33Z", "body": "Restarted nginx, observing" }
      ]
    },
    {
      "id": "maintenance-20260516T100000Z-b12c4f5a",
      "type": "maintenance",
      "body": "Lab maintenance — expect failures",
      "created_at": "2026-05-16T10:00:00Z",
      "expires_at": "2026-05-16T18:00:00Z",
      "effective_from": null,
      "updates": []
    }
  ]
}
```

Per-type field rules (enforced by the CLI on `post`):

| Type        | `expires_at`   | `effective_from` | `updates` accepted? |
|-------------|----------------|------------------|---------------------|
| incident    | always `null`  | always `null`    | yes                 |
| maintenance | required ISO   | always `null`    | yes                 |
| scheduled   | required ISO   | required ISO     | no (single-body)    |

All ISO timestamp inputs MUST match `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$` (the CLI validates this before writing).

This illustrates the intended shape and is directional guidance for review, not implementation specification. Exact key names, formatting, and `jq` filters are the implementer's call.

---

## Implementation Units

### U1. CLI helper script (`scripts/announce.sh`)

**Goal:** Implement the operator-facing CLI for `post | update | clear | list` plus the internal `auto-clear-if-all-green` subcommand, persisting to `/results/announcement-state.json` via atomic `tmpfile + mv` writes.

**Requirements:** R1, R2, R3, R6, R8, R9, R10, R11, R12 — plus the *input-side* half of R13 (length cap, whitespace trim, ISO timestamp validation, type enum validation). The *output-side* half of R13 (XSS-safe rendering) is owned by U3.

**Dependencies:** none — foundation for everything else.

**Files:**
- `scripts/announce.sh` (new)
- `scripts/test_announce.py` (new — pytest harness, mirrors `scripts/test_mirror_job_logs.py`)

**Approach:**

- Bash script with a small case-dispatch on first argument: `post | update | clear | list | auto-clear-if-all-green`.
- Use `set -euo pipefail` at the top and a `cleanup` trap that removes any leftover `*.tmp` file on exit.
- `RESULTS_DIR` defaults to `/results` (matches existing scripts), overridable via env var for tests. The state file path is `${RESULTS_DIR}/announcement-state.json` (directly under `/results/`, not under `branding/`).
- **`post`:** validates `--type ∈ {incident, maintenance, scheduled}`, validates type-specific required flags (`--expires-at` for maintenance/scheduled, `--effective-from` for scheduled), validates ISO 8601 format (`^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$`) for every timestamp input, validates body length (≤500 after whitespace trim), generates an ID (`<type>-$(date -u +%Y%m%dT%H%M%SZ)-$(openssl rand -hex 4)` — 8 hex chars / 32 bits of entropy), appends a new record to the `announcements` array via `jq --arg`, atomically replaces the file. Prints the new ID to stdout for scripting.
- **`update`:** reads the file, finds the record by ID via `jq`, rejects if type is `scheduled` (R11), validates body length and whitespace-trim, appends `{ts, body}` to `updates`, atomically writes back. Returns non-zero on missing ID.
- **`clear`:** removes the record by ID from the array, atomically writes back. No-op (exit 0) if ID is missing; configurable strictness via `--strict` flag deferred to follow-up.
- **`list`:** reads the file and outputs the `announcements` array as JSON. Optionally filters with `--active-only` (server-side time check against current UTC).
- **`auto-clear-if-all-green`:** reads a path passed via `--summary-file` (defaults to `${RESULTS_DIR}/latest_summary.json`), and removes every record where `type == "incident"` from the announcement state if and only if the summary represents an unambiguous all-green run. The predicate, as exact `jq` expression:

  ```
  (.services | length) > 0
  and (.error // null) == null
  and (.services | to_entries | all(.value.status == "passed"))
  ```

  The empty-services guard is critical: `scripts/run_tests.sh` writes `{"services": {}, "error": "deployment_setup_failed", ...}` when deployment setup fails (lines 508-516), and `jq '[.services[]] | all(...)'` returns `true` on an empty iteration — without the guard, the exact failure mode the incident banner exists to communicate would silently erase the banner. The `.error` null check is a belt-and-braces guard against future error-bearing summary shapes. If the summary file is missing, malformed, or the predicate is `false`, the command exits 0 with a logged warning (the cron path must not fail because of announcement state).
- **Initial file creation:** when the state file does not exist, `post` (or `update`) treats it as `{"announcements": []}` and creates the file atomically on first write. The file is created lazily on first `post` rather than at entrypoint, so an idle dashboard never has a state file at all — the dashboard fetch handles 404 gracefully (U3).

**Execution note:** Implement test-first. The CLI behavior is fully specifiable from the acceptance examples in the origin doc; writing the pytest tests first makes the implementation contract concrete.

**Patterns to follow:**
- Atomic write idiom from `scripts/run_tests.sh`: `jq ... > "${FILE}.tmp" && mv "${FILE}.tmp" "${FILE}"`.
- Subprocess-driven pytest harness from `scripts/test_mirror_job_logs.py`.
- Bash CLI ergonomics from `scripts/purge_orphans.sh` (argparse-style flag handling with `--`).

**Test scenarios** (pytest with subprocess invocations of `announce.sh`; all fixtures use the keyed-object `.services` shape, not arrays):
- Covers AE6. `post` creates a new record with the right fields and prints the ID to stdout; the on-disk JSON is well-formed.
- `post --type incident` succeeds with no expires_at; the record has `expires_at: null`.
- `post --type maintenance` requires `--expires-at`; missing flag exits non-zero with a clear error.
- `post --type scheduled` requires both `--effective-from` and `--expires-at`; either missing exits non-zero.
- `post --type unknown` exits non-zero with a clear error.
- `post` with a 501-char body exits non-zero (rejects overlong input); 500 chars succeeds.
- `post` with a body containing leading/trailing whitespace stores the trimmed value (whitespace-trim happens at input).
- `post --expires-at 2026-13-45T99:99:99Z` exits non-zero (ISO 8601 format validation rejects bogus values before reaching jq).
- `post --expires-at 2026-05-16T18:00:00` (missing trailing `Z`) exits non-zero.
- `update <id> --body "..."` appends `{ts, body}` to the incident's `updates`; subsequent `list` returns the appended entry.
- Covers AE7. `update <id>` on a `scheduled`-type record exits non-zero ("scheduled announcements do not accept updates").
- `update <missing-id>` exits non-zero with "no announcement with id <missing-id>".
- `clear <id>` removes the record; subsequent `list` does not include it.
- `clear <missing-id>` exits 0 (idempotent) by default.
- `list` on an empty or missing state file prints `{"announcements": []}`-shaped output.
- Covers AE1. `auto-clear-if-all-green --summary-file <fixture>` with a fixture summary where every service in the `.services` object has `status: passed` and `.error` is absent removes all incident-type records but leaves maintenance and scheduled records in place.
- Covers AE2. `auto-clear-if-all-green` with a fixture summary where one service has `status: failed` leaves incident records unchanged.
- **Empty-services guard:** `auto-clear-if-all-green` with a fixture summary where `.services` is `{}` (deployment-setup-failed shape, even when `.error` is also absent) leaves incident records UNCHANGED. This is the failure-mode-trap the predicate exists to handle.
- **Error-field guard:** `auto-clear-if-all-green` with a fixture summary that has populated `.services` AND `.error == "deployment_setup_failed"` leaves incident records UNCHANGED.
- `auto-clear-if-all-green` with a missing or malformed summary file exits 0 (logged warning, no state change).
- Atomic write: when `jq` is forced to fail mid-write (simulate via a malformed input), the on-disk state file is never partially written — either the original is intact or the new file replaced it cleanly.
- ID entropy: 100 sequential `post` invocations within the same UTC second produce 100 distinct IDs (statistical sanity check on the 32-bit random suffix).

**Verification:** All pytest cases pass. The CLI can post, update, list, clear, and auto-clear records against a fresh state file inside a temporary `RESULTS_DIR` set by the test harness.

---

### U2. `serve.py` allowlist + entrypoint symlink

**Goal:** Make the announcement state file fetchable from the dashboard at `/announcement-state.json`, reusing the existing deny-by-default allowlist pattern.

**Requirements:** R12, R14

**Dependencies:** U1 produces the file shape; this unit makes it reachable.

**Files:**
- `dashboard/serve.py` (modified)
- `dashboard/test_serve.py` (modified — add allowlist regression test)
- `scripts/entrypoint.sh` (modified — add symlink line)

**Approach:**

- Add `"announcement-state.json"` to `ALLOWED_JSON_SYMLINKS` in `dashboard/serve.py`. The state file lives at `/results/announcement-state.json` (directly under `RESULTS_ROOT`), which is exactly the containment the existing branch `elif rel_str in ALLOWED_JSON_SYMLINKS and target.is_relative_to(RESULTS_ROOT)` enforces — same shape as the existing `results.json` / `history.json` / `health.json` entries.
- Add one symlink-creation line in `scripts/entrypoint.sh` next to the existing symlink block (lines 146–150):

  ```
  ln -sf "${RESULTS_DIR}/announcement-state.json" /dashboard/announcement-state.json
  ```

  This line is unconditional; the target file may not exist at container start, in which case the symlink is dangling and `serve.py`'s `target.exists()` check 404s gracefully.

- **No new permissions setup is needed.** `/results/` is the same directory the cron jobs already write to (`latest_summary.json`, `cleanup_metrics.json`, `history.json`, `rally_project_ids.log`), so the `rally` user inherits the existing write access. This avoids the `chown` problem flagged during plan review: the Docker base image deliberately drops `CAP_CHOWN` (see `scripts/entrypoint.sh` capability handling), so any plan that added `chown` calls at runtime would silently fail.

**Patterns to follow:**
- Symlink block style from lines 146–150 of `scripts/entrypoint.sh`.
- Test style from existing `dashboard/test_serve.py` (path-traversal, allowlist, MIME tests).

**Test scenarios:**
- GET `/announcement-state.json` returns 200 with the file contents when the file exists.
- GET `/announcement-state.json` returns 404 when the file does not exist (dangling symlink).
- GET `/announcement-state.json` returns `Content-Type: application/json` and `Cache-Control: no-store` (matches existing JSON-symlink behavior).
- GET `/branding/somefile.json` (a path NOT in the allowlist) still returns 403 — verifies that adding the new entry did not widen the allowlist semantics.
- GET `/announcement-state.json/../../etc/passwd` is rejected (existing path-traversal defense still applies).

**Verification:** All pytest cases in `dashboard/test_serve.py` pass, including the new ones. Manual: `curl http://localhost:8080/announcement-state.json` against a running container returns the expected JSON.

---

### U3. Dashboard fetch and render

**Goal:** Fetch the announcement state on the existing refresh cycle, render the active set as a single-slot banner above the 7-day timeline, and provide an expand affordance for progress updates and other concurrent announcements.

**Requirements:** R4, R5, R7, R14, R15 — plus the *output-side* half of R13 (XSS-safe rendering via `textContent`; the input-side half is owned by U1).

**Dependencies:** U2 (file must be fetchable).

**Files:**
- `dashboard/index.html` (modified — banner container element)
- `dashboard/app.js` (modified — `fetchAnnouncements`, `renderAnnouncements`)
- `dashboard/style.css` (modified — banner styles, type-specific severity colors, type labels, `<details>` styles)

**Approach:**

- **index.html.** Add a banner container element directly under the header, ABOVE the 7-day timeline section. The container is `<div id="announcementBanner" role="region" aria-label="Operator announcement" style="display:none;"></div>`. The visible markup uses the existing `historicalBanner` block (lines 78–84) only as a structural pattern reference — the announcement banner and historical banner coexist on the page (see Co-existence rule below), neither suppresses the other.
- **app.js.**
  - New `fetchAnnouncements()` mirroring the existing `fetchHealth()` shape: fetches `/announcement-state.json`, returns the parsed object or `{"announcements": []}` on any failure (including 404 AND `JSON.parse` errors on malformed JSON). The catch covers both `fetch` rejection and `SyntaxError` from a successful HTTP response with an invalid body.
  - New `renderAnnouncements(state)`:
    1. Compute the *active* subset client-side using the current time: drop records where `effective_from > now` (scheduled-not-yet) or `expires_at <= now` (expired). Incidents have neither, so they're always active until removed from the state file.
    2. If active set is empty, hide the banner container, return.
    3. Sort active by `created_at` desc; pick the first as the primary.
    4. The visible banner renders three pieces: (a) a non-color type label `[INCIDENT]` / `[MAINTENANCE]` / `[SCHEDULED]` as a leading pill or prefix, (b) the primary's `body` (or the latest update's body if `updates.length > 0`), and (c) the `<details>` element when there's anything to expand. The whole banner gets a CSS class derived from `data-type` (`announcement-banner type-incident` / `type-maintenance` / `type-scheduled`). Body and update text are rendered via `textContent`, never `innerHTML` — no markdown, no HTML injection.
    5. The `<details>` element contains: the primary's full update timeline (each update as a row with timestamp + body), plus a section listing every other active announcement (also each with its own update timeline). The `<summary>` text includes a visible count when secondary content exists (e.g., `Show 2 updates and 1 other notice` / `Show 3 updates` / `Show 2 other notices`). When the active set has exactly one record with zero updates, omit the `<details>` entirely; the banner shows only the type label and body line with no expand affordance and no "no updates yet" placeholder text.
    6. The auto-refresh path must NOT steal focus or collapse a `<details>` element the user has expanded. When re-rendering on the 5-minute interval, preserve `<details>` `open` state if the same primary announcement is still active.
  - Wire `fetchAnnouncements` into `refresh()` alongside the existing `Promise.all([fetchResults(), fetchHistory(), fetchHealth(), fetchHealthHistory()])`. The added fetch is independent of pinned-run logic and runs every refresh.
- **style.css.** Add styles for:
  - `.announcement-banner` — container shape; padding; border-radius matching existing `historical-banner`.
  - `.announcement-banner.type-incident` → `border-left: 4px solid var(--color-failure)` and a subtle tint of the same color.
  - `.announcement-banner.type-maintenance` → same shape with `var(--color-warning)`.
  - `.announcement-banner.type-scheduled` → same shape with `var(--color-brand-secondary)`.
  - `.announcement-banner .type-label` — uppercase, small, bold, color-matched to the type. This is the redundant non-color signal that makes the banner usable for color-vision-deficient operators.
  - `.announcement-banner details summary` — pointer cursor, accessible focus ring. The summary's text content is rendered dynamically by `renderAnnouncements`, not hardcoded in CSS.
  - `.announcement-banner .update-row` — small monospace timestamp + body.
- **Co-existence rule (announcement banner vs `historicalBanner`).** Both banners may be visible simultaneously and neither suppresses the other. They serve different concerns: the announcement banner carries operator-authored status; `historicalBanner` signals UI-navigation state ("Viewing run X"). The visible disambiguation is the type label on the announcement banner (`[INCIDENT]` / `[MAINTENANCE]` / `[SCHEDULED]`) versus `historicalBanner`'s clock icon and "Viewing run" text. If both happen to use amber (maintenance + historical-run), the type label disambiguates without requiring color discrimination.

**Patterns to follow:**
- `fetchHealth` / `fetchResults` async shape in `app.js`.
- Element insertion + `textContent` discipline already used in `renderTimeline` (uses `innerHTML` with `escapeHtml`, but for new code prefer `textContent` to eliminate the escape step).
- CSS token usage in `dashboard/themes/default/tokens.css` and `dashboard/style.css` — public tokens only, no hardcoded hex outside theme files.

**Test scenarios:**
- Covers AE3. With a `scheduled` announcement whose `effective_from` is 1 hour in the future, the dashboard renders no banner. With `effective_from` in the past and `expires_at` in the future, the dashboard renders it as a blue banner with a `[SCHEDULED]` type label. With `expires_at` in the past, no banner.
- Covers AE4. With a maintenance announcement (3 updates) and a newer incident (1 update) both active, the visible banner shows the incident's latest update with the incident's red styling and `[INCIDENT]` type label. Expanding reveals the incident's full timeline AND the maintenance announcement with its 3 updates. The summary text reads `Show 1 update and 1 other notice` (or equivalent count phrasing).
- Covers AE5. With a single maintenance announcement whose `expires_at` is in the past, no banner renders.
- Single-record-no-updates appearance: with one active maintenance announcement and `updates: []`, the banner shows only the type label and the body line — no `<details>` element, no expand affordance, no placeholder text.
- `fetchAnnouncements` returning `{announcements: []}` hides the banner container.
- `fetchAnnouncements` returning a 404 (state file missing) hides the banner — no console error visible to operators.
- `fetchAnnouncements` receiving syntactically invalid JSON (simulate a 200 response with body `{broken`) silently returns the empty default and hides the banner — no uncaught `SyntaxError` reaches the console.
- Body containing `<script>alert(1)</script>` renders as literal text via `textContent` — no script execution, no HTML structure injected (XSS regression test).
- Banner stays hidden when only expired records exist (client-side filter active even when CLI hasn't cleaned them up yet).
- Co-existence: when the user has pinned a historical run AND an active maintenance announcement exists, both the announcement banner and `historicalBanner` are visible simultaneously. Each is identifiable by its non-color signal (type label vs clock icon).
- Auto-refresh focus preservation: when the user has expanded `<details>` to view updates, a subsequent 5-minute refresh that returns the same primary announcement leaves the `<details>` `open` state intact; focus is not stolen back to the page top.
- Accessibility: the banner container exposes `role="region"` and `aria-label="Operator announcement"`. Screen readers announce the type label as part of the banner's text content (no separate ARIA dance needed beyond the visible label).

**Verification:** Manual: `docker compose up -d --build`, drop fixture state files into `/results/announcement-state.json` for each scenario (single incident, mixed types, all expired, malformed), confirm dashboard renders match the test scenarios above. Type-checking via existing dashboard test harness; no JS unit-test framework is present in the repo today, so dashboard tests stay at the integration level (fixtures + browser-style smoke check) plus the serve.py allowlist coverage from U2.

---

### U4. Auto-clear-on-pass integration in `run_tests.sh`

**Goal:** After every Rally test run, invoke the CLI's `auto-clear-if-all-green` subcommand so incident-type announcements disappear once the system recovers.

**Requirements:** R8 (and origin F3)

**Dependencies:** U1 (the CLI subcommand must exist with its correct predicate — see U1 Approach).

**Files:**
- `scripts/run_tests.sh` (modified)

**Approach:**

- Insert one function call in `scripts/run_tests.sh::main`, after `build_summary` and before `publish_dashboard_files`:

  ```
  # Auto-clear incident-type announcements when the latest run is fully green.
  /scripts/announce.sh auto-clear-if-all-green --summary-file "${SUMMARY_FILE}" || true
  ```

  The functional constraint is "after `build_summary`, before `publish_dashboard_files`": auto-clear reads the per-service outcomes that `build_summary` writes, and `publish_dashboard_files` is what makes the dashboard pick up the resulting state. Placing the call between `build_summary` and `auto_purge_rgw` (the existing call site) is one valid position; any position before `publish_dashboard_files` satisfies the constraint.

  The `|| true` guard ensures the rally run continues even if the announce script errors — announcement state is operational metadata, not gating. The correctness of the auto-clear decision itself depends on U1's predicate (`(.services | length) > 0 AND (.error // null) == null AND (.services | to_entries | all(.value.status == "passed"))`); U1 is canonical for the exact jq form.

**Patterns to follow:**
- Side-effect ordering at the bottom of `main()` in `scripts/run_tests.sh`.
- The same `|| true` post-failure-tolerance pattern used by `auto_purge_rgw` (also non-blocking).

**Test scenarios:**
- Test expectation: covered indirectly by U1's `auto-clear-if-all-green` test cases. No new unit test is added at the `run_tests.sh` level because the integration is a single function call; if the CLI works, the cron hook works. A future smoke test that exercises a full cron cycle could verify end-to-end, but is not added here.

**Verification:** Manual: post a fixture incident, trigger a manual `docker exec -u rally rally-monitor /scripts/run_tests.sh`, confirm the incident disappears from the dashboard within one refresh cycle when all services pass. Repeat with a forced failure (one service down) and confirm the incident remains.

---

### U5. Documentation

**Goal:** Make the announcement system discoverable and operable from the existing operator-facing docs.

**Requirements:** Success criterion "an operator can publish in one CLI invocation from a host shell with no manual file editing"; preserves the project's pattern of operator-facing surfaces being documented in `README.md` and architectural facts in `CLAUDE.md`.

**Dependencies:** U1–U4 complete (so docs reflect implementation, not aspiration).

**Files:**
- `README.md` (modified — new "Operator Announcements" subsection under existing operator docs)
- `CLAUDE.md` (modified — add a brief Announcement System subsection under Architecture, parallel to Theme Customization)
- `CHANGELOG.md` (modified — release entry; see Documentation Plan for the full set of doc touches)

**Approach:**

- **README.md** — Add a short subsection covering: what announcements are, the three types, the four CLI verbs with one-line examples each (`post`, `update`, `clear`, `list`), the auto-clear-on-pass behavior, and a note that publishing requires host shell access (`docker exec`).
- **CLAUDE.md** — Add an "Announcement System" subsection mirroring the existing "Theme Customization" subsection: where the state file lives (`/results/announcement-state.json` — explicitly outside `/results/branding/` to avoid the documented read-only theme bind-mount collision), how the dashboard fetches it (allowlist + symlink), where the auto-clear hook lives in `run_tests.sh`, and the no-HTTP-write policy.

**Test scenarios:**
- Test expectation: none — documentation update.

**Verification:** Read both files end-to-end; confirm a new operator could publish their first announcement from the README alone, and a new contributor could understand the integration points from `CLAUDE.md` alone.

---

## Sequencing

```
U1 (CLI + state file) ─┬─► U2 (serve.py + symlink) ──► U3 (dashboard render)
                       └─► U4 (run_tests.sh hook)
                                                                          │
                                                            U5 (docs) ◄───┘
```

U1 is the foundation; everything else depends on it. U2 and U4 can land in either order after U1. U3 depends on U2. U5 should land after U1–U4 so documentation reflects shipped behavior.

---

## System-Wide Impact

- **`/results/` Docker volume layout.** Adds one new file path (`/results/announcement-state.json`, directly under `/results/` — deliberately outside `/results/branding/` to avoid the read-only theme bind-mount collision). Existing volume permissions and the `/rally/rally_env` 0640 protection are unchanged.
- **`serve.py` allowlist.** Grows by one entry. Behavioral impact: one new GET path serves; no other paths gain access. Path traversal and MIME-type defenses unchanged.
- **`scripts/entrypoint.sh` startup.** Adds one `ln -sf` line. Idempotent on container restart; matches the existing symlink block.
- **`scripts/run_tests.sh` cron flow.** Adds one bounded function call after `build_summary`. Failure-tolerant (`|| true`). Adds <100ms to the post-run flow (a `jq` read + conditional rewrite).
- **Dashboard refresh cycle.** Adds one more parallel `fetch()` to `refresh()`. Independent of pinned-run state. Net cost: one HTTP request per 5-minute refresh (~12 per hour per active dashboard tab).
- **Exporter (`rally_exporter.py`).** No change. Announcement state is dashboard-only in v1; future Prometheus gauge integration is explicitly deferred.

---

## Risks & Mitigations

- **Risk:** Operator publishes an announcement with a `body` containing JSON-breaking characters (quotes, backslashes) that `jq` mishandles.
  - **Mitigation:** Use `jq --arg body "$BODY"` everywhere; `--arg` is JSON-safe for arbitrary input. Test scenario in U1 covers a body with embedded quotes and backslashes.

- **Risk:** Operator supplies a malformed ISO 8601 timestamp (`2026-13-45T99:99:99Z`, `2026-05-16 18:00:00`, etc.) that passes `jq --arg`'s JSON-safety but produces `NaN` from `Date.parse` in the dashboard — the resulting banner would never expire client-side.
  - **Mitigation:** `announce.sh` validates every timestamp input against `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$` before passing to `jq`; invalid timestamps cause `post`/`update` to exit non-zero with a clear error. Test scenarios in U1 cover both the bogus-date and missing-Z cases.

- **Risk:** Auto-clear fires when the post-run summary indicates a setup failure, silently erasing the incident announcement during the exact failure mode operators most need it.
  - **Mitigation:** The auto-clear `jq` predicate (specified in U1) is `(.services | length) > 0 AND (.error // null) == null AND (.services | to_entries | all(.value.status == "passed"))`. The empty-services and `.error` checks are belt-and-braces guards against the `deployment_setup_failed` summary shape (`{"services": {}, "error": "deployment_setup_failed", ...}`) where a naive `all(...)` returns `true` on empty input. Two dedicated test scenarios in U1 (`empty-services guard` and `error-field guard`) verify both branches.

- **Risk:** Two `docker exec` invocations from different terminals race on the state file and one overwrites the other's edit (lost update).
  - **Mitigation:** Single-operator model makes this rare. The `tmpfile + mv` pattern guarantees no torn writes (one of the two writes wins atomically; neither leaves the file in a partial state). If the race becomes real, add a `flock` on `${RESULTS_DIR}/.announce.lock` — deferred to follow-up unless v1 user feedback surfaces it.

- **Risk:** A malformed announcement state file (manual operator edit, partial write recovery, schema drift) breaks the dashboard's `JSON.parse`.
  - **Mitigation:** `fetchAnnouncements` catches both fetch errors AND `SyntaxError` from invalid JSON, returning the empty default — the dashboard renders no banner rather than blowing up the page. Test scenario in U3 covers both the 404 path and the malformed-JSON path.

- **Risk:** State file ownership flips between `root` and `rally` across invocations (cron runs as `rally`; ad-hoc `docker exec` without `-u rally` defaults to root). A root-owned state file blocks the next rally-as-cron write.
  - **Mitigation:** Operator-facing documentation (U5) standardizes on `docker exec -u rally rally-monitor /scripts/announce.sh ...` in every example. The verification checklist invocations also include `-u rally`. The cron path is already `-u rally` via the cron table setup in `entrypoint.sh`.

- **Risk:** The dashboard's existing 5-minute refresh feels too slow for "just published an urgent maintenance banner."
  - **Mitigation:** Inherited from the brainstorm (R14). If operators report this is a problem, a future iteration can lower the refresh interval for the announcement fetch specifically. Out of scope for v1.

---

## Documentation Plan

- `README.md` — new "Operator Announcements" subsection (U5).
- `CLAUDE.md` — new "Announcement System" subsection under Architecture (U5).
- `CHANGELOG.md` — add an entry for the next release noting the feature, the CLI surface, and the no-HTTP-write decision.

---

## Verification Checklist (end-to-end)

After all five units land, the following should all be true:

- [ ] `docker exec -u rally rally-monitor /scripts/announce.sh post --type incident --body "Test"` prints an ID and creates `/results/announcement-state.json` with one record.
- [ ] The dashboard, on its next refresh, shows a red `[INCIDENT]`-labeled banner with body "Test".
- [ ] `docker exec -u rally rally-monitor /scripts/announce.sh update <id> --body "Update one"` appends an entry; the banner now shows "Update one" and expand reveals both entries.
- [ ] Triggering a manual Rally run that passes (all services green, no `.error` field) auto-clears the incident; banner disappears within one refresh.
- [ ] Triggering a manual Rally run that has at least one failure leaves the incident visible.
- [ ] Triggering a manual Rally run that hits `setup_deployment` failure (writes `services: {}, error: deployment_setup_failed`) LEAVES the incident banner visible — verifies the empty-services guard.
- [ ] `docker exec -u rally rally-monitor /scripts/announce.sh post --type scheduled --effective-from <future> --expires-at <further-future> --body "Upgrade"` produces no immediate dashboard change; the `[SCHEDULED]`-labeled blue banner appears after `effective_from`.
- [ ] `docker exec -u rally rally-monitor /scripts/announce.sh post --expires-at 2026-13-45T99:99:99Z ...` exits non-zero with a clear error (ISO 8601 validation).
- [ ] `docker exec -u rally rally-monitor /scripts/announce.sh clear <id>` removes any record by ID; the dashboard reflects the change on next refresh.
- [ ] A body with HTML/JS payload renders as literal text on the dashboard (no XSS regression).
- [ ] Bind-mounting a read-only theme bundle at `/results/branding/` (the documented `:ro` pattern from `docs/CUSTOMIZING.md`) does NOT block announcement publishing — the state file lives at `/results/announcement-state.json`, outside `branding/`.
- [ ] When viewing a historical run AND an announcement banner is active, both banners are visible simultaneously and each is identifiable by its non-color signal.
- [ ] `pytest dashboard/test_serve.py scripts/test_announce.py` passes.
