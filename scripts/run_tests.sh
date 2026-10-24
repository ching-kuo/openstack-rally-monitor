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

# The monitored service set is configurable via RALLY_SERVICES (comma-separated).
# parse_rally_services normalizes it (trim/lowercase/drop-empties/dedupe, then
# drop any token not matching the ^[a-z0-9_-]+$ allowlist, order preserved);
# the same parsing rules are mirrored in api_health_check.py's
# parse_rally_services and health_check.sh's all-down jq fallback -- keep the
# three in sync. The allowlist is path-traversal hardening: service names index
# rally/scenarios/<name>.yaml and runs/<ts>/<name>.html, so a token like
# "../etc" must never survive. A configured service with no
# rally/scenarios/<name>.yaml still logs a SKIP in run_service_tests (the
# operator's signal for a typo'd name); build_summary then reports it "skipped".
DEFAULT_RALLY_SERVICES="keystone,nova,neutron,glance,cinder,swift"

parse_rally_services() {
    # Echo the normalized service list, one per line, preserving operator order.
    # Reads $1 (the raw RALLY_SERVICES string). Falls back to the default when
    # the input is unset/empty or normalizes to nothing (including when every
    # token is dropped by the allowlist).
    local raw="${1:-}"
    [[ -n "${raw}" ]] || raw="${DEFAULT_RALLY_SERVICES}"
    local out
    out=$(printf '%s' "${raw}" | tr ',' '\n' | tr '[:upper:]' '[:lower:]' \
        | awk '{ gsub(/[[:space:]]/, ""); if ($0 ~ /^[a-z0-9_-]+$/ && !seen[$0]++) print }')
    [[ -n "${out}" ]] || out=$(printf '%s' "${DEFAULT_RALLY_SERVICES}" | tr ',' '\n')
    printf '%s\n' "${out}"
}

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

list_rally_context_project_ids() {
    local json
    json=$(openstack project list -f json 2>/dev/null) || return 1
    echo "${json}" | jq -r '.[] | select(.Name | startswith("c_rally_")) | .ID' 2>/dev/null || return 1
}

append_rally_project_id() {
    local project_id="$1" service="$2"
    printf '%s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${project_id}" "${service}" >> "${RALLY_PROJECT_LEDGER_FILE}"
}

record_new_rally_context_projects() {
    local service="$1" seen_file="$2"
    local current_ids project_id
    current_ids=$(list_rally_context_project_ids) || return 1

    while IFS= read -r project_id; do
        [[ -n "${project_id}" ]] || continue
        if ! grep -Fxq "${project_id}" "${seen_file}"; then
            printf '%s\n' "${project_id}" >> "${seen_file}"
            append_rally_project_id "${project_id}" "${service}"
            log "  ${service}: recorded Rally project ${project_id}"
        fi
    done <<< "${current_ids}"
}

track_rally_context_projects() {
    local service="$1" task_pid="$2" seen_file="$3"
    local warned=0

    while kill -0 "${task_pid}" 2>/dev/null; do
        if ! record_new_rally_context_projects "${service}" "${seen_file}"; then
            if [[ "${warned}" -eq 0 ]]; then
                log "  ${service}: WARNING - failed to poll Rally context projects; RGW provenance may be incomplete for this task"
                warned=1
            fi
        fi
        sleep 5
    done

    record_new_rally_context_projects "${service}" "${seen_file}" || true
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
    local scenario_file="${RALLY_CONFIG_DIR}/scenarios/${service}.yaml"
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
    # by tag below. Verified supported on `rally task start` and `rally task
    # list` in rally 5.0.1 (rally/cli/commands/task.py: --tag on start line 182,
    # on list line 558; --tag composes with --uuids-only).
    local task_tag="run-${TIMESTAMP}-${service}"
    local tracker_seen_file
    tracker_seen_file=$(mktemp)
    if ! list_rally_context_project_ids > "${tracker_seen_file}"; then
        : > "${tracker_seen_file}"
        log "  ${service}: WARNING - failed to snapshot pre-run Rally projects"
    fi

    if [[ "${RALLY_DEBUG:-false}" == "true" ]]; then
        log "  DEBUG mode enabled. Full logs saving to ${log_file}"
        (
            rally --debug task start "${scenario_file}" --task-args-file "${task_args_file}" --tag "${task_tag}" 2>&1 | tee "${log_file}" /dev/stderr >/dev/null
        ) &
    else
        rally task start "${scenario_file}" --task-args-file "${task_args_file}" --tag "${task_tag}" > "${log_file}" 2>&1 &
    fi

    local task_pid=$!
    track_rally_context_projects "${service}" "${task_pid}" "${tracker_seen_file}" &
    local tracker_pid=$!
    local task_rc=0
    wait "${task_pid}" || task_rc=$?
    wait "${tracker_pid}" || true
    rm -f "${tracker_seen_file}"

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
# 7. Auto-purge rally-owned RGW orphans
# --------------------------------------------------------------------------
auto_purge_rgw() {
    rgw_available || return 0

    log "Auto-purging rally-owned RGW orphans..."

    local orphans_file
    orphans_file=$(mktemp)
    if ! rgw_find_orphaned_users > "${orphans_file}"; then
        log "WARNING: RGW orphan scan failed; skipping auto-purge"
        rm -f "${orphans_file}"
        return 0
    fi

    if [[ "${RGW_LAST_FIND_ERRORS}" -gt 0 ]]; then
        log "WARNING: RGW scan had ${RGW_LAST_FIND_ERRORS} error(s); skipping auto-purge (fail-closed)"
        rm -f "${orphans_file}"
        return 0
    fi

    local purged=0 failed=0 skipped=0
    local uid ownership bucket_json bucket_name bucket_ok
    while IFS= read -r uid; do
        [[ -n "${uid}" ]] || continue
        ownership=$(rgw_classify_owner "${uid}")

        if [[ "${ownership}" != "rally_owned" ]]; then
            skipped=$((skipped + 1))
            continue
        fi

        if ! bucket_json=$(rgw_list_user_buckets "${uid}"); then
            log "  RGW user ${uid}: bucket listing failed; skipping"
            failed=$((failed + 1))
            continue
        fi

        local bucket_names
        if ! bucket_names=$(echo "${bucket_json}" | jq -r '.[].name // empty' 2>/dev/null); then
            log "  RGW user ${uid}: bucket JSON parse failed; skipping"
            failed=$((failed + 1))
            continue
        fi

        bucket_ok=1
        while IFS= read -r bucket_name; do
            [[ -n "${bucket_name}" ]] || continue
            if rgw_delete_bucket "${bucket_name}"; then
                log "  RGW bucket ${bucket_name}: deleted"
            else
                log "  RGW bucket ${bucket_name}: deletion FAILED"
                bucket_ok=0
                failed=$((failed + 1))
            fi
        done <<< "${bucket_names}"

        if [[ "${bucket_ok}" -eq 0 ]]; then
            log "  RGW user ${uid}: skipped (bucket deletion incomplete)"
            continue
        fi

        if rgw_delete_user "${uid}"; then
            log "  RGW user ${uid}: deleted"
            purged=$((purged + 1))
        else
            log "  RGW user ${uid}: deletion FAILED"
            failed=$((failed + 1))
        fi
    done < "${orphans_file}"

    rm -f "${orphans_file}"

    log "RGW auto-purge complete: purged=${purged} failed=${failed} skipped_unknown=${skipped}"
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
main() {
    log "=========================================="
    log "Rally Test Run: ${TIMESTAMP}"
    log "=========================================="
    LOCKFILE="/rally/logs/rally-run.lock"
    exec 200>"${LOCKFILE}"
    flock -n 200 || { log "Another run is already in progress, exiting."; exit 0; }
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
