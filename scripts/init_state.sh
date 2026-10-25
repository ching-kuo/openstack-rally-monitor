#!/usr/bin/env bash
# ==============================================================================
# Boot-time state seeding (run by entrypoint.sh as the rally user)
# ==============================================================================
# Creates the lock files and first-boot JSON seeds under ${RESULTS_DIR} and
# /rally/logs, and repairs what a root run of an older image left there. Both
# directories are rally-writable, so this must NOT run as root: root writing
# there would follow a symlink the rally user planted (e.g. a lock pointing at
# a root-owned file) and truncate or chmod the target. Run as rally, every file is rally-owned and a planted
# symlink gains nothing. entrypoint.sh keeps only the root-only steps.
set -euo pipefail

RESULTS_DIR="${RESULTS_DIR:-/results}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# parse_rally_services, seed_cleanup_metrics and the lock paths.
source "${SCRIPT_DIR}/os_helpers.sh"
reexec_as_rally "$@"
# or_default, normalize_interval/_timeout and cron_spacing_seconds.
source "${SCRIPT_DIR}/entrypoint_config.sh"

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [init-state] $*"
}

mkdir -p "${RESULTS_DIR}" "${RESULTS_DIR}/branding"

# Lock files. run_tests.sh, health_check.sh, cleanup_monitor.sh and announce.sh
# open theirs for writing; purge_orphans.sh --confirm opens the run lock
# read-only and refuses when it is missing, so it must exist. A lock this user
# cannot write (left by a root run of an older image) would make every later
# cron run fail; we own the directory, so replace it. Nothing holds a lock
# yet: cron and the initial run start after this script. All four paths
# come from os_helpers.sh.
for lock in "${RALLY_RUN_LOCK_FILE}" "${HEALTH_LOCKFILE}" "${CLEANUP_LOCK_FILE}" \
        "${ANNOUNCE_LOCK_FILE}"; do
    mkdir -p "$(dirname "${lock}")"
    if [[ -e "${lock}" && ! -w "${lock}" ]]; then
        rm -f "${lock}"
        log "Replaced unwritable lock ${lock}"
    fi
    touch "${lock}"
done

# State files a root run left root-owned (0644) cannot be rewritten in place
# (rally_project_ids.log and the cron logs are appended to). Rally owns
# ${RESULTS_DIR}, so swap in a rally-owned copy: copy + mv replaces the entry
# whatever the old file's owner. Run directories cannot be fixed this way
# (rally cannot empty a root-owned directory, nor rename one), so name them
# with the remedy; until fixed, retention cannot prune them.
shopt -s nullglob dotglob
_stuck=0
for path in "${RESULTS_DIR}"/*; do
    [[ -w "${path}" || -L "${path}" ]] && continue
    if [[ -f "${path}" ]]; then
        if cp -p "${path}" "${path}.repair.$$" 2>/dev/null \
                && chmod u+w "${path}.repair.$$" && mv -f "${path}.repair.$$" "${path}"; then
            log "Replaced unwritable state file ${path}"
            continue
        fi
        rm -f "${path}.repair.$$"
        log "WARNING: ${path} is not writable by $(id -un) and could not be replaced"
        _stuck=1
    # Run directories only: branding/ may be a documented read-only bind mount.
    elif [[ -d "${path}" && "$(basename "${path}")" =~ ^[0-9]{8}T[0-9]{6}Z$ ]]; then
        log "WARNING: ${path} is not writable by $(id -un); it cannot be pruned or updated"
        _stuck=1
    fi
done
shopt -u nullglob dotglob
if (( _stuck )); then
    log "WARNING: fix once from the host (CAP_CHOWN is dropped inside this container):"
    log "  docker run --rm -v <rally-results volume>:/results busybox chown -R $(id -u):0 /results"
fi
unset _stuck

# Run a producer command and publish its stdout as ${RESULTS_DIR}/<name>:
#   publish_json <name> <command> [args...]
# The output goes to <name>.tmp and is moved over the target only when the
# producer succeeded and wrote something; otherwise the previous file is kept
# (a failed seed leaves the name missing, so the next boot seeds it again) and
# a WARNING is logged. Failure is not fatal: one unwritable dashboard file
# must not stop the container. A directory, or a symlink, squatting on the
# name or on its .tmp is removed first: `mv` onto a directory (or a symlink to
# one) drops the file INSIDE it, and the dashboard would then 404 the name.
# This runs as rally, which owns RESULTS_DIR, so it can replace whatever rally
# left there. (GNU `mv -T` would refuse instead of replacing; it is also
# missing from the BSD mv used in local development.)
publish_json() {
    local name="$1" dest="${RESULTS_DIR}/$1" path
    shift
    for path in "${dest}" "${dest}.tmp"; do
        if [[ -L "${path}" || -d "${path}" ]]; then
            rm -rf -- "${path}"
            log "Replaced non-file ${path}"
        fi
    done
    if "$@" > "${dest}.tmp" && [[ -s "${dest}.tmp" ]] && mv -f -- "${dest}.tmp" "${dest}"; then
        return 0
    fi
    rm -f -- "${dest}.tmp"
    log "WARNING: could not write ${name}; the previous copy (if any) is kept"
    return 1
}

# Create a seed summary if none exists (so dashboard works on first boot).
# Generate the pending cards from RALLY_SERVICES rather than a hardcoded six so a
# trimmed deployment (e.g. a Swift-less cloud) doesn't show a phantom pending
# card forever. The list comes from parse_rally_services (os_helpers.sh), so the
# seed shows exactly the services run_tests.sh will run. Seed-only-if-missing.
seed_summary() {
    parse_rally_services "${RALLY_SERVICES:-}" | jq -R . | jq -s '
        {
            timestamp: "waiting_for_first_run",
            services: (map({(.): {status: "pending", duration: 0, total_iterations: 0,
                                  failed_iterations: 0, sla_passed: true, scenarios: []}})
                | add)
        }'
}
if [[ ! -f "${RESULTS_DIR}/latest_summary.json" ]] && publish_json latest_summary.json seed_summary; then
    log "Created seed summary"
fi

if seed_cleanup_metrics "${RESULTS_DIR}/cleanup_metrics.json"; then
    log "Created seed cleanup_metrics.json"
fi

# Seed dashboard JSON files into the persistent results volume if missing
# shellcheck disable=SC2016  # a jq program: $names are jq variables
if [[ ! -f "${RESULTS_DIR}/results.json" ]] && publish_json results.json \
        jq -n \
        --slurpfile summary "${RESULTS_DIR}/latest_summary.json" \
        --slurpfile cleanup "${RESULTS_DIR}/cleanup_metrics.json" \
        '{
            summary: $summary[0],
            cleanup: $cleanup[0]
        }'; then
    log "Created seed results.json"
fi

# Usage: seed_if_missing <name under RESULTS_DIR> <json>
seed_if_missing() {
    [[ -f "${RESULTS_DIR}/$1" ]] && return 0
    if publish_json "$1" printf '%s\n' "$2"; then
        log "Created seed $1"
    fi
}
seed_if_missing history.json '{"runs": []}'
seed_if_missing health.json '{"timestamp":"waiting","overall":"unknown","services":{}}'
seed_if_missing health_history.json '{"checks":[]}'

# Thresholds for the dashboard's "overdue" hints, rewritten on EVERY boot so a
# changed schedule takes effect on restart. They are the values the exporter
# and alerts use: the cron spacing of each schedule and the health stale-after
# age. KEEP IN SYNC with exporter/metrics_common.py::cron_spacing_seconds and
# health_metrics.py::stale_after_seconds (max(3I, I + HEALTH_CHECK_TIMEOUT +
# 60)); the dashboard applies them like RallyStaleResults and
# rally_api_stale_after_seconds (dashboard/js/status.js). entrypoint.sh has
# already refused invalid values; or_default only covers a direct invocation.
_schedule_s=$(cron_spacing_seconds \
    "$(or_default normalize_interval "${RALLY_SCHEDULE_INTERVAL-}" 240 RALLY_SCHEDULE_INTERVAL)")
_health_s=$(cron_spacing_seconds \
    "$(or_default normalize_interval "${HEALTH_CHECK_INTERVAL-}" 15 HEALTH_CHECK_INTERVAL)")
_timeout_s=$(or_default normalize_timeout "${HEALTH_CHECK_TIMEOUT-}" 300 HEALTH_CHECK_TIMEOUT)
_stale_s=$(( 3 * _health_s ))
if (( _health_s + _timeout_s + 60 > _stale_s )); then
    _stale_s=$(( _health_s + _timeout_s + 60 ))
fi
# shellcheck disable=SC2016  # a jq program: $names are jq variables
if publish_json dashboard_config.json \
        jq -n --argjson schedule "${_schedule_s}" --argjson health "${_health_s}" \
        --argjson stale "${_stale_s}" \
        '{schedule_interval_seconds: $schedule, health_check_interval_seconds: $health,
          health_stale_after_seconds: $stale}'; then
    log "Wrote dashboard_config.json (run every ${_schedule_s}s, health every ${_health_s}s, stale after ${_stale_s}s)"
fi
unset _schedule_s _health_s _timeout_s _stale_s

# Reset run-progress state to idle on EVERY boot (not just when missing). A run
# that was SIGKILL'd by a container stop/crash would have left state="running"
# in the persistent volume; nothing else would clear it, so the dashboard would
# show a phantom "Test run in progress" forever. The running run is necessarily
# dead after a restart, so idle is always correct here. run_tests.sh flips it
# back to "running" under its flock when a real run starts.
if publish_json run_state.json \
        printf '{"state":"idle","finished_at":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"; then
    log "Reset run_state.json to idle"
fi
