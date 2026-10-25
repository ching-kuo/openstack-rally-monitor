/**
 * Operator announcement banner (announcement-state.json, written by
 * scripts/announce.sh).
 */
import { formatTimestamp, plural } from "./format.js";

export const TYPE_LABELS = {
  incident: "INCIDENT",
  maintenance: "MAINTENANCE",
  scheduled: "SCHEDULED",
};

// Active = currently displayable. effective_from in the future or expires_at
// in the past hides the record on the client even if the CLI has not yet
// pruned it. Incidents have neither bound and stay active until cleared.
// KEEP IN SYNC with announcement_metrics.py::is_announcement_active — both encode
// the same active-ness semantics (the repo uses this convention for
// ALL_GREEN_PREDICATE across announce.sh/run_tests.sh):
//   - unknown type                      -> not active
//   - effective_from parseable & future -> not active
//   - expires_at parseable & <= now     -> not active
//   - records missing both bounds       -> active until cleared
// Unparseable bounds are ignored (Number.isFinite guard), matching Python.
export function isAnnouncementActive(rec, nowMs) {
  // Own keys only: "toString" or "constructor" must not pass as a type.
  if (typeof rec.type !== "string" || !Object.hasOwn(TYPE_LABELS, rec.type)) return false;
  if (rec.effective_from) {
    const startMs = Date.parse(rec.effective_from);
    if (Number.isFinite(startMs) && startMs > nowMs) return false;
  }
  if (rec.expires_at) {
    const endMs = Date.parse(rec.expires_at);
    if (Number.isFinite(endMs) && endMs <= nowMs) return false;
  }
  return true;
}

function buildSummaryText(primaryUpdateCount, otherCount) {
  const parts = [];
  if (primaryUpdateCount > 0) {
    parts.push(plural(primaryUpdateCount, "update"));
  }
  if (otherCount > 0) {
    parts.push(plural(otherCount, "other notice"));
  }
  return `Show ${parts.join(" and ")}`;
}

function appendUpdateRow(parent, update) {
  const row = document.createElement("div");
  row.className = "update-row";
  const ts = document.createElement("span");
  ts.className = "update-ts";
  ts.textContent = formatTimestamp(update.ts);
  const body = document.createElement("span");
  body.className = "update-body";
  body.textContent = update.body;
  row.appendChild(ts);
  row.appendChild(body);
  parent.appendChild(row);
}

function appendAnnouncementSection(parent, rec, isSecondary) {
  const section = document.createElement("div");
  section.className = isSecondary
    ? "announcement-section is-secondary"
    : "announcement-section";

  const label = document.createElement("span");
  label.className = "type-label";
  label.textContent = `[${TYPE_LABELS[rec.type]}]`;
  section.appendChild(label);

  const bodyLine = document.createElement("div");
  bodyLine.className = "announcement-body";
  bodyLine.textContent = rec.body;
  section.appendChild(bodyLine);

  if (rec.updates && rec.updates.length > 0) {
    const updatesContainer = document.createElement("div");
    updatesContainer.className = "update-list";
    rec.updates.forEach((u) => appendUpdateRow(updatesContainer, u));
    section.appendChild(updatesContainer);
  }
  parent.appendChild(section);
}

export function renderAnnouncements(state) {
  const container = document.getElementById("announcementBanner");
  if (!container) return;

  // Defensive: a state file with a non-array `announcements` (operator hand-
  // edit, schema drift, etc.) must not crash refresh() and take the timeline
  // down with it.
  const rawRecords = Array.isArray(state?.announcements) ? state.announcements : [];
  const nowMs = Date.now();
  const active = rawRecords
    .filter((r) => r && typeof r === "object")
    .filter((r) => isAnnouncementActive(r, nowMs));

  if (active.length === 0) {
    container.hidden = true;
    container.replaceChildren();
    container.dataset.primaryId = "";
    return;
  }

  // Newest by created_at first. Records without a parseable created_at fall to
  // the end rather than being silently dropped.
  active.sort((a, b) => {
    const ta = Date.parse(a.created_at) || 0;
    const tb = Date.parse(b.created_at) || 0;
    return tb - ta;
  });

  const primary = active[0];
  const others = active.slice(1);
  const latestUpdate = (primary.updates && primary.updates.length > 0)
    ? primary.updates[primary.updates.length - 1]
    : null;
  const primaryBodyText = latestUpdate ? latestUpdate.body : primary.body;
  const primaryUpdateCount = (primary.updates && primary.updates.length) || 0;

  // Preserve user-expanded state across the 5-minute refresh as long as the
  // primary announcement is the same record.
  const previousPrimaryId = container.dataset.primaryId || "";
  const previousDetailsOpen = container.querySelector("details")?.open ?? false;
  const shouldKeepOpen = previousPrimaryId === primary.id && previousDetailsOpen;

  container.replaceChildren();
  container.className = `announcement-banner type-${primary.type}`;
  container.hidden = false;
  container.dataset.primaryId = primary.id;

  // Visible primary line: type label + body/latest update.
  const primaryLine = document.createElement("div");
  primaryLine.className = "announcement-primary";

  const typeLabel = document.createElement("span");
  typeLabel.className = "type-label";
  typeLabel.textContent = `[${TYPE_LABELS[primary.type]}]`;
  primaryLine.appendChild(typeLabel);

  const bodyText = document.createElement("span");
  bodyText.className = "announcement-body";
  bodyText.textContent = primaryBodyText;
  primaryLine.appendChild(bodyText);
  container.appendChild(primaryLine);

  // If there's nothing to expand, stop here.
  const hasUpdatesToShow = primaryUpdateCount > 0;
  const hasOthers = others.length > 0;
  if (!hasUpdatesToShow && !hasOthers) return;

  const details = document.createElement("details");
  if (shouldKeepOpen) details.open = true;
  const summary = document.createElement("summary");
  summary.textContent = buildSummaryText(primaryUpdateCount, others.length);
  details.appendChild(summary);

  const detailsBody = document.createElement("div");
  detailsBody.className = "announcement-details-body";

  if (hasUpdatesToShow) {
    appendAnnouncementSection(detailsBody, primary, false);
  }
  others.forEach((rec) => appendAnnouncementSection(detailsBody, rec, true));

  details.appendChild(detailsBody);
  container.appendChild(details);
}
