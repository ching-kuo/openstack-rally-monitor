---
date: 2026-05-16
topic: dashboard-admin-status
---

# Dashboard Admin Status Posting

## Summary

A `rally-announce` CLI (invoked via `docker exec`) lets the operator publish three types of status notes — maintenance windows, ongoing incidents (with appendable progress updates), and scheduled-future announcements. The dashboard surfaces a single visible banner with an expand affordance for progress and other concurrent announcements, backed by a file in the existing `/results/branding/` overlay. No new HTTP write surface is introduced.

---

## Problem Frame

The Rally Monitor dashboard is a read-only mirror of the most recent test runs and health checks. When an operator triggers planned maintenance, when an incident is in progress, or when a scheduled change is upcoming, viewers have no in-band way to know — they see only red cells with no operator context. Coordinating the explanation lives in a separate channel today (Slack, email), and anyone glancing at the dashboard during the affected window misreads "things are broken" as a real regression.

The user weighed a localhost-only POST endpoint on the exporter but flagged uncertainty about whether a full admin dashboard would be overkill. A prior ideation session (`docs/ideation/2026-05-15-dashboard-admin-status-ideation.md`) established the documented project preference for file-drop over HTTP-write and surfaced the additional richness operators actually want: typed lifecycles, incident progress timelines, and the "I'm working, ignore the failures" maintenance use case.

---

## Actors

- A1. **Operator**: Person running the rally-monitor container with `docker exec` access. Publishes announcements, appends progress updates, and clears them explicitly when needed.
- A2. **Dashboard viewer**: Anyone reading the dashboard in a browser. Reads announcements; does not author or dismiss them.
- A3. **Rally cron**: Automated process that runs the test suite on a schedule. Acts as the auto-clear trigger for incident-type announcements when all services pass.

---

## Key Flows

- F1. **Publish a maintenance window**
  - **Trigger:** Operator about to perform lab maintenance
  - **Actors:** A1
  - **Steps:** Operator invokes the CLI with type=maintenance, a body, and an `expires_at`. The CLI writes the announcement atomically into the branding overlay. Within one dashboard refresh cycle, viewers see the yellow maintenance banner.
  - **Outcome:** Banner is visible until `expires_at`, after which it disappears automatically. Operator may also clear it earlier via the CLI.
  - **Covered by:** R1, R3, R7, R9, R10, R12

- F2. **Publish an incident and append progress updates**
  - **Trigger:** Operator observing a real failure they want to communicate
  - **Actors:** A1, A2
  - **Steps:** Operator publishes type=incident with an initial body; the CLI returns a stable identifier. As the operator investigates, they invoke the CLI again to append updates against that identifier. Each update becomes a new entry in the incident's timeline. Viewers see the latest update by default and can expand to read prior updates.
  - **Outcome:** Banner stays visible until cleared (manually or by auto-clear). The timeline is preserved while the announcement is active.
  - **Covered by:** R2, R4, R5, R10, R11

- F3. **Auto-clear on recovery**
  - **Trigger:** Rally cron completes a run where every service passes
  - **Actors:** A3
  - **Steps:** Post-run processing detects the all-green outcome and removes every active incident-type announcement. Within one dashboard refresh cycle, the incident banner disappears.
  - **Outcome:** Incident banner is gone; the active record is no longer surfaced by the dashboard.
  - **Covered by:** R2, R8

- F4. **Scheduled future announcement**
  - **Trigger:** Operator writes a scheduled-future note before the window starts
  - **Actors:** A1, A2
  - **Steps:** Operator publishes type=scheduled with both `effective_from` (in the future) and `expires_at`. The record exists in the branding overlay but is hidden from the dashboard until `effective_from`. Once active, it renders as a blue banner until `expires_at`.
  - **Outcome:** Banner appears and disappears autonomously within the configured window.
  - **Covered by:** R6, R7

---

## Requirements

**Announcement types and lifecycle**
- R1. The system supports three announcement types: `maintenance`, `incident`, and `scheduled`.
- R2. `incident` announcements have no `expires_at`. They clear when (a) the operator clears them via the CLI, OR (b) the next Rally run completes with every service passing.
- R3. `maintenance` announcements require an `expires_at` timestamp and disappear automatically after that time. They can also be cleared earlier via the CLI.
- R6. `scheduled` announcements require both `effective_from` and `expires_at`. They are not rendered before `effective_from` or after `expires_at`.
- R8. After every Rally run, post-run processing inspects the run's per-service outcomes; if every service passed, every active `incident` announcement is removed from the active set.

**Authoring (operator CLI)**
- R9. The operator publishes and manages announcements through a CLI invoked via `docker exec` on the rally-monitor container. No new HTTP write endpoint is introduced anywhere in the stack.
- R10. The CLI supports four operations: publish a new announcement, append a progress update to an existing announcement (incident or maintenance only), clear an existing announcement explicitly (any type), and list active announcements (with their identifiers).
- R11. `incident` and `maintenance` announcements support a timeline of appended updates. `scheduled` announcements are single-body and do not accept updates.
- R12. CLI writes are atomic — a partially-written or partially-applied update never becomes visible to the dashboard.

**Dashboard rendering**
- R4. The dashboard renders one announcement at a time as a single visible banner placed above the run-status grid; it does not overlay charts or run cards.
- R5. When the active announcement has multiple updates, the banner shows the most recent update by default. A click/tap affordance expands the banner to reveal the full timeline of updates for that announcement, plus any other currently-active announcements (each with its own timeline if relevant).
- R7. Announcement type drives visual treatment: `incident` styled as an error/red banner, `maintenance` styled as a warning/yellow banner, `scheduled` styled as an informational/blue banner. There is no separate severity field. The exact color values come from the dashboard's existing theme tokens, not new hardcoded constants.
- R13. Banner body is plain text only. The dashboard renders it without parsing Markdown or HTML and without resolving inline links.
- R14. The dashboard polls announcement state on its existing 5-minute refresh cycle. No new poll cadence or push mechanism is introduced.

**Concurrency**
- R15. When more than one announcement is active at the same moment, the newest by publish timestamp is shown in the visible slot. Older concurrently-active announcements are accessible through the same expand affordance described in R5.

---

## Acceptance Examples

- AE1. **Covers R2, R8.** Given an active incident announcement, when the next Rally run completes and every one of the six services passes, the incident is removed from the active set and the banner disappears within one dashboard refresh cycle.
- AE2. **Covers R2.** Given an active incident announcement, when the next Rally run completes with at least one service failing, the incident remains active (no auto-clear) regardless of which specific service failed.
- AE3. **Covers R3.** Given a maintenance announcement with `expires_at` two hours in the past, the dashboard does not render it. Given the same maintenance announcement with `expires_at` two hours in the future, the dashboard renders it as a yellow banner.
- AE4. **Covers R6.** Given a scheduled announcement published at T with `effective_from = T+1h` and `expires_at = T+3h`: at T+30m the dashboard renders nothing; at T+2h the dashboard renders it as a blue banner; at T+4h the dashboard renders nothing.
- AE5. **Covers R5, R15.** Given an active maintenance announcement with 3 progress updates and a newly-published incident announcement, the visible banner shows the incident's latest update (red). Clicking expand reveals (a) the incident's full update timeline and (b) the maintenance announcement and its 3 updates.
- AE6. **Covers R10, R12.** Given an active incident, when the operator invokes the CLI to append an update, no partial state ever appears in the dashboard — the dashboard either still shows the prior latest update or shows the new one.
- AE7. **Covers R11.** Given a scheduled announcement, when the operator invokes the CLI's update operation against its identifier, the CLI rejects the invocation with an error rather than appending an update.

---

## Success Criteria

- An operator can publish a maintenance, incident, or scheduled-future announcement in one CLI invocation from a host shell, with no manual JSON/YAML editing.
- A viewer arriving at the dashboard during an active incident sees both the banner AND can expand to view the operator's progress notes without contacting the operator out-of-band.
- When the system recovers (all services pass), incident banners disappear within one refresh cycle without operator intervention. Stale banners do not accumulate.
- `ce-plan` can implement this without re-litigating product behavior: the CLI's operation set, the three lifecycles, the render model, and the scope of updates are all specified here. Planning's open work is implementation choice (schema format, storage shape, integration point in `run_tests.sh`, exact CLI names).

---

## Scope Boundaries

- **Persistent caveats / standing context** ("this lab is shared with other tenants; results may vary") are explicitly excluded. The three time-bounded types cover the use cases the operator named.
- **Localhost-only HTTP POST endpoint** is not in v1. The design and its corrected security boundary remain captured as idea #5 in `docs/ideation/2026-05-15-dashboard-admin-status-ideation.md` for possible later revisit.
- **Prometheus gauge emission** for active announcements (idea #7 in the same ideation doc) is deferred. Banners are dashboard-only in v1.
- **Per-service annotation pills** on individual service cards (idea #6) are not in v1; the surface is a single global banner with expand.
- **Viewer-side dismissibility** is not supported. Operators control visibility; viewers see what is active.
- **Forensic / historical view** of expired or cleared announcements is not in v1. Once a record is auto-cleared, manually cleared, or expired, it is gone from the dashboard's view. The branding overlay may or may not retain the file on disk for operator-side audit — that is a planning concern.
- **Markdown, HTML, or rich-content rendering** is not in v1. Plain text only.
- **GPG signing or multi-operator authentication.** The threat model assumes a single trusted operator with host shell access. Multi-operator coordination, attribution, and signing are out.

---

## Key Decisions

- **No new HTTP write surface.** All writes go through a CLI script invoked via `docker exec`. Authorization equals "you can run `docker exec` on this host." This matches the documented project preference for file-drop over HTTP-write (per `CLAUDE.md` Theme Customization), avoids opening a new attack surface on the exporter process (which holds `OS_PASSWORD`), and inherits the same trust model already used for manual Rally runs (e.g., `docker exec -u rally rally-monitor /scripts/run_tests.sh`).
- **Type implies styling, no separate severity field.** The three named types (maintenance, incident, scheduled) map 1:1 to three visual treatments. A separate severity field would add knobs without paying for them in v1; if operators later report wanting "P1 incident" vs "P3 incident" distinctions, severity can be added.
- **Newest-wins for concurrent announcements.** When multiple announcements are simultaneously active, the primary visible slot shows the newest by publish timestamp; others are accessible via expand. Decided as a sensible default rather than priority-based selection because it is the simplest mental model and the operator can always re-post a more important banner to push it to the top.
- **Auto-clear on all-green run, not on specific-service passes.** Any service failure in the next Rally run keeps incident banners up. Operator does not need to tag the incident with affected services; the all-green signal is a single global trigger.
- **No retention / no historical archive in the dashboard.** Cleared and expired announcements are gone from the dashboard entirely. Forensic history is a different feature, deferred. V1 optimizes for in-the-moment communication, not post-mortem review.
- **Plain-text-only body.** Eliminates the entire Markdown sanitization surface (no `marked`, no `DOMPurify`) and keeps the threat model simple. Operators paste URLs as literal text; the dashboard renders via `textContent`.

---

## Dependencies / Assumptions

- The existing `/results/branding/` bind-mount and the entrypoint's symlink to `/dashboard/themes/custom` continue to be the operator overlay primitive. Verified against `scripts/entrypoint.sh` and the deny-by-default allowlist in `dashboard/serve.py`.
- The dashboard's existing 5-minute refresh cycle (`REFRESH_INTERVAL` in `dashboard/app.js`) is the polling cadence for announcements. No new cadence is introduced.
- Rally's post-run hook in `scripts/run_tests.sh` is a viable integration point for incident auto-clear. The script already performs post-run side effects (auto-purge of RGW orphans, cleanup metric refresh); incident auto-clearing can run in the same point.
- Single-operator model. The product does not assume multiple concurrent operators publishing announcements; the CLI is the only writer and atomic rename is sufficient for the "operator runs two terminals at once" edge case.
- Trust boundary equals host-level `docker exec` access. Anyone who can `docker exec` on the host can also already edit the bind-mount directly; the CLI does not add a new privilege surface, it formalizes an existing one.

---

## Outstanding Questions

### Resolve Before Planning

(none — all product decisions resolved during brainstorm)

### Deferred to Planning

- [Affects R1, R12][Technical] Schema serialization format and field names: JSON vs YAML, ID generation strategy (timestamp-slug vs short UUID), exact key names. The seed proposed JSON; planning may revisit.
- [Affects R10, R12][Technical] Storage shape: single mutable file containing an array of announcements, vs a directory of per-record files. Both can satisfy the requirements; trade-offs (concurrent-write safety, glob complexity, retention semantics, dashboard read pattern) are implementation-side.
- [Affects R8][Technical] Exact integration point for auto-clear in `scripts/run_tests.sh` — whether a new step at the end of `build_summary()` or a separate post-run trigger; whether the auto-clear lives in shell or invokes the CLI helper itself.
- [Affects R13][Technical] Body length cap. The seed proposed ≤280 chars; the brainstorm did not lock this. Planning should pick a default (suggest 280 or 500 chars per update) and document it; the CLI should reject overlong inputs.
- [Affects R7][Needs research] The exact CSS tokens the existing theme system exposes for warning / error / info colors — confirm that the new banner reuses the public token contract rather than hardcoding hex values. See `docs/CUSTOMIZING.md` for the documented public token surface.
- [Affects R4, R5, R15][Technical] DOM/component shape for the expand affordance (native `<details>`, custom collapsible, modal, etc.). Pure implementation detail.
- [Affects R10][Technical] CLI command surface naming — `rally-announce post|update|clear|list` is one obvious shape; planning may pick alternatives that fit the project's existing script ergonomics.
- [Affects R12][Technical] Whether the dashboard's `serve.py` allowlist needs one or more new entries to GET the announcement state, and whether the path should live directly under `/results/branding/` or in a sub-path (e.g., `/results/branding/announcements/`).
