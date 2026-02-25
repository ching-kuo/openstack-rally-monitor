#!/usr/bin/env bash
# ==============================================================================
# Purge Orphans — Manual cleanup of Rally-created orphaned resources
# ==============================================================================
# Identifies and optionally deletes resources left behind by failed Rally
# cleanup. Matches both s_rally_* (scenario) and c_rally_* (context) prefixes.
#
# Usage:
#   /scripts/purge_orphans.sh            # dry-run: list what would be deleted
#   /scripts/purge_orphans.sh --confirm  # permanently delete orphaned resources
#
# Via Docker:
#   docker exec rally-monitor /scripts/purge_orphans.sh
#   docker exec rally-monitor /scripts/purge_orphans.sh --confirm
#
# Deletion order respects OpenStack dependencies:
#   1. Servers      (depend on networks, security groups, volumes)
#   2. Volumes      (may be attached to servers)
#   3. Routers      (subnet interfaces and gateway detached before deletion)
#   4. Security Groups
#   5. Networks
#   6. Images
#   7. Users
#   8. Projects     (last — users and resources may belong to them)
# ==============================================================================
set -euo pipefail

CONFIRM=false
for arg in "$@"; do
    case "$arg" in
        --confirm) CONFIRM=true ;;
        -h|--help)
            sed -n '2,/^# =\+$/p' "$0" | sed 's/^# \?//' | head -20
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            echo "Usage: $(basename "$0") [--confirm]" >&2
            exit 1
            ;;
    esac
done

# jq filter matching both Rally resource name prefixes
RALLY_FILTER='test("^[sc]_rally")'

TOTAL_FOUND=0
TOTAL_DELETED=0

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [purge-orphans] $*"; }

# Number of resources in a JSON array that match the Rally filter
rally_count() {
    echo "$1" | jq "[.[] | select(.Name | ${RALLY_FILTER})] | length" 2>/dev/null || echo 0
}

# Extract IDs (or alternate key) of matching resources, one per line
rally_ids() {
    local id_key="${2:-.ID}"
    echo "$1" | jq -r ".[] | select(.Name | ${RALLY_FILTER}) | ${id_key}" 2>/dev/null || true
}

# Print "  Name  (ID)" for each matching resource
rally_list() {
    local id_key="${2:-.ID}"
    echo "$1" | jq -r ".[] | select(.Name | ${RALLY_FILTER}) | \"  \(.Name)  (\(${id_key}))\"" 2>/dev/null || true
}

# --------------------------------------------------------------------------
# Per-resource-type functions
# --------------------------------------------------------------------------

purge_servers() {
    local json count
    json=$(openstack server list --all-projects -f json 2>/dev/null) || return 0
    count=$(rally_count "$json")
    [[ "$count" -eq 0 ]] && { log "Servers: none"; return; }
    log "Servers: ${count} orphaned"
    rally_list "$json"
    TOTAL_FOUND=$((TOTAL_FOUND + count))
    if $CONFIRM; then
        rally_ids "$json" | xargs -r openstack server delete || true
        log "Servers: delete requests sent for ${count}"
        TOTAL_DELETED=$((TOTAL_DELETED + count))
    fi
}

purge_volumes() {
    local json count
    json=$(openstack volume list --all-projects -f json 2>/dev/null) || return 0
    count=$(rally_count "$json")
    [[ "$count" -eq 0 ]] && { log "Volumes: none"; return; }
    log "Volumes: ${count} orphaned"
    rally_list "$json"
    TOTAL_FOUND=$((TOTAL_FOUND + count))
    if $CONFIRM; then
        rally_ids "$json" | xargs -r openstack volume delete || true
        log "Volumes: delete requests sent for ${count}"
        TOTAL_DELETED=$((TOTAL_DELETED + count))
    fi
}

purge_routers() {
    local json count
    json=$(openstack router list -f json 2>/dev/null) || return 0
    count=$(rally_count "$json")
    [[ "$count" -eq 0 ]] && { log "Routers: none"; return; }
    log "Routers: ${count} orphaned"
    rally_list "$json"
    TOTAL_FOUND=$((TOTAL_FOUND + count))
    if $CONFIRM; then
        # Routers must have interfaces and gateway removed before deletion
        while IFS= read -r router_id; do
            [[ -z "$router_id" ]] && continue
            log "  Detaching router ${router_id}..."
            # Remove each subnet interface
            openstack router show "$router_id" -f json 2>/dev/null \
                | jq -r '.interfaces_info[]?.subnet_id // empty' \
                | while IFS= read -r subnet_id; do
                    [[ -z "$subnet_id" ]] && continue
                    openstack router remove subnet "$router_id" "$subnet_id" 2>/dev/null || true
                done
            # Clear external gateway
            openstack router unset --external-gateway "$router_id" 2>/dev/null || true
            # Now delete
            if openstack router delete "$router_id" 2>/dev/null; then
                log "  Router ${router_id}: deleted"
            else
                log "  Router ${router_id}: deletion failed — check manually"
            fi
        done < <(rally_ids "$json")
        TOTAL_DELETED=$((TOTAL_DELETED + count))
    fi
}

purge_security_groups() {
    local json count
    json=$(openstack security group list -f json 2>/dev/null) || return 0
    count=$(rally_count "$json")
    [[ "$count" -eq 0 ]] && { log "Security groups: none"; return; }
    log "Security groups: ${count} orphaned"
    rally_list "$json"
    TOTAL_FOUND=$((TOTAL_FOUND + count))
    if $CONFIRM; then
        rally_ids "$json" | xargs -r openstack security group delete || true
        log "Security groups: delete requests sent for ${count}"
        TOTAL_DELETED=$((TOTAL_DELETED + count))
    fi
}

purge_networks() {
    local json count
    json=$(openstack network list -f json 2>/dev/null) || return 0
    count=$(rally_count "$json")
    [[ "$count" -eq 0 ]] && { log "Networks: none"; return; }
    log "Networks: ${count} orphaned"
    rally_list "$json"
    TOTAL_FOUND=$((TOTAL_FOUND + count))
    if $CONFIRM; then
        rally_ids "$json" | xargs -r openstack network delete || true
        log "Networks: delete requests sent for ${count}"
        TOTAL_DELETED=$((TOTAL_DELETED + count))
    fi
}

purge_images() {
    local json count
    json=$(openstack image list -f json 2>/dev/null) || return 0
    count=$(rally_count "$json")
    [[ "$count" -eq 0 ]] && { log "Images: none"; return; }
    log "Images: ${count} orphaned"
    rally_list "$json"
    TOTAL_FOUND=$((TOTAL_FOUND + count))
    if $CONFIRM; then
        rally_ids "$json" | xargs -r openstack image delete || true
        log "Images: delete requests sent for ${count}"
        TOTAL_DELETED=$((TOTAL_DELETED + count))
    fi
}

purge_users() {
    local json count
    json=$(openstack user list -f json 2>/dev/null) || return 0
    count=$(rally_count "$json")
    [[ "$count" -eq 0 ]] && { log "Users: none"; return; }
    log "Users: ${count} orphaned"
    rally_list "$json"
    TOTAL_FOUND=$((TOTAL_FOUND + count))
    if $CONFIRM; then
        rally_ids "$json" | xargs -r openstack user delete || true
        log "Users: delete requests sent for ${count}"
        TOTAL_DELETED=$((TOTAL_DELETED + count))
    fi
}

purge_projects() {
    local json count
    json=$(openstack project list -f json 2>/dev/null) || return 0
    count=$(rally_count "$json")
    [[ "$count" -eq 0 ]] && { log "Projects: none"; return; }
    log "Projects: ${count} orphaned"
    rally_list "$json"
    TOTAL_FOUND=$((TOTAL_FOUND + count))
    if $CONFIRM; then
        rally_ids "$json" | xargs -r openstack project delete || true
        log "Projects: delete requests sent for ${count}"
        TOTAL_DELETED=$((TOTAL_DELETED + count))
    fi
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
main() {
    if $CONFIRM; then
        log "*** CONFIRM mode — orphaned Rally resources will be PERMANENTLY DELETED ***"
    else
        log "Dry-run mode — listing orphaned resources only (pass --confirm to delete)"
    fi
    echo

    purge_servers
    purge_volumes
    purge_routers
    purge_security_groups
    purge_networks
    purge_images
    purge_users
    purge_projects

    echo
    if $CONFIRM; then
        log "Done. Issued deletes for ${TOTAL_DELETED} of ${TOTAL_FOUND} orphaned resources."
        # Refresh cleanup metrics so the dashboard and Prometheus reflect the new state
        if [[ -x "/scripts/cleanup_monitor.sh" ]]; then
            log "Refreshing cleanup metrics..."
            /scripts/cleanup_monitor.sh || true
        fi
    else
        log "Dry-run complete. Found ${TOTAL_FOUND} orphaned resources."
        if [[ "${TOTAL_FOUND}" -gt 0 ]]; then
            echo
            log "To delete these resources run:"
            log "  $(basename "$0") --confirm"
        fi
    fi
}

main "$@"
