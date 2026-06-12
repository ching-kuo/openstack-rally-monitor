#!/usr/bin/env bash
# ==============================================================================
# Health Check - Non-destructive API availability checks (cron wrapper)
# ==============================================================================
# Runs read-only OpenStack API calls to verify each service endpoint is
# reachable and responding. No resources are created or modified.
#
# The actual checking is done by api_health_check.py, which authenticates ONCE
# (one openstacksdk session, one Keystone token) and times one read-only GET
# per service. This script is the cron wrapper: it holds the lock, writes
# health.json atomically, appends to the rolling history via the shipped jq
# filter, and logs per-service status. See CLAUDE.md "Health check refactor".
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_DIR="${RESULTS_DIR:-/results}"
HEALTH_FILE="${RESULTS_DIR}/health.json"
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [health-check] $*" >&2; }

log "Starting health checks..."
LOCKFILE="/rally/logs/rally-health.lock"
exec 200>"${LOCKFILE}"
flock -n 200 || { log "Another health check is already in progress, exiting."; exit 0; }

# Run the single-session checker. It prints the full health.json document on
# stdout and exits 0 even when services are down (producing the report IS the
# success); a nonzero exit means an internal error (e.g. openstacksdk missing),
# in which case we must still publish an all-down document so monitoring never
# silently stops. The output is validated with `jq empty` before publishing so
# stdout pollution (a stray banner from a dependency) can never replace
# health.json with an unparseable document — consumers (the history filter,
# the exporter, the dashboard) all assume valid JSON.
if python3 "${SCRIPT_DIR}/api_health_check.py" > "${HEALTH_FILE}.tmp" 2>>/dev/null \
        && jq empty "${HEALTH_FILE}.tmp" 2>/dev/null; then
    mv "${HEALTH_FILE}.tmp" "${HEALTH_FILE}"
else
    log "ERROR: api_health_check.py failed or emitted invalid JSON; writing all-down health document."
    rm -f "${HEALTH_FILE}.tmp"
    # The all-down set honors RALLY_SERVICES like the checker itself (keystone
    # always present), so a trimmed deployment never gains phantom down
    # services that would depress API uptime and create stale Prometheus label
    # series. The shipped filter file is used (rather than an inline program)
    # so tests exercise exactly this code path; normalization rules and the
    # keep-in-sync contract are documented in health_fallback_filter.jq.
    jq -n -f "${SCRIPT_DIR}/health_fallback_filter.jq" \
          --arg ts "${TIMESTAMP}" \
          --arg raw "${RALLY_SERVICES:-}" \
          --arg default "keystone,nova,neutron,glance,cinder,swift" \
        > "${HEALTH_FILE}.tmp" && mv "${HEALTH_FILE}.tmp" "${HEALTH_FILE}"
fi

# Log per-service status from the produced JSON so the log stays as informative
# as the old per-check log lines (status + latency for every service).
while IFS=$'\t' read -r svc status ms; do
    log "  ${svc}: ${status} (${ms}ms)"
done < <(jq -r '.services | to_entries[] | "\(.key)\t\(.value.status)\t\(.value.latency_ms)"' "${HEALTH_FILE}")

OVERALL=$(jq -r '.overall' "${HEALTH_FILE}")
log "Health check complete. Overall: ${OVERALL}"

# Append to rolling health history. The cap is sized to cover the uptime
# window at the configured check interval, floored at 672 entries (7 days at
# 15-min intervals) so the dashboard health timeline never loses data.
HISTORY_FILE="${RESULTS_DIR}/health_history.json"
UPTIME_WINDOW_DAYS="${UPTIME_WINDOW_DAYS:-30}"
MAX_ENTRIES=$(( UPTIME_WINDOW_DAYS * 24 * 60 / ${HEALTH_CHECK_INTERVAL:-15} ))
if (( MAX_ENTRIES < 672 )); then MAX_ENTRIES=672; fi

[ -f "${HISTORY_FILE}" ] || echo '{"checks": []}' > "${HISTORY_FILE}"
jq --slurpfile cur "${HEALTH_FILE}" \
    --argjson max "${MAX_ENTRIES}" \
    --argjson days "${UPTIME_WINDOW_DAYS}" \
    -f "${SCRIPT_DIR}/health_history_filter.jq" \
    "${HISTORY_FILE}" > "${HISTORY_FILE}.tmp" \
    && mv "${HISTORY_FILE}.tmp" "${HISTORY_FILE}"
log "History updated ($(jq '.checks | length' "${HISTORY_FILE}") entries)"
