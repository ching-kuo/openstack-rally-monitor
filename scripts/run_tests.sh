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
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${RESULTS_DIR}/${TIMESTAMP}"
SUMMARY_FILE="${RESULTS_DIR}/latest_summary.json"
RALLY_PROJECT_LEDGER_FILE="${RESULTS_DIR}/rally_project_ids.log"
RUN_LOG="${RUN_DIR}/run.log"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SERVICES=("keystone" "nova" "neutron" "glance" "cinder" "swift")

log() {
    local msg="[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
    echo "${msg}"
    # Also append to run log if it exists
    if [[ -f "${RUN_LOG}" ]]; then
        echo "${msg}" >> "${RUN_LOG}"
    fi
}

source "${SCRIPT_DIR}/rgw_helpers.sh"

# --------------------------------------------------------------------------
# 0. Log environment for debugging (passwords redacted)
# --------------------------------------------------------------------------
log_environment() {
    log "--- Environment ---"
    log "  OS_AUTH_URL=${OS_AUTH_URL:-<not set>}"
    log "  OS_USERNAME=${OS_USERNAME:-<not set>}"
    log "  OS_PASSWORD=$(if [[ -n "${OS_PASSWORD:-}" ]]; then echo '***SET***'; else echo '<not set>'; fi)"
    log "  OS_PROJECT_NAME=${OS_PROJECT_NAME:-<not set>}"
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
    local task_uuid=""
    local log_file="${RUN_DIR}/${service}.log"
    local tracker_seen_file
    tracker_seen_file=$(mktemp)
    if ! list_rally_context_project_ids > "${tracker_seen_file}"; then
        : > "${tracker_seen_file}"
        log "  ${service}: WARNING - failed to snapshot pre-run Rally projects"
    fi

    if [[ "${RALLY_DEBUG:-false}" == "true" ]]; then
        log "  DEBUG mode enabled. Full logs saving to ${log_file}"
        (
            rally --debug task start "${scenario_file}" --task-args-file "${task_args_file}" 2>&1 | tee "${log_file}" /dev/stderr >/dev/null
        ) &
    else
        rally task start "${scenario_file}" --task-args-file "${task_args_file}" > "${log_file}" 2>&1 &
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

    task_uuid=$(grep -oP 'Task \K[a-f0-9-]+' "${log_file}" | head -1) || true

    if [[ -z "${task_uuid}" ]]; then
        # Try to get latest task UUID
        task_uuid=$(rally task list --uuids-only 2>/dev/null | tail -1) || true
    fi

    if [[ -n "${task_uuid}" ]]; then
        # Export JSON results
        rally task results "${task_uuid}" > "${result_file}" 2>/dev/null || true

        # Generate HTML report
        rally task report "${task_uuid}" --out "${html_file}" 2>/dev/null || true

        # Get task status
        local status
        status=$(rally task status "${task_uuid}" 2>/dev/null | awk '{print $NF}') || status="unknown"

        log "  ${service}: task=${task_uuid} status=${status}"
    else
        log "  ${service}: FAILED to start task (no task UUID found)"
        # Log full task output for debugging
        if [[ -f "${log_file}" ]]; then
            log "  --- FULL ${service} task log ---"
            while IFS= read -r line; do log "    ${line}"; done < "${log_file}"
            log "  --- end ---"
        fi
    fi
}

# --------------------------------------------------------------------------
# 3. Build summary JSON from all results
# --------------------------------------------------------------------------
build_summary() {
    log "Building summary..."
    local summary='{"timestamp":"'"${TIMESTAMP}"'","services":{}}'

    for service in "${SERVICES[@]}"; do
        local result_file="${RUN_DIR}/${service}.json"
        local svc_status="skipped"
        local duration=0
        local total_iterations=0
        local failed_iterations=0
        local sla_passed="true"
        local scenarios_detail="[]"

        if [[ -f "${result_file}" ]]; then
            # Verify if file is valid JSON array (crashed tasks return plain text)
            if ! jq -e '. | type == "array"' "${result_file}" >/dev/null 2>&1; then
                svc_status="failed"
                log "  ${service}: result file is not valid JSON array"
                # Log first 5 lines of the file for debug
                head -5 "${result_file}" | while IFS= read -r line; do log "    ${line}"; done
            else
                # Parse Rally JSON results
                svc_status="passed"

                # Extract per-scenario metrics using Rally's actual JSON structure
                scenarios_detail=$(jq -c '
                    [.[] | {
                        name: .key.name,
                        duration: .full_duration,
                        iterations: (if (.result | length) > 0 then (.result | length) else (.key.kw.runner.times // 0) end),
                        failures: ([.result[]? | select(.error | length > 0)] | length),
                        sla: (([.sla[] | select(.success == true)] | length) == ([.sla[]] | length))
                    }]
                ' "${result_file}" 2>/dev/null) || scenarios_detail="[]"

                # Aggregate metrics
                duration=$(echo "${scenarios_detail}" | jq '[.[].duration] | add // 0' 2>/dev/null) || duration=0
                total_iterations=$(echo "${scenarios_detail}" | jq '[.[].iterations] | add // 0' 2>/dev/null) || total_iterations=0
                failed_iterations=$(echo "${scenarios_detail}" | jq '[.[].failures] | add // 0' 2>/dev/null) || failed_iterations=0
                sla_passed=$(echo "${scenarios_detail}" | jq 'all(.[]; .sla == true)' 2>/dev/null) || sla_passed="false"

                if [[ "${failed_iterations}" -gt 0 ]] || [[ "${sla_passed}" != "true" ]] || [[ "${total_iterations}" -eq 0 ]]; then
                    svc_status="failed"
                fi

                log "  ${service}: status=${svc_status} dur=${duration}s iters=${total_iterations} fails=${failed_iterations} sla=${sla_passed}"
            fi
        else
            log "  ${service}: no result file found"
        fi

        summary=$(echo "${summary}" | jq \
            --arg svc "${service}" \
            --arg status "${svc_status}" \
            --argjson dur "${duration}" \
            --argjson total "${total_iterations}" \
            --argjson failed "${failed_iterations}" \
            --argjson sla "${sla_passed}" \
            --argjson scenarios "${scenarios_detail}" \
            '.services[$svc] = {
                status: $status,
                duration: $dur,
                total_iterations: $total,
                failed_iterations: $failed,
                sla_passed: $sla,
                scenarios: $scenarios
            }')
    done

    echo "${summary}" | jq '.' > "${RUN_DIR}/summary.json.tmp" && mv "${RUN_DIR}/summary.json.tmp" "${RUN_DIR}/summary.json"

    # Update latest summary symlink
    cp "${RUN_DIR}/summary.json" "${SUMMARY_FILE}.tmp" && mv "${SUMMARY_FILE}.tmp" "${SUMMARY_FILE}"
    log "Summary written to ${RUN_DIR}/summary.json"
}

# --------------------------------------------------------------------------
# 4. Publish static JSON files for the dashboard
# --------------------------------------------------------------------------
ensure_cleanup_metrics_file() {
    local cleanup_file="${RESULTS_DIR}/cleanup_metrics.json"
    [[ -f "${cleanup_file}" ]] && return 0

    cat > "${cleanup_file}.tmp" <<'EOF'
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
EOF
    mv "${cleanup_file}.tmp" "${cleanup_file}"
    log "Seeded missing cleanup metrics file at ${cleanup_file}"
}

publish_dashboard_files() {
    log "Publishing dashboard static files..."
    local cleanup_file="${RESULTS_DIR}/cleanup_metrics.json"
    ensure_cleanup_metrics_file

    # Write into the persistent results volume so files survive container restarts.
    # /dashboard/results.json and /dashboard/history.json are symlinks pointing here.

    # results.json: combined summary + cleanup for the current-run card view
    jq -n \
        --slurpfile summary "${SUMMARY_FILE}" \
        --slurpfile cleanup "${cleanup_file}" \
        '{summary: $summary[0], cleanup: $cleanup[0]}' \
        > "${RESULTS_DIR}/results.json.tmp" && mv "${RESULTS_DIR}/results.json.tmp" "${RESULTS_DIR}/results.json"

    # history.json: all retained per-run summary files for the timeline
    find "${RESULTS_DIR}" -maxdepth 2 -name "summary.json" \
        -path "*/20*T*Z/*" | sort | \
        xargs jq -s '{runs: .}' \
        > "${RESULTS_DIR}/history.json.tmp" && mv "${RESULTS_DIR}/history.json.tmp" "${RESULTS_DIR}/history.json"

    log "Dashboard files published to ${RESULTS_DIR}"
}

# --------------------------------------------------------------------------
# 5. Prune old results beyond retention period
# --------------------------------------------------------------------------
prune_old_results() {
    log "Pruning results older than ${RETENTION_DAYS} days..."
    find "${RESULTS_DIR}" -maxdepth 1 -type d -name '????????T??????Z' -mtime +"${RETENTION_DAYS}" -exec rm -rf {} + 2>/dev/null || true
    prune_rally_project_ledger
    log "Pruning complete"
}

prune_rally_project_ledger() {
    [[ -f "${RALLY_PROJECT_LEDGER_FILE}" ]] || return 0

    local cutoff
    cutoff=$(date -u -d "${RETENTION_DAYS} days ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null) || {
        log "Skipping Rally project ledger pruning (date arithmetic unavailable)"
        return 0
    }

    awk -v cutoff="${cutoff}" '
        NF == 1 { print; next }
        NF >= 2 && $1 >= cutoff { print }
    ' "${RALLY_PROJECT_LEDGER_FILE}" > "${RALLY_PROJECT_LEDGER_FILE}.tmp" \
        && mv "${RALLY_PROJECT_LEDGER_FILE}.tmp" "${RALLY_PROJECT_LEDGER_FILE}"
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
        local error_detail=""
        if [[ -f "${RUN_DIR}/deployment.log" ]]; then
            error_detail=$(tail -5 "${RUN_DIR}/deployment.log" | tr '\n' ' ' | head -c 500)
        fi
        jq -n \
            --arg ts "${TIMESTAMP}" \
            --arg err "deployment_setup_failed" \
            --arg detail "${error_detail}" \
            '{timestamp: $ts, error: $err, error_detail: $detail, services: {}}' \
            > "${RUN_DIR}/summary.json.tmp" && mv "${RUN_DIR}/summary.json.tmp" "${RUN_DIR}/summary.json"
        cp "${RUN_DIR}/summary.json" "${SUMMARY_FILE}.tmp" && mv "${SUMMARY_FILE}.tmp" "${SUMMARY_FILE}"
        exit 1
    }

    # Create task args file once — all services share the same scenario parameters.
    local task_args_file="${RUN_DIR}/task_args.json"
    cat <<EOF > "${task_args_file}"
{
    "env": {
        "RALLY_NOVA_FLAVOR": "${RALLY_NOVA_FLAVOR:-m1.tiny}",
        "RALLY_NOVA_IMAGE": "${RALLY_NOVA_IMAGE:-cirros-0.6.2-x86_64-disk}",
        "OS_AUTH_URL": "${OS_AUTH_URL:-}"
    }
}
EOF

    # Run all service tests
    for service in "${SERVICES[@]}"; do
        run_service_tests "${service}" "${task_args_file}" || true
    done

    # Build summary
    build_summary

    # Check cleanup
    check_cleanup

    # Auto-purge rally-owned RGW orphans (only when RGW admin is configured)
    auto_purge_rgw

    # Publish static files for the dashboard (replaces API calls)
    publish_dashboard_files

    # Prune old results
    prune_old_results

    RUN_END_EPOCH=$(date +%s)
    RUN_DURATION=$(( RUN_END_EPOCH - RUN_START_EPOCH ))
    # Inject run_duration_seconds into the latest_summary.json
    jq --argjson dur "${RUN_DURATION}" '. + {run_duration_seconds: $dur}' \
        "${SUMMARY_FILE}" > "${SUMMARY_FILE}.tmp" && mv "${SUMMARY_FILE}.tmp" "${SUMMARY_FILE}"
    log "Total run duration: ${RUN_DURATION}s"

    log "=========================================="
    log "Rally Test Run Complete"
    log "=========================================="
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
