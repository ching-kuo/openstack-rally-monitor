/**
 * API health check timeline and the section uptime badges.
 */
import { formatTimestamp, plural } from "./format.js";
import { focusedKey, placeRovingFocus, setSpanLabel, tooltipEdgeClass, tooltipHtml } from "./dom.js";
import { HEALTH_STATUSES, knownStatus, servicesWithStatus } from "./status.js";

// Renders "<percent>% uptime / <N>d" into a section-header badge. Stays hidden
// until the backing scripts have published an uptime object (first boots and
// data files written before the uptime feature have none).
export function renderUptimeBadge(id, uptime) {
  const badge = document.getElementById(id);
  if (!badge) return;
  if (!uptime || typeof uptime.percent !== "number") {
    badge.hidden = true;
    badge.removeAttribute("title");
    return;
  }
  badge.textContent = `${uptime.percent}% uptime / ${uptime.window_days}d`;

  // Hover tooltip with the underlying counts. The two uptime objects use
  // different field names -- smoke uptime: runs_passed/runs_total; API uptime:
  // checks_up/checks_total -- so detect whichever pair is present.
  let up;
  let total;
  let noun;
  if (typeof uptime.runs_total === "number") {
    up = uptime.runs_passed;
    total = uptime.runs_total;
    noun = "runs";
  } else if (typeof uptime.checks_total === "number") {
    up = uptime.checks_up;
    total = uptime.checks_total;
    noun = "checks";
  }
  if (typeof total === "number") {
    badge.title =
      `${up} of ${total} ${noun} healthy over the last ${uptime.window_days} days`;
  } else {
    badge.removeAttribute("title");
  }
  badge.hidden = false;
}

// Plain-text detail for one health-timeline cell (the tooltip escapes it; the
// aria-label uses it as is). `status` is already whitelisted.
export function healthCellDetail(status, services) {
  if (status === "down") return `Down: ${servicesWithStatus(services, "down").join(", ")}`;
  if (status === "degraded") {
    // List the slow services with their latencies so the tooltip reads e.g.
    // "Degraded: nova (6200ms)".
    const slowSvcs = Object.entries(services || {})
      .filter(([, v]) => v && v.status === "degraded")
      .map(([k, v]) => `${k} (${v.latency_ms}ms)`);
    return `Degraded: ${slowSvcs.join(", ")}`;
  }
  if (status === "up") return "All services up";
  return "Status unknown";
}

export function renderHealthTimeline(healthHistory) {
  const container = document.getElementById("healthTimeline");
  const countBadge = document.getElementById("healthTimelineCount");

  const allChecks = (healthHistory && healthHistory.checks) || [];
  let checks = allChecks;
  setSpanLabel("healthTimelineSpan", allChecks);

  // Trim checks to the number that can physically fit in the container.
  // Each cell is at least 2px wide with a 2px gap between cells.
  // Container padding is 1rem (16px) on each side.
  const innerWidth = container.clientWidth - 32;
  if (innerWidth > 0) {
    const maxCells = Math.floor((innerWidth + 2) / 4); // (minCell + gap) = 4px per slot
    if (checks.length > maxCells) {
      checks = allChecks.slice(allChecks.length - maxCells);
    }
  }

  const showing = checks.length;
  const total = allChecks.length;
  countBadge.textContent = showing < total
    ? `${showing} of ${total} checks`
    : plural(total, "check");

  if (checks.length === 0) {
    container.innerHTML =
      '<div class="timeline-loading">No health check data yet</div>';
    return;
  }

  const hadFocus = focusedKey(container);
  container.replaceChildren();
  const cells = checks.map((check, i) => {
    const status = knownStatus(check && check.overall, HEALTH_STATUSES);
    const when = formatTimestamp(check && check.timestamp);
    const detail = healthCellDetail(status, check && check.services);
    const cell = document.createElement("div");
    cell.className = `htl-cell ${status} ${tooltipEdgeClass(i, checks.length)}`.trim();
    // Keyed by check time, so focus follows the same check as the row shifts.
    cell.dataset.key = String(check && check.timestamp);
    cell.setAttribute("role", "img");
    cell.setAttribute("aria-label", `${when}: ${detail}`);
    cell.innerHTML = tooltipHtml(when, detail);
    container.appendChild(cell);
    return cell;
  });
  placeRovingFocus(cells, null, hadFocus);
}
