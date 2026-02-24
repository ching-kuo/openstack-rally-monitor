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
RUN_LOG="${RUN_DIR}/run.log"

SERVICES=("keystone" "nova" "neutron" "glance" "cinder" "swift")

log() {
    local msg="[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
    echo "${msg}"
    # Also append to run log if it exists
    if [[ -f "${RUN_LOG}" ]]; then
        echo "${msg}" >> "${RUN_LOG}"
    fi
}

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
            cat "${deploy_log}" | while IFS= read -r line; do log "  ${line}"; done
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
        cat "${deploy_log}" | while IFS= read -r line; do log "  ${line}"; done
        return 1
    fi

    # Check the deployment
    log "Checking deployment connectivity..."
    if ! rally deployment check >> "${deploy_log}" 2>&1; then
        log "WARNING: Deployment check reported issues. Details:"
        cat "${deploy_log}" | while IFS= read -r line; do log "  ${line}"; done
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
    local service="$1"
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

    if [[ "${RALLY_DEBUG:-false}" == "true" ]]; then
        log "  DEBUG mode enabled. Full logs saving to ${log_file}"
        rally --debug task start "${scenario_file}" --task-args-file "${task_args_file}" 2>&1 | tee "${log_file}" /dev/stderr >/dev/null || true
    else
        rally task start "${scenario_file}" --task-args-file "${task_args_file}" > "${log_file}" 2>&1 || true
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
        echo "${status}"
    else
        log "  ${service}: FAILED to start task (no task UUID found)"
        # Log full task output for debugging
        if [[ -f "${log_file}" ]]; then
            log "  --- FULL ${service} task log ---"
            cat "${log_file}" | while IFS= read -r line; do log "    ${line}"; done
            log "  --- end ---"
        fi
        echo "failed"
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

    echo "${summary}" | jq '.' > "${RUN_DIR}/summary.json"

    # Update latest summary symlink
    cp "${RUN_DIR}/summary.json" "${SUMMARY_FILE}"
    log "Summary written to ${RUN_DIR}/summary.json"
}

# --------------------------------------------------------------------------
# 4. Publish static JSON files for the dashboard
# --------------------------------------------------------------------------
publish_dashboard_files() {
    log "Publishing dashboard static files..."
    local cleanup_file="${RESULTS_DIR}/cleanup_metrics.json"

    # Write into the persistent results volume so files survive container restarts.
    # /dashboard/results.json and /dashboard/history.json are symlinks pointing here.

    # results.json: combined summary + cleanup for the current-run card view
    jq -n \
        --slurpfile summary "${SUMMARY_FILE}" \
        --slurpfile cleanup "${cleanup_file}" \
        '{summary: $summary[0], cleanup: $cleanup[0]}' \
        > "${RESULTS_DIR}/results.json"

    # history.json: all retained per-run summary files for the timeline
    find "${RESULTS_DIR}" -maxdepth 2 -name "summary.json" \
        -path "*/20*T*Z/*" | sort | \
        xargs jq -s '{runs: .}' \
        > "${RESULTS_DIR}/history.json"

    log "Dashboard files published to ${RESULTS_DIR}"
}

# --------------------------------------------------------------------------
# 5. Prune old results beyond retention period
# --------------------------------------------------------------------------
prune_old_results() {
    log "Pruning results older than ${RETENTION_DAYS} days..."
    find "${RESULTS_DIR}" -maxdepth 1 -type d -name "20*" -mtime +"${RETENTION_DAYS}" -exec rm -rf {} + 2>/dev/null || true
    log "Pruning complete"
}

# --------------------------------------------------------------------------
# 5. Notify cleanup monitor
# --------------------------------------------------------------------------
check_cleanup() {
    log "Running cleanup monitor..."
    /scripts/cleanup_monitor.sh "${RUN_DIR}/summary.json" || {
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
            > "${RUN_DIR}/summary.json"
        cp "${RUN_DIR}/summary.json" "${SUMMARY_FILE}"
        exit 1
    }

    # Run all service tests
    for service in "${SERVICES[@]}"; do
        run_service_tests "${service}" || true
    done

    # Build summary
    build_summary

    # Check cleanup
    check_cleanup

    # Publish static files for the dashboard (replaces API calls)
    publish_dashboard_files

    # Prune old results
    prune_old_results

    log "=========================================="
    log "Rally Test Run Complete"
    log "=========================================="
}

main "$@"
