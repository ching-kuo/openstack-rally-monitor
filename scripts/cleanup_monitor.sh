#!/usr/bin/env bash
# ==============================================================================
# Cleanup Monitor - Orphaned Resource Detection
# ==============================================================================
# After each Rally run, checks for orphaned resources left behind by failed
# cleanup operations. Writes cleanup failure metrics for the Prometheus exporter.
#
# Distinguishes between two Rally resource naming prefixes:
#   s_rally_* — scenario-created resources (deleted by the scenario itself)
#   c_rally_* — context-created resources (deleted by the context teardown)
#
# Orphaned s_rally_* resources indicate a scenario cleanup failure — something
# went wrong during the test itself. Severity: WARNING/CRITICAL.
#
# Orphaned c_rally_* resources indicate a context teardown failure on an
# otherwise successful run (Rally marks tasks passed before context cleanup).
# Severity: INFO — monitor for accumulation but not immediately actionable.
set -euo pipefail

RESULTS_DIR="${RESULTS_DIR:-/results}"
CLEANUP_METRICS_FILE="${RESULTS_DIR}/cleanup_metrics.json"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/rgw_helpers.sh"

S_FILTER='test("^s_rally")'
C_FILTER='test("^c_rally")'

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [cleanup-monitor] $*"
}

# Count resources in a JSON array matching a jq filter expression
count_match() {
    echo "$1" | jq "[.[] | select(.Name | $2)] | length" 2>/dev/null || echo 0
}

# --------------------------------------------------------------------------
# check_resource: generic helper used by all per-resource-type checks.
# Runs the given openstack command, returns "s_count c_count" on stdout.
# Usage: check_resource openstack <subcommand> [flags...] -f json
# --------------------------------------------------------------------------
check_resource() {
    local json
    json=$("$@" -f json 2>/dev/null) || json="[]"
    echo "$(count_match "$json" "$S_FILTER") $(count_match "$json" "$C_FILTER")"
}

check_servers()         { check_resource openstack server list --all-projects; }
check_networks()        { check_resource openstack network list; }
check_volumes()         { check_resource openstack volume list --all-projects; }
check_images()          { check_resource openstack image list; }
check_users()           { check_resource openstack user list; }
check_projects()        { check_resource openstack project list; }
check_routers()         { check_resource openstack router list; }
check_security_groups() { check_resource openstack security group list; }

check_rgw_cleanup() {
    RGW_SCAN_STATUS="skipped"
    RGW_SCAN_ERRORS=0
    RGW_ORPHANED_USERS=0
    RGW_ORPHANED_BUCKETS=0
    RGW_ORPHANED_OBJECTS=0
    RGW_RALLY_OWNED_ORPHANS=0
    RGW_UNKNOWN_OWNER_ORPHANS=0

    if ! rgw_available; then
        log "RGW orphan scan skipped (RGW admin credentials not configured)"
        return 0
    fi

    RGW_SCAN_STATUS="ok"

    local orphans_file
    orphans_file=$(mktemp)
    if ! rgw_find_orphaned_users > "${orphans_file}"; then
        RGW_SCAN_STATUS="error"
        if [[ "${RGW_LAST_FIND_ERRORS}" -gt 0 ]]; then
            RGW_SCAN_ERRORS="${RGW_LAST_FIND_ERRORS}"
        else
            RGW_SCAN_ERRORS=1
        fi
        rm -f "${orphans_file}"
        log "RGW orphan scan failed during user enumeration"
        return 0
    fi

    RGW_SCAN_ERRORS="${RGW_LAST_FIND_ERRORS}"

    local uid bucket_json bucket_count object_count
    while IFS= read -r uid; do
        [[ -n "${uid}" ]] || continue

        RGW_ORPHANED_USERS=$((RGW_ORPHANED_USERS + 1))
        local ownership
        ownership=$(rgw_classify_owner "${uid}")
        if [[ "${ownership}" == "rally_owned" ]]; then
            RGW_RALLY_OWNED_ORPHANS=$((RGW_RALLY_OWNED_ORPHANS + 1))
        else
            RGW_UNKNOWN_OWNER_ORPHANS=$((RGW_UNKNOWN_OWNER_ORPHANS + 1))
        fi

        if ! bucket_json=$(rgw_list_user_buckets "${uid}"); then
            RGW_SCAN_ERRORS=$((RGW_SCAN_ERRORS + 1))
            RGW_SCAN_STATUS="error"
            log "RGW bucket listing failed for orphaned user ${uid}"
            continue
        fi

        bucket_count=$(rgw_count_buckets "${bucket_json}")
        object_count=$(rgw_count_objects "${bucket_json}")
        RGW_ORPHANED_BUCKETS=$((RGW_ORPHANED_BUCKETS + bucket_count))
        RGW_ORPHANED_OBJECTS=$((RGW_ORPHANED_OBJECTS + object_count))
    done < "${orphans_file}"

    rm -f "${orphans_file}"

    if [[ "${RGW_SCAN_ERRORS}" -gt 0 ]]; then
        RGW_SCAN_STATUS="error"
        log "RGW orphan scan degraded: users=${RGW_ORPHANED_USERS} buckets=${RGW_ORPHANED_BUCKETS} objects=${RGW_ORPHANED_OBJECTS} errors=${RGW_SCAN_ERRORS}"
    else
        log "RGW orphan scan complete: users=${RGW_ORPHANED_USERS} buckets=${RGW_ORPHANED_BUCKETS} objects=${RGW_ORPHANED_OBJECTS}"
    fi
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
main() {
    log "Starting orphaned resource check..."
    LOCKFILE="/rally/logs/rally-cleanup.lock"
    exec 200>"${LOCKFILE}"
    flock -n 200 || { log "Another cleanup check is already in progress, exiting."; exit 0; }

    local s_servers c_servers
    local s_networks c_networks
    local s_volumes c_volumes
    local s_images c_images
    local s_users c_users
    local s_projects c_projects
    local s_routers c_routers
    local s_secgroups c_secgroups

    read -r s_servers   c_servers   <<< "$(check_servers)"
    read -r s_networks  c_networks  <<< "$(check_networks)"
    read -r s_volumes   c_volumes   <<< "$(check_volumes)"
    read -r s_images    c_images    <<< "$(check_images)"
    read -r s_users     c_users     <<< "$(check_users)"
    read -r s_projects  c_projects  <<< "$(check_projects)"
    read -r s_routers   c_routers   <<< "$(check_routers)"
    read -r s_secgroups c_secgroups <<< "$(check_security_groups)"
    check_rgw_cleanup

    local s_total c_total
    s_total=$(( s_servers + s_networks + s_volumes + s_images + s_users + s_projects + s_routers + s_secgroups ))
    c_total=$(( c_servers + c_networks + c_volumes + c_images + c_users + c_projects + c_routers + c_secgroups ))

    local cleanup_failed=0
    local context_cleanup_warning=0
    [[ "${s_total}" -gt 0 ]] && cleanup_failed=1
    [[ "${c_total}" -gt 0 ]] && context_cleanup_warning=1

    if [[ "${s_total}" -gt 0 ]]; then
        log "WARNING: ${s_total} scenario-created (s_rally_*) orphaned resources!"
        log "  Servers: ${s_servers}, Networks: ${s_networks}, Volumes: ${s_volumes}"
        log "  Images: ${s_images}, Users: ${s_users}, Projects: ${s_projects}"
        log "  Routers: ${s_routers}, Security Groups: ${s_secgroups}"
    else
        log "No scenario-created (s_rally_*) orphaned resources"
    fi

    if [[ "${c_total}" -gt 0 ]]; then
        log "INFO: ${c_total} context-created (c_rally_*) orphaned resources (context teardown failed on successful run)"
        log "  Servers: ${c_servers}, Networks: ${c_networks}, Volumes: ${c_volumes}"
        log "  Images: ${c_images}, Users: ${c_users}, Projects: ${c_projects}"
        log "  Routers: ${c_routers}, Security Groups: ${c_secgroups}"
    else
        log "No context-created (c_rally_*) orphaned resources"
    fi

    CLEANUP_METRICS_TMP="${CLEANUP_METRICS_FILE}.tmp"
    cat > "${CLEANUP_METRICS_TMP}" <<EOF
{
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "cleanup_failed": ${cleanup_failed},
    "context_cleanup_warning": ${context_cleanup_warning},
    "rgw_scan_status": "${RGW_SCAN_STATUS}",
    "rgw_scan_errors": ${RGW_SCAN_ERRORS},
    "rgw_orphaned_users": ${RGW_ORPHANED_USERS},
    "rgw_orphaned_buckets": ${RGW_ORPHANED_BUCKETS},
    "rgw_orphaned_objects": ${RGW_ORPHANED_OBJECTS},
    "rgw_rally_owned_orphans": ${RGW_RALLY_OWNED_ORPHANS},
    "rgw_unknown_owner_orphans": ${RGW_UNKNOWN_OWNER_ORPHANS},
    "orphaned_resources": {
        "nova": ${s_servers},
        "neutron": $((s_networks + s_routers + s_secgroups)),
        "cinder": ${s_volumes},
        "glance": ${s_images},
        "keystone": $((s_users + s_projects))
    },
    "context_orphaned_resources": {
        "nova": ${c_servers},
        "neutron": $((c_networks + c_routers + c_secgroups)),
        "cinder": ${c_volumes},
        "glance": ${c_images},
        "keystone": $((c_users + c_projects))
    },
    "details": {
        "servers": ${s_servers},
        "networks": ${s_networks},
        "volumes": ${s_volumes},
        "images": ${s_images},
        "users": ${s_users},
        "projects": ${s_projects},
        "routers": ${s_routers},
        "security_groups": ${s_secgroups}
    },
    "context_details": {
        "servers": ${c_servers},
        "networks": ${c_networks},
        "volumes": ${c_volumes},
        "images": ${c_images},
        "users": ${c_users},
        "projects": ${c_projects},
        "routers": ${c_routers},
        "security_groups": ${c_secgroups}
    }
}
EOF
    mv "${CLEANUP_METRICS_TMP}" "${CLEANUP_METRICS_FILE}"

    log "Cleanup metrics written to ${CLEANUP_METRICS_FILE}"

    if [[ "${cleanup_failed}" -eq 1 || "${RGW_SCAN_STATUS}" == "error" ]]; then
        return 1
    fi
}

main "$@"
