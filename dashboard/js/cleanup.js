/**
 * Cleanup status: orphaned Rally resources from cleanup_metrics.json
 * (scripts/cleanup_monitor.sh), with the badge from status.js::cleanupBadge.
 */
import { plural, toCount } from "./format.js";
import { cleanupBadge, contextLeftovers } from "./status.js";

const RESOURCE_TYPES = [
  { key: "servers", label: "Servers", icon: "🖥️" },
  { key: "networks", label: "Networks", icon: "🌐" },
  { key: "routers", label: "Routers", icon: "🔀" },
  { key: "volumes", label: "Volumes", icon: "💾" },
  { key: "images", label: "Images", icon: "🖼️" },
  { key: "users", label: "Users", icon: "👤" },
  { key: "projects", label: "Projects", icon: "📁" },
  { key: "security_groups", label: "Sec Groups", icon: "🛡️" },
];

// Notes explaining the badge: a blind scan, RGW orphans, context leftovers.
function cleanupNotes(cleanup) {
  const c = cleanup || {};
  const notes = [];
  if (c.openstack_scan_status === "error") {
    notes.push(
      `${plural(toCount(c.openstack_scan_errors), "OpenStack list call")} failed; ` +
        "the counts below may be incomplete.",
    );
  }
  if (c.rgw_scan_status === "error") {
    notes.push(`RGW orphan scan failed (${plural(toCount(c.rgw_scan_errors), "error")}).`);
  }
  const contextTotal = contextLeftovers(c);
  if (contextTotal > 0) {
    notes.push(
      `${plural(contextTotal, "context resource")} (c_rally_*) left behind by Rally ` +
        "context teardown; purge_orphans.sh removes them.",
    );
  }
  return notes;
}

function tileHtml(icon, label, count, sub, unit) {
  return `
            <div class="cleanup-item ${count !== 0 ? "has-orphans" : ""}">
                <div class="cleanup-service">${icon} ${label}</div>
                <div class="cleanup-count ${count !== 0 ? "nonzero" : "zero"}">${count}</div>
                <div class="cleanup-label">${unit}</div>
                ${sub ? `<div class="cleanup-sub">${sub}</div>` : ""}
            </div>
        `;
}

// RGW tile, shown only when the RGW scan is configured (not "skipped").
function rgwTileHtml(c) {
  if (!c.rgw_scan_status || c.rgw_scan_status === "skipped") return "";
  if (c.rgw_scan_status === "error") {
    return tileHtml("🪣", "RGW Users", "?", "", "scan error");
  }
  const users = toCount(c.rgw_orphaned_users);
  const sub = users > 0
    ? `${plural(toCount(c.rgw_orphaned_buckets), "bucket")} · ` +
      `${plural(toCount(c.rgw_orphaned_objects), "object")}<br>` +
      `${toCount(c.rgw_rally_owned_orphans)} rally-owned · ` +
      `${toCount(c.rgw_unknown_owner_orphans)} unknown owner`
    : "";
  return tileHtml("🪣", "RGW Users", users, sub, "orphaned");
}

export function renderCleanup(cleanup) {
  const c = cleanup || {};
  const grid = document.getElementById("cleanupGrid");
  const badge = document.getElementById("cleanupBadge");
  const note = document.getElementById("cleanupNote");

  const { text, cls } = cleanupBadge(c);
  badge.textContent = text;
  badge.className = `section-badge ${cls}`;

  if (note) {
    const notes = cleanupNotes(c);
    note.textContent = notes.join(" ");
    note.hidden = notes.length === 0;
  }

  // Every interpolated value is a toCount() number or a fixed label.
  const details = c.details || {};
  const context = c.context_details || {};
  grid.innerHTML =
    RESOURCE_TYPES.map((rt) => {
      const contextCount = toCount(context[rt.key]);
      const sub = contextCount > 0 ? `+${contextCount} context (c_rally_*)` : "";
      return tileHtml(rt.icon, rt.label, toCount(details[rt.key]), sub, "orphaned");
    }).join("") + rgwTileHtml(c);
}
