/*
 * report.js -- client renderer for the self-contained Rally report.
 *
 * render_report.py embeds a normalized payload in a non-executable
 * <script type="application/json" id="rally-report-data"> block (exempt from
 * the strict CSP script-src) and pulls this file plus vendored Chart.js by
 * same-origin relative path. We render the whole report here: a WORKING
 * collapsible left nav (our own click handlers -- the Rally/AngularJS report's
 * ng-show/ng-hide nav broke when the CDN scripts did not load), an overview
 * table, per-scenario detail with an atomic-action duration chart, the input
 * config, and error tracebacks.
 *
 * The pure helpers below (groupScenarios / scenarioStatus / formatSeconds /
 * atomicBars) are self-contained top-level functions so the Node contract tests
 * can extract and evaluate them in isolation, mirroring app.js.
 */

// --- Pure helpers (Node-testable) -------------------------------------------

function scenarioStatus(scenario) {
  // A scenario passes only with at least one iteration, no iteration errors,
  // and every SLA criterion satisfied -- mirrors the all-green intent used for
  // service status in run_tests.sh::build_summary.
  if (!scenario || !scenario.iterations) return "failed";
  if (scenario.errors && scenario.errors > 0) return "failed";
  if (scenario.sla_passed === false) return "failed";
  return "passed";
}

function groupScenarios(scenarios) {
  // Group scenarios by their class (e.g. "NovaServers"), preserving first-seen
  // class order and scenario order within a class. Returns
  // [{cls, scenarios:[...]}], matching the Rally report's grouped nav.
  const order = [];
  const byClass = {};
  (scenarios || []).forEach((s, idx) => {
    const cls = (s && s.cls) || "unknown";
    if (!byClass[cls]) {
      byClass[cls] = [];
      order.push(cls);
    }
    byClass[cls].push({ ...s, _idx: idx });
  });
  return order.map((cls) => ({ cls, scenarios: byClass[cls] }));
}

function formatSeconds(value) {
  const n = Number(value);
  if (!isFinite(n)) return "0.000 s";
  return `${n.toFixed(3)} s`;
}

function atomicBars(atomics) {
  // Chart-ready labels + average durations for the atomic-action breakdown.
  const labels = (atomics || []).map((a) => a.name);
  const data = (atomics || []).map((a) => Number(a.avg) || 0);
  return { labels, data };
}

// --- DOM rendering ----------------------------------------------------------

function cssVar(name, fallback) {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  } catch (_e) {
    return fallback;
  }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function readPayload() {
  const block = document.getElementById("rally-report-data");
  if (!block) return { service: "", timestamp: "", scenarios: [] };
  try {
    return JSON.parse(block.textContent);
  } catch (_e) {
    return { service: "", timestamp: "", scenarios: [] };
  }
}

let activeChart = null;

function buildNav(navEl, payload, select) {
  navEl.textContent = "";

  const fixed = el("div", "report-nav-fixed");
  fixed.appendChild(navButton("Task overview", () => select({ view: "overview" })));
  fixed.appendChild(navButton("Input file", () => select({ view: "input" })));
  navEl.appendChild(fixed);

  groupScenarios(payload.scenarios).forEach((group) => {
    const groupEl = el("div", "report-nav-group");
    const header = el("button", "report-nav-grouphead");
    header.setAttribute("type", "button");
    header.setAttribute("aria-expanded", "true");
    const caret = el("span", "report-nav-caret", "▾"); // down triangle
    header.appendChild(caret);
    header.appendChild(el("span", "report-nav-grouptitle", group.cls));

    const list = el("div", "report-nav-list");
    group.scenarios.forEach((s) => {
      const btn = navButton(s.method, () => select({ view: "scenario", idx: s._idx }));
      btn.classList.add("report-nav-scenario");
      btn.dataset.idx = String(s._idx);
      const dot = el("span", `report-nav-dot status-${scenarioStatus(s)}`);
      btn.insertBefore(dot, btn.firstChild);
      list.appendChild(btn);
    });

    header.addEventListener("click", () => {
      const collapsed = groupEl.classList.toggle("collapsed");
      header.setAttribute("aria-expanded", String(!collapsed));
    });

    groupEl.appendChild(header);
    groupEl.appendChild(list);
    navEl.appendChild(groupEl);
  });
}

function navButton(label, onClick) {
  const btn = el("button", "report-nav-item");
  btn.setAttribute("type", "button");
  btn.appendChild(el("span", "report-nav-label", label));
  btn.addEventListener("click", onClick);
  return btn;
}

function highlightNav(navEl, sel) {
  navEl.querySelectorAll(".report-nav-item").forEach((b) => b.classList.remove("active"));
  const items = navEl.querySelectorAll(".report-nav-fixed .report-nav-item");
  if (sel.view === "overview" && items[0]) items[0].classList.add("active");
  if (sel.view === "input" && items[1]) items[1].classList.add("active");
  if (sel.view === "scenario") {
    const btn = navEl.querySelector(`.report-nav-scenario[data-idx="${sel.idx}"]`);
    if (btn) btn.classList.add("active");
  }
}

function destroyChart() {
  if (activeChart) {
    activeChart.destroy();
    activeChart = null;
  }
}

function renderOverview(mainEl, payload) {
  mainEl.appendChild(el("h1", "report-h1", "Task overview"));
  if (!payload.scenarios.length) {
    mainEl.appendChild(el("p", "report-empty", "No scenario results were recorded for this run."));
    return;
  }
  const table = el("table", "report-table");
  const head = el("tr");
  ["Scenario", "Load (s)", "Full (s)", "Iterations", "Runner", "Errors", "Hooks", "SLA"].forEach((h) => {
    head.appendChild(el("th", null, h));
  });
  const thead = el("thead");
  thead.appendChild(head);
  table.appendChild(thead);

  const tbody = el("tbody");
  payload.scenarios.forEach((s) => {
    const tr = el("tr");
    tr.appendChild(el("td", "report-cell-name", s.name));
    tr.appendChild(el("td", null, (Number(s.load_duration) || 0).toFixed(3)));
    tr.appendChild(el("td", null, (Number(s.full_duration) || 0).toFixed(3)));
    tr.appendChild(el("td", null, String(s.iterations || 0)));
    tr.appendChild(el("td", null, s.runner || "—"));
    const errTd = el("td", s.errors ? "report-err" : null, String(s.errors || 0));
    tr.appendChild(errTd);
    tr.appendChild(el("td", null, String(s.hooks || 0)));
    const slaTd = el("td");
    slaTd.appendChild(statusPill(s.sla_passed ? "passed" : "failed", s.sla_passed ? "Pass" : "Fail"));
    tr.appendChild(slaTd);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  mainEl.appendChild(table);
}

function statusPill(status, label) {
  return el("span", `report-pill status-${status}`, label || status);
}

function renderInput(mainEl, payload) {
  mainEl.appendChild(el("h1", "report-h1", "Input file"));
  const config = payload.scenarios.map((s) => ({ [s.name]: s.config }));
  const pre = el("pre", "report-json");
  pre.textContent = JSON.stringify(config, null, 2);
  mainEl.appendChild(pre);
}

function renderScenario(mainEl, scenario) {
  const head = el("div", "report-scenario-head");
  head.appendChild(el("h1", "report-h1", scenario.name));
  const status = scenarioStatus(scenario);
  head.appendChild(statusPill(status, status === "passed" ? "Passed" : "Failed"));
  mainEl.appendChild(head);
  if (scenario.description) mainEl.appendChild(el("p", "report-desc", scenario.description));

  // Summary stat tiles.
  const stats = el("div", "report-stats");
  const ds = scenario.duration_stats || {};
  [
    ["Load duration", formatSeconds(scenario.load_duration)],
    ["Full duration", formatSeconds(scenario.full_duration)],
    ["Iterations", String(scenario.iterations || 0)],
    ["Runner", scenario.runner || "—"],
    ["Avg / iter", formatSeconds(ds.avg)],
    ["p95 / iter", formatSeconds(ds.p95)],
    ["Errors", String(scenario.errors || 0)],
  ].forEach(([label, value]) => {
    const tile = el("div", "report-stat");
    tile.appendChild(el("span", "report-stat-label", label));
    tile.appendChild(el("span", "report-stat-value", value));
    stats.appendChild(tile);
  });
  mainEl.appendChild(stats);

  // Atomic-action duration chart.
  if (scenario.atomics && scenario.atomics.length) {
    mainEl.appendChild(el("h2", "report-h2", "Atomic action durations (avg)"));
    const wrap = el("div", "report-chart-wrap");
    const canvas = el("canvas");
    canvas.id = "atomicChart";
    wrap.appendChild(canvas);
    mainEl.appendChild(wrap);
    drawAtomicChart(canvas, scenario.atomics);
  }

  // SLA table.
  if (scenario.sla && scenario.sla.length) {
    mainEl.appendChild(el("h2", "report-h2", "SLA"));
    const table = el("table", "report-table");
    const thead = el("thead");
    const hr = el("tr");
    ["Criterion", "Result", "Detail"].forEach((h) => hr.appendChild(el("th", null, h)));
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = el("tbody");
    scenario.sla.forEach((s) => {
      const tr = el("tr");
      tr.appendChild(el("td", null, s.criterion || "—"));
      const r = el("td");
      r.appendChild(statusPill(s.success ? "passed" : "failed", s.success ? "Pass" : "Fail"));
      tr.appendChild(r);
      tr.appendChild(el("td", "report-cell-detail", s.detail || ""));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    mainEl.appendChild(table);
  }

  // Errors with collapsible tracebacks.
  if (scenario.error_samples && scenario.error_samples.length) {
    mainEl.appendChild(el("h2", "report-h2", "Errors"));
    scenario.error_samples.forEach((err) => {
      const details = el("details", "report-error");
      const summary = el("summary");
      summary.appendChild(el("span", "report-error-type", err.type || "Error"));
      if (err.count > 1) summary.appendChild(el("span", "report-error-count", `×${err.count}`));
      summary.appendChild(el("span", "report-error-msg", err.message || ""));
      details.appendChild(summary);
      if (err.traceback) {
        const pre = el("pre", "report-trace");
        pre.textContent = err.traceback;
        details.appendChild(pre);
      }
      mainEl.appendChild(details);
    });
  }

  // Per-scenario input config.
  mainEl.appendChild(el("h2", "report-h2", "Input file"));
  const pre = el("pre", "report-json");
  pre.textContent = JSON.stringify(scenario.config || {}, null, 2);
  mainEl.appendChild(pre);
}

function drawAtomicChart(canvas, atomics) {
  if (typeof Chart === "undefined") return;
  const { labels, data } = atomicBars(atomics);
  destroyChart();
  activeChart = new Chart(canvas.getContext("2d"), {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          label: "Avg duration (s)",
          data,
          backgroundColor: cssVar("--chart-series-1", "#6366f1"),
          borderRadius: 4,
        },
      ],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: {
          ticks: { color: cssVar("--chart-axis-tick", "#64748b") },
          grid: { color: cssVar("--chart-grid", "rgba(255,255,255,0.04)") },
        },
        y: {
          ticks: { color: cssVar("--chart-axis-label", "#94a3b8") },
          grid: { display: false },
        },
      },
    },
  });
}

function initReport() {
  const payload = readPayload();
  const navEl = document.getElementById("reportNav");
  const mainEl = document.getElementById("reportMain");
  if (!navEl || !mainEl) return;

  function select(sel) {
    destroyChart();
    mainEl.textContent = "";
    if (sel.view === "input") {
      renderInput(mainEl, payload);
    } else if (sel.view === "scenario" && payload.scenarios[sel.idx]) {
      renderScenario(mainEl, payload.scenarios[sel.idx]);
    } else {
      sel = { view: "overview" };
      renderOverview(mainEl, payload);
    }
    highlightNav(navEl, sel);
    mainEl.scrollTop = 0;
  }

  buildNav(navEl, payload, select);
  pruneFailedThemeLinks();
  select({ view: "overview" });
}

function pruneFailedThemeLinks() {
  // A dangling custom-theme <link> (no custom theme installed) 404s; the browser
  // ignores it, but we drop the node to keep the DOM clean -- same approach as
  // app.js, and it avoids an inline onerror handler so the strict CSP holds.
  document.querySelectorAll("link.custom-theme-css").forEach((link) => {
    if (link.sheet === null) {
      // Sheet may still be loading; only prune once clearly failed.
      link.addEventListener("error", () => link.remove());
    }
  });
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initReport);
  } else {
    initReport();
  }
}
