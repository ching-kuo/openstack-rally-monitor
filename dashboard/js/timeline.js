/**
 * Run status timeline and historical run pinning. A pinned run is identified
 * by its timestamp and mirrored in the "#run=<ts>" permalink (the full
 * report's back link uses it too).
 */
import { state } from "./data.js";
import { formatTimestamp, plural } from "./format.js";
import {
  focusedKey,
  makeActivatable,
  placeRovingFocus,
  setRovingTabStop,
  setSpanLabel,
  tooltipEdgeClass,
  tooltipHtml,
} from "./dom.js";
import { renderServiceCards } from "./services.js";
import { RUN_TIMESTAMP_RE, getRunStatus, parseRunHash, servicesWithStatus } from "./status.js";

// Plain-text tooltip/aria detail for one run.
function runCellDetail(run, status) {
  if (run.error) {
    // Deployment-failure shape: surface the error verbatim instead of the
    // misleading "No service data" (which read as benign).
    return `Error: ${run.error}`;
  }
  const failed = servicesWithStatus(run.services, "failed");
  if (status === "failed" && failed.length > 0) return `Failed: ${failed.join(", ")}`;
  if (status === "failed") return "No service data";
  if (status === "pending") return "Some services pending or skipped";
  return "All services passed";
}

export function renderTimeline() {
  const container = document.getElementById("timeline");
  const countBadge = document.getElementById("timelineCount");

  const runs = state.history.runs || [];
  countBadge.textContent = plural(runs.length, "run");
  setSpanLabel("timelineSpan", runs);

  if (runs.length === 0) {
    container.innerHTML = '<div class="timeline-loading">No historical data yet</div>';
    return;
  }

  const hadFocus = focusedKey(container);
  container.replaceChildren();
  let selectedCell = null;
  const cells = runs.map((run, i) => {
    const status = getRunStatus(run);
    const when = formatTimestamp(run.timestamp);
    const detail = runCellDetail(run, status);
    const cell = document.createElement("div");
    cell.className = `timeline-cell ${status} ${tooltipEdgeClass(i, runs.length)}`.trim();
    cell.dataset.key = String(run.timestamp);
    if (state.pinnedRunTs !== null && run.timestamp === state.pinnedRunTs) {
      cell.classList.add("selected");
      cell.setAttribute("aria-current", "true");
      selectedCell = cell;
    }
    cell.innerHTML = tooltipHtml(when, detail);
    // Click or Enter/Space pins this run into the Core Services view.
    makeActivatable(cell, `Run ${when}: ${detail}. Show this run`, () => selectRun(run.timestamp));
    container.appendChild(cell);
    return cell;
  });
  placeRovingFocus(cells, selectedCell, hadFocus);
}

// main.js registers a function that runs a fresh refresh (after any in-flight
// one); a #run= permalink to a run newer than the cached history uses it.
let refreshHistory = null;
// The permalink timestamp whose refresh is in flight; its pin is kept until
// that refresh has settled.
let checkingRunTs = null;

export function setRefreshHook(fn) {
  refreshHistory = fn;
}

function pinnedRun() {
  if (state.pinnedRunTs === null) return null;
  return (state.history.runs || []).find((r) => r.timestamp === state.pinnedRunTs) || null;
}

function setHash(ts) {
  // replaceState: no history entry per click and no hashchange event.
  const base = window.location.pathname + window.location.search;
  window.history.replaceState(null, "", ts && RUN_TIMESTAMP_RE.test(ts) ? `${base}#run=${ts}` : base);
}

function showNotice(text) {
  const notice = document.getElementById("runNotice");
  if (!notice) return;
  notice.textContent = text;
  notice.hidden = !text;
}

// Render the cards, banner and title for the pinned run, or the live view.
// A pinned run that is no longer in a successfully loaded history (pruned)
// falls back to latest. Until history.json has loaded, or while a permalink's
// history refresh is in flight, the pin and the #run= hash are kept and the
// live view shows meanwhile: a failed or slow fetch must not erase a permalink.
export function renderSelection() {
  const runs = state.history.runs || [];
  const pinned = pinnedRun();
  if (state.pinnedRunTs !== null && !pinned) {
    if (state.historyLoaded && checkingRunTs !== state.pinnedRunTs) {
      showNotice(`Run ${formatTimestamp(state.pinnedRunTs)} is not in the retained history; showing the latest run.`);
      state.pinnedRunTs = null;
      setHash(null);
    } else {
      showNotice("");
    }
  }

  const banner = document.getElementById("historicalBanner");
  const isHistorical = pinned !== null && pinned !== runs[runs.length - 1];
  banner.hidden = !isHistorical;
  if (isHistorical) {
    document.getElementById("historicalBannerText").textContent =
      `Viewing run: ${formatTimestamp(pinned.timestamp)}`;
  }
  document.getElementById("servicesSectionTitle").textContent = isHistorical
    ? "Core Services (Historical)"
    : "Core Services";

  if (pinned) {
    renderServiceCards(pinned, state.history, state.health);
  } else if (state.results) {
    renderServiceCards(state.results.summary, state.history, state.health);
  }
}

// Move the "selected" mark to the pinned run's cell without rebuilding the
// row (same DOM and Tab stop renderTimeline would produce; the row only needs a
// rebuild when the data changes).
function markSelected() {
  const container = document.getElementById("timeline");
  const cells = Array.from(container.querySelectorAll(".timeline-cell"));
  let selectedCell = null;
  for (const cell of cells) {
    const selected = state.pinnedRunTs !== null && cell.dataset.key === String(state.pinnedRunTs);
    cell.classList.toggle("selected", selected);
    if (selected) {
      cell.setAttribute("aria-current", "true");
      selectedCell = cell;
    } else {
      cell.removeAttribute("aria-current");
    }
  }
  if (cells.length === 0) return;
  const key = focusedKey(container);
  const focused = key === null ? null : cells.find((c) => c.dataset.key === key);
  setRovingTabStop(cells, focused || selectedCell);
}

export function selectRun(ts) {
  state.pinnedRunTs = ts;
  showNotice("");
  setHash(ts);
  markSelected();
  renderSelection();
}

export function backToLatest() {
  selectRun(null);
}

// Apply "#run=<ts>" from the address bar (initial load and hashchange). An
// empty hash or a bare "#" returns to the latest run; any other fragment that
// is not a valid run timestamp is ignored. A valid one missing from the cached
// history may be a run that finished since the last refresh, so history is
// refreshed once first; only if it is still missing does the page fall back
// to the latest run with a notice.
export function applyLocationHash() {
  const hash = window.location.hash;
  if (hash === "" || hash === "#") {
    if (state.pinnedRunTs !== null) backToLatest();
    return;
  }
  const ts = parseRunHash(hash);
  if (ts === null) return;
  state.pinnedRunTs = ts;
  showNotice("");
  markSelected();
  if (!pinnedRun() && state.historyLoaded && refreshHistory) {
    checkingRunTs = ts;
    renderSelection();
    Promise.resolve(refreshHistory())
      .catch(() => {})
      .finally(() => {
        if (checkingRunTs !== ts) return;
        checkingRunTs = null;
        markSelected();
        renderSelection();
      });
    return;
  }
  renderSelection();
}
