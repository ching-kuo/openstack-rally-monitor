#!/usr/bin/env bash
# shellcheck shell=bash
# ==============================================================================
# Rally run bookkeeping helpers -- sourced by run_tests.sh, never run directly.
# ==============================================================================
# Everything that turns a finished (or aborted) run into published state:
# build_summary, the smoke-test uptime ledger, the dashboard JSON publish,
# pruning, run_state.json, and the abort path. Uses run_tests.sh's globals
# (TIMESTAMP, RUN_DIR, RESULTS_DIR, SUMMARY_FILE, SERVICES, ...) and its log().
# --------------------------------------------------------------------------
# Log tail for error_detail
# --------------------------------------------------------------------------
# Print the last 5 lines of a log as one line (newlines -> spaces), cut to 500
# characters. The cut is by character, not byte, so a multi-byte character is
# never split, and jq -R replaces invalid UTF-8 with U+FFFD: the text goes into
# JSON summaries and render_report.py arguments, which both need valid UTF-8.
# Prints nothing for a missing file. Used for an aborted run's error_detail and
# for a task that never started.
log_tail() {
    local file="$1"
    [[ -r "${file}" ]] || return 0
    tail -n 5 "${file}" | jq -Rrs 'gsub("\n"; " ") | sub(" +$"; "") | .[0:500]'
}

# --------------------------------------------------------------------------
# 3. Build summary JSON from all results
# --------------------------------------------------------------------------
build_summary() {
    log "Building summary..."
    local summary='{"timestamp":"'"${TIMESTAMP}"'","services":{}}'

    for service in "${SERVICES[@]}"; do
        local result_file="${RUN_DIR}/${service}.json"
        local start_failed_file="${RUN_DIR}/${service}.start_failed"
        local svc_status="skipped"
        local svc_error=""
        local svc_error_detail=""
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

                # Extract per-scenario metrics using Rally's actual JSON structure.
                # first_error: the message of the first failed iteration, so the
                # dashboard modal can show WHY a scenario failed without a shell
                # in. Rally iteration errors are arrays [type, message, traceback];
                # take .error[1] (message) and fall back to .error[0] defensively
                # (older/edge shapes may carry a single element). Truncated to 300
                # chars server-side so summaries stay small. Empty string for a
                # passing scenario (the dashboard hides the line when empty).
                scenarios_detail=$(jq -c '
                    [.[] | {
                        name: .key.name,
                        duration: .full_duration,
                        iterations: (if (.result | length) > 0 then (.result | length) else (.key.kw.runner.times // 0) end),
                        failures: ([.result[]? | select(.error | length > 0)] | length),
                        first_error: (([.result[]? | select(.error | length > 0)][0].error // []) | (.[1] // .[0] // "") | tostring | .[0:300]),
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
        elif [[ -f "${start_failed_file}" ]]; then
            # run_service_tests found no task UUID: the task never started.
            svc_status="failed"
            svc_error="task_failed_to_start"
            # Already normalized by log_tail when run_service_tests wrote it.
            svc_error_detail=$(<"${start_failed_file}")
            log "  ${service}: status=failed (task failed to start)"
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
            --arg error "${svc_error}" \
            --arg detail "${svc_error_detail}" \
            '.services[$svc] = {
                status: $status,
                duration: $dur,
                total_iterations: $total,
                failed_iterations: $failed,
                sla_passed: $sla,
                scenarios: $scenarios
            } + (if $error == "" then {} else {error: $error, error_detail: $detail} end)')
    done

    echo "${summary}" | jq '.' > "${RUN_DIR}/summary.json.tmp" && mv "${RUN_DIR}/summary.json.tmp" "${RUN_DIR}/summary.json"

    # Update latest summary symlink
    cp "${RUN_DIR}/summary.json" "${SUMMARY_FILE}.tmp" && mv "${SUMMARY_FILE}.tmp" "${SUMMARY_FILE}"
    log "Summary written to ${RUN_DIR}/summary.json"
}

# --------------------------------------------------------------------------
# 3.5 Record run outcome in the rolling smoke-test uptime ledger
# --------------------------------------------------------------------------
# Kept separately from the per-run directories (which are pruned after
# RALLY_RESULTS_RETENTION_DAYS) so uptime can cover UPTIME_WINDOW_DAYS.
# A run counts as "passed" only when it has services, no top-level error,
# and every service passed -- a copy of ALL_GREEN_PREDICATE in announce.sh;
# keep the two in sync.
#
# The computed pass/fail is exported in the RUN_SMOKE_STATUS global so main()
# can hand the same value to notify.sh without re-deriving the predicate a
# third time (notify.sh is intentionally predicate-free; it just compares the
# status it is given against the last-notified one).
RUN_SMOKE_STATUS="failed"
# An optional $1 overrides the classification: abort_run passes "failed",
# because if it could not replace latest_summary.json that file still holds the
# PREVIOUS (possibly green) run.
record_smoke_result() {
    local status="${1:-}"
    if [[ -z "${status}" ]]; then
        status=$(jq -r '
            if (.services | length) > 0 and ((.error // null) == null)
               and (.services | to_entries | all(.value.status == "passed"))
            then "passed" else "failed" end' "${SUMMARY_FILE}" 2>/dev/null) || status="failed"
    fi
    RUN_SMOKE_STATUS="${status}"

    [[ -f "${SMOKE_HISTORY_FILE}" ]] || echo '{"runs": []}' > "${SMOKE_HISTORY_FILE}"

    if jq -c --arg ts "${TIMESTAMP}" --arg status "${status}" \
          --argjson days "${UPTIME_WINDOW_DAYS}" '
        ((now - ($days * 86400)) | strftime("%Y%m%dT%H%M%SZ")) as $cutoff
        | .runs = ([.runs[], {timestamp: $ts, status: $status}]
                   | map(select(.timestamp >= $cutoff)))
        | ([.runs[] | select(.status == "passed")] | length) as $up
        | .uptime = {
            window_days: $days,
            runs_total: (.runs | length),
            runs_passed: $up,
            percent: (if (.runs | length) > 0
                      then (10000 * $up / (.runs | length) | round / 100)
                      else null end)
        }' "${SMOKE_HISTORY_FILE}" > "${SMOKE_HISTORY_FILE}.tmp"; then
        mv "${SMOKE_HISTORY_FILE}.tmp" "${SMOKE_HISTORY_FILE}"
        log "Smoke uptime ledger updated (status=${status}, window=${UPTIME_WINDOW_DAYS}d)"
    else
        rm -f "${SMOKE_HISTORY_FILE}.tmp"
        log "WARNING: failed to update smoke uptime ledger"
        return 0
    fi

    # Keep the published uptime in sync directly. The deployment-failure path
    # now calls publish_dashboard_files() right after this (so results.json is
    # rewritten there too), but this standalone sync is retained so a direct
    # `record_smoke_result` invocation -- and any future early-exit path -- still
    # refreshes an existing results.json without depending on a later publish.
    local results_file="${RESULTS_DIR}/results.json"
    [[ -f "${results_file}" ]] || return 0
    if jq --slurpfile smoke "${SMOKE_HISTORY_FILE}" \
          '.uptime = ($smoke[0].uptime // null)' \
          "${results_file}" > "${results_file}.tmp"; then
        mv "${results_file}.tmp" "${results_file}"
    else
        rm -f "${results_file}.tmp"
        log "WARNING: failed to refresh uptime in results.json"
    fi
}

# --------------------------------------------------------------------------
# 4. Publish static JSON files for the dashboard
# --------------------------------------------------------------------------
ensure_cleanup_metrics_file() {
    local cleanup_file="${RESULTS_DIR}/cleanup_metrics.json"
    # Same seed entrypoint.sh writes at boot (os_helpers.sh).
    if seed_cleanup_metrics "${cleanup_file}"; then
        log "Seeded missing cleanup metrics file at ${cleanup_file}"
    fi
}

publish_dashboard_files() {
    log "Publishing dashboard static files..."
    local cleanup_file="${RESULTS_DIR}/cleanup_metrics.json"
    ensure_cleanup_metrics_file

    # Write into the persistent results volume so files survive container restarts.
    # /dashboard/results.json and /dashboard/history.json are symlinks pointing here.

    [[ -f "${SMOKE_HISTORY_FILE}" ]] || echo '{"runs": []}' > "${SMOKE_HISTORY_FILE}"

    # results.json: combined summary + cleanup + smoke uptime for the current-run card view
    jq -n \
        --slurpfile summary "${SUMMARY_FILE}" \
        --slurpfile cleanup "${cleanup_file}" \
        --slurpfile smoke "${SMOKE_HISTORY_FILE}" \
        '{summary: $summary[0], cleanup: $cleanup[0], uptime: ($smoke[0].uptime // null)}' \
        > "${RESULTS_DIR}/results.json.tmp" && mv "${RESULTS_DIR}/results.json.tmp" "${RESULTS_DIR}/results.json"

    # history.json: all retained per-run summary files for the timeline.
    # xargs may split a long file list across several commands, so it only
    # concatenates (cat); a single jq -s then builds one document. `xargs jq -s`
    # would emit one {runs: ...} per batch -- invalid JSON past ~3k runs.
    # Compact (-c) like the other history files: the dashboard re-fetches it.
    find "${RESULTS_DIR}" -maxdepth 2 -name "summary.json" \
        -path "*/20*T*Z/*" -print0 | sort -z | \
        xargs -0 -r cat -- | jq -cs '{runs: .}' \
        > "${RESULTS_DIR}/history.json.tmp" && mv "${RESULTS_DIR}/history.json.tmp" "${RESULTS_DIR}/history.json"

    log "Dashboard files published to ${RESULTS_DIR}"
}

# --------------------------------------------------------------------------
# 5. Prune old results beyond retention period
# --------------------------------------------------------------------------
prune_old_results() {
    log "Pruning results older than ${RETENTION_DAYS} days..."
    # A run directory a root run left root-owned cannot be deleted by rally.
    # Check BEFORE deleting and skip it whole: a partial rm -rf would remove
    # the deletable files, bump the directory's mtime out of the -mtime
    # window and silence this warning on every later run. init_state.sh
    # prints the one-off chown fix at boot.
    local dir sub stuck
    while IFS= read -r dir; do
        stuck=0
        while IFS= read -r -d '' sub; do
            [[ -w "${sub}" ]] || { stuck=1; break; }
        done < <(find "${dir}" -type d -print0)
        if (( stuck )) || ! rm -rf "${dir}" 2>/dev/null; then
            log "WARNING: could not prune ${dir} (not deletable by $(id -un); see the init-state chown fix)"
        fi
    done < <(find "${RESULTS_DIR}" -maxdepth 1 -type d -name '????????T??????Z' -mtime +"${RETENTION_DAYS}")
    # The provenance ledger is pruned on its own longer window (see below).
    log "Pruning Rally project ledger older than ${PROVENANCE_RETENTION_DAYS} days..."
    prune_rally_project_ledger
    log "Pruning complete"
}

prune_rally_project_ledger() {
    [[ -f "${RALLY_PROJECT_LEDGER_FILE}" ]] || return 0

    # Prune on PROVENANCE_RETENTION_DAYS (default 90), NOT RETENTION_DAYS (7).
    # This ledger is the SOLE authorization basis for auto_purge_rgw:
    # rgw_classify_owner greps it, and unknown-owner orphans are never purged
    # (fail-closed). Pruning at the 7-day run-directory window would silently
    # degrade any RGW orphan that outlives it (container downtime, RGW creds
    # added after the fact, repeated fail-closed scan errors) from
    # rally_owned to unknown_owner -- RallyRgwOrphanedUsers would then fire
    # forever until a human intervenes. Decoupled for the same reason
    # smoke_history.json is pruned by UPTIME_WINDOW_DAYS rather than the
    # run-directory retention window (CLAUDE.md "Uptime Tracking").
    local cutoff
    cutoff=$(date -u -d "${PROVENANCE_RETENTION_DAYS} days ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null) || {
        log "Skipping Rally project ledger pruning (date arithmetic unavailable)"
        return 0
    }

    awk -v cutoff="${cutoff}" '
        NF == 1 { print; next }
        NF >= 2 && $1 >= cutoff { print }
    ' "${RALLY_PROJECT_LEDGER_FILE}" > "${RALLY_PROJECT_LEDGER_FILE}.tmp" \
        && mv "${RALLY_PROJECT_LEDGER_FILE}.tmp" "${RALLY_PROJECT_LEDGER_FILE}"
}

# Inject the freshly-written cleanup_metrics.json into the current run's
# summary.json (and latest_summary.json) so history.json carries per-run
# cleanup counts instead of showing zeros for every historical entry.
attach_cleanup_to_summary() {
    local cleanup_file="${RESULTS_DIR}/cleanup_metrics.json"
    local run_summary="${RUN_DIR}/summary.json"
    [[ -f "${cleanup_file}" ]] || return 0

    for target in "${run_summary}" "${SUMMARY_FILE}"; do
        [[ -f "${target}" ]] || continue
        if jq --slurpfile cleanup "${cleanup_file}" '. + {cleanup: $cleanup[0]}' \
                "${target}" > "${target}.tmp"; then
            mv "${target}.tmp" "${target}"
        else
            rm -f "${target}.tmp"
            log "WARNING: failed to attach cleanup metrics to ${target}"
        fi
    done
}

# --------------------------------------------------------------------------
# Run-progress state (run_state.json)
# --------------------------------------------------------------------------
# Surfaces "a Rally run is in progress" to the dashboard (a small pulsing chip)
# so the multi-minute run doesn't look like a stale/hung dashboard. Written
# atomically (tmp+mv) like every other file-drop. Only ever touched by the
# process that HOLDS the flock: write_run_state_running is called immediately
# after flock succeeds, and the EXIT trap is installed only after that, so the
# flock-contention path (early `exit 0` before we own the lock) never writes
# idle and clobbers the winning run's "running" state.
write_run_state_running() {
    if ! {
        printf '{"state":"running","started_at":"%s","timestamp":"%s"}\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${TIMESTAMP}" \
            > "${RUN_STATE_FILE}.tmp" 2>/dev/null \
            && mv "${RUN_STATE_FILE}.tmp" "${RUN_STATE_FILE}" 2>/dev/null
    }; then
        rm -f "${RUN_STATE_FILE}.tmp" 2>/dev/null
    fi
}

# EXIT-trap handler: flips state back to idle on normal exit, the
# deployment_setup_failed `exit 1`, and signals. Best-effort and never fails the
# run (the trap fires during shutdown). Safe to clobber here because we only
# reach the trap-install point while holding the flock.
write_run_state_idle() {
    if ! {
        printf '{"state":"idle","finished_at":"%s"}\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            > "${RUN_STATE_FILE}.tmp" 2>/dev/null \
            && mv "${RUN_STATE_FILE}.tmp" "${RUN_STATE_FILE}" 2>/dev/null
    }; then
        rm -f "${RUN_STATE_FILE}.tmp" 2>/dev/null
    fi
}

# --------------------------------------------------------------------------
# Finish a completed run
# --------------------------------------------------------------------------
# Prunes BEFORE publishing so history.json never lists run directories that were
# just deleted, and records run_duration_seconds before publishing so
# results.json carries it. Neither may stop the publish: their failures are
# logged and the run is still published.
finish_run() {
    prune_old_results || log "WARNING: pruning failed; publishing anyway"

    local duration=$(( $(date +%s) - RUN_START_EPOCH ))
    if ! { jq --argjson dur "${duration}" '. + {run_duration_seconds: $dur}' \
            "${SUMMARY_FILE}" > "${SUMMARY_FILE}.tmp" && mv "${SUMMARY_FILE}.tmp" "${SUMMARY_FILE}"; }; then
        rm -f "${SUMMARY_FILE}.tmp"
        log "WARNING: could not record run_duration_seconds"
    fi
    log "Total run duration: ${duration}s"

    # Publish static files for the dashboard (replaces API calls)
    publish_dashboard_files
}

# --------------------------------------------------------------------------
# Abort a run before any test ran
# --------------------------------------------------------------------------
# Writes the {timestamp, error, error_detail, services: {}} failure summary and
# runs the same bookkeeping as a completed run, then exits 1. Every consumer
# treats this shape as a failed run: the smoke ledger and announce.sh
# predicates (non-empty services required), the dashboard's getRunStatus, and
# the exporter (rally_overall_success 0, rally_run_setup_ok 0).
abort_run() {
    local error="$1" detail="$2" payload target
    # One payload, written independently to each destination: latest_summary.json
    # in RESULTS_DIR (RUN_DIR may be the very thing that failed, e.g.
    # task_args_write_failed, and the failure must still reach the exporter,
    # ledger, and dashboard) and the per-run summary.json that history.json's
    # find picks up. Never copy latest_summary.json into RUN_DIR: if replacing
    # it failed, it still holds the PREVIOUS run, which would then appear twice
    # in history.json and hide this failure.
    payload=$(jq -n \
        --arg ts "${TIMESTAMP}" \
        --arg err "${error}" \
        --arg detail "${detail}" \
        '{timestamp: $ts, error: $err, error_detail: $detail, services: {}}')
    for target in "${SUMMARY_FILE}" "${RUN_DIR}/summary.json"; do
        if ! { printf '%s\n' "${payload}" > "${target}.tmp" 2>/dev/null \
                && mv "${target}.tmp" "${target}" 2>/dev/null; }; then
            rm -f "${target}.tmp" 2>/dev/null || true
            log "ERROR: could not write ${target}"
        fi
    done
    # Count the aborted run against smoke-test uptime -- always as failed, even
    # if latest_summary.json could not be replaced (RUN_SMOKE_STATUS, which
    # notify.sh gets below, follows).
    record_smoke_result failed
    # Publish so results.json/history.json reflect the failure instead of
    # showing the last green run.
    publish_dashboard_files || log "WARNING: publishing dashboard files failed"
    # Fire a transition notification (no-op unless NOTIFY_WEBHOOK_URL is set
    # and the status actually changed). Never let it affect the run.
    /scripts/notify.sh "${RUN_SMOKE_STATUS}" || true
    exit 1
}
