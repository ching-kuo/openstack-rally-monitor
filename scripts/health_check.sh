#!/usr/bin/env bash
# ==============================================================================
# Health Check - Non-destructive API availability checks
# ==============================================================================
# Runs read-only OpenStack API calls to verify each service endpoint is
# reachable and responding. No resources are created or modified.
# Results are written to /results/health.json for the dashboard.
set -euo pipefail

RESULTS_DIR="${RESULTS_DIR:-/results}"
HEALTH_FILE="${RESULTS_DIR}/health.json"
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [health-check] $*" >&2; }

# Run a single check and return a JSON snippet {status, latency_ms, checked_at}
check() {
    local svc="$1"; shift
    local t0 t1 ms status
    t0=$(date +%s%3N)
    if "$@" > /dev/null 2>&1; then status="up"; else status="down"; fi
    t1=$(date +%s%3N)
    ms=$(( t1 - t0 ))
    log "  ${svc}: ${status} (${ms}ms)"
    jq -n --arg s "${status}" --argjson m "${ms}" --arg t "${TIMESTAMP}" \
        '{status: $s, latency_ms: $m, checked_at: $t}'
}

log "Starting health checks..."
LOCKFILE="/tmp/rally-health.lock"
exec 200>"${LOCKFILE}"
flock -n 200 || { log "Another health check is already in progress, exiting."; exit 0; }

keystone_r=$(check keystone openstack token issue -f value -c id)
nova_r=$(    check nova     openstack server list     --limit 1)
neutron_r=$( check neutron  openstack network list)
glance_r=$(  check glance   openstack image list      --limit 1)
cinder_r=$(  check cinder   openstack volume list     --limit 1)
swift_r=$(   check swift    openstack object store account show)

jq -n \
    --arg     ts       "${TIMESTAMP}" \
    --argjson keystone "${keystone_r}" \
    --argjson nova     "${nova_r}" \
    --argjson neutron  "${neutron_r}" \
    --argjson glance   "${glance_r}" \
    --argjson cinder   "${cinder_r}" \
    --argjson swift    "${swift_r}" \
    '{
        timestamp: $ts,
        overall: (
            [$keystone, $nova, $neutron, $glance, $cinder, $swift]
            | map(.status) | if any(. == "down") then "down" else "up" end
        ),
        services: {
            keystone: $keystone,
            nova:     $nova,
            neutron:  $neutron,
            glance:   $glance,
            cinder:   $cinder,
            swift:    $swift
        }
    }' > "${HEALTH_FILE}.tmp" && mv "${HEALTH_FILE}.tmp" "${HEALTH_FILE}"

OVERALL=$(jq -r '.overall' "${HEALTH_FILE}")
log "Health check complete. Overall: ${OVERALL}"

# Append to rolling health history (keep last 672 entries = 7 days at 15-min intervals)
HISTORY_FILE="${RESULTS_DIR}/health_history.json"
if [ -f "${HISTORY_FILE}" ]; then
    jq --slurpfile cur "${HEALTH_FILE}" \
        '.checks += $cur | .checks = .checks[-672:]' \
        "${HISTORY_FILE}" > "${HISTORY_FILE}.tmp" \
        && mv "${HISTORY_FILE}.tmp" "${HISTORY_FILE}"
else
    jq -n --slurpfile cur "${HEALTH_FILE}" '{checks: $cur}' > "${HISTORY_FILE}"
fi
log "History updated ($(jq '.checks | length' "${HISTORY_FILE}") entries)"
