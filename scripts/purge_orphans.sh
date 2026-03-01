#!/usr/bin/env bash
# ==============================================================================
# Purge Orphans — Manual cleanup of Rally-created orphaned resources
# ==============================================================================
# Identifies and optionally deletes resources left behind by failed Rally
# cleanup. Matches both s_rally_* (scenario) and c_rally_* (context) prefixes.
#
# Usage:
#   /scripts/purge_orphans.sh                    # dry-run: list what would be deleted
#   /scripts/purge_orphans.sh --confirm          # permanently delete orphaned resources
#   /scripts/purge_orphans.sh --confirm --force  # bypass MAX_DELETE_THRESHOLD guard
#
# Via Docker:
#   docker exec rally-monitor /scripts/purge_orphans.sh
#   docker exec rally-monitor /scripts/purge_orphans.sh --confirm
#
# Two-phase design:
#   Phase 1 (always): list orphaned resources, snapshot their IDs, count total.
#   Threshold check: abort if total exceeds MAX_DELETE_THRESHOLD (unless --force).
#   Phase 2 (--confirm only): delete exactly the IDs captured in Phase 1.
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
FORCE=false
for arg in "$@"; do
    case "$arg" in
        --confirm) CONFIRM=true ;;
        --force)   FORCE=true ;;
        -h|--help)
            sed -n '2,/^# =\+$/p' "$0" | sed 's/^# \?//' | head -25
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            echo "Usage: $(basename "$0") [--confirm] [--force]" >&2
            exit 1
            ;;
    esac
done

# Maximum resources that can be deleted in a single run without --force.
# Prevents accidental mass deletion from name prefix collisions.
MAX_DELETE_THRESHOLD="${MAX_DELETE_THRESHOLD:-50}"

# jq filter matching both Rally resource name prefixes
RALLY_FILTER='test("^[sc]_rally")'

TOTAL_FOUND=0
TOTAL_DELETED=0
TOTAL_FAILED=0

# Tracks OpenStack list-call failures during Phase 1.
# Any failure means TOTAL_FOUND is an undercount; we abort CONFIRM mode
# rather than risk deleting beyond the threshold with a bad count.
_LISTING_ERRORS=0

# Phase 1 ID snapshots — populated during listing, consumed during deletion.
# Phase 2 deletes exactly these IDs (no re-listing), ensuring the threshold
# check applies to exactly the same set of resources that gets deleted.
declare -a _SNAP_SERVERS=()
declare -a _SNAP_VOLUMES=()
declare -a _SNAP_ROUTERS=()
declare -a _SNAP_SECGROUPS=()
declare -a _SNAP_NETWORKS=()
declare -a _SNAP_IMAGES=()
declare -a _SNAP_USERS=()
declare -a _SNAP_PROJECTS=()

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

# Run an OpenStack list command and echo the JSON result.
# On failure, increment _LISTING_ERRORS so main() can abort CONFIRM mode
# with an incomplete count rather than risk exceeding the threshold silently.
_os_list() {
    local label="$1"; shift
    local json
    if ! json=$("$@" 2>/dev/null); then
        log "${label}: WARNING — list call failed; threshold count may be incomplete"
        _LISTING_ERRORS=$((_LISTING_ERRORS + 1))
        return 1
    fi
    echo "$json"
}

# Delete a single resource by ID, updating TOTAL_DELETED / TOTAL_FAILED.
_delete_resource() {
    local label="$1" id="$2"; shift 2
    if "$@" "$id" 2>/dev/null; then
        log "  ${label} ${id}: deleted"
        TOTAL_DELETED=$((TOTAL_DELETED + 1))
    else
        log "  ${label} ${id}: deletion FAILED"
        TOTAL_FAILED=$((TOTAL_FAILED + 1))
    fi
}

# --------------------------------------------------------------------------
# Per-resource-type functions — each has a listing phase and deletion phase
# --------------------------------------------------------------------------

purge_servers() {
    local json count
    local -a project_filter=()
    [[ -n "${OS_PROJECT_NAME:-}" ]] && project_filter=("--project" "${OS_PROJECT_NAME}")

    # Listing phase
    json=$(_os_list "Servers" openstack server list --all-projects "${project_filter[@]}" -f json) || return 0
    count=$(rally_count "$json")
    if [[ "$count" -eq 0 ]]; then log "Servers: none"; return; fi
    log "Servers: ${count} orphaned"
    rally_list "$json"
    TOTAL_FOUND=$((TOTAL_FOUND + count))
    while IFS= read -r id; do [[ -n "$id" ]] && _SNAP_SERVERS+=("$id"); done < <(rally_ids "$json")
}
purge_servers_delete() {
    [[ "${#_SNAP_SERVERS[@]}" -eq 0 ]] && return
    for id in "${_SNAP_SERVERS[@]}"; do
        _delete_resource "Server" "$id" openstack server delete
    done
}

purge_volumes() {
    local json count
    local -a project_filter=()
    [[ -n "${OS_PROJECT_NAME:-}" ]] && project_filter=("--project" "${OS_PROJECT_NAME}")

    json=$(_os_list "Volumes" openstack volume list --all-projects "${project_filter[@]}" -f json) || return 0
    count=$(rally_count "$json")
    if [[ "$count" -eq 0 ]]; then log "Volumes: none"; return; fi
    log "Volumes: ${count} orphaned"
    rally_list "$json"
    TOTAL_FOUND=$((TOTAL_FOUND + count))
    while IFS= read -r id; do [[ -n "$id" ]] && _SNAP_VOLUMES+=("$id"); done < <(rally_ids "$json")
}
purge_volumes_delete() {
    [[ "${#_SNAP_VOLUMES[@]}" -eq 0 ]] && return
    for id in "${_SNAP_VOLUMES[@]}"; do
        _delete_resource "Volume" "$id" openstack volume delete
    done
}

purge_routers() {
    local json count
    local -a project_filter=()
    [[ -n "${OS_PROJECT_NAME:-}" ]] && project_filter=("--project" "${OS_PROJECT_NAME}")

    json=$(_os_list "Routers" openstack router list "${project_filter[@]}" -f json) || return 0
    count=$(rally_count "$json")
    if [[ "$count" -eq 0 ]]; then log "Routers: none"; return; fi
    log "Routers: ${count} orphaned"
    rally_list "$json"
    TOTAL_FOUND=$((TOTAL_FOUND + count))
    while IFS= read -r id; do [[ -n "$id" ]] && _SNAP_ROUTERS+=("$id"); done < <(rally_ids "$json")
}
purge_routers_delete() {
    [[ "${#_SNAP_ROUTERS[@]}" -eq 0 ]] && return
    for router_id in "${_SNAP_ROUTERS[@]}"; do
        log "  Detaching router ${router_id}..."
        # Detach subnet interfaces — guard the whole pipeline so a failed
        # `router show` (race, already deleted, permission) does not abort
        # the script under set -euo pipefail.
        local router_json
        router_json=$(openstack router show "$router_id" -f json 2>/dev/null) || true
        if [[ -n "${router_json:-}" ]]; then
            while IFS= read -r subnet_id; do
                [[ -z "$subnet_id" ]] && continue
                openstack router remove subnet "$router_id" "$subnet_id" 2>/dev/null || true
            done < <(echo "$router_json" | jq -r '.interfaces_info[]?.subnet_id // empty' 2>/dev/null || true)
        fi
        openstack router unset --external-gateway "$router_id" 2>/dev/null || true
        _delete_resource "Router" "$router_id" openstack router delete
    done
}

purge_security_groups() {
    local json count
    local -a project_filter=()
    [[ -n "${OS_PROJECT_NAME:-}" ]] && project_filter=("--project" "${OS_PROJECT_NAME}")

    json=$(_os_list "Security groups" openstack security group list "${project_filter[@]}" -f json) || return 0
    count=$(rally_count "$json")
    if [[ "$count" -eq 0 ]]; then log "Security groups: none"; return; fi
    log "Security groups: ${count} orphaned"
    rally_list "$json"
    TOTAL_FOUND=$((TOTAL_FOUND + count))
    while IFS= read -r id; do [[ -n "$id" ]] && _SNAP_SECGROUPS+=("$id"); done < <(rally_ids "$json")
}
purge_security_groups_delete() {
    [[ "${#_SNAP_SECGROUPS[@]}" -eq 0 ]] && return
    for id in "${_SNAP_SECGROUPS[@]}"; do
        _delete_resource "Security group" "$id" openstack security group delete
    done
}

purge_networks() {
    local json count
    local -a project_filter=()
    [[ -n "${OS_PROJECT_NAME:-}" ]] && project_filter=("--project" "${OS_PROJECT_NAME}")

    json=$(_os_list "Networks" openstack network list "${project_filter[@]}" -f json) || return 0
    count=$(rally_count "$json")
    if [[ "$count" -eq 0 ]]; then log "Networks: none"; return; fi
    log "Networks: ${count} orphaned"
    rally_list "$json"
    TOTAL_FOUND=$((TOTAL_FOUND + count))
    while IFS= read -r id; do [[ -n "$id" ]] && _SNAP_NETWORKS+=("$id"); done < <(rally_ids "$json")
}
purge_networks_delete() {
    [[ "${#_SNAP_NETWORKS[@]}" -eq 0 ]] && return
    for id in "${_SNAP_NETWORKS[@]}"; do
        _delete_resource "Network" "$id" openstack network delete
    done
}

purge_images() {
    local json count
    # Images: must be owner-scoped when OS_PROJECT_NAME is set.
    # If the project ID lookup fails, skip images entirely to avoid listing
    # every image in the cloud (fail closed, not open).
    local -a owner_filter=()
    if [[ -n "${OS_PROJECT_NAME:-}" ]]; then
        local project_id
        project_id=$(openstack project show "${OS_PROJECT_NAME}" -f value -c id 2>/dev/null) || true
        if [[ -z "${project_id:-}" ]]; then
            log "Images: WARNING — could not resolve project ID for '${OS_PROJECT_NAME}'; skipping to avoid global listing."
            _LISTING_ERRORS=$((_LISTING_ERRORS + 1))
            return 0
        fi
        owner_filter=("--owner" "${project_id}")
    fi
    json=$(_os_list "Images" openstack image list "${owner_filter[@]}" -f json) || return 0
    count=$(rally_count "$json")
    if [[ "$count" -eq 0 ]]; then log "Images: none"; return; fi
    log "Images: ${count} orphaned"
    rally_list "$json"
    TOTAL_FOUND=$((TOTAL_FOUND + count))
    while IFS= read -r id; do [[ -n "$id" ]] && _SNAP_IMAGES+=("$id"); done < <(rally_ids "$json")
}
purge_images_delete() {
    [[ "${#_SNAP_IMAGES[@]}" -eq 0 ]] && return
    for id in "${_SNAP_IMAGES[@]}"; do
        _delete_resource "Image" "$id" openstack image delete
    done
}

purge_users() {
    # Users are global in Keystone; match by name prefix only.
    local json count
    json=$(_os_list "Users" openstack user list -f json) || return 0
    count=$(rally_count "$json")
    if [[ "$count" -eq 0 ]]; then log "Users: none"; return; fi
    log "Users: ${count} orphaned"
    rally_list "$json"
    TOTAL_FOUND=$((TOTAL_FOUND + count))
    while IFS= read -r id; do [[ -n "$id" ]] && _SNAP_USERS+=("$id"); done < <(rally_ids "$json")
}
purge_users_delete() {
    [[ "${#_SNAP_USERS[@]}" -eq 0 ]] && return
    for id in "${_SNAP_USERS[@]}"; do
        _delete_resource "User" "$id" openstack user delete
    done
}

purge_projects() {
    # Projects are global in Keystone; match by name prefix only.
    local json count
    json=$(_os_list "Projects" openstack project list -f json) || return 0
    count=$(rally_count "$json")
    if [[ "$count" -eq 0 ]]; then log "Projects: none"; return; fi
    log "Projects: ${count} orphaned"
    rally_list "$json"
    TOTAL_FOUND=$((TOTAL_FOUND + count))
    while IFS= read -r id; do [[ -n "$id" ]] && _SNAP_PROJECTS+=("$id"); done < <(rally_ids "$json")
}
purge_projects_delete() {
    [[ "${#_SNAP_PROJECTS[@]}" -eq 0 ]] && return
    for id in "${_SNAP_PROJECTS[@]}"; do
        _delete_resource "Project" "$id" openstack project delete
    done
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
main() {
    # Phase 1: listing pass — never deletes anything.
    # Populates ID snapshots and TOTAL_FOUND for threshold enforcement.
    _LISTING_ERRORS=0
    if $CONFIRM; then
        log "Phase 1: listing orphaned resources..."
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

    if ! $CONFIRM; then
        log "Dry-run complete. Found ${TOTAL_FOUND} orphaned resources."
        if [[ "${TOTAL_FOUND}" -gt 0 ]]; then
            echo
            log "To delete these resources run:"
            log "  $(basename "$0") --confirm"
            if [[ "${TOTAL_FOUND}" -gt "${MAX_DELETE_THRESHOLD}" ]]; then
                log "  Note: total (${TOTAL_FOUND}) exceeds threshold (${MAX_DELETE_THRESHOLD}). Also pass --force."
            fi
        fi
        return 0
    fi

    # Abort if any Phase 1 list calls failed — the count may be too low to
    # trust the threshold check.
    if [[ "${_LISTING_ERRORS}" -gt 0 ]]; then
        log "ABORTED: ${_LISTING_ERRORS} resource list call(s) failed during Phase 1."
        log "The resource count may be under-reported; cannot safely enforce threshold."
        log "Investigate connectivity and retry."
        exit 3
    fi

    # Safety check: abort if total found exceeds threshold unless --force is set.
    # This check happens BEFORE Phase 2 so no resources are deleted if aborted.
    if [[ "${TOTAL_FOUND}" -gt "${MAX_DELETE_THRESHOLD}" ]] && ! $FORCE; then
        log "ABORTED: Found ${TOTAL_FOUND} resources, which exceeds MAX_DELETE_THRESHOLD=${MAX_DELETE_THRESHOLD}."
        log "Pass --force to override, or investigate why so many orphans exist."
        exit 2
    fi

    # Phase 2: deletion pass — uses only the IDs snapshotted in Phase 1.
    # No new listing occurs; the set being deleted matches exactly what was counted.
    log "Phase 2: deleting ${TOTAL_FOUND} orphaned Rally resources (from Phase 1 snapshot)..."
    [[ -n "${OS_PROJECT_NAME:-}" ]] && log "Project-scoped to: ${OS_PROJECT_NAME}"
    local found_in_listing="${TOTAL_FOUND}"
    echo

    purge_servers_delete
    purge_volumes_delete
    purge_routers_delete
    purge_security_groups_delete
    purge_networks_delete
    purge_images_delete
    purge_users_delete
    purge_projects_delete

    echo
    log "Done. Deleted ${TOTAL_DELETED} of ${found_in_listing} orphaned resources (${TOTAL_FAILED} failed)."

    if [[ "${TOTAL_FAILED}" -gt 0 ]]; then
        log "WARNING: ${TOTAL_FAILED} deletion(s) failed — check logs above for details."
    fi

    # Refresh cleanup metrics so the dashboard and Prometheus reflect the new state
    if [[ -x "/scripts/cleanup_monitor.sh" ]]; then
        log "Refreshing cleanup metrics..."
        /scripts/cleanup_monitor.sh || true
    fi

    # Exit non-zero if any deletions failed
    [[ "${TOTAL_FAILED}" -gt 0 ]] && exit 1
}

main "$@"
