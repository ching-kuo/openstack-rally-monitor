/**
 * Data fetching and the dashboard's shared in-memory state.
 */

// Last good copy of each data file plus the view state. Render modules read
// it; only main.js (refresh) and timeline.js (run pinning) write it.
export const state = {
  history: { runs: [] },
  results: null,
  health: null,
  healthHistory: null,
  // Timestamp of the pinned historical run, or null for the live view (newly
  // completed runs then appear automatically). A timestamp, not an index, so
  // the pin does not drift onto another run when old runs are pruned.
  pinnedRunTs: null,
  // Whether history.json has loaded at least once: until then a pinned run
  // cannot be resolved (and must not be dropped).
  historyLoaded: false,
  // dashboard_config.json (init_state.sh): thresholds for the overdue hints.
  config: null,
  // Last good announcement-state.json and run_state.json (null when absent).
  announcements: null,
  runState: null,
  // When results.json last loaded (ms).
  resultsLoadedMs: null,
};

// Deadline for one data file, request and body together. A request that never
// settles would otherwise hold the refresh (and every later one) forever.
const FETCH_TIMEOUT_MS = 30_000;

// Fetch a JSON file relative to the page (never "/x.json": absolute paths
// break behind a sub-path reverse proxy). Returns {data, reachable}: `data` is
// null on any failure; `reachable` is false only when the server could not be
// reached, failed (network error, 5xx) or missed the deadline. A 404 (a file
// not written yet, or a dangling symlink) or a corrupt file is a reachable
// server without data.
async function fetchJson(url, options, timeoutMs) {
  const controller = new AbortController();
  // Rejects at the deadline even if the fetch implementation ignores the
  // abort signal; raced against both the request and the body read.
  const deadline = new Promise((_, reject) => {
    controller.signal.addEventListener("abort", () => reject(new Error("timed out")), {
      once: true,
    });
  });
  deadline.catch(() => {});
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    let res;
    try {
      res = await Promise.race([fetch(url, { ...options, signal: controller.signal }), deadline]);
    } catch (err) {
      console.error(`Failed to fetch ${url}:`, err);
      return { data: null, reachable: false };
    }
    // `missing`: a real 404 (the file does not exist), as opposed to a failure.
    if (!res.ok) return { data: null, reachable: res.status < 500, missing: res.status === 404 };
    try {
      return { data: await Promise.race([res.json(), deadline]), reachable: true };
    } catch (err) {
      console.error(`Failed to read ${url}:`, err);
      return { data: null, reachable: !controller.signal.aborted };
    }
  } finally {
    clearTimeout(timer);
  }
}

// Everything one refresh needs, fetched in parallel. announcement-state.json
// and run_state.json are no-store: both change outside the run cycle and are
// tiny. A missing run_state.json (404 before the first run) reads as idle.
export async function fetchAll(timeoutMs = FETCH_TIMEOUT_MS) {
  const [results, history, health, healthHistory, announcements, runState, config] =
    await Promise.all([
      fetchJson("results.json", {}, timeoutMs),
      fetchJson("history.json", {}, timeoutMs),
      fetchJson("health.json", {}, timeoutMs),
      fetchJson("health_history.json", {}, timeoutMs),
      fetchJson("announcement-state.json", { cache: "no-store" }, timeoutMs),
      fetchJson("run_state.json", { cache: "no-store" }, timeoutMs),
      fetchJson("dashboard_config.json", {}, timeoutMs),
    ]);
  return {
    results: results.data,
    history: history.data,
    health: health.data,
    healthHistory: healthHistory.data,
    announcements: announcements.data,
    runState: runState.data,
    // A 404 clears the banner / run chip; any other failure keeps the last copy.
    announcementsMissing: Boolean(announcements.missing),
    runStateMissing: Boolean(runState.missing),
    config: config.data,
    // results.json is the one file every install has; it decides whether the
    // page shows the "can't reach server" chip.
    reachable: results.reachable,
  };
}
