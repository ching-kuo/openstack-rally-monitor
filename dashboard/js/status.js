/**
 * Pure classification helpers (run/service/health/cleanup status) and the
 * service catalog (icons, descriptions, display order). No DOM
 * access, so the node tests import them directly.
 */
import { toCount } from "./format.js";

// Status values that may become CSS class names or chip text. Anything else in
// the data files (a corrupt or hostile value) renders as "unknown" instead of
// being interpolated into markup.
export const SERVICE_STATUSES = ["passed", "failed", "skipped", "pending"];
export const HEALTH_STATUSES = ["up", "degraded", "down", "unknown"];

export function knownStatus(value, allowed) {
  return allowed.includes(value) ? value : "unknown";
}

// Matches the compact rally run timestamp (also the run-directory name). Gates
// the "Full Rally report" link (seed/placeholder timestamps like
// "waiting_for_first_run" have no run directory) and the #run= permalink.
// KEEP IN SYNC with serve.py RUN_REPORT_RE and render_report.py _RUN_TS_RE.
export const RUN_TIMESTAMP_RE = /^\d{8}T\d{6}Z$/;

// The run timestamp named by a "#run=<ts>" location hash, or null when the hash
// is absent or does not name a real run timestamp.
export function parseRunHash(hash) {
  const m = /^#run=(.*)$/.exec(hash || "");
  return m && RUN_TIMESTAMP_RE.test(m[1]) ? m[1] : null;
}

export const SERVICE_ICONS = {
  keystone: "🔐",
  nova: "🖥️",
  neutron: "🌐",
  glance: "🖼️",
  cinder: "💾",
  swift: "📦",
};

export const SERVICE_DESCRIPTIONS = {
  keystone: "Identity & Authentication",
  nova: "Compute (VMs)",
  neutron: "Networking",
  glance: "Image Service",
  cinder: "Block Storage",
  swift: "Object Storage",
};

// Visual ordering for the standard six services; any service not listed here
// (e.g. a cloud running Octavia) is appended in first-seen order. Used to keep
// chart series/colors stable across refreshes while staying data-driven.
const PREFERRED_SERVICE_ORDER = Object.keys(SERVICE_ICONS);

// Build a stable, ordered union of service names appearing across `items`
// (each item exposes a `.services` object). The preferred six come first in
// their canonical order; unknown services follow in the order first observed.
export function orderedServiceUnion(items) {
  const seen = new Set();
  for (const item of items) {
    for (const name of Object.keys((item && item.services) || {})) {
      seen.add(name);
    }
  }
  const ordered = PREFERRED_SERVICE_ORDER.filter((name) => seen.has(name));
  for (const name of seen) {
    if (!ordered.includes(name)) ordered.push(name);
  }
  return ordered;
}

// KEEP IN SYNC with scripts/run_lib.sh::record_smoke_result,
// scripts/announce.sh::ALL_GREEN_PREDICATE, and
// exporter/rally_exporter.py::update_metrics (testdata/run_status_cases.json).
export function getRunStatus(runData) {
  const services = (runData && runData.services) || {};
  // A run with a top-level error (e.g. deployment_setup_failed) or with no
  // service data at all is a failed run, not "All Healthy". The empty-services
  // failure summary {timestamp, services:{}, error:...} would otherwise pass
  // a vacuous check and render green.
  if ((runData && runData.error) || Object.keys(services).length === 0) return "failed";
  const statuses = Object.values(services).map((s) => s && s.status);
  if (statuses.some((s) => s === "failed" || !SERVICE_STATUSES.includes(s))) return "failed";
  if (statuses.includes("pending")) return "pending";
  // "skipped" is neutral when at least one service passed; an all-skipped run
  // is not green and classifies as "failed".
  return statuses.includes("passed") ? "passed" : "failed";
}

// Names of the services whose entry in `services` has the given status.
export function servicesWithStatus(services, status) {
  return Object.entries(services || {})
    .filter(([, v]) => v && v.status === status)
    .map(([k]) => k);
}

// Service-level failure reason (build_summary sets error/error_detail when a
// service's Rally task never started), or "" when there is none.
export function serviceErrorText(svc) {
  if (!svc || !svc.error) return "";
  const label = svc.error === "task_failed_to_start" ? "Task failed to start" : "Error";
  return `${label}: ${svc.error_detail || svc.error}`;
}

function positive(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : null;
}

// The overdue rules use the thresholds init_state.sh writes into
// dashboard_config.json. An unknown threshold (no config yet) is never overdue.

// A run is overdue like RallyStaleResults: measured from its END (start +
// run_duration_seconds; an aborted run without a duration ends at its start),
// past 2 x the schedule spacing + the run's duration. KEEP IN SYNC with
// prometheus/rally_alerts.yml RallyStaleResults and
// exporter/metrics_common.py::cron_spacing_seconds.
export function runOverdue(startMs, durationSeconds, nowMs, scheduleSeconds) {
  const schedule = positive(scheduleSeconds);
  if (!Number.isFinite(startMs) || schedule === null) return false;
  const duration = Math.max(0, Number(durationSeconds) || 0);
  return nowMs - (startMs + duration * 1000) > (2 * schedule + duration) * 1000;
}

// A health check is stale past the exporter's rally_api_stale_after_seconds.
// KEEP IN SYNC with exporter/health_metrics.py::stale_after_seconds.
export function healthStale(checkMs, nowMs, staleAfterSeconds) {
  const staleAfter = positive(staleAfterSeconds);
  if (!Number.isFinite(checkMs) || staleAfter === null) return false;
  return nowMs - checkMs > staleAfter * 1000;
}

// Total c_rally_* context resources left behind (cleanup_metrics.json
// context_details); the badge and the cleanup note both count it.
export function contextLeftovers(cleanup) {
  return Object.values((cleanup && cleanup.context_details) || {}).reduce(
    (sum, v) => sum + toCount(v),
    0,
  );
}

// Cleanup section badge from cleanup_metrics.json, consistent with the
// exporter's gauges: scenario orphans (rally_cleanup_failed) win, then a blind
// OpenStack or RGW scan (rally_cleanup_scan_ok / rally_rgw_scan_ok 0; a failed
// listing reads as 0 orphans, so it must never show "OK"), then RGW orphans
// (RallyRgwOrphanedUsers), then context leftovers (rally_context_cleanup_warning).
export function cleanupBadge(cleanup) {
  const c = cleanup || {};
  if (toCount(c.cleanup_failed) === 1) return { text: "ISSUES", cls: "error" };
  if (c.openstack_scan_status === "error" || c.rgw_scan_status === "error") {
    return { text: "SCAN ERROR", cls: "error" };
  }
  if (toCount(c.rgw_orphaned_users) > 0) return { text: "RGW ORPHANS", cls: "warn" };
  if (toCount(c.context_cleanup_warning) === 1 || contextLeftovers(c) > 0) {
    return { text: "CONTEXT LEFTOVERS", cls: "warn" };
  }
  return { text: "OK", cls: "ok" };
}
