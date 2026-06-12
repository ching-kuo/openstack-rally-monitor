#!/usr/bin/env bash
# ==============================================================================
# notify.sh — Webhook notification on Rally status transitions
# ==============================================================================
# Invoked from run_tests.sh::main right after record_smoke_result (on BOTH the
# normal flow and the deployment_setup_failed early-exit path), always with
# `|| true` so a notification failure never affects the run.
#
# Usage:
#   notify.sh <passed|failed>
#
# The caller passes the smoke status it just computed (RUN_SMOKE_STATUS). This
# script is intentionally PREDICATE-FREE: it does not re-derive the all-green
# predicate (that lives in run_tests.sh::record_smoke_result and announce.sh's
# ALL_GREEN_PREDICATE). It only compares the status it is given against the
# last-notified one and fires on a transition.
#
# Behavior:
#   - NOTIFY_WEBHOOK_URL unset/empty  -> silent no-op, exit 0.
#   - Otherwise compare <status> to ${RESULTS_DIR}/.last_notified_status.
#     Only a TRANSITION (passed->failed or failed->passed) sends.
#     Missing state file = baseline "passed", so a first-ever failed run
#     notifies while a first-ever green run does not.
#   - After a successful send (curl exit 0) the new status is written
#     atomically to the state file. On send failure the state file is left
#     UNCHANGED so the next run retries, and a warning is logged.
#
# Secrets: NOTIFY_WEBHOOK_URL may embed a token and lands in /rally/rally_env
# (mode 0640) alongside OS_PASSWORD. The URL is NEVER echoed into logs — only
# "webhook notified (status=...)" / warnings are logged.
# ==============================================================================
set -euo pipefail

RESULTS_DIR="${RESULTS_DIR:-/results}"
SUMMARY_FILE="${RESULTS_DIR}/latest_summary.json"
STATE_FILE="${RESULTS_DIR}/.last_notified_status"
NOTIFY_FORMAT="${NOTIFY_FORMAT:-generic}"
NOTIFY_CURL_TIMEOUT="${NOTIFY_CURL_TIMEOUT:-10}"

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [notify] $*"
}

die() {
    log "ERROR: $*"
    exit 1
}

main() {
    local new_status="${1:-}"
    case "${new_status}" in
        passed|failed) ;;
        "") die "usage: notify.sh <passed|failed>" ;;
        *) die "invalid status '${new_status}' (must be passed|failed)" ;;
    esac

    # Unset/empty webhook URL is a silent no-op — the feature is off by default.
    if [[ -z "${NOTIFY_WEBHOOK_URL:-}" ]]; then
        exit 0
    fi

    # Missing state file => baseline "passed". This makes a first-ever failed
    # run notify (passed->failed) while a first-ever green run stays quiet
    # (passed->passed, no transition).
    local previous_status="passed"
    if [[ -f "${STATE_FILE}" ]]; then
        previous_status=$(< "${STATE_FILE}")
        # Tolerate accidental whitespace/newline in the persisted value.
        previous_status="${previous_status//[$' \t\r\n']/}"
        [[ "${previous_status}" == "passed" || "${previous_status}" == "failed" ]] \
            || previous_status="passed"
    fi

    # Transition-only semantics: identical status is a no-op (prevents spam).
    if [[ "${new_status}" == "${previous_status}" ]]; then
        exit 0
    fi

    local payload
    payload=$(build_payload "${new_status}" "${previous_status}") || {
        log "WARNING: failed to build notification payload; skipping"
        exit 0
    }

    # --fail: non-2xx HTTP is a curl error. --silent --show-error: quiet but
    # surface the error on failure. Never echo NOTIFY_WEBHOOK_URL.
    if printf '%s' "${payload}" | curl \
            --fail --silent --show-error \
            --max-time "${NOTIFY_CURL_TIMEOUT}" \
            -X POST \
            -H "Content-Type: application/json" \
            --data-binary @- \
            "${NOTIFY_WEBHOOK_URL}" >/dev/null 2>&1; then
        # Persist the new status only on success, atomically, so a failed send
        # leaves the prior baseline intact and the next run retries.
        write_state_atomic "${new_status}"
        log "webhook notified (status=${new_status}, previous=${previous_status})"
    else
        log "WARNING: webhook POST failed (status=${new_status}); state unchanged, will retry next run"
    fi
}

# Build the JSON request body. The generic shape is assembled from the summary
# file; slack/discord wrap a human-readable line. Summary path is env-overridable
# (SUMMARY_FILE) like the other scripts so tests can point it at a tmp dir.
build_payload() {
    local status="$1" previous_status="$2"
    local ts
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    # failed_services: every service whose status is not "passed" (failed,
    # skipped, pending). This mirrors the inverse of the all-green predicate
    # without duplicating it. Tolerate a missing/garbage summary file by
    # falling back to an empty list and a null error.
    local generic
    generic=$(jq -n \
        --arg status "${status}" \
        --arg previous "${previous_status}" \
        --arg ts "${ts}" \
        --arg dashboard "${NOTIFY_DASHBOARD_URL:-}" \
        --slurpfile summary_arr <(jq -c '.' "${SUMMARY_FILE}" 2>/dev/null || echo '{}') \
        '
        ($summary_arr[0] // {}) as $s
        | {
            event: "rally_status_change",
            status: $status,
            previous_status: $previous,
            timestamp: $ts,
            failed_services: [
                ($s.services // {}) | to_entries[]
                | select(.value.status != "passed") | .key
            ],
            error: ($s.error // null)
          }
        | if $dashboard == "" then . else . + {dashboard_url: $dashboard} end
        ') || return 1

    case "${NOTIFY_FORMAT}" in
        generic)
            printf '%s' "${generic}"
            ;;
        slack)
            wrap_text "${generic}" text
            ;;
        discord)
            wrap_text "${generic}" content
            ;;
        *)
            log "WARNING: unknown NOTIFY_FORMAT='${NOTIFY_FORMAT}', falling back to generic"
            printf '%s' "${generic}"
            ;;
    esac
}

# Wrap the generic payload as {"<key>": "<human-readable line>"} for chat
# webhooks (slack uses "text", discord uses "content").
wrap_text() {
    local generic="$1" key="$2"
    printf '%s' "${generic}" | jq \
        --arg key "${key}" \
        '
        (.failed_services | length) as $n
        | (if .status == "passed"
           then "Rally status recovered: all services passed (was \(.previous_status))."
           else "Rally status FAILED (was \(.previous_status)): "
                + (if .error != null then "\(.error)"
                   elif $n > 0 then "\($n) service(s) down — " + (.failed_services | join(", "))
                   else "see dashboard" end)
                + (if .dashboard_url then " | \(.dashboard_url)" else "" end)
           end) as $line
        | {($key): $line}
        '
}

write_state_atomic() {
    local value="$1"
    local tmp="${STATE_FILE}.tmp.$$"
    printf '%s\n' "${value}" > "${tmp}" && mv "${tmp}" "${STATE_FILE}"
}

main "$@"
