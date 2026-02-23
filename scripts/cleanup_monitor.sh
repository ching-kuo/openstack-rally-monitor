#!/usr/bin/env bash
# ==============================================================================
# Cleanup Monitor - Orphaned Resource Detection
# ==============================================================================
# After each Rally run, checks for orphaned resources left behind by failed
# cleanup operations. Writes cleanup failure metrics for the Prometheus exporter.
set -euo pipefail

RESULTS_DIR="${RESULTS_DIR:-/results}"
CLEANUP_METRICS_FILE="${RESULTS_DIR}/cleanup_metrics.json"
SUMMARY_FILE="${1:-${RESULTS_DIR}/latest_summary.json}"

# Rally resource naming prefix
RALLY_PREFIX="s_rally"

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [cleanup-monitor] $*"
}

# --------------------------------------------------------------------------
# Check for orphaned resources per service
# --------------------------------------------------------------------------
check_orphaned_servers() {
    local count=0
    count=$(openstack server list --all-projects -f json 2>/dev/null \
        | jq "[.[] | select(.Name | startswith(\"${RALLY_PREFIX}\"))] | length" 2>/dev/null) || count=0
    echo "${count}"
}

check_orphaned_networks() {
    local count=0
    count=$(openstack network list -f json 2>/dev/null \
        | jq "[.[] | select(.Name | startswith(\"${RALLY_PREFIX}\"))] | length" 2>/dev/null) || count=0
    echo "${count}"
}

check_orphaned_volumes() {
    local count=0
    count=$(openstack volume list --all-projects -f json 2>/dev/null \
        | jq "[.[] | select(.Name | startswith(\"${RALLY_PREFIX}\"))] | length" 2>/dev/null) || count=0
    echo "${count}"
}

check_orphaned_images() {
    local count=0
    count=$(openstack image list -f json 2>/dev/null \
        | jq "[.[] | select(.Name | startswith(\"${RALLY_PREFIX}\"))] | length" 2>/dev/null) || count=0
    echo "${count}"
}

check_orphaned_users() {
    local count=0
    count=$(openstack user list -f json 2>/dev/null \
        | jq "[.[] | select(.Name | startswith(\"${RALLY_PREFIX}\"))] | length" 2>/dev/null) || count=0
    echo "${count}"
}

check_orphaned_projects() {
    local count=0
    count=$(openstack project list -f json 2>/dev/null \
        | jq "[.[] | select(.Name | startswith(\"${RALLY_PREFIX}\"))] | length" 2>/dev/null) || count=0
    echo "${count}"
}

check_orphaned_routers() {
    local count=0
    count=$(openstack router list -f json 2>/dev/null \
        | jq "[.[] | select(.Name | startswith(\"${RALLY_PREFIX}\"))] | length" 2>/dev/null) || count=0
    echo "${count}"
}

check_orphaned_security_groups() {
    local count=0
    count=$(openstack security group list -f json 2>/dev/null \
        | jq "[.[] | select(.Name | startswith(\"${RALLY_PREFIX}\"))] | length" 2>/dev/null) || count=0
    echo "${count}"
}

# --------------------------------------------------------------------------
# Main cleanup check
# --------------------------------------------------------------------------
main() {
    log "Starting orphaned resource check..."

    local orphaned_servers orphaned_networks orphaned_volumes orphaned_images
    local orphaned_users orphaned_projects orphaned_routers orphaned_secgroups
    local total_orphaned cleanup_failed

    orphaned_servers=$(check_orphaned_servers)
    orphaned_networks=$(check_orphaned_networks)
    orphaned_volumes=$(check_orphaned_volumes)
    orphaned_images=$(check_orphaned_images)
    orphaned_users=$(check_orphaned_users)
    orphaned_projects=$(check_orphaned_projects)
    orphaned_routers=$(check_orphaned_routers)
    orphaned_secgroups=$(check_orphaned_security_groups)

    total_orphaned=$((orphaned_servers + orphaned_networks + orphaned_volumes + orphaned_images + orphaned_users + orphaned_projects + orphaned_routers + orphaned_secgroups))

    if [[ "${total_orphaned}" -gt 0 ]]; then
        cleanup_failed=1
        log "WARNING: Found ${total_orphaned} orphaned resources!"
        log "  Servers: ${orphaned_servers}"
        log "  Networks: ${orphaned_networks}"
        log "  Volumes: ${orphaned_volumes}"
        log "  Images: ${orphaned_images}"
        log "  Users: ${orphaned_users}"
        log "  Projects: ${orphaned_projects}"
        log "  Routers: ${orphaned_routers}"
        log "  Security Groups: ${orphaned_secgroups}"
    else
        cleanup_failed=0
        log "No orphaned resources found"
    fi

    # Write cleanup metrics for the Prometheus exporter
    cat > "${CLEANUP_METRICS_FILE}" <<EOF
{
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "cleanup_failed": ${cleanup_failed},
    "orphaned_resources": {
        "nova": ${orphaned_servers},
        "neutron": $((orphaned_networks + orphaned_routers + orphaned_secgroups)),
        "cinder": ${orphaned_volumes},
        "glance": ${orphaned_images},
        "keystone": $((orphaned_users + orphaned_projects))
    },
    "details": {
        "servers": ${orphaned_servers},
        "networks": ${orphaned_networks},
        "volumes": ${orphaned_volumes},
        "images": ${orphaned_images},
        "users": ${orphaned_users},
        "projects": ${orphaned_projects},
        "routers": ${orphaned_routers},
        "security_groups": ${orphaned_secgroups}
    }
}
EOF

    log "Cleanup metrics written to ${CLEANUP_METRICS_FILE}"

    if [[ "${cleanup_failed}" -eq 1 ]]; then
        return 1
    fi
}

main "$@"
