#!/usr/bin/env bash
# ==============================================================================
# rally-announce — Operator announcement CLI
# ==============================================================================
# Publishes dashboard announcements without introducing an HTTP write surface.
# State lives at ${RESULTS_DIR}/announcement-state.json and is read by the
# dashboard on its existing 5-minute refresh cycle.
#
# Usage:
#   announce.sh post   --type <incident|maintenance|scheduled> --body <text>
#                      [--expires-at <ISO8601-Z>] [--effective-from <ISO8601-Z>]
#   announce.sh update <id> --body <text>
#   announce.sh clear  <id>
#   announce.sh list
#   announce.sh auto-clear-if-all-green --summary-file <path>
#
# Invoke via `docker exec -u rally rally-monitor /scripts/announce.sh ...`.
# The cron user is `rally`; running as a different UID can leave a root-owned
# state file that the next cron-driven auto-clear cannot rewrite.
# ==============================================================================
set -euo pipefail

RESULTS_DIR="${RESULTS_DIR:-/results}"
STATE_FILE="${RESULTS_DIR}/announcement-state.json"
LOCK_FILE="${RESULTS_DIR}/.announce.lock"
MAX_BODY_LEN=500
ISO8601_UTC_RE='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'
VALID_TYPES_RE='^(incident|maintenance|scheduled)$'

# Set to 1 inside a flock-wrapped subshell so we don't recursively re-flock.
LOCK_HELD="${LOCK_HELD:-0}"

# Track tmp files so the trap cleans them up on any exit (including failures
# inside jq), preserving the invariant that a partial write never lands.
_TMP_FILES=()
cleanup_tmpfiles() {
    local rc=$?
    local f
    for f in "${_TMP_FILES[@]:-}"; do
        [[ -n "${f}" && -f "${f}" ]] && rm -f "${f}"
    done
    return "${rc}"
}
trap cleanup_tmpfiles EXIT

die() {
    echo "announce.sh: $*" >&2
    exit 1
}

usage() {
    cat <<'EOF' >&2
Usage:
  announce.sh post   --type <incident|maintenance|scheduled> --body <text>
                     [--expires-at <ISO8601-Z>] [--effective-from <ISO8601-Z>]
  announce.sh update <id> --body <text>
  announce.sh clear  <id>
  announce.sh list
  announce.sh auto-clear-if-all-green --summary-file <path>
EOF
}

now_iso() {
    date -u +%Y-%m-%dT%H:%M:%SZ
}

# Validate ISO 8601 UTC strictly *before* the value reaches jq. jq --arg is
# JSON-safe but accepts any string, so a bogus value like "2026-13-45T99:99:99Z"
# would write fine and produce NaN in the dashboard's Date.parse, leaving a
# banner that never expires client-side. The regex catches shape errors
# (missing Z, wrong separator); python3's datetime catches semantic errors
# (month 13, day 45, hour 99).
validate_iso8601_utc() {
    local label="$1" value="$2"
    [[ "${value}" =~ ${ISO8601_UTC_RE} ]] || die "${label} must be ISO 8601 UTC (YYYY-MM-DDTHH:MM:SSZ), got: ${value}"
    python3 -c '
import sys
from datetime import datetime
try:
    datetime.strptime(sys.argv[1], "%Y-%m-%dT%H:%M:%SZ")
except ValueError as exc:
    sys.exit(f"invalid datetime: {exc}")
' "${value}" || die "${label} is not a valid UTC datetime: ${value}"
}

trim_body() {
    # Trim leading/trailing whitespace; preserve interior whitespace verbatim.
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "${s}"
}

validate_body() {
    local body="$1"
    [[ -n "${body}" ]] || die "body cannot be empty"
    if (( ${#body} > MAX_BODY_LEN )); then
        die "body exceeds ${MAX_BODY_LEN} characters (got ${#body})"
    fi
}

# Read the current state, or return the empty default when the file is absent.
# A corrupt file is a hard error — silently overwriting could discard operator
# work mid-incident.
read_state_or_empty() {
    if [[ -f "${STATE_FILE}" ]]; then
        if ! jq -e '.announcements | type == "array"' "${STATE_FILE}" >/dev/null 2>&1; then
            die "state file ${STATE_FILE} is malformed; refusing to overwrite. Inspect and repair manually."
        fi
        cat "${STATE_FILE}"
    else
        echo '{"announcements":[]}'
    fi
}

write_state_atomic() {
    local payload="$1"
    local tmp="${STATE_FILE}.tmp.$$"
    _TMP_FILES+=("${tmp}")
    mkdir -p "$(dirname "${STATE_FILE}")"
    # Re-encode via jq so any malformed input fails before mv lands.
    printf '%s' "${payload}" | jq '.' > "${tmp}"
    mv "${tmp}" "${STATE_FILE}"
}

generate_id() {
    local type="$1"
    local ts suffix
    ts=$(date -u +%Y%m%dT%H%M%SZ)
    # 8 hex chars = 32 bits of entropy. openssl is in the base image; fallback
    # to /dev/urandom keeps the script portable for local dev environments.
    if command -v openssl >/dev/null 2>&1; then
        suffix=$(openssl rand -hex 4)
    else
        suffix=$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n')
    fi
    printf '%s-%s-%s' "${type}" "${ts}" "${suffix}"
}

# Serialize every mutating subcommand on a shared advisory lock. Operators and
# the cron-driven auto-clear may run concurrently — without locking, a cron
# auto-clear running between an operator's read-modify-write cycle would lose
# the operator's edit. flock is best-effort: if it is unavailable (rare
# minimal environments) we fall through unguarded; the single-operator model
# still makes the race rare. The lock scope wraps each mutating subcommand,
# not the entire process, so `list` does not block on a stuck mutator.
with_state_lock() {
    if [[ "${LOCK_HELD}" == "1" ]] || ! command -v flock >/dev/null 2>&1; then
        "$@"
        return $?
    fi
    mkdir -p "$(dirname "${LOCK_FILE}")"
    # Bash function can't be exec'd by flock directly; acquire the lock on a
    # dedicated FD inside a subshell, then call the function while holding it.
    # The subshell inherits LOCK_HELD=1 so nested calls don't re-lock.
    (
        LOCK_HELD=1
        flock 9
        "$@"
    ) 9>"${LOCK_FILE}"
}

# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------

cmd_post() {
    local type="" body="" expires_at="" effective_from=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --type)            type="$2"; shift 2 ;;
            --body)            body="$2"; shift 2 ;;
            --expires-at)      expires_at="$2"; shift 2 ;;
            --effective-from)  effective_from="$2"; shift 2 ;;
            *) die "unknown post flag: $1" ;;
        esac
    done

    [[ -n "${type}" ]] || die "post requires --type"
    [[ "${type}" =~ ${VALID_TYPES_RE} ]] || die "invalid --type: ${type} (must be incident|maintenance|scheduled)"

    body=$(trim_body "${body}")
    validate_body "${body}"

    case "${type}" in
        incident)
            [[ -z "${expires_at}"     ]] || die "incident announcements do not accept --expires-at"
            [[ -z "${effective_from}" ]] || die "incident announcements do not accept --effective-from"
            ;;
        maintenance)
            [[ -n "${expires_at}"     ]] || die "maintenance requires --expires-at"
            [[ -z "${effective_from}" ]] || die "maintenance announcements do not accept --effective-from"
            validate_iso8601_utc "--expires-at" "${expires_at}"
            ;;
        scheduled)
            [[ -n "${expires_at}"     ]] || die "scheduled requires --expires-at"
            [[ -n "${effective_from}" ]] || die "scheduled requires --effective-from"
            validate_iso8601_utc "--expires-at"     "${expires_at}"
            validate_iso8601_utc "--effective-from" "${effective_from}"
            ;;
    esac

    local id current_state new_state created_at
    id=$(generate_id "${type}")
    created_at=$(now_iso)
    current_state=$(read_state_or_empty)

    new_state=$(jq \
        --arg id "${id}" \
        --arg type "${type}" \
        --arg body "${body}" \
        --arg created_at "${created_at}" \
        --arg expires_at "${expires_at}" \
        --arg effective_from "${effective_from}" \
        '
        .announcements += [{
            id: $id,
            type: $type,
            body: $body,
            created_at: $created_at,
            expires_at: (if $expires_at == "" then null else $expires_at end),
            effective_from: (if $effective_from == "" then null else $effective_from end),
            updates: []
        }]' <<<"${current_state}")

    write_state_atomic "${new_state}"
    printf '%s\n' "${id}"
}

cmd_update() {
    local id="${1:-}"
    [[ -n "${id}" ]] || die "update requires an announcement id"
    shift || true

    local body=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --body) body="$2"; shift 2 ;;
            *) die "unknown update flag: $1" ;;
        esac
    done

    body=$(trim_body "${body}")
    validate_body "${body}"

    [[ -f "${STATE_FILE}" ]] || die "no announcement with id ${id}"
    local current_state
    current_state=$(read_state_or_empty)

    # Refuse early if the id is missing or points at a scheduled record.
    local record_type
    record_type=$(jq -r --arg id "${id}" '
        (.announcements[] | select(.id == $id) | .type) // "__MISSING__"
    ' <<<"${current_state}")

    if [[ "${record_type}" == "__MISSING__" ]]; then
        die "no announcement with id ${id}"
    fi
    if [[ "${record_type}" == "scheduled" ]]; then
        die "scheduled announcements do not accept updates"
    fi

    local ts new_state
    ts=$(now_iso)
    new_state=$(jq \
        --arg id "${id}" \
        --arg body "${body}" \
        --arg ts "${ts}" \
        '
        .announcements |= map(
            if .id == $id
            then .updates += [{ ts: $ts, body: $body }]
            else .
            end
        )' <<<"${current_state}")

    write_state_atomic "${new_state}"
}

cmd_clear() {
    local id="${1:-}"
    [[ -n "${id}" ]] || die "clear requires an announcement id"

    # No state file means nothing to clear — succeed silently.
    [[ -f "${STATE_FILE}" ]] || return 0

    local current_state new_state
    current_state=$(read_state_or_empty)
    new_state=$(jq --arg id "${id}" '
        .announcements |= map(select(.id != $id))
    ' <<<"${current_state}")
    write_state_atomic "${new_state}"
}

cmd_list() {
    read_state_or_empty | jq '.'
}

# Exact predicate: an unambiguous all-green run requires non-empty .services,
# no top-level .error field, and every service value's status == "passed".
# The empty-services and .error checks guard against the deployment_setup_failed
# summary shape (services: {}, error: "deployment_setup_failed") — `jq all(...)`
# returns true on empty input, which would silently erase the very banner the
# operator posted to explain the failure.
# NOTE: run_tests.sh::record_smoke_result duplicates this predicate for its
# uptime pass/fail classification — keep the two in sync.
ALL_GREEN_PREDICATE='
    (.services | length) > 0
    and (.error // null) == null
    and (.services | to_entries | all(.value.status == "passed"))
'

cmd_auto_clear_if_all_green() {
    local summary_file=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --summary-file) summary_file="$2"; shift 2 ;;
            *) die "unknown auto-clear-if-all-green flag: $1" ;;
        esac
    done

    [[ -n "${summary_file}" ]] || summary_file="${RESULTS_DIR}/latest_summary.json"

    # Nothing to do without a state file. Idle dashboard never writes one.
    [[ -f "${STATE_FILE}" ]] || return 0

    if [[ ! -f "${summary_file}" ]]; then
        echo "announce.sh: auto-clear skipped (summary file missing: ${summary_file})" >&2
        return 0
    fi

    local is_green
    if ! is_green=$(jq -e "${ALL_GREEN_PREDICATE}" "${summary_file}" 2>/dev/null); then
        # Either malformed JSON or predicate was false — both are no-ops.
        if ! jq -e . "${summary_file}" >/dev/null 2>&1; then
            echo "announce.sh: auto-clear skipped (summary file malformed: ${summary_file})" >&2
        fi
        return 0
    fi

    [[ "${is_green}" == "true" ]] || return 0

    local current_state new_state
    current_state=$(read_state_or_empty)
    new_state=$(jq '
        .announcements |= map(select(.type != "incident"))
    ' <<<"${current_state}")

    write_state_atomic "${new_state}"
}

# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

main() {
    [[ $# -gt 0 ]] || { usage; exit 64; }
    local subcommand="$1"; shift
    case "${subcommand}" in
        post)                     with_state_lock cmd_post "$@" ;;
        update)                   with_state_lock cmd_update "$@" ;;
        clear)                    with_state_lock cmd_clear "$@" ;;
        list)                     cmd_list "$@" ;;
        auto-clear-if-all-green)  with_state_lock cmd_auto_clear_if_all_green "$@" ;;
        -h|--help)                usage; exit 0 ;;
        *) usage; die "unknown subcommand: ${subcommand}" ;;
    esac
}

main "$@"
