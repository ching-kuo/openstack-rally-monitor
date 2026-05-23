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
#   9. RadosGW users (after Keystone projects are gone; buckets first, then user)
# ==============================================================================
set -euo pipefail

CONFIRM=false
FORCE=false
RESULTS_DIR="${RESULTS_DIR:-/results}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/rgw_helpers.sh"
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
_RGW_LISTING_ERRORS=0
_RGW_SKIPPED_UNKNOWN=0
RGW_PURGE_SKIPPED=false

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
declare -a _SNAP_RGW_USERS=()
declare -a _SNAP_RGW_BUCKETS=()

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [purge-orphans] $*"; }

# Write a JSON audit record of the deletion run to RESULTS_DIR.
# Records the Phase 1 snapshot (found IDs) and deletion outcome (deleted/failed counts).
# The audit file is named purge_audit_<timestamp>.json to preserve history.
write_audit_log() {
    local ts found_in_listing="$1" deleted="$2" failed="$3"
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    _arr_to_json() {
        if [[ "${#@}" -eq 0 ]]; then echo "[]"; return; fi
        printf '%s\n' "$@" | jq -R . | jq -s .
    }
    _pairs_to_json() {
        if [[ "${#@}" -eq 0 ]]; then echo "[]"; return; fi
        printf '%s\n' "$@" | jq -R 'split("|") | {uid: .[0], bucket: .[1]}' | jq -s .
    }

    local servers_json volumes_json routers_json secgroups_json
    local networks_json images_json users_json projects_json
    local rgw_users_json rgw_buckets_json
    servers_json=$(_arr_to_json "${_SNAP_SERVERS[@]+"${_SNAP_SERVERS[@]}"}")
    volumes_json=$(_arr_to_json "${_SNAP_VOLUMES[@]+"${_SNAP_VOLUMES[@]}"}")
    routers_json=$(_arr_to_json "${_SNAP_ROUTERS[@]+"${_SNAP_ROUTERS[@]}"}")
    secgroups_json=$(_arr_to_json "${_SNAP_SECGROUPS[@]+"${_SNAP_SECGROUPS[@]}"}")
    networks_json=$(_arr_to_json "${_SNAP_NETWORKS[@]+"${_SNAP_NETWORKS[@]}"}")
    images_json=$(_arr_to_json "${_SNAP_IMAGES[@]+"${_SNAP_IMAGES[@]}"}")
    users_json=$(_arr_to_json "${_SNAP_USERS[@]+"${_SNAP_USERS[@]}"}")
    projects_json=$(_arr_to_json "${_SNAP_PROJECTS[@]+"${_SNAP_PROJECTS[@]}"}")
    rgw_users_json=$(_arr_to_json "${_SNAP_RGW_USERS[@]+"${_SNAP_RGW_USERS[@]}"}")
    rgw_buckets_json=$(_pairs_to_json "${_SNAP_RGW_BUCKETS[@]+"${_SNAP_RGW_BUCKETS[@]}"}")

    # Sanitize filename: colons in ISO timestamp → hyphens.
    local safe_ts="${ts//:/-}"
    local audit_file="${RESULTS_DIR}/purge_audit_${safe_ts}.json"
    local audit_tmp="${audit_file}.tmp"

    jq -n \
        --arg ts "$ts" \
        --arg host "$(hostname -s 2>/dev/null || echo unknown)" \
        --argjson confirm "$( $CONFIRM && echo true || echo false )" \
        --argjson force "$( $FORCE && echo true || echo false )" \
        --arg project "${OS_PROJECT_NAME:-}" \
        --argjson total_found "$found_in_listing" \
        --argjson total_deleted "$deleted" \
        --argjson total_failed "$failed" \
        --argjson servers "$servers_json" \
        --argjson volumes "$volumes_json" \
        --argjson routers "$routers_json" \
        --argjson security_groups "$secgroups_json" \
        --argjson networks "$networks_json" \
        --argjson images "$images_json" \
        --argjson users "$users_json" \
        --argjson projects "$projects_json" \
        --argjson rgw_users "$rgw_users_json" \
        --argjson rgw_buckets "$rgw_buckets_json" \
        --argjson rgw_listing_errors "${_RGW_LISTING_ERRORS}" \
        --argjson rgw_skipped_unknown_owner "${_RGW_SKIPPED_UNKNOWN}" \
        --argjson rgw_purge_skipped "$( $RGW_PURGE_SKIPPED && echo true || echo false )" \
        '{
            timestamp: $ts,
            hostname: $host,
            confirm: $confirm,
            force: $force,
            project: $project,
            total_found: $total_found,
            total_deleted: $total_deleted,
            total_failed: $total_failed,
            rgw_listing_errors: $rgw_listing_errors,
            rgw_skipped_unknown_owner: $rgw_skipped_unknown_owner,
            rgw_purge_skipped: $rgw_purge_skipped,
            found_ids: {
                servers: $servers,
                volumes: $volumes,
                routers: $routers,
                security_groups: $security_groups,
                networks: $networks,
                images: $images,
                users: $users,
                projects: $projects,
                rgw_users: $rgw_users,
                rgw_buckets: $rgw_buckets
            }
        }' > "${audit_tmp}" && mv "${audit_tmp}" "${audit_file}"

    log "Audit log written to ${audit_file}"
}

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
# Generic listing helper — records found resources into the named array.
# Usage: _list_orphans LABEL SNAP_ARRAY_NAME openstack <subcommand> [args...]
# The trailing "-f json" flag is appended automatically; do not include it.
# Requires bash 4.3+ for declare -n nameref support.
# --------------------------------------------------------------------------
_list_orphans() {
    local label="$1" snap_var="$2"; shift 2
    declare -n _arr="$snap_var"
    local json count
    json=$(_os_list "$label" "$@" -f json) || return 0
    count=$(rally_count "$json")
    if [[ "$count" -eq 0 ]]; then log "${label}: none"; return; fi
    log "${label}: ${count} orphaned"
    rally_list "$json"
    TOTAL_FOUND=$((TOTAL_FOUND + count))
    while IFS= read -r id; do [[ -n "$id" ]] && _arr+=("$id"); done < <(rally_ids "$json")
}

# --------------------------------------------------------------------------
# Per-resource-type functions — each has a listing phase and deletion phase
# --------------------------------------------------------------------------

purge_servers() {
    # Rally orphans live in c_rally_* context projects, not OS_PROJECT_NAME.
    # Name-prefix matching already provides scope; admin role is required.
    _list_orphans "Servers" _SNAP_SERVERS openstack server list --all-projects
}
purge_servers_delete() {
    [[ "${#_SNAP_SERVERS[@]}" -eq 0 ]] && return
    for id in "${_SNAP_SERVERS[@]}"; do
        _delete_resource "Server" "$id" openstack server delete
    done
}

purge_volumes() {
    _list_orphans "Volumes" _SNAP_VOLUMES openstack volume list --all-projects
}
purge_volumes_delete() {
    [[ "${#_SNAP_VOLUMES[@]}" -eq 0 ]] && return
    for id in "${_SNAP_VOLUMES[@]}"; do
        # Volumes can get stuck in error_deleting / deleting after a failed
        # cleanup; cinder protects those states from plain `volume delete`.
        # Reset to `error` first, then force-delete.
        local status
        status=$(openstack volume show "$id" -f value -c status 2>/dev/null || echo "")
        case "$status" in
            error_deleting|deleting|error)
                openstack volume set --state error "$id" 2>/dev/null || true
                ;;
        esac
        _delete_resource "Volume" "$id" openstack volume delete --force
    done
}

purge_routers() {
    _list_orphans "Routers" _SNAP_ROUTERS openstack router list
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
    _list_orphans "Security groups" _SNAP_SECGROUPS openstack security group list
}
purge_security_groups_delete() {
    [[ "${#_SNAP_SECGROUPS[@]}" -eq 0 ]] && return
    for id in "${_SNAP_SECGROUPS[@]}"; do
        _delete_resource "Security group" "$id" openstack security group delete
    done
}

purge_networks() {
    _list_orphans "Networks" _SNAP_NETWORKS openstack network list
}
purge_networks_delete() {
    [[ "${#_SNAP_NETWORKS[@]}" -eq 0 ]] && return
    for id in "${_SNAP_NETWORKS[@]}"; do
        _delete_resource "Network" "$id" openstack network delete
    done
}

purge_images() {
    _list_orphans "Images" _SNAP_IMAGES openstack image list
}
purge_images_delete() {
    [[ "${#_SNAP_IMAGES[@]}" -eq 0 ]] && return
    for id in "${_SNAP_IMAGES[@]}"; do
        _delete_resource "Image" "$id" openstack image delete
    done
}

purge_users() {
    # Users are global in Keystone; match by name prefix only.
    _list_orphans "Users" _SNAP_USERS openstack user list
}
purge_users_delete() {
    [[ "${#_SNAP_USERS[@]}" -eq 0 ]] && return
    for id in "${_SNAP_USERS[@]}"; do
        _delete_resource "User" "$id" openstack user delete
    done
}

purge_projects() {
    # Projects are global in Keystone; match by name prefix only.
    _list_orphans "Projects" _SNAP_PROJECTS openstack project list
}
purge_projects_delete() {
    [[ "${#_SNAP_PROJECTS[@]}" -eq 0 ]] && return
    for id in "${_SNAP_PROJECTS[@]}"; do
        _delete_resource "Project" "$id" openstack project delete
    done
}

purge_rgw() {
    if ! rgw_available; then
        log "RGW users: skipped (RGW admin credentials not configured)"
        return
    fi

    local orphans_file
    orphans_file=$(mktemp)
    if ! rgw_find_orphaned_users > "${orphans_file}"; then
        _RGW_LISTING_ERRORS=$((_RGW_LISTING_ERRORS + (RGW_LAST_FIND_ERRORS > 0 ? RGW_LAST_FIND_ERRORS : 1)))
        log "RGW users: WARNING — orphan scan failed; RGW purge is disabled for this run"
        rm -f "${orphans_file}"
        return
    fi

    _RGW_LISTING_ERRORS=$((_RGW_LISTING_ERRORS + RGW_LAST_FIND_ERRORS))

    local listed_any=0
    local uid bucket_json bucket_count object_count ownership bucket_name
    while IFS= read -r uid; do
        [[ -n "${uid}" ]] || continue
        listed_any=1
        ownership=$(rgw_classify_owner "${uid}")

        if ! bucket_json=$(rgw_list_user_buckets "${uid}"); then
            _RGW_LISTING_ERRORS=$((_RGW_LISTING_ERRORS + 1))
            log "RGW user ${uid}: WARNING — bucket listing failed; skipping from purge snapshot"
            continue
        fi

        bucket_count=$(rgw_count_buckets "${bucket_json}")
        object_count=$(rgw_count_objects "${bucket_json}")

        if [[ "${ownership}" == "rally_owned" ]]; then
            log "RGW user ${uid}: orphaned (rally-owned, ${bucket_count} buckets, ${object_count} objects)"
            TOTAL_FOUND=$((TOTAL_FOUND + 1))
            _SNAP_RGW_USERS+=("${uid}")
            while IFS= read -r bucket_name; do
                [[ -n "${bucket_name}" ]] || continue
                log "  RGW bucket ${bucket_name}"
                TOTAL_FOUND=$((TOTAL_FOUND + 1))
                _SNAP_RGW_BUCKETS+=("${uid}|${bucket_name}")
            done < <(echo "${bucket_json}" | jq -r '.[].name // empty' 2>/dev/null || true)
        else
            _RGW_SKIPPED_UNKNOWN=$((_RGW_SKIPPED_UNKNOWN + 1))
            log "RGW user ${uid}: SKIPPED (unknown owner, ${bucket_count} buckets, ${object_count} objects)"
        fi
    done < "${orphans_file}"

    rm -f "${orphans_file}"

    if [[ "${listed_any}" -eq 0 ]]; then
        log "RGW users: none"
    elif [[ "${#_SNAP_RGW_USERS[@]}" -eq 0 && "${_RGW_SKIPPED_UNKNOWN}" -gt 0 ]]; then
        log "RGW users: no Rally-owned orphans eligible for purge"
    fi
}

purge_rgw_delete() {
    if [[ "${_RGW_LISTING_ERRORS}" -gt 0 ]]; then
        RGW_PURGE_SKIPPED=true
        log "RGW purge skipped: ${_RGW_LISTING_ERRORS} RGW listing error(s) occurred during Phase 1."
        log "Investigate RGW or Keystone connectivity before retrying."
        return
    fi

    [[ "${#_SNAP_RGW_USERS[@]}" -eq 0 ]] && return

    local uid entry bucket_uid bucket_name user_failed
    for uid in "${_SNAP_RGW_USERS[@]}"; do
        user_failed=0
        for entry in "${_SNAP_RGW_BUCKETS[@]}"; do
            bucket_uid="${entry%%|*}"
            bucket_name="${entry#*|}"
            [[ "${bucket_uid}" == "${uid}" ]] || continue
            if rgw_delete_bucket "${bucket_name}"; then
                log "  RGW bucket ${bucket_name}: deleted"
                TOTAL_DELETED=$((TOTAL_DELETED + 1))
            else
                log "  RGW bucket ${bucket_name}: deletion FAILED"
                TOTAL_FAILED=$((TOTAL_FAILED + 1))
                user_failed=1
            fi
        done

        if [[ "${user_failed}" -ne 0 ]]; then
            log "  RGW user ${uid}: deletion SKIPPED because bucket deletion did not fully succeed"
            TOTAL_FAILED=$((TOTAL_FAILED + 1))
            continue
        fi

        if rgw_delete_user "${uid}"; then
            log "  RGW user ${uid}: deleted"
            TOTAL_DELETED=$((TOTAL_DELETED + 1))
        else
            log "  RGW user ${uid}: deletion FAILED"
            TOTAL_FAILED=$((TOTAL_FAILED + 1))
        fi
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
    purge_rgw

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
    log "Matching by name prefix (s_rally_*, c_rally_*) across all projects."
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
    purge_rgw_delete

    echo
    log "Done. Deleted ${TOTAL_DELETED} of ${found_in_listing} orphaned resources (${TOTAL_FAILED} failed)."

    if [[ "${TOTAL_FAILED}" -gt 0 ]]; then
        log "WARNING: ${TOTAL_FAILED} deletion(s) failed — check logs above for details."
    fi

    write_audit_log "${found_in_listing}" "${TOTAL_DELETED}" "${TOTAL_FAILED}"

    # Refresh cleanup metrics so the dashboard and Prometheus reflect the new state
    if [[ -x "/scripts/cleanup_monitor.sh" ]]; then
        log "Refreshing cleanup metrics..."
        /scripts/cleanup_monitor.sh || true
    fi

    # Exit non-zero if any deletions failed
    if $RGW_PURGE_SKIPPED; then
        exit 1
    fi
    [[ "${TOTAL_FAILED}" -gt 0 ]] && exit 1
}

main "$@"
