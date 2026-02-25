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
SUMMARY_FILE="${1:-${RESULTS_DIR}/latest_summary.json}"

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
# Check functions
# Each fetches the resource list once and returns "s_count c_count"
# --------------------------------------------------------------------------
check_servers() {
    local json
    json=$(openstack server list --all-projects -f json 2>/dev/null) || json="[]"
    echo "$(count_match "$json" "$S_FILTER") $(count_match "$json" "$C_FILTER")"
}

check_networks() {
    local json
    json=$(openstack network list -f json 2>/dev/null) || json="[]"
    echo "$(count_match "$json" "$S_FILTER") $(count_match "$json" "$C_FILTER")"
}

check_volumes() {
    local json
    json=$(openstack volume list --all-projects -f json 2>/dev/null) || json="[]"
    echo "$(count_match "$json" "$S_FILTER") $(count_match "$json" "$C_FILTER")"
}

check_images() {
    local json
    json=$(openstack image list -f json 2>/dev/null) || json="[]"
    echo "$(count_match "$json" "$S_FILTER") $(count_match "$json" "$C_FILTER")"
}

check_users() {
    local json
    json=$(openstack user list -f json 2>/dev/null) || json="[]"
    echo "$(count_match "$json" "$S_FILTER") $(count_match "$json" "$C_FILTER")"
}

check_projects() {
    local json
    json=$(openstack project list -f json 2>/dev/null) || json="[]"
    echo "$(count_match "$json" "$S_FILTER") $(count_match "$json" "$C_FILTER")"
}

check_routers() {
    local json
    json=$(openstack router list -f json 2>/dev/null) || json="[]"
    echo "$(count_match "$json" "$S_FILTER") $(count_match "$json" "$C_FILTER")"
}

check_security_groups() {
    local json
    json=$(openstack security group list -f json 2>/dev/null) || json="[]"
    echo "$(count_match "$json" "$S_FILTER") $(count_match "$json" "$C_FILTER")"
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
main() {
    log "Starting orphaned resource check..."

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

    cat > "${CLEANUP_METRICS_FILE}" <<EOF
{
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "cleanup_failed": ${cleanup_failed},
    "context_cleanup_warning": ${context_cleanup_warning},
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

    log "Cleanup metrics written to ${CLEANUP_METRICS_FILE}"

    if [[ "${cleanup_failed}" -eq 1 ]]; then
        return 1
    fi
}

main "$@"
