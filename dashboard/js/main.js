/**
 * Rally OpenStack Monitor - dashboard entry point (loaded by index.html as
 * <script type="module">). Fetches the data files, renders every section, and
 * refreshes every 5 minutes while the page is visible.
 */
import { renderAnnouncements } from "./announcements.js";
import { renderCharts, renderHealthChart } from "./charts.js";
import { renderCleanup } from "./cleanup.js";
import { fetchAll, state } from "./data.js";
import { initRovingFocus } from "./dom.js";
import { formatClock } from "./format.js";
import { renderConnection, renderFreshness, renderRunState, showLoadError, updateHeader } from "./header.js";
import { renderHealthTimeline, renderUptimeBadge } from "./health.js";
import { initModal } from "./modal.js";
import { parseRunHash } from "./status.js";
import { pruneFailedThemeLinks, resolveThemeAssets } from "./theme.js";
import {
  applyLocationHash,
  backToLatest,
  renderSelection,
  renderTimeline,
  setRefreshHook,
} from "./timeline.js";

const REFRESH_INTERVAL = 300_000; // 5 minutes
// Relative ages ("3m ago") are re-rendered from cached data this often.
const AGE_TICK_INTERVAL = 60_000;

let lastRefreshMs = 0;
let inFlight = null;

function renderAll(data) {
  renderAnnouncements(state.announcements);
  renderRunState(state.runState);

  renderTimeline();
  renderCharts(state.history);
  renderHealthTimeline(state.healthHistory);
  renderHealthChart(state.healthHistory);

  // Smoke-test uptime rides on results.json, API uptime on health_history.json.
  renderUptimeBadge("smokeUptimeBadge", state.results && state.results.uptime);
  renderUptimeBadge("apiUptimeBadge", state.healthHistory && state.healthHistory.uptime);

  if (state.results) {
    updateHeader(state.results.summary, state.health);
    renderCleanup(state.results.cleanup);
  } else {
    showLoadError();
  }
  // Service cards for the pinned run or the live view (keeps live health
  // indicators fresh either way).
  renderSelection();
  renderFreshness(state.results, state.health, state.config, Date.now());
  renderConnection(data.reachable, data.results !== null, state.resultsLoadedMs);
  // The time of the last refresh that actually loaded data, not of the attempt.
  if (state.resultsLoadedMs !== null) {
    document.getElementById("lastUpdated").textContent =
      `Updated ${formatClock(state.resultsLoadedMs)}`;
  }
}

async function doRefresh() {
  const data = await fetchAll();
  const firstLoad = lastRefreshMs === 0;
  lastRefreshMs = Date.now();

  // A failed fetch keeps the last good copy (the connection chip says so).
  if (data.history) {
    state.history = { runs: Array.isArray(data.history.runs) ? data.history.runs : [] };
    state.historyLoaded = true;
  }
  if (data.config) state.config = data.config;
  if (data.results) {
    state.results = data.results;
    state.resultsLoadedMs = lastRefreshMs;
  }
  if (data.health) state.health = data.health;
  // One failed fetch must not hide an active incident banner or the run chip:
  // only valid data or a real 404 (the file is gone) replaces the last copy.
  if (data.announcements || data.announcementsMissing) state.announcements = data.announcements;
  if (data.runState || data.runStateMissing) state.runState = data.runState;
  if (data.healthHistory) state.healthHistory = data.healthHistory;
  // The #run=<ts> permalink is applied once the history it refers to is known.
  if (firstLoad) state.pinnedRunTs = parseRunHash(window.location.hash);

  renderAll(data);
}

// One refresh at a time: the timer, the button and a tab becoming visible can
// coincide. The button is marked aria-disabled rather than disabled: disabling
// a focused button drops keyboard focus to <body>; the inFlight guard is what
// ignores the extra clicks.
function refresh() {
  if (!inFlight) {
    const button = document.getElementById("refreshBtn");
    button.setAttribute("aria-disabled", "true");
    inFlight = doRefresh()
      .catch((err) => console.error("Refresh failed:", err))
      .finally(() => {
        inFlight = null;
        button.setAttribute("aria-disabled", "false");
      });
  }
  return inFlight;
}

// A refresh that starts after any in-flight one, so its data is newer than the
// moment of the call (timeline.js uses it to check a #run= permalink).
function refreshAfterCurrent() {
  return inFlight ? inFlight.then(refresh) : refresh();
}

function wireEvents() {
  initModal();
  initRovingFocus(document.getElementById("timeline"), ".timeline-cell");
  initRovingFocus(document.getElementById("healthTimeline"), ".htl-cell");
  document.getElementById("backToLatestBtn").addEventListener("click", backToLatest);
  document.getElementById("refreshBtn").addEventListener("click", refresh);
  window.addEventListener("hashchange", applyLocationHash);
  setRefreshHook(refreshAfterCurrent);

  // No polling while the tab is hidden; catch up as soon as it is visible
  // again if the data is older than one refresh interval.
  setInterval(() => {
    if (!document.hidden) refresh();
  }, REFRESH_INTERVAL);
  setInterval(() => {
    if (!document.hidden) renderFreshness(state.results, state.health, state.config, Date.now());
  }, AGE_TICK_INTERVAL);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && Date.now() - lastRefreshMs >= REFRESH_INTERVAL) refresh();
  });
}

wireEvents();
pruneFailedThemeLinks();
Promise.all([resolveThemeAssets(), refresh()]);
