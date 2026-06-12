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

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [entrypoint] $*"
}

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

# --------------------------------------------------------------------------
# Initialize
# --------------------------------------------------------------------------
mkdir -p "${RESULTS_DIR}"
mkdir -p "${RESULTS_DIR}/branding"
mkdir -p /rally/logs

# Initialize Rally's ephemeral SQLite database as the rally user.
# rally db recreate was removed from the Dockerfile build step because it ran
# as root, creating /tmp/rally.sqlite owned by root and unwritable at runtime.
log "Initializing Rally database..."
su -s /bin/bash rally -c "rally db recreate" || {
    log "ERROR: Failed to initialize Rally database"
    exit 1
}

# Detect volume ownership mismatch (can happen if an old volume pre-dates the
# pinned UID in the Dockerfile). CAP_CHOWN is dropped so we cannot fix it here.
# Log a warning with remediation steps; see CHANGELOG.md Migration Guide.
_RALLY_UID=$(id -u rally 2>/dev/null || true)
_RESULTS_UID=$(stat -c '%u' "${RESULTS_DIR}" 2>/dev/null || true)
if [[ -n "${_RALLY_UID}" && -n "${_RESULTS_UID}" && "${_RESULTS_UID}" != "${_RALLY_UID}" ]]; then
    log "WARNING: ${RESULTS_DIR} is owned by UID ${_RESULTS_UID} but rally user is UID ${_RALLY_UID}"
    log "WARNING: Scripts will fail with Permission denied. Fix with a temporary container (see CHANGELOG.md):"
    log "  docker volume ls | grep rally-results   # find your volume name"
    log "  docker run --rm -v <volume>:/results busybox chown -R ${_RALLY_UID}:0 /results"
fi
unset _RALLY_UID _RESULTS_UID

# Create a seed summary if none exists (so dashboard works on first boot).
# Generate the pending cards from RALLY_SERVICES rather than a hardcoded six so a
# trimmed deployment (e.g. a Swift-less cloud) doesn't show a phantom pending
# card forever. The normalization here -- split on commas, lowercase, strip ALL
# whitespace, drop empty segments, dedupe preserving first-seen order -- mirrors
# parse_rally_services in run_tests.sh and api_health_check.py; keep the three in
# sync. An unset/empty value (or one that normalizes to nothing) falls back to
# the same default. Seed-only-if-missing semantics are preserved.
if [[ ! -f "${RESULTS_DIR}/latest_summary.json" ]]; then
    jq -n \
        --arg raw "${RALLY_SERVICES:-keystone,nova,neutron,glance,cinder,swift}" \
        --arg default "keystone,nova,neutron,glance,cinder,swift" \
        '
        def normalize($s):
            ($s | split(",") | map(ascii_downcase | gsub("\\s"; "")) | map(select(. != ""))
             | reduce .[] as $x ([]; if any(.[]; . == $x) then . else . + [$x] end));
        (normalize($raw)) as $parsed
        | (if ($parsed | length) > 0 then $parsed else normalize($default) end) as $services
        | {
            timestamp: "waiting_for_first_run",
            services: ($services
                | map({(.): {status: "pending", duration: 0, total_iterations: 0,
                             failed_iterations: 0, sla_passed: true, scenarios: []}})
                | add)
          }' \
        > "${RESULTS_DIR}/latest_summary.json.tmp" \
        && mv "${RESULTS_DIR}/latest_summary.json.tmp" "${RESULTS_DIR}/latest_summary.json"
    log "Created seed summary"
fi

if [[ ! -f "${RESULTS_DIR}/cleanup_metrics.json" ]]; then
    cat > "${RESULTS_DIR}/cleanup_metrics.json" <<'EOF_CLEANUP'
{
    "timestamp": "waiting_for_first_run",
    "cleanup_failed": 0,
    "context_cleanup_warning": 0,
    "rgw_scan_status": "skipped",
    "rgw_scan_errors": 0,
    "rgw_orphaned_users": 0,
    "rgw_orphaned_buckets": 0,
    "rgw_orphaned_objects": 0,
    "rgw_rally_owned_orphans": 0,
    "rgw_unknown_owner_orphans": 0,
    "orphaned_resources": {},
    "context_orphaned_resources": {},
    "details": {},
    "context_details": {}
}
EOF_CLEANUP
    log "Created seed cleanup_metrics.json"
fi

# Seed dashboard JSON files into the persistent results volume if missing
if [[ ! -f "${RESULTS_DIR}/results.json" ]]; then
    jq -n \
        --slurpfile summary "${RESULTS_DIR}/latest_summary.json" \
        --slurpfile cleanup "${RESULTS_DIR}/cleanup_metrics.json" \
        '{
            summary: $summary[0],
            cleanup: $cleanup[0]
        }' \
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

# Reset run-progress state to idle on EVERY boot (not just when missing). A run
# that was SIGKILL'd by a container stop/crash would have left state="running"
# in the persistent volume; nothing else would clear it, so the dashboard would
# show a phantom "Test run in progress" forever. The running run is necessarily
# dead after a restart, so idle is always correct here. run_tests.sh flips it
# back to "running" under its flock when a real run starts.
echo '{"state":"idle","finished_at":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"}' \
    > "${RESULTS_DIR}/run_state.json.tmp" \
    && mv "${RESULTS_DIR}/run_state.json.tmp" "${RESULTS_DIR}/run_state.json"
log "Reset run_state.json to idle"

# Symlink JSON data files into /dashboard so the HTTP server can serve them.
# Recreated on every startup since /dashboard is ephemeral (not a volume).
ln -sf "${RESULTS_DIR}/results.json"            /dashboard/results.json
ln -sf "${RESULTS_DIR}/history.json"            /dashboard/history.json
ln -sf "${RESULTS_DIR}/health.json"             /dashboard/health.json
ln -sf "${RESULTS_DIR}/health_history.json"     /dashboard/health_history.json
# Operator-announcement state. The symlink is unconditional; the target file
# appears lazily on the first announce.sh post. serve.py's target.exists()
# check 404s on the dangling symlink until then.
ln -sf "${RESULTS_DIR}/announcement-state.json" /dashboard/announcement-state.json
# Run-progress state, seeded to idle just above. Same lazy/atomic pattern.
ln -sf "${RESULTS_DIR}/run_state.json"          /dashboard/run_state.json
# Expose per-run directories so the dashboard can link to the generated Rally
# HTML reports (/dashboard/runs/<TIMESTAMP>/<service>.html). serve.py gates this
# behind a strict regex + RESULTS_ROOT containment, so the symlink only widens
# what that allowlist branch explicitly permits. The source tree ships no
# dashboard/runs, so this is a plain symlink; `-n` keeps a re-run from nesting a
# link inside a pre-existing one.
ln -sfn "${RESULTS_DIR}"                        /dashboard/runs
# `ln -sfn` cannot overwrite an existing directory, so if the image was built
# with a preview `dashboard/themes/custom/` staged in the source tree (e.g.
# operator-side scaffolding), the symlink would silently turn into a child
# entry and the dashboard would serve the baked-in files instead of the
# operator's runtime branding. Remove first, then link.
rm -rf /dashboard/themes/custom
ln -s "${RESULTS_DIR}/branding"                 /dashboard/themes/custom
log "Custom theme slot: ${RESULTS_DIR}/branding"
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

# Export environment variables to a file for cron.
# Use an explicit whitelist loop with printf %q to safely quote all values,
# preventing shell injection if a variable value contains special characters.
# File contains OS_PASSWORD -- permissions kept 0640 (root:root, group-readable).
# CAP_CHOWN is dropped; use group permissions so the rally user (GID 0) can source it.
RALLY_ENV_VARS=(
    OS_AUTH_URL OS_USERNAME OS_PASSWORD OS_PROJECT_NAME
    OS_PROJECT_DOMAIN_NAME OS_USER_DOMAIN_NAME
    OS_IDENTITY_API_VERSION OS_REGION_NAME
    OS_CACERT OS_CERT OS_KEY OS_INSECURE
    OS_AUTH_TYPE OS_ENDPOINT_TYPE OS_INTERFACE
    RALLY_SCHEDULE_INTERVAL RALLY_RESULTS_RETENTION_DAYS RALLY_SERVICES
    RALLY_NOVA_FLAVOR RALLY_NOVA_IMAGE RALLY_NEUTRON_NETWORK_CIDR RALLY_DEBUG
    RGW_ADMIN_URL RGW_ACCESS_KEY RGW_SECRET_KEY RGW_REGION
    RALLY_CONFIG_DIR
    RESULTS_DIR EXPORTER_PORT DASHBOARD_PORT HEALTH_CHECK_INTERVAL
    HEALTH_LATENCY_WARN_MS
    UPTIME_WINDOW_DAYS PROVENANCE_RETENTION_DAYS
    NOTIFY_WEBHOOK_URL NOTIFY_FORMAT NOTIFY_DASHBOARD_URL
)
{
    for k in "${RALLY_ENV_VARS[@]}"; do
        if [[ -n "${!k+x}" ]]; then
            printf 'export %s=%q\n' "$k" "${!k}"
        fi
    done
} > /rally/rally_env.tmp && mv /rally/rally_env.tmp /rally/rally_env
chmod 0640 /rally/rally_env

# Create cron jobs.
# cron.d format: <schedule> <user> <command>
# The command sources /rally/rally_env (written by entrypoint, mode 0640) to
# inject OpenStack credentials; set -a / set +a export all sourced variables.
# mirror_job_logs.sh appends to the retained file log and forwards the same
# stream to PID 1 stdout so docker logs shows scheduled job output as well.
cat > /etc/cron.d/rally-tests <<EOF_CRON
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
${CRON_SCHEDULE} rally set -a; . /rally/rally_env; set +a; /scripts/mirror_job_logs.sh /rally/logs/rally-tests.log /scripts/run_tests.sh
${HEALTH_CRON} rally set -a; . /rally/rally_env; set +a; /scripts/mirror_job_logs.sh /rally/logs/health-check.log /scripts/health_check.sh
0 0 * * * root logrotate /etc/logrotate.d/rally-monitor > /dev/null 2>&1
EOF_CRON
chmod 0644 /etc/cron.d/rally-tests

# Create log files before cron writes to them.
# Owned by root:root, group-writable so the rally user (GID 0) can append via cron redirection.
touch /rally/logs/rally-tests.log  && chmod 0660 /rally/logs/rally-tests.log
touch /rally/logs/health-check.log && chmod 0660 /rally/logs/health-check.log

# Forward cron job logs into `docker logs` (cron jobs run as rally and can't
# write to PID 1's stdout). `-F` handles the `copytruncate` rotation used in
# docker/logrotate.conf — re-reading from offset 0 when the file shrinks.
tail -F -n 0 -q /rally/logs/rally-tests.log /rally/logs/health-check.log &
LOG_TAIL_PID=$!

# Start cron daemon
cron

log "Rally tests scheduled:  ${CRON_SCHEDULE} (every ${SCHEDULE_INTERVAL} min)"
log "Health checks scheduled: ${HEALTH_CRON} (every ${HEALTH_CHECK_INTERVAL} min)"

# --------------------------------------------------------------------------
# Run initial test if no results exist
# --------------------------------------------------------------------------
if [[ "$(jq -r '.timestamp' "${RESULTS_DIR}/latest_summary.json")" == "waiting_for_first_run" ]]; then
    log "Running initial test suite..."
    su -s /bin/bash rally -c "/scripts/mirror_job_logs.sh /rally/logs/rally-tests.log /scripts/run_tests.sh" &
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
trap 'log "Shutting down..."; kill ${EXPORTER_PID} ${DASHBOARD_PID} ${LOG_TAIL_PID} 2>/dev/null; exit 0' SIGTERM SIGINT

# Wait for any child to exit. Tailer death is fatal — otherwise `docker logs`
# would silently go dark.
wait -n ${EXPORTER_PID} ${DASHBOARD_PID} ${LOG_TAIL_PID} 2>/dev/null || true
log "A child process exited, shutting down..."
kill ${EXPORTER_PID} ${DASHBOARD_PID} ${LOG_TAIL_PID} 2>/dev/null || true
exit 1
