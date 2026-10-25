/**
 * Scenario detail modal: an accessible dialog (focus moved in and trapped,
 * Escape closes, focus returns to the opener).
 */
import { serviceCardOrHeading } from "./dom.js";
import { escapeHtml, formatDuration } from "./format.js";
import { RUN_TIMESTAMP_RE, SERVICE_ICONS, serviceErrorText } from "./status.js";

// The element that opened the dialog, and the service it showed: a refresh
// while the dialog is open replaces the cards, so the opener may be gone by
// the time the dialog closes.
let returnFocusTo = null;
let returnService = null;

function scenarioHtml(s) {
  const failures = Number(s.failures) || 0;
  return `
            <div class="scenario-row">
                <div class="scenario-name">${escapeHtml(s.name)}</div>
                <div class="scenario-stats">
                    <div class="scenario-stat">
                        <div class="scenario-stat-value">${formatDuration(s.duration || 0)}</div>
                        <div class="scenario-stat-label">Duration</div>
                    </div>
                    <div class="scenario-stat">
                        <div class="scenario-stat-value">${escapeHtml(s.iterations || 0)}</div>
                        <div class="scenario-stat-label">Iters</div>
                    </div>
                    <div class="scenario-stat">
                        <div class="scenario-stat-value" style="color: ${failures > 0 ? "var(--color-failure)" : "var(--color-success)"}">${escapeHtml(s.failures || 0)}</div>
                        <div class="scenario-stat-label">Fails</div>
                    </div>
                    <span class="status-chip ${s.sla ? "passed" : "failed"}">${s.sla ? "SLA OK" : "SLA FAIL"}</span>
                </div>
            </div>
            ${s.first_error ? `<div class="scenario-error">${escapeHtml(s.first_error)}</div>` : ""}
        `;
}

export function openModal(serviceName, data, runTimestamp) {
  const overlay = document.getElementById("modalOverlay");
  const title = document.getElementById("modalTitle");
  const body = document.getElementById("modalBody");

  title.textContent = `${SERVICE_ICONS[serviceName] || ""} ${serviceName} — Scenario Details`;

  // Built as one string and assigned once: appending with `innerHTML +=`
  // re-parses the whole body each time.
  const errorText = serviceErrorText(data);
  let html = errorText ? `<div class="scenario-error">${escapeHtml(errorText)}</div>` : "";

  const scenarios = (data && data.scenarios) || [];
  if (scenarios.length === 0) {
    html += '<p class="modal-empty">No scenario data available</p>';
  } else {
    html += scenarios.map(scenarioHtml).join("");
  }

  // Link to the full self-contained Rally HTML report for the currently-viewed
  // run. The target may 404 after retention pruning removes the run directory;
  // that's acceptable -- it opens in a new tab (rel=noopener) and we
  // deliberately do not pre-check. Only shown for a real run timestamp.
  if (RUN_TIMESTAMP_RE.test(runTimestamp || "")) {
    const href = `runs/${encodeURIComponent(runTimestamp)}/${encodeURIComponent(serviceName)}.html`;
    html +=
      `<a class="report-link" href="${escapeHtml(href)}" target="_blank" rel="noopener">` +
      `Full Rally report ↗</a>`;
  }

  body.innerHTML = html;
  returnFocusTo = document.activeElement;
  returnService = serviceName;
  overlay.classList.add("active");
  overlay.setAttribute("aria-hidden", "false");
  document.getElementById("modalClose").focus();
}

function closeModal() {
  const overlay = document.getElementById("modalOverlay");
  if (!overlay.classList.contains("active")) return;
  overlay.classList.remove("active");
  overlay.setAttribute("aria-hidden", "true");
  const target = focusTargetAfterClose();
  if (target) target.focus();
  returnFocusTo = null;
  returnService = null;
}

// The opener if it is still in the page; else the re-rendered card of the same
// service, or the services heading (dom.js serviceCardOrHeading).
function focusTargetAfterClose() {
  if (returnFocusTo && returnFocusTo.isConnected) return returnFocusTo;
  if (returnFocusTo === null) return null;
  return serviceCardOrHeading(returnService);
}

function focusableIn(root) {
  return Array.from(
    root.querySelectorAll('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'),
  );
}

// Keep Tab and Shift+Tab inside the open dialog.
function trapFocus(e) {
  const overlay = document.getElementById("modalOverlay");
  if (e.key !== "Tab" || !overlay.classList.contains("active")) return;
  const focusable = focusableIn(overlay.querySelector(".modal"));
  if (focusable.length === 0) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  } else if (!overlay.contains(document.activeElement)) {
    e.preventDefault();
    first.focus();
  }
}

export function initModal() {
  const overlay = document.getElementById("modalOverlay");
  document.getElementById("modalClose").addEventListener("click", closeModal);
  overlay.addEventListener("click", (e) => {
    if (e.target === e.currentTarget) closeModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModal();
    else trapFocus(e);
  });
}
