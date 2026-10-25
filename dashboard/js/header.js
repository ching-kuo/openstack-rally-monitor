/**
 * Header: overall health badge, data freshness, the run-in-progress chip and
 * the "can't reach server" chip.
 */
import { formatAge, formatClock, formatTimestamp, parseTimestampMs, summarizeNames } from "./format.js";
import { getRunStatus, healthStale, runOverdue, servicesWithStatus } from "./status.js";

// How many service names the header badge lists before "+N".
const MAX_NAMED_SERVICES = 3;

function withNames(label, names) {
  return names.length ? `${label}: ${summarizeNames(names, MAX_NAMED_SERVICES)}` : label;
}

// The badge state for a run summary and health snapshot. API failure takes
// precedence over the Rally run result; degraded (reachable but slow) loses to
// both an outage and a Rally failure.
export function headerBadge(summary, health) {
  const rallyStatus = getRunStatus(summary);
  if (health && health.overall === "down") {
    return {
      cls: "unhealthy",
      label: withNames("API Issues Detected", servicesWithStatus(health.services, "down")),
    };
  }
  if (rallyStatus === "failed") {
    return {
      cls: "unhealthy",
      label: withNames("Issues Detected", servicesWithStatus(summary && summary.services, "failed")),
    };
  }
  if (health && health.overall === "degraded") {
    return {
      cls: "degraded",
      label: withNames("API Degraded", servicesWithStatus(health.services, "degraded")),
    };
  }
  if (rallyStatus === "passed") return { cls: "healthy", label: "All Healthy" };
  return { cls: "", label: "Pending" };
}

function setBadge(cls, label) {
  const badge = document.getElementById("healthBadge");
  badge.className = `health-badge ${cls}`;
  badge.querySelector(".health-text").textContent = label;
}

export function updateHeader(summary, health) {
  const { cls, label } = headerBadge(summary, health);
  setBadge(cls, label);
}

// results.json has never loaded: say so instead of "Loading..." forever.
export function showLoadError() {
  setBadge("unhealthy", "Data unavailable");
  const lastRun = document.getElementById("lastRun");
  lastRun.textContent = "Could not load results.json";
  lastRun.classList.add("stale");
}

// One "<prefix> 12m ago" line; amber with an "(overdue)" suffix (so color is not
// the only signal) when `isOverdue(ms)` says so.
function renderAge(el, prefix, ts, isOverdue, nowMs) {
  if (!el) return;
  const ms = parseTimestampMs(ts);
  if (!Number.isFinite(ms)) {
    el.textContent = `${prefix}: ${formatTimestamp(ts)}`;
    el.classList.remove("stale");
    el.removeAttribute("title");
    return;
  }
  const overdue = isOverdue(ms);
  el.textContent = `${prefix} ${formatAge(ms, nowMs)}${overdue ? " (overdue)" : ""}`;
  el.title = formatTimestamp(ts);
  el.classList.toggle("stale", overdue);
}

// Relative ages of the latest run (from its start) and health check. Called on
// every refresh and once a minute from cached data, so "3m ago" does not
// freeze. `config` is dashboard_config.json (init_state.sh); the overdue rules
// match the exporter and alerts (status.js runOverdue / healthStale), and
// without a config nothing is marked overdue.
export function renderFreshness(results, health, config, nowMs) {
  const cfg = config || {};
  const summary = results && results.summary;
  if (summary) {
    renderAge(
      document.getElementById("lastRun"),
      "Last run",
      summary.timestamp,
      (ms) => runOverdue(ms, summary.run_duration_seconds, nowMs, cfg.schedule_interval_seconds),
      nowMs,
    );
  }
  const healthEl = document.getElementById("healthAge");
  // Only a real check time: the first-boot seed (init_state.sh) has
  // timestamp "waiting", which must not read as "Health checked: waiting".
  if (health && Number.isFinite(parseTimestampMs(health.timestamp))) {
    renderAge(
      healthEl,
      "Health checked",
      health.timestamp,
      (ms) => healthStale(ms, nowMs, cfg.health_stale_after_seconds),
      nowMs,
    );
    healthEl.hidden = false;
  } else if (healthEl) {
    healthEl.hidden = true;
  }
}

// Shown when the latest refresh could not load results.json; the page keeps
// rendering the last good copy, so say how old it is.
export function renderConnection(reachable, resultsOk, loadedMs) {
  const chip = document.getElementById("connectionChip");
  if (!chip) return;
  if (resultsOk) {
    chip.hidden = true;
    chip.textContent = "";
    return;
  }
  const problem = reachable ? "Can't load results.json" : "Can't reach server";
  chip.textContent = loadedMs
    ? `${problem}, showing data from ${formatClock(loadedMs)}`
    : problem;
  chip.hidden = false;
}

// A "running" state older than this is treated as idle. entrypoint.sh resets
// run_state.json to idle on every boot, so a stale "running" can only arise on
// a long-lived container whose run was SIGKILL'd without firing run_tests.sh's
// EXIT trap (e.g. OOM-kill). Without this guard the chip would pulse forever.
const RUN_STATE_STALE_MS = 24 * 60 * 60 * 1000; // 24h

// Shows a small pulsing header chip while a Rally run is in progress, e.g.
// "Test run in progress · started 12:04". Hidden when idle, missing, corrupt,
// or stale (see RUN_STATE_STALE_MS).
export function renderRunState(runState) {
  const chip = document.getElementById("runStateChip");
  if (!chip) return;

  // Parse once via the shared helper (it accepts both ISO 8601 and the compact
  // rally form). Unparseable started_at fails safe by NOT showing a forever-chip.
  const startedMs =
    runState && runState.state === "running" ? parseTimestampMs(runState.started_at) : NaN;
  const isRunning =
    Number.isFinite(startedMs) && Date.now() - startedMs < RUN_STATE_STALE_MS;

  chip.replaceChildren();
  if (!isRunning) {
    chip.hidden = true;
    return;
  }

  const dot = document.createElement("span");
  dot.className = "run-state-dot";
  const text = document.createElement("span");
  text.textContent = `Test run in progress · started ${formatClock(startedMs)}`;
  chip.append(dot, text);
  chip.hidden = false;
}
