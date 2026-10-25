/**
 * Pure formatting helpers: no DOM access, so the node tests import them
 * directly.
 */

const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

// Escape HTML special characters to prevent XSS when interpolating untrusted
// data (service names, scenario names, timestamps) into innerHTML. Quotes are
// escaped too, so the result is also safe inside a quoted attribute.
export function escapeHtml(str) {
  return String(str ?? "").replace(/[&<>"']/g, (c) => HTML_ESCAPES[c]);
}

// Seed values standing in for a timestamp before the first run / check
// (init_state.sh): "waiting_for_first_run" in summaries, "waiting" in
// health.json, "none" elsewhere.
const PLACEHOLDER_TIMESTAMPS = new Set(["waiting_for_first_run", "waiting", "none"]);

// Parse a run/check timestamp into epoch milliseconds, accepting both the
// compact rally form (20260220T143021Z) and ISO 8601. Returns NaN for the
// placeholder/unparseable values so callers can skip them.
export function parseTimestampMs(ts) {
  if (!ts || PLACEHOLDER_TIMESTAMPS.has(ts)) return NaN;
  const compact = String(ts).match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/);
  if (compact) {
    const [, y, mo, d, h, mi, s] = compact;
    return Date.UTC(+y, +mo - 1, +d, +h, +mi, +s);
  }
  return Date.parse(ts);
}

// Full local date and time with the zone name, e.g. "Oct 26, 2026, 02:15 PM
// GMT+1", so a reader in another zone is not misled.
export function formatTimestamp(ts) {
  if (!ts || PLACEHOLDER_TIMESTAMPS.has(ts))
    return "Waiting for first run...";
  const ms = parseTimestampMs(ts);
  if (!Number.isFinite(ms)) return String(ts);
  return new Date(ms).toLocaleString([], {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  });
}

// Short chart axis label. toLocaleString().substring(0, 16) used to cut the
// AM/PM marker off 12-hour locales.
export function formatChartLabel(ts) {
  const ms = parseTimestampMs(ts);
  if (!Number.isFinite(ms)) return String(ts ?? "");
  return new Date(ms).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// The viewer's short time-zone name, e.g. "GMT+1" or "PST", for chart axes
// whose tick labels are too short to carry it.
export function localZoneName() {
  const part = new Intl.DateTimeFormat([], { timeZoneName: "short" })
    .formatToParts(new Date())
    .find((p) => p.type === "timeZoneName");
  return part ? part.value : "local";
}

// Local wall-clock time, e.g. "14:05 GMT+1", for "showing data from ...".
export function formatClock(ms) {
  return new Date(ms).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  });
}

// Relative age of `ms` at `nowMs`: "just now", "12m ago", "3h 5m ago",
// "2d 4h ago". A future timestamp (clock skew) reads as "just now".
export function formatAge(ms, nowMs) {
  const minutes = Math.floor(Math.max(0, nowMs - ms) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return minutes % 60 ? `${hours}h ${minutes % 60}m ago` : `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return hours % 24 ? `${days}d ${hours % 24}h ago` : `${days}d ago`;
}

// "a, b, c +2": at most `max` names, then a count of the rest.
export function summarizeNames(names, max = 3) {
  const shown = names.slice(0, max).join(", ");
  return names.length > max ? `${shown} +${names.length - max}` : shown;
}

// Build a neutral, data-driven span label from a list of records carrying a
// `.timestamp`. Returns "" when fewer than two parseable timestamps exist (a
// single data point has no span to describe -- "Last hour" for one run would
// be misleading) so the empty subhead stays hidden via CSS `:empty`. The
// dashboard cannot know RALLY_RESULTS_RETENTION_DAYS, so this reports the
// actually-covered window rather than hardcoding "7-Day".
// "1 run", "3 runs": a count with a naive English plural.
export function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

export function formatCoveredSpan(records) {
  // Single pass min/max instead of Math.max(...times): the health history can
  // hold thousands of entries and argument-spreading that many values risks a
  // RangeError on large UPTIME_WINDOW_DAYS configurations.
  let earliest = Infinity;
  let latest = -Infinity;
  let count = 0;
  for (const r of records || []) {
    const ms = parseTimestampMs(r && r.timestamp);
    if (!Number.isFinite(ms)) continue;
    count += 1;
    if (ms < earliest) earliest = ms;
    if (ms > latest) latest = ms;
  }
  if (count < 2) return "";
  const span = latest - earliest;
  const days = Math.round(span / 86_400_000);
  if (days >= 1) return `Last ${plural(days, "day")}`;
  const hours = Math.round(span / 3_600_000);
  if (hours >= 1) return `Last ${plural(hours, "hour")}`;
  return "Last hour";
}

export function formatDuration(value) {
  // Coerced: a non-number duration in the data must not throw in .toFixed and
  // abort the card loop or the modal.
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 1) return `${(seconds * 1000).toFixed(0)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  return `${(seconds / 60).toFixed(1)}m`;
}

// A count from a data file, or 0 when it is not a finite non-negative number
// (a corrupt or hostile value must not reach markup).
export function toCount(value) {
  const n = Number(value);
  return Number.isFinite(n) && n >= 0 ? Math.floor(n) : 0;
}
