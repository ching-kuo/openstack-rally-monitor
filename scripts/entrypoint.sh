#!/usr/bin/env bash
# ==============================================================================
# Docker Entrypoint - Rally Monitor
# ==============================================================================
# Starts the Prometheus exporter, the dashboard server, and the cron-based
# test runner in a single container.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_DIR="${RESULTS_DIR:-/results}"
EXPORTER_PORT="${EXPORTER_PORT:-9101}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8080}"
SCHEDULE_INTERVAL="${RALLY_SCHEDULE_INTERVAL:-240}"
HEALTH_CHECK_INTERVAL="${HEALTH_CHECK_INTERVAL:-15}"

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [entrypoint] $*"
}

source "${SCRIPT_DIR}/entrypoint_config.sh"

# Validate environment-derived numeric values before initialization has any
# side effects. Downstream shell command strings receive normalized digits only.
SCHEDULE_INTERVAL=$(normalize_interval "${SCHEDULE_INTERVAL}" "RALLY_SCHEDULE_INTERVAL")
HEALTH_CHECK_INTERVAL=$(normalize_interval "${HEALTH_CHECK_INTERVAL}" "HEALTH_CHECK_INTERVAL")
# Normalized once and exported so both the exporter (started below) and cron's
# health_check.sh (via rally_env) read the same value. KEEP IN SYNC: the 300
# default and 1..3600 bounds are repeated as fallbacks for runs without this
# entrypoint in health_check.sh and exporter/health_metrics.py::parse_check_timeout.
HEALTH_CHECK_TIMEOUT=$(normalize_timeout "${HEALTH_CHECK_TIMEOUT:-300}" "HEALTH_CHECK_TIMEOUT")
export HEALTH_CHECK_TIMEOUT
EXPORTER_PORT=$(normalize_port "${EXPORTER_PORT}" "EXPORTER_PORT")
DASHBOARD_PORT=$(normalize_port "${DASHBOARD_PORT}" "DASHBOARD_PORT")
# Day counts: normalized here so a malformed value ("30d") cannot break
# find/date/jq arithmetic mid-run, but never fatal -- values that ran before
# validation existed must not crash-loop the container: out-of-range values
# are clamped and a non-number falls back to the default, both with a
# WARNING (the same policy health_check.sh and run_tests.sh apply). Exported
# so cron (via rally_env) and the initial run see the normalized digits.
UPTIME_WINDOW_DAYS=$(or_default normalize_days "${UPTIME_WINDOW_DAYS-}" 30 UPTIME_WINDOW_DAYS)
RALLY_RESULTS_RETENTION_DAYS=$(or_default normalize_retention_days "${RALLY_RESULTS_RETENTION_DAYS-}" 7 RALLY_RESULTS_RETENTION_DAYS)
PROVENANCE_RETENTION_DAYS=$(or_default normalize_provenance_days "${PROVENANCE_RETENTION_DAYS-}" 90 PROVENANCE_RETENTION_DAYS)
export UPTIME_WINDOW_DAYS RALLY_RESULTS_RETENTION_DAYS PROVENANCE_RETENTION_DAYS
# Missing credentials exit here (names only, never values); env.sample
# placeholders only warn.
check_os_credentials

# --------------------------------------------------------------------------
# Initialize
# --------------------------------------------------------------------------
# Root (with every capability but SETUID/SETGID dropped) only writes where it
# owns the directory: /rally (rally_env), /dashboard, /etc/cron.d. Everything
# under the rally-writable /results and /rally/logs is created by
# init_state.sh running as rally, so root never follows a symlink the rally
# user could have planted there. os_helpers.sh: shared helpers.
source "${SCRIPT_DIR}/os_helpers.sh"

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

# Locks, repair of root-owned leftovers, first-boot JSON seeds and the idle
# run_state.json reset.
# %q-quote RESULTS_DIR so a value with shell metacharacters can't break out of
# the su -c command string (same hardening as the rally_env export below).
_safe_results_dir="$(printf '%q' "${RESULTS_DIR}")"
su -s /bin/bash rally -c "RESULTS_DIR=${_safe_results_dir} /scripts/init_state.sh" || {
    log "ERROR: Failed to initialize state under ${RESULTS_DIR} and /rally/logs"
    exit 1
}

# Symlink JSON data files into /dashboard so the HTTP server can serve them.
# Recreated on every startup since /dashboard is ephemeral (not a volume).
# Every link uses `ln -sfnT` (GNU coreutils; the image is Debian): plain
# `ln -sf` follows a destination that is already a symlink to a directory and
# creates the new link INSIDE it -- root writing into the rally-writable
# ${RESULTS_DIR} if rally turned a target into a directory. -n never
# dereferences the destination, -T never treats it as a directory.
ln -sfnT "${RESULTS_DIR}/results.json"            /dashboard/results.json
ln -sfnT "${RESULTS_DIR}/history.json"            /dashboard/history.json
# Thresholds for the dashboard's overdue hints, written by init_state.sh above.
ln -sfnT "${RESULTS_DIR}/dashboard_config.json"   /dashboard/dashboard_config.json
ln -sfnT "${RESULTS_DIR}/health.json"             /dashboard/health.json
ln -sfnT "${RESULTS_DIR}/health_history.json"     /dashboard/health_history.json
# Operator-announcement state. The symlink is unconditional; the target file
# appears lazily on the first announce.sh post. serve.py's target.exists()
# check 404s on the dangling symlink until then.
ln -sfnT "${RESULTS_DIR}/announcement-state.json" /dashboard/announcement-state.json
# Run-progress state, seeded to idle just above. Same lazy/atomic pattern.
ln -sfnT "${RESULTS_DIR}/run_state.json"          /dashboard/run_state.json
# Expose per-run directories so the dashboard can link to the generated Rally
# HTML reports (/dashboard/runs/<TIMESTAMP>/<service>.html). serve.py gates this
# behind a strict regex + RESULTS_ROOT containment, so the symlink only widens
# what that allowlist branch explicitly permits. The source tree ships no
# dashboard/runs, so this is a plain symlink; `-nT` keeps a re-run from nesting a
# link inside a pre-existing one.
ln -sfnT "${RESULTS_DIR}"                       /dashboard/runs
# `ln -snT` refuses to replace an existing directory (and set -e would stop
# the container), so if the image was built with a preview
# `dashboard/themes/custom/` staged in the source tree (e.g. operator-side
# scaffolding), it is removed first. /dashboard is root-owned, so this never
# touches the rally-writable branding directory itself.
rm -rf /dashboard/themes/custom
ln -snT "${RESULTS_DIR}/branding"               /dashboard/themes/custom
log "Custom theme slot: ${RESULTS_DIR}/branding"
log "Dashboard symlinks updated"

# --------------------------------------------------------------------------
# Heal historical Rally reports
# --------------------------------------------------------------------------
# Pre-upgrade run directories still hold AngularJS reports (old `rally task
# report` output) that render broken under the strict CSP because their CDN
# scripts are blocked. backfill_reports.sh re-renders them from the persisted
# per-run JSON via render_report.py. Idempotent (skips already-migrated reports)
# and best-effort. Backgrounded as the rally user so it never delays
# dashboard/exporter startup and keeps regenerated files volume-owned.
log "Backfilling historical Rally reports (background)..."
su -s /bin/bash rally -c "RESULTS_DIR=${_safe_results_dir} /scripts/backfill_reports.sh" &

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
# /rally itself is root-owned and not rally-writable (Dockerfile), so the
# rally user cannot swap rally_env or plant a symlink at rally_env.tmp.
RALLY_ENV_VARS=(
    OS_AUTH_URL OS_USERNAME OS_PASSWORD OS_PROJECT_NAME
    OS_PROJECT_DOMAIN_NAME OS_USER_DOMAIN_NAME
    OS_IDENTITY_API_VERSION OS_REGION_NAME
    OS_CACERT OS_CERT OS_KEY OS_INSECURE
    OS_AUTH_TYPE OS_ENDPOINT_TYPE OS_INTERFACE
    RALLY_SCHEDULE_INTERVAL RALLY_RESULTS_RETENTION_DAYS RALLY_SERVICES
    RALLY_NOVA_FLAVOR RALLY_NOVA_IMAGE RALLY_NEUTRON_NETWORK_CIDR RALLY_DEBUG
    RGW_ADMIN_URL RGW_ACCESS_KEY RGW_SECRET_KEY RGW_REGION RGW_PURGE_TIMEOUT
    RALLY_RUN_LOCK_FILE
    RALLY_CONFIG_DIR
    RESULTS_DIR EXPORTER_PORT DASHBOARD_PORT HEALTH_CHECK_INTERVAL
    HEALTH_LATENCY_WARN_MS HEALTH_CHECK_TIMEOUT
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

# Forward cron job logs into `docker logs` (cron jobs run as rally and can't
# write to PID 1's stdout). `-F` handles the `copytruncate` rotation used in
# docker/logrotate.conf — re-reading from offset 0 when the file shrinks.
# The log files live in the rally-writable /rally/logs, so they are created
# and tailed as rally: as root, touch/tail would follow a planted symlink
# (and print any root-readable file into docker logs).
su -s /bin/bash rally -c "touch /rally/logs/rally-tests.log /rally/logs/health-check.log \
    && exec tail -F -n 0 -q /rally/logs/rally-tests.log /rally/logs/health-check.log" &
LOG_TAIL_PID=$!

# Start cron in the foreground as a supervised child: if it dies, `wait -n`
# below exits the container (and the restart policy brings it back) instead of
# health checks and test runs silently stopping while the exporter stays up.
cron -f &
CRON_PID=$!

log "Rally tests scheduled:  ${CRON_SCHEDULE} (every ${SCHEDULE_INTERVAL} min)"
log "Health checks scheduled: ${HEALTH_CRON} (every ${HEALTH_CHECK_INTERVAL} min)"

# --------------------------------------------------------------------------
# Run initial test if no results exist
# --------------------------------------------------------------------------
# Read as rally: latest_summary.json sits in the rally-writable RESULTS_DIR,
# where root must not follow a planted symlink.
_first_ts=$(su -s /bin/bash rally -c \
    "jq -r '.timestamp' ${_safe_results_dir}/latest_summary.json" 2>/dev/null || true)
unset _safe_results_dir
if [[ "${_first_ts}" == "waiting_for_first_run" ]]; then
    log "Running initial test suite..."
    su -s /bin/bash rally -c "/scripts/mirror_job_logs.sh /rally/logs/rally-tests.log /scripts/run_tests.sh" &
    log "Initial test started in background"
fi
unset _first_ts

# --------------------------------------------------------------------------
# Keep container alive, monitor child processes
# --------------------------------------------------------------------------
log "Rally Monitor is running"
log "  - Exporter: http://0.0.0.0:${EXPORTER_PORT}/metrics"
log "  - Dashboard: http://0.0.0.0:${DASHBOARD_PORT}"
log "  - Rally tests:   every ${SCHEDULE_INTERVAL} min (${CRON_SCHEDULE})"
log "  - Health checks: every ${HEALTH_CHECK_INTERVAL} min (${HEALTH_CRON})"

# Trap signals for graceful shutdown
trap 'log "Shutting down..."; kill ${EXPORTER_PID} ${DASHBOARD_PID} ${LOG_TAIL_PID} ${CRON_PID} 2>/dev/null; exit 0' SIGTERM SIGINT

# Wait for any child to exit. Tailer death is fatal — otherwise `docker logs`
# would silently go dark — and so is cron's (no more runs or health checks).
wait -n ${EXPORTER_PID} ${DASHBOARD_PID} ${LOG_TAIL_PID} ${CRON_PID} 2>/dev/null || true
log "A child process exited, shutting down..."
kill ${EXPORTER_PID} ${DASHBOARD_PID} ${LOG_TAIL_PID} ${CRON_PID} 2>/dev/null || true
exit 1
