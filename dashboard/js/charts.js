/**
 * Trend charts (run duration, API latency). Chart.js is the vendored classic
 * script loaded before the modules, so it is read from the global scope.
 */
import { formatChartLabel, localZoneName } from "./format.js";
import { orderedServiceUnion } from "./status.js";

// Health history retains up to UPTIME_WINDOW_DAYS of checks (for the uptime
// figure); cap what the chart plots so it stays readable and cheap.
// 672 = 7 days at 15-min intervals, the chart's historical span.
const HEALTH_CHART_MAX_POINTS = 672;

const charts = {};

function readChartTheme() {
  const cs = getComputedStyle(document.documentElement);
  const readToken = (name) => cs.getPropertyValue(name).trim();
  return {
    series: Array.from({ length: 7 }, (_, i) => readToken(`--chart-series-${i + 1}`)),
    axisLabel: readToken("--chart-axis-label"),
    axisTick: readToken("--chart-axis-tick"),
    grid: readToken("--chart-grid"),
  };
}

function withAlpha(color, alpha) {
  const hex = color.match(/^#([0-9a-f]{6})$/i);
  if (hex) {
    const alphaHex = Math.round(alpha * 255).toString(16).padStart(2, "0");
    return `${color}${alphaHex}`;
  }

  const rgb = color.match(/^rgba?\(([^)]+)\)$/i);
  if (rgb) {
    const channels = rgb[1].split(",").slice(0, 3).map((part) => part.trim());
    if (channels.length === 3) return `rgba(${channels.join(", ")}, ${alpha})`;
  }

  return color;
}

function prefersReducedMotion() {
  return (
    typeof globalThis.matchMedia === "function" &&
    globalThis.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

// Show the canvas or the "Not enough data yet" note in its place. A line needs
// at least two points; below that the chart box would be empty.
function toggleEmpty(canvasId, empty) {
  const canvas = document.getElementById(canvasId);
  canvas.hidden = empty;
  const note = canvas.parentElement.querySelector(".chart-empty");
  if (note) note.hidden = !empty;
  return canvas;
}

// One line chart of `valueOf(item, service)` per service across `items`.
function drawLineChart(canvasId, items, valueOf, { yTitle, fill, pointRadius, beginAtZero, maxTicks }) {
  charts[canvasId]?.destroy();
  delete charts[canvasId];
  const empty = items.length < 2;
  const canvas = toggleEmpty(canvasId, empty);
  if (empty) return;

  const services = orderedServiceUnion(items);
  const theme = readChartTheme();
  const color = (i) => theme.series[i % theme.series.length];

  charts[canvasId] = new globalThis.Chart(canvas.getContext("2d"), {
    type: "line",
    data: {
      labels: items.map((item) => formatChartLabel(item.timestamp)),
      datasets: services.map((svc, i) => ({
        label: svc,
        data: items.map((item) => valueOf(item, svc)),
        borderColor: color(i),
        backgroundColor: withAlpha(color(i), 0.08),
        borderWidth: 2,
        tension: 0.4,
        fill,
        pointRadius,
        pointHoverRadius: pointRadius + 2,
        spanGaps: true,
      })),
    },
    options: {
      // The CSS prefers-reduced-motion rule cannot reach canvas drawing.
      ...(prefersReducedMotion() ? { animation: false } : {}),
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: "bottom",
          labels: {
            color: theme.axisLabel,
            font: { family: "Inter", size: 11 },
            boxWidth: 12,
            padding: 12,
          },
        },
      },
      scales: {
        x: {
          title: {
            display: true,
            text: `Local time (${localZoneName()})`,
            color: theme.axisTick,
            font: { size: 11 },
          },
          ticks: { color: theme.axisTick, font: { size: 10 }, maxTicksLimit: maxTicks },
          grid: { color: theme.grid },
        },
        y: {
          title: { display: true, text: yTitle, color: theme.axisTick, font: { size: 11 } },
          ticks: { color: theme.axisTick, font: { size: 10 } },
          grid: { color: theme.grid },
          beginAtZero,
        },
      },
    },
  });
}

export function renderCharts(history) {
  // Only runs that have actual service data. The service list is the union of
  // all runs, so a service added via RALLY_SERVICES appears immediately.
  const runs = ((history && history.runs) || []).filter(
    (r) => Object.keys((r && r.services) || {}).length > 0,
  );
  drawLineChart(
    "durationChart",
    runs,
    (run, svc) => ((run.services || {})[svc] || {}).duration || 0,
    { yTitle: "Seconds", fill: true, pointRadius: 3, beginAtZero: false },
  );
}

export function renderHealthChart(healthHistory) {
  const checks = ((healthHistory && healthHistory.checks) || [])
    .filter((c) => c && c.services && Object.keys(c.services).length > 0)
    .slice(-HEALTH_CHART_MAX_POINTS);
  drawLineChart(
    "healthLatencyChart",
    checks,
    (check, svc) => ((check.services || {})[svc] || {}).latency_ms ?? null,
    { yTitle: "Latency (ms)", fill: false, pointRadius: 2, beginAtZero: true, maxTicks: 8 },
  );
}
