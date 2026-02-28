#!/usr/bin/env bash
# ==============================================================================
# Docker Entrypoint - Rally Monitor
# ==============================================================================
# Starts the Prometheus exporter, the dashboard server, and the cron-based
# test runner in a single container.
set -euo pipefail

RESULTS_DIR="${RESULTS_DIR:-/results}"
EXPORTER_PORT="${EXPORTER_PORT:-9101}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8080}"
SCHEDULE_INTERVAL="${RALLY_SCHEDULE_INTERVAL:-240}"
HEALTH_CHECK_INTERVAL="${HEALTH_CHECK_INTERVAL:-15}"

# Convert a minute-based interval to a valid cron schedule expression.
# Intervals >= 60 that are exact multiples of 60 use the hours field.
make_cron_schedule() {
    local mins="$1"
    if (( mins < 1 )); then
        log "ERROR: Schedule interval must be >= 1 minute, got ${mins}"
        exit 1
    fi
    if (( mins > 1440 )); then
        log "ERROR: Schedule interval must be <= 1440 minutes (24h), got ${mins}"
        exit 1
    fi
    if (( mins >= 60 )); then
        local hrs
        if (( mins % 60 == 0 )); then
            hrs=$(( mins / 60 ))
        else
            local rounded=$(( ((mins + 59) / 60) * 60 ))
            log "WARNING: Interval ${mins}m is not a multiple of 60. Rounding up to ${rounded}m for cron."
            hrs=$(( rounded / 60 ))
        fi
        echo "0 */${hrs} * * *"
    else
        echo "*/${mins} * * * *"
    fi
}

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [entrypoint] $*"
}

# --------------------------------------------------------------------------
# Initialize
# --------------------------------------------------------------------------
mkdir -p "${RESULTS_DIR}"
mkdir -p /rally/logs
# Note: /results ownership is set in the Dockerfile image layer (for new volumes)
# and via one-time migration for existing volumes. See CHANGELOG.md for instructions.

# Create a seed summary if none exists (so dashboard works on first boot)
if [[ ! -f "${RESULTS_DIR}/latest_summary.json" ]]; then
    cat > "${RESULTS_DIR}/latest_summary.json" <<'EOF_SUMMARY'
{
    "timestamp": "waiting_for_first_run",
    "services": {
        "keystone": {"status": "pending", "duration": 0, "total_iterations": 0, "failed_iterations": 0, "sla_passed": true, "scenarios": []},
        "nova": {"status": "pending", "duration": 0, "total_iterations": 0, "failed_iterations": 0, "sla_passed": true, "scenarios": []},
        "neutron": {"status": "pending", "duration": 0, "total_iterations": 0, "failed_iterations": 0, "sla_passed": true, "scenarios": []},
        "glance": {"status": "pending", "duration": 0, "total_iterations": 0, "failed_iterations": 0, "sla_passed": true, "scenarios": []},
        "cinder": {"status": "pending", "duration": 0, "total_iterations": 0, "failed_iterations": 0, "sla_passed": true, "scenarios": []},
        "swift": {"status": "pending", "duration": 0, "total_iterations": 0, "failed_iterations": 0, "sla_passed": true, "scenarios": []}
    }
}
EOF_SUMMARY
    log "Created seed summary"
fi

# Seed dashboard JSON files into the persistent results volume if missing
if [[ ! -f "${RESULTS_DIR}/results.json" ]]; then
    jq -n \
        --slurpfile summary "${RESULTS_DIR}/latest_summary.json" \
        '{summary: $summary[0], cleanup: {cleanup_failed: 0, orphaned_resources: {}, details: {}}}' \
        > "${RESULTS_DIR}/results.json"
    log "Created seed results.json"
fi

if [[ ! -f "${RESULTS_DIR}/history.json" ]]; then
    echo '{"runs": []}' > "${RESULTS_DIR}/history.json"
    log "Created seed history.json"
fi

if [[ ! -f "${RESULTS_DIR}/health.json" ]]; then
    echo '{"timestamp":"waiting","overall":"unknown","services":{}}' \
        > "${RESULTS_DIR}/health.json"
    log "Created seed health.json"
fi

if [[ ! -f "${RESULTS_DIR}/health_history.json" ]]; then
    echo '{"checks":[]}' > "${RESULTS_DIR}/health_history.json"
    log "Created seed health_history.json"
fi

# Symlink JSON data files into /dashboard so the HTTP server can serve them.
# Recreated on every startup since /dashboard is ephemeral (not a volume).
ln -sf "${RESULTS_DIR}/results.json"         /dashboard/results.json
ln -sf "${RESULTS_DIR}/history.json"         /dashboard/history.json
ln -sf "${RESULTS_DIR}/health.json"          /dashboard/health.json
ln -sf "${RESULTS_DIR}/health_history.json"  /dashboard/health_history.json
log "Dashboard symlinks updated"

# --------------------------------------------------------------------------
# Start Prometheus Exporter (background)
# --------------------------------------------------------------------------
log "Starting Prometheus exporter on port ${EXPORTER_PORT}..."
su -s /bin/bash rally -c "cd /exporter && gunicorn -w 1 -b 0.0.0.0:${EXPORTER_PORT} --timeout 30 rally_exporter:app" &
EXPORTER_PID=$!
log "Exporter started (PID: ${EXPORTER_PID})"

# --------------------------------------------------------------------------
# Start Dashboard Server (background)
# --------------------------------------------------------------------------
log "Starting dashboard on port ${DASHBOARD_PORT}..."
su -s /bin/bash rally -c "cd /dashboard && python serve.py ${DASHBOARD_PORT}" &
DASHBOARD_PID=$!
log "Dashboard started (PID: ${DASHBOARD_PID})"

# --------------------------------------------------------------------------
# Setup cron schedule
# --------------------------------------------------------------------------
log "Setting up schedules..."
CRON_SCHEDULE=$(make_cron_schedule "${SCHEDULE_INTERVAL}")
HEALTH_CRON=$(make_cron_schedule "${HEALTH_CHECK_INTERVAL}")

# Export all environment variables to a file for cron
# Use `set -a` format so every var is auto-exported when sourced
env | grep -E '^(OS_|RALLY_|RESULTS_|EXPORTER_|DASHBOARD_|HEALTH_)' \
    | sed 's/^/export /' > /rally/rally_env.tmp && mv /rally/rally_env.tmp /rally/rally_env
# Restrict permissions: file contains OS_PASSWORD.
# Owned by root:root, group-readable so the rally user (GID 0) can source it in cron.
# CAP_CHOWN is dropped; use group permissions instead of chown.
chmod 0640 /rally/rally_env

# Create cron jobs
cat > /etc/cron.d/rally-tests <<EOF_CRON
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
${CRON_SCHEDULE} rally set -a; . /rally/rally_env; set +a; /scripts/run_tests.sh >> /rally/logs/rally-tests.log 2>&1
${HEALTH_CRON} rally set -a; . /rally/rally_env; set +a; /scripts/health_check.sh >> /rally/logs/health-check.log 2>&1
0 0 * * * root logrotate /etc/logrotate.d/rally-monitor > /dev/null 2>&1
EOF_CRON
chmod 0644 /etc/cron.d/rally-tests

# Create log files before cron writes to them.
# Owned by root:root, group-writable so the rally user (GID 0) can append via cron redirection.
touch /rally/logs/rally-tests.log  && chmod 0660 /rally/logs/rally-tests.log
touch /rally/logs/health-check.log && chmod 0660 /rally/logs/health-check.log

# Start cron daemon
cron

log "Rally tests scheduled:  ${CRON_SCHEDULE} (every ${SCHEDULE_INTERVAL} min)"
log "Health checks scheduled: ${HEALTH_CRON} (every ${HEALTH_CHECK_INTERVAL} min)"

# --------------------------------------------------------------------------
# Run initial test if no results exist
# --------------------------------------------------------------------------
if [[ "$(cat ${RESULTS_DIR}/latest_summary.json | jq -r '.timestamp')" == "waiting_for_first_run" ]]; then
    log "Running initial test suite..."
    su -s /bin/bash rally -c "/scripts/run_tests.sh >> /rally/logs/rally-tests.log 2>&1" &
    log "Initial test started in background"
fi

# --------------------------------------------------------------------------
# Keep container alive, monitor child processes
# --------------------------------------------------------------------------
log "Rally Monitor is running"
log "  - Exporter: http://0.0.0.0:${EXPORTER_PORT}/metrics"
log "  - Dashboard: http://0.0.0.0:${DASHBOARD_PORT}"
log "  - Rally tests:   every ${SCHEDULE_INTERVAL} min (${CRON_SCHEDULE})"
log "  - Health checks: every ${HEALTH_CHECK_INTERVAL} min (${HEALTH_CRON})"

# Trap signals for graceful shutdown
trap 'log "Shutting down..."; kill ${EXPORTER_PID} ${DASHBOARD_PID} 2>/dev/null; exit 0' SIGTERM SIGINT

# Wait for any child to exit
wait -n ${EXPORTER_PID} ${DASHBOARD_PID} 2>/dev/null || true
log "A child process exited, shutting down..."
kill ${EXPORTER_PID} ${DASHBOARD_PID} 2>/dev/null || true
exit 1
