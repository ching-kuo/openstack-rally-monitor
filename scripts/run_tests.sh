#!/usr/bin/env bash
# ==============================================================================
# Rally Test Runner - Main Orchestrator
# ==============================================================================
# Runs all Rally scenarios, exports results as timestamped JSON, and triggers
# cleanup monitoring. Prunes results older than RALLY_RESULTS_RETENTION_DAYS.
set -euo pipefail

RESULTS_DIR="${RESULTS_DIR:-/results}"
RALLY_CONFIG_DIR="${RALLY_CONFIG_DIR:-/rally/config}"
RETENTION_DAYS="${RALLY_RESULTS_RETENTION_DAYS:-7}"
# The RGW provenance ledger is pruned on its OWN, longer window -- see
# prune_rally_project_ledger for the rationale.
PROVENANCE_RETENTION_DAYS="${PROVENANCE_RETENTION_DAYS:-90}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${RESULTS_DIR}/${TIMESTAMP}"
SUMMARY_FILE="${RESULTS_DIR}/latest_summary.json"
RUN_STATE_FILE="${RESULTS_DIR}/run_state.json"
RALLY_PROJECT_LEDGER_FILE="${RESULTS_DIR}/rally_project_ids.log"
SMOKE_HISTORY_FILE="${RESULTS_DIR}/smoke_history.json"
UPTIME_WINDOW_DAYS="${UPTIME_WINDOW_DAYS:-30}"
RUN_LOG="${RUN_DIR}/run.log"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# RALLY_SERVICES parsing (parse_rally_services) and the shared OpenStack
# listing helpers live in os_helpers.sh.
source "${SCRIPT_DIR}/os_helpers.sh"
reexec_as_rally "$@"
# or_default/normalize_days: the day counts below are validated after log().
source "${SCRIPT_DIR}/entrypoint_config.sh"

mapfile -t SERVICES < <(parse_rally_services "${RALLY_SERVICES:-}")

log() {
    local msg
    msg="[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
    echo "${msg}"
    # Also append to run log if it exists. Best effort: a full disk or broken
    # run directory must not abort the run (set -e) from inside log().
    if [[ -f "${RUN_LOG}" ]]; then
        echo "${msg}" >> "${RUN_LOG}" 2>/dev/null || true
    fi
}

# A malformed value ("30d") would otherwise break find -mtime, date -d and
# jq --argjson mid-run; the entrypoint already refuses one at boot, this
# covers direct invocations.
RETENTION_DAYS=$(or_default normalize_retention_days "${RETENTION_DAYS}" 7 RALLY_RESULTS_RETENTION_DAYS)
PROVENANCE_RETENTION_DAYS=$(or_default normalize_provenance_days "${PROVENANCE_RETENTION_DAYS}" 90 PROVENANCE_RETENTION_DAYS)
UPTIME_WINDOW_DAYS=$(or_default normalize_days "${UPTIME_WINDOW_DAYS}" 30 UPTIME_WINDOW_DAYS)

source "${SCRIPT_DIR}/rgw_helpers.sh"
# Summary, ledger, publish, prune, run-state and abort helpers.
source "${SCRIPT_DIR}/run_lib.sh"

# --------------------------------------------------------------------------
# 0. Log environment for debugging (passwords redacted)
# --------------------------------------------------------------------------
log_environment() {
    log "--- Environment ---"
    log "  OS_AUTH_URL=${OS_AUTH_URL:-<not set>}"
    log "  OS_USERNAME=${OS_USERNAME:-<not set>}"
    log "  OS_PASSWORD=$(if [[ -n "${OS_PASSWORD:-}" ]]; then echo '***SET***'; else echo '<not set>'; fi)"
    log "  OS_PROJECT_NAME=${OS_PROJECT_NAME:-<not set>}"
    log "  RALLY_SERVICES=${SERVICES[*]}"
    log "  OS_USER_DOMAIN_NAME=${OS_USER_DOMAIN_NAME:-<not set>}"
    log "  OS_PROJECT_DOMAIN_NAME=${OS_PROJECT_DOMAIN_NAME:-<not set>}"
    log "  OS_REGION_NAME=${OS_REGION_NAME:-<not set>}"
    log "  RALLY_CONFIG_DIR=${RALLY_CONFIG_DIR}"
    log "  RESULTS_DIR=${RESULTS_DIR}"
    log "  RALLY_NOVA_FLAVOR=${RALLY_NOVA_FLAVOR:-m1.tiny}"
    log "  RALLY_NOVA_IMAGE=${RALLY_NOVA_IMAGE:-cirros-0.6.2-x86_64-disk}"
    log "  RALLY_DEBUG=${RALLY_DEBUG:-false}"
    log "-------------------"
}

# Provenance tracker (rally_project_tracker.py), one per service task: one
# long-lived process with one openstacksdk session instead of spawning
# `openstack project list` (a fresh interpreter + token) every 5s; see the
# script's docstring for the snapshot / poll / final-poll / ledger semantics.
#
# TRACKER_PID is set only while the tracker is our running child: it is
# cleared as soon as the tracker is known to have exited (and been reaped),
# and _tracker_running checks bash's own job table rather than `kill -0`, so
# a recycled PID is never signalled. The tracker reports readiness on a FIFO
# held open read-write here (so its write never blocks and our `read -t`
# waits without forking a `sleep` every 100ms).
TRACKER_PID=""
TRACKER_READY_FD=""

_tracker_running() {
    [[ -n "${TRACKER_PID}" ]] || return 1
    local pid
    while IFS= read -r pid; do
        [[ "${pid}" == "${TRACKER_PID}" ]] && return 0
    done < <(jobs -rp)
    return 1
}

# Reap an exited tracker and forget its PID. Prints nothing; sets
# TRACKER_EXIT_CODE.
_tracker_reap() {
    TRACKER_EXIT_CODE=0
    if [[ -n "${TRACKER_PID}" ]]; then
        wait "${TRACKER_PID}" 2>/dev/null || TRACKER_EXIT_CODE=$?
    fi
    TRACKER_PID=""
}

# Start the tracker and wait until it has snapshotted the pre-existing
# c_rally_* projects, so the snapshot never races the task it brackets.
start_project_tracker() {
    local service="$1" ready_fifo="$2" line
    TRACKER_PID=""
    if ! mkfifo "${ready_fifo}" || ! exec {TRACKER_READY_FD}<>"${ready_fifo}"; then
        TRACKER_READY_FD=""
        log "  ${service}: WARNING - cannot create the tracker ready FIFO; RGW provenance is not recorded for this task"
        return 0
    fi
    # Tracker stderr (tracebacks) goes to the run log; its own messages are
    # written by its log() to stdout and the run log, like ours. (--run-log is
    # only a path argument; the tracker appends to it, as does 2>>.)
    # shellcheck disable=SC2094
    python3 "${SCRIPT_DIR}/rally_project_tracker.py" \
        --service "${service}" --ledger "${RALLY_PROJECT_LEDGER_FILE}" \
        --ready-file "${ready_fifo}" --run-log "${RUN_LOG}" 2>>"${RUN_LOG}" &
    TRACKER_PID=$!

    # Bounded: the snapshot makes at most 3 attempts of one 30s-timeout
    # request (~100s worst case), so 120s only trips on a pathological
    # Keystone. Starting the task anyway keeps the smoke test running; the
    # cost is possibly-incomplete provenance, logged.
    local seconds=0
    while ! read -r -t 1 -u "${TRACKER_READY_FD}" line; do
        if ! _tracker_running; then
            _tracker_reap
            log "  ${service}: WARNING - provenance tracker exited (code ${TRACKER_EXIT_CODE}) before its snapshot; RGW provenance is not recorded for this task (see ${RUN_LOG})"
            return 0
        fi
        seconds=$((seconds + 1))
        if (( seconds >= 120 )); then
            log "  ${service}: WARNING - provenance tracker not ready after 120s; starting task anyway"
            return 0
        fi
    done
}

# Signal the tracker that the task exited; it takes one final poll (one
# request, 30s timeout) and exits. Bounded at 60s, then SIGKILL, so a wedged
# tracker can never hang the run. A no-op when the tracker already exited.
stop_project_tracker() {
    local line
    if _tracker_running; then
        kill -TERM "${TRACKER_PID}" 2>/dev/null || true
        local ticks=0
        while _tracker_running; do
            if (( ticks >= 300 )); then
                log "  WARNING - provenance tracker did not exit 60s after SIGTERM; killing it"
                kill -KILL "${TRACKER_PID}" 2>/dev/null || true
                break
            fi
            # A 0.2s wait without forking a sleep: nothing more arrives on the
            # FIFO. TRACKER_READY_FD is always open here -- the tracker is
            # only started after the FIFO is opened.
            read -r -t 0.2 -u "${TRACKER_READY_FD}" line || true
            ticks=$((ticks + 1))
        done
    fi
    _tracker_reap
    if [[ -n "${TRACKER_READY_FD}" ]]; then
        exec {TRACKER_READY_FD}>&-
        TRACKER_READY_FD=""
    fi
}

# --------------------------------------------------------------------------
# 1. Ensure deployment is registered
# --------------------------------------------------------------------------
setup_deployment() {
    log "Setting up Rally deployment..."
    local deploy_log="${RUN_DIR}/deployment.log"

    # Check if deployment already exists
    if ! rally deployment list 2>"${deploy_log}" | grep -q "openstack-monitor"; then
        log "Creating new deployment from environment..."
        if ! rally deployment create \
            --fromenv \
            --name "openstack-monitor" \
            >> "${deploy_log}" 2>&1; then
            log "ERROR: Failed to create deployment. Details:"
            log "--- deployment.log start ---"
            while IFS= read -r line; do log "  ${line}"; done < "${deploy_log}"
            log "--- deployment.log end ---"
            return 1
        fi
        log "Deployment created successfully"
    else
        log "Deployment 'openstack-monitor' already exists"
    fi

    # Use the deployment
    log "Activating deployment..."
    if ! rally deployment use "openstack-monitor" >> "${deploy_log}" 2>&1; then
        log "ERROR: Failed to activate deployment. Details:"
        while IFS= read -r line; do log "  ${line}"; done < "${deploy_log}"
        return 1
    fi

    # Check the deployment
    log "Checking deployment connectivity..."
    if ! rally deployment check >> "${deploy_log}" 2>&1; then
        log "WARNING: Deployment check reported issues. Details:"
        while IFS= read -r line; do log "  ${line}"; done < "${deploy_log}"
        # Show the deployment config (without secrets) for debugging
        log "Current deployment config:"
        rally deployment config 2>/dev/null | \
            python3 -c "import sys,json; d=json.load(sys.stdin); \
            [d['openstack'].pop(k,None) for k in ['admin','users'] if isinstance(d.get('openstack'),dict)]; \
            print(json.dumps(d, indent=2))" 2>/dev/null | \
            while IFS= read -r line; do log "  ${line}"; done || true
        return 1
    fi
    log "Deployment check passed"
}

# --------------------------------------------------------------------------
# 2. Run scenarios for each service
# --------------------------------------------------------------------------
write_task_args() {
    local target="$1"
    local temporary="${target}.tmp"

    if ! jq -n \
        --arg flavor "${RALLY_NOVA_FLAVOR:-m1.tiny}" \
        --arg image "${RALLY_NOVA_IMAGE:-cirros-0.6.2-x86_64-disk}" \
        --arg auth_url "${OS_AUTH_URL:-}" \
        '{
            env: {
                RALLY_NOVA_FLAVOR: $flavor,
                RALLY_NOVA_IMAGE: $image,
                OS_AUTH_URL: $auth_url
            }
        }' > "${temporary}"; then
        rm -f "${temporary}"
        return 1
    fi

    if ! mv "${temporary}" "${target}"; then
        rm -f "${temporary}"
        return 1
    fi
}

run_service_tests() {
    local service="$1" task_args_file="$2"
    local scenario_file
    scenario_file=$(scenario_file_for "${service}")
    local result_file="${RUN_DIR}/${service}.json"
    local html_file="${RUN_DIR}/${service}.html"

    if [[ ! -f "${scenario_file}" ]]; then
        log "SKIP: No scenario file for ${service}"
        return 0
    fi

    log "Running ${service} scenarios..."
    local task_uuid="" start_error="" start_error_code=""
    local log_file="${RUN_DIR}/${service}.log"
    # Tag every task with a run+service-unique marker so we can resolve its UUID
    # by tag below (`task start --tag`, `task list --tag --uuids-only`; an
    # unknown tag prints nothing, so the lookup yields an empty UUID).
    local task_tag="run-${TIMESTAMP}-${service}"
    local tracker_dir
    tracker_dir=$(mktemp -d)
    start_project_tracker "${service}" "${tracker_dir}/ready.fifo"

    if [[ "${RALLY_DEBUG:-false}" == "true" ]]; then
        log "  DEBUG mode enabled. Full logs saving to ${log_file}"
        (
            rally --debug task start "${scenario_file}" --task-args-file "${task_args_file}" --tag "${task_tag}" 2>&1 | tee "${log_file}" /dev/stderr >/dev/null
        ) &
    else
        rally task start "${scenario_file}" --task-args-file "${task_args_file}" --tag "${task_tag}" > "${log_file}" 2>&1 &
    fi

    local task_pid=$!
    local task_rc=0
    wait "${task_pid}" || task_rc=$?
    stop_project_tracker
    rm -rf "${tracker_dir}"

    if [[ "${task_rc}" -ne 0 ]]; then
        log "  ${service}: Rally task command exited with code ${task_rc}"
    fi

    # Log task output for debugging
    if [[ -f "${log_file}" ]] && [[ -s "${log_file}" ]]; then
        local lines
        lines=$(wc -l < "${log_file}")
        log "  ${service}: task log has ${lines} lines (see ${log_file})"
        # Log last 10 lines as a quick summary
        log "  --- ${service} task log (last 10 lines) ---"
        tail -10 "${log_file}" | while IFS= read -r line; do log "    ${line}"; done
        log "  --- end ---"
    else
        log "  ${service}: WARNING - task log is empty or missing"
    fi

    # Match a full canonical UUID only: a loose `Task [a-f0-9-]+` also matched
    # words like "Task aborted" (-> task "ab"). -E rather than -P so this works
    # with any grep.
    task_uuid=$(grep -oE 'Task [0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' \
        "${log_file}" | head -1 | cut -d' ' -f2) || true

    if [[ -z "${task_uuid}" ]]; then
        # Fall back to a TAG-SCOPED lookup, not `rally task list --uuids-only |
        # tail -1`. The bare-list fallback returns the most recent task overall,
        # so if THIS task failed to start it would misattribute a previous (or
        # another service's) task's results to ${service}. Scoping by our unique
        # tag means a miss yields an empty UUID and an honest "FAILED to start"
        # rather than a wrong-but-green result.
        task_uuid=$(rally task list --tag "${task_tag}" --uuids-only 2>/dev/null | tail -1) || true
    fi

    if [[ -n "${task_uuid}" ]]; then
        # Export JSON results
        rally task results "${task_uuid}" > "${result_file}" 2>/dev/null || true

        # Get task status
        local status
        status=$(rally task status "${task_uuid}" 2>/dev/null | awk '{print $NF}') || status="unknown"

        log "  ${service}: task=${task_uuid} status=${status}"
    else
        log "  ${service}: FAILED to start task (no task UUID found)"
        # Leave a marker so build_summary reports this service as failed (with
        # the tail of the task log as the reason) rather than "skipped", which
        # is reserved for a service with no scenario file.
        start_error_code="task_failed_to_start"
        start_error=$(log_tail "${log_file}") || true
        printf '%s' "${start_error}" > "${RUN_DIR}/${service}.start_failed" || true
        # Log full task output for debugging
        if [[ -f "${log_file}" ]]; then
            log "  --- FULL ${service} task log ---"
            while IFS= read -r line; do log "    ${line}"; done < "${log_file}"
            log "  --- end ---"
        fi
    fi

    # Render the self-contained, dashboard-themed HTML report from the JSON
    # results. Replaces `rally task report`, whose AngularJS page pulled
    # d3/nvd3/Angular from public CDNs (breaking offline and forcing a
    # relaxed CSP/sandbox in serve.py) and never matched the dashboard.
    # render_report.py is best-effort: a malformed/empty/missing result file
    # still yields a valid empty-state report so the dashboard link never 404s.
    # On a failed start, --error-code/--error carry the same code and task-log
    # tail build_summary records, so the report explains the failure (empty
    # values are the same as omitting them).
    python3 "${SCRIPT_DIR}/render_report.py" "${result_file}" "${html_file}" \
        --service "${service}" --timestamp "${TIMESTAMP}" \
        --error-code "${start_error_code}" --error "${start_error}" 2>/dev/null || true
}

# --------------------------------------------------------------------------
# 6. Notify cleanup monitor
# --------------------------------------------------------------------------
check_cleanup() {
    log "Running cleanup monitor..."
    /scripts/cleanup_monitor.sh || {
        log "WARNING: Cleanup monitor detected issues"
    }
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
main() {
    log "=========================================="
    log "Rally Test Run: ${TIMESTAMP}"
    log "=========================================="
    # RALLY_RUN_LOCK_FILE (os_helpers.sh) is shared with purge_orphans.sh
    # --confirm, which holds it to refuse deleting while a run is live.
    exec 200>"${RALLY_RUN_LOCK_FILE}"
    flock -n 200 || {
        log "SKIPPED: ${RALLY_RUN_LOCK_FILE} is held by another test run or by purge_orphans.sh --confirm; this run did not execute."
        exit 0
    }
    RUN_START_EPOCH=$(date +%s)

    # We now own the flock. Mark the run in progress, THEN install the EXIT trap
    # that resets to idle -- in this order so the early `exit 0` above (lock
    # contention, before we owned the lock) cannot trip the trap and overwrite
    # the other process's "running" state.
    write_run_state_running
    trap write_run_state_idle EXIT

    mkdir -p "${RUN_DIR}"

    # Initialize run log
    touch "${RUN_LOG}"
    log "Run log: ${RUN_LOG}"

    # Log environment for debugging
    log_environment

    # Setup deployment
    setup_deployment || {
        log "FATAL: Cannot set up deployment, aborting."
        log "HINT: Check ${RUN_DIR}/deployment.log for detailed error output."
        log "HINT: Verify OS_AUTH_URL is reachable and credentials are correct."
        abort_run deployment_setup_failed "$(log_tail "${RUN_DIR}/deployment.log" || true)"
    }

    # Create task args file once — all services share the same scenario parameters.
    # Called as a condition so a failure takes the abort path (summary, ledger,
    # publish) instead of a silent `set -e` exit with no record of the run.
    local task_args_file="${RUN_DIR}/task_args.json"
    if ! write_task_args "${task_args_file}"; then
        log "FATAL: Cannot write ${task_args_file}, aborting."
        abort_run task_args_write_failed "could not write ${task_args_file}"
    fi

    # Run all service tests
    for service in "${SERVICES[@]}"; do
        run_service_tests "${service}" "${task_args_file}" || true
    done

    # Build summary
    build_summary

    # Record the run outcome in the rolling uptime ledger
    record_smoke_result

    # Fire a transition notification (no-op unless NOTIFY_WEBHOOK_URL is set and
    # the status actually changed since the last notification). `|| true` so a
    # webhook failure never affects the run result.
    /scripts/notify.sh "${RUN_SMOKE_STATUS}" || true

    # Auto-clear incident-type announcements when the run is unambiguously
    # all-green. The deployment_setup_failed path in setup_deployment's failure
    # branch already exits 1 before reaching here, so the empty-services guard
    # in ALL_GREEN_PREDICATE inside announce.sh primarily protects direct
    # `docker exec` invocations against a stale latest_summary.json — not the
    # normal cron flow. It also stays load-bearing if a future refactor
    # removes the early exit.
    /scripts/announce.sh auto-clear-if-all-green --summary-file "${SUMMARY_FILE}" || true

    # Auto-purge rally-owned RGW orphans before writing cleanup metrics so
    # dashboard/Prometheus state reflects the post-purge resource state.
    # (auto_purge_rgw lives in rgw_helpers.sh.)
    auto_purge_rgw

    # Check cleanup
    check_cleanup

    # Fold cleanup metrics into the run's summary so history.json carries
    # per-run cleanup counts (results.json already merges them at publish time).
    attach_cleanup_to_summary

    # Prune, record the run duration, and publish the dashboard files.
    finish_run

    log "=========================================="
    log "Rally Test Run Complete"
    log "=========================================="
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
