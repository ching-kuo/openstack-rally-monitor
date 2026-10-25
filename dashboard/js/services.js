/**
 * Service cards grid, and the full-width error card shown in its place when a
 * run failed before any service was tested.
 */
import { escapeHtml, formatDuration } from "./format.js";
import { makeActivatable, serviceCardOrHeading } from "./dom.js";
import { openModal } from "./modal.js";
import {
  HEALTH_STATUSES,
  RUN_TIMESTAMP_RE,
  SERVICE_DESCRIPTIONS,
  SERVICE_ICONS,
  SERVICE_STATUSES,
  knownStatus,
} from "./status.js";

// The cards fade in once, on the first render; replaying the animation on
// every 5-minute refresh made the grid flicker.
let animateCards = true;

// Where an aborted run's full log lives. deployment_setup_failed writes
// <RUN_DIR>/deployment.log (run_tests.sh); every other abort logs to run.log.
export function runLogHint(summary) {
  const ts = summary && summary.timestamp;
  const file = summary && summary.error === "deployment_setup_failed" ? "deployment.log" : "run.log";
  const dir = RUN_TIMESTAMP_RE.test(ts || "") ? `/results/${ts}` : "/results/<RUN_DIR>";
  return `Full output: ${dir}/${file} inside the container ` +
    `(docker exec rally-monitor cat ${dir}/${file}).`;
}

function appendText(parent, tag, className, text) {
  const el = document.createElement(tag);
  el.className = className;
  el.textContent = text;
  parent.appendChild(el);
  return el;
}

// A run with a top-level error has no service results, so the grid would be
// empty with no explanation. Every value is inserted as text, never as HTML.
function renderRunErrorCard(grid, summary) {
  const card = document.createElement("div");
  card.className = "run-error-card";
  appendText(card, "h3", "run-error-title", "The run failed before any service was tested");
  appendText(card, "p", "run-error-code", `Error: ${summary.error}`);
  if (summary.error_detail) {
    appendText(card, "pre", "run-error-detail", String(summary.error_detail));
  }
  appendText(card, "p", "run-error-hint", runLogHint(summary));
  grid.appendChild(card);
}

function liveLabel(liveStatus, svcHealth) {
  const latency = svcHealth ? ` · ${svcHealth.latency_ms}ms` : "";
  // "degraded" = reachable but slow: keep it subtle ("API slow · 6200ms").
  if (liveStatus === "up") return `API live${latency}`;
  if (liveStatus === "degraded") return `API slow${latency}`;
  if (liveStatus === "down") return "API down";
  return "API …";
}

export function renderServiceCards(summary, history, health) {
  const grid = document.getElementById("servicesGrid");
  const services = (summary && summary.services) || {};
  // The run timestamp threads into openModal so the modal's "Full Rally report"
  // link targets the correct run directory: the live latest summary in the
  // live view, or the pinned historical run's own timestamp.
  const runTimestamp = summary && summary.timestamp;

  // A refresh replaces the cards; remember which one had keyboard focus.
  const active = document.activeElement;
  const focusedService =
    active && active !== grid && grid.contains(active) ? active.dataset.service : undefined;

  grid.replaceChildren();
  grid.classList.toggle("animate-in", animateCards);
  if (summary && summary.error) renderRunErrorCard(grid, summary);

  for (const [name, data] of Object.entries(services)) {
    const status = knownStatus(data && data.status, SERVICE_STATUSES);
    const card = document.createElement("div");
    card.className = `service-card status-${status}`;
    card.tabIndex = 0;
    // Lets the modal return focus to this service's card after a refresh.
    card.dataset.service = name;
    makeActivatable(
      card,
      `${name}: ${status}. Open scenario details`,
      () => openModal(name, data, runTimestamp),
    );

    // Build mini timeline from history
    const miniTimeline = ((history && history.runs) || []).map((run) => {
      const svc = ((run && run.services) || {})[name];
      return svc ? knownStatus(svc.status, SERVICE_STATUSES) : "pending";
    });

    // Live API health indicator
    const svcHealth = health && health.services && health.services[name];
    const liveStatus = knownStatus(svcHealth && svcHealth.status, HEALTH_STATUSES);
    const failures = Number(data && data.failed_iterations) || 0;

    card.innerHTML = `
            <div class="card-header">
                <div>
                    <div class="card-title">${SERVICE_ICONS[name] || "⚙️"} ${escapeHtml(name)}</div>
                    <div class="card-description">${SERVICE_DESCRIPTIONS[name] || ""}</div>
                </div>
                <div class="card-status">
                    <span class="status-chip ${status}">${status}</span>
                    <div class="live-indicator ${liveStatus}">
                        <span class="live-dot"></span>
                        <span>${escapeHtml(liveLabel(liveStatus, svcHealth))}</span>
                    </div>
                </div>
            </div>
            <div class="card-metrics">
                <div class="metric">
                    <div class="metric-value">${formatDuration((data && data.duration) || 0)}</div>
                    <div class="metric-label">Duration</div>
                </div>
                <div class="metric">
                    <div class="metric-value">${escapeHtml((data && data.total_iterations) || 0)}</div>
                    <div class="metric-label">Iterations</div>
                </div>
                <div class="metric">
                    <div class="metric-value" style="color: ${failures > 0 ? "var(--color-failure)" : "var(--color-success)"}">
                        ${escapeHtml((data && data.failed_iterations) || 0)}
                    </div>
                    <div class="metric-label">Failures</div>
                </div>
            </div>
            <div class="card-timeline" aria-hidden="true">
                ${miniTimeline.map((s) => `<div class="card-timeline-cell ${s}"></div>`).join("")}
            </div>
        `;

    grid.appendChild(card);
  }
  if (grid.children.length > 0) animateCards = false;
  if (focusedService !== undefined) serviceCardOrHeading(focusedService).focus();
}
