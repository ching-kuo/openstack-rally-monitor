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
SCHEDULE_INTERVAL="${RALLY_SCHEDULE_INTERVAL:-60}"

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [entrypoint] $*"
}

# --------------------------------------------------------------------------
# Initialize
# --------------------------------------------------------------------------
mkdir -p "${RESULTS_DIR}"

# Create a seed summary if none exists (so dashboard works on first boot)
if [[ ! -f "${RESULTS_DIR}/latest_summary.json" ]]; then
    cat > "${RESULTS_DIR}/latest_summary.json" <<'EOF'
{
    "timestamp": "waiting_for_first_run",
    "services": {
        "keystone": {"status": "pending", "duration": 0, "total_iterations": 0, "failed_iterations": 0, "sla_passed": true, "scenarios": []},
        "nova": {"status": "pending", "duration": 0, "total_iterations": 0, "failed_iterations": 0, "sla_passed": true, "scenarios": []},
        "neutron": {"status": "pending", "duration": 0, "total_iterations": 0, "failed_iterations": 0, "sla_passed": true, "scenarios": []},
        "glance": {"status": "pending", "duration": 0, "total_iterations": 0, "failed_iterations": 0, "sla_passed": true, "scenarios": []},
        "cinder": {"status": "pending", "duration": 0, "total_iterations": 0, "failed_iterations": 0, "sla_passed": true, "scenarios": []},
        "swift": {"status": "pending", "duration": 0, "total_iterations": 0, "failed_iterations": 0, "sla_passed": true, "scenarios": []},
        "placement": {"status": "pending", "duration": 0, "total_iterations": 0, "failed_iterations": 0, "sla_passed": true, "scenarios": []}
    }
}
EOF
    log "Created seed summary"
fi

# Seed dashboard static JSON files if they don't exist yet
if [[ ! -f /dashboard/results.json ]]; then
    jq -n \
        --slurpfile summary "${RESULTS_DIR}/latest_summary.json" \
        '{summary: $summary[0], cleanup: {cleanup_failed: 0, orphaned_resources: {}, details: {}}}' \
        > /dashboard/results.json
    log "Created seed results.json"
fi

if [[ ! -f /dashboard/history.json ]]; then
    echo '{"runs": []}' > /dashboard/history.json
    log "Created seed history.json"
fi

# --------------------------------------------------------------------------
# Start Prometheus Exporter (background)
# --------------------------------------------------------------------------
log "Starting Prometheus exporter on port ${EXPORTER_PORT}..."
cd /exporter && python rally_exporter.py &
EXPORTER_PID=$!
log "Exporter started (PID: ${EXPORTER_PID})"

# --------------------------------------------------------------------------
# Start Dashboard Server (background)
# --------------------------------------------------------------------------
log "Starting dashboard on port ${DASHBOARD_PORT}..."
cd /dashboard && python -m http.server "${DASHBOARD_PORT}" --bind 0.0.0.0 &
DASHBOARD_PID=$!
log "Dashboard started (PID: ${DASHBOARD_PID})"

# --------------------------------------------------------------------------
# Setup cron schedule
# --------------------------------------------------------------------------
log "Setting up test schedule (every ${SCHEDULE_INTERVAL} minutes)..."
CRON_SCHEDULE="*/${SCHEDULE_INTERVAL} * * * *"

# Export all environment variables to a file for cron
# Use `set -a` format so every var is auto-exported when sourced
env | grep -E '^(OS_|RALLY_|RESULTS_|EXPORTER_|DASHBOARD_)' | sed 's/^/export /' > /etc/rally_env 2>/dev/null || true
# Restrict permissions: file contains OS_PASSWORD
chmod 0600 /etc/rally_env

# Create cron job
# Use `set -a; source /etc/rally_env; set +a` to ensure all vars are exported
# into the environment of the child process (run_tests.sh)
cat > /etc/cron.d/rally-tests <<EOF
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
${CRON_SCHEDULE} root set -a; . /etc/rally_env; set +a; /scripts/run_tests.sh >> /var/log/rally-tests.log 2>&1
EOF
chmod 0644 /etc/cron.d/rally-tests

# Create log file with restricted permissions before cron writes to it
touch /var/log/rally-tests.log
chmod 0600 /var/log/rally-tests.log

# Start cron daemon
cron

log "Cron scheduled: ${CRON_SCHEDULE}"

# --------------------------------------------------------------------------
# Run initial test if no results exist
# --------------------------------------------------------------------------
if [[ "$(cat ${RESULTS_DIR}/latest_summary.json | jq -r '.timestamp')" == "waiting_for_first_run" ]]; then
    log "Running initial test suite..."
    /scripts/run_tests.sh >> /var/log/rally-tests.log 2>&1 &
    log "Initial test started in background"
fi

# --------------------------------------------------------------------------
# Keep container alive, monitor child processes
# --------------------------------------------------------------------------
log "Rally Monitor is running"
log "  - Exporter: http://0.0.0.0:${EXPORTER_PORT}/metrics"
log "  - Dashboard: http://0.0.0.0:${DASHBOARD_PORT}"
log "  - Schedule: every ${SCHEDULE_INTERVAL} minutes"

# Trap signals for graceful shutdown
trap 'log "Shutting down..."; kill ${EXPORTER_PID} ${DASHBOARD_PID} 2>/dev/null; exit 0' SIGTERM SIGINT

# Wait for any child to exit
wait -n ${EXPORTER_PID} ${DASHBOARD_PID} 2>/dev/null || true
log "A child process exited, shutting down..."
kill ${EXPORTER_PID} ${DASHBOARD_PID} 2>/dev/null || true
exit 1
