/**
 * Small DOM helpers shared by the render modules. Nothing here runs at import
 * time, so modules importing it stay importable under node.
 */
import { escapeHtml, formatCoveredSpan } from "./format.js";

export function setSpanLabel(id, records) {
  const el = document.getElementById(id);
  if (el) el.textContent = formatCoveredSpan(records);
}

// Make a non-button element behave like one for keyboard and assistive tech:
// role, accessible name, and Enter/Space activation alongside the click.
// Tab order is left to the caller (tabIndex 0, or a roving tab stop).
export function makeActivatable(el, label, onActivate) {
  el.setAttribute("role", "button");
  el.setAttribute("aria-label", label);
  el.addEventListener("click", onActivate);
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onActivate();
    }
  });
}

// A timeline can hold hundreds of cells; one Tab stop per cell would bury the
// rest of the page. A roving tab stop gives the whole row a single stop and
// moves between cells with the arrow keys, Home and End.
export function initRovingFocus(container, selector) {
  container.addEventListener("keydown", (e) => {
    const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
    if (!keys.includes(e.key)) return;
    const cells = Array.from(container.querySelectorAll(selector));
    const current = cells.indexOf(document.activeElement);
    if (current === -1) return;
    e.preventDefault();
    let next = current;
    if (e.key === "ArrowLeft") next = Math.max(0, current - 1);
    else if (e.key === "ArrowRight") next = Math.min(cells.length - 1, current + 1);
    else if (e.key === "Home") next = 0;
    else next = cells.length - 1;
    cells[current].tabIndex = -1;
    cells[next].tabIndex = 0;
    cells[next].focus();
  });
}

// After a render: `stop` (or the last cell) is the row's one Tab stop.
export function setRovingTabStop(cells, stop) {
  const target = stop || cells[cells.length - 1];
  for (const cell of cells) cell.tabIndex = cell === target ? 0 : -1;
  return target;
}

// The data-key of the cell holding keyboard focus inside `container`, or null
// when focus is elsewhere. Read it before a re-render replaces the cells:
// replaceChildren drops focus to <body>.
export function focusedKey(container) {
  const active = document.activeElement;
  if (!active || active === container || !container.contains(active)) return null;
  return active.dataset.key ?? null;
}

// Set the row's Tab stop after a render and, when a cell had focus before it
// (`key` from focusedKey), put focus back on the cell with the same key (or on
// the stop when that cell is gone), so keyboard users keep their place.
export function placeRovingFocus(cells, stop, key) {
  const previous = key === null ? null : cells.find((c) => c.dataset.key === key);
  const target = setRovingTabStop(cells, previous || stop);
  if (key !== null && target) target.focus();
}

// Hover/focus tooltip markup shared by the run and health timeline cells.
export function tooltipHtml(when, detail) {
  return `
            <div class="timeline-tooltip" aria-hidden="true">
                <strong>${escapeHtml(when)}</strong><br>
                ${escapeHtml(detail)}
            </div>
        `;
}

// Tooltips anchor away from the nearer screen edge so they are not clipped on
// phones: cells in the left half of a row anchor left (the tooltip extends
// rightwards), cells in the right half anchor right, and the exact middle
// cell of an odd row stays centred. Tooltips are capped at about half the
// viewport (style.css .timeline-tooltip), so an anchored one always fits.
export function tooltipEdgeClass(index, count) {
  const middle = (count - 1) / 2;
  if (index < middle) return "tip-left";
  if (index > middle) return "tip-right";
  return "";
}

// The service card for `service` (services.js sets data-service), or, when
// that service has no card any more, the services heading made
// programmatically focusable, so focus never falls back to <body>. Used after
// a re-render replaced the card that had focus.
export function serviceCardOrHeading(service) {
  const grid = document.getElementById("servicesGrid");
  const card = Array.from(grid.children).find((c) => c.dataset.service === service);
  if (card) return card;
  const heading = document.getElementById("servicesSectionTitle");
  heading.tabIndex = -1;
  return heading;
}
