#!/usr/bin/env bash
# ==============================================================================
# Shared RadosGW admin helpers
# ==============================================================================
# Source-only helper library used by cleanup_monitor.sh, purge_orphans.sh
# (with purge_rgw.sh) and run_tests.sh (auto_purge_rgw, at the bottom of this file). Talks to the RGW
# admin REST API via curl + AWS SigV4 so no Ceph CLI is needed.

# curl_config_quote. Sourced here too (idempotent: defaults and functions
# only) because tests and callers may source this library on its own.
# shellcheck source=os_helpers.sh
source "$(dirname "${BASH_SOURCE[0]}")/os_helpers.sh"

RGW_RESULTS_DIR="${RESULTS_DIR:-/results}"
RGW_LEDGER_FILE="${RGW_LEDGER_FILE:-${RGW_RESULTS_DIR}/rally_project_ids.log}"
RGW_CURL_TIMEOUT="${RGW_CURL_TIMEOUT:-30}"
# Whole-request timeout for a purge-data user delete (see rgw_delete_user).
RGW_PURGE_TIMEOUT="${RGW_PURGE_TIMEOUT:-600}"
RGW_PAGE_SIZE="${RGW_PAGE_SIZE:-1000}"

RGW_LAST_FIND_ERRORS=0
# curl exit code of the most recent rgw_curl (28 = timed out: the server may
# still be executing the request, e.g. a long purge-data delete).
RGW_LAST_CURL_RC=0

rgw_log() {
    # Always write to stderr — this library's stdout is frequently redirected
    # into temp files by callers (rgw_find_orphaned_users, rgw_list_implicit_users).
    if declare -F log >/dev/null 2>&1; then
        log "$@" >&2
    else
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [rgw] $*" >&2
    fi
}

# Progress messages for operators: stdout via the caller's log() when defined
# (run_tests.sh), unlike rgw_log's stderr diagnostics.
rgw_info() {
    if declare -F log >/dev/null 2>&1; then
        log "$@"
    else
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [rgw] $*"
    fi
}

rgw_available() {
    [[ -n "${RGW_ADMIN_URL:-}" && -n "${RGW_ACCESS_KEY:-}" && -n "${RGW_SECRET_KEY:-}" ]]
}

rgw_urlencode() {
    jq -nr --arg value "$1" '$value | @uri'
}

rgw_sigv4_spec() {
    if [[ -n "${RGW_REGION:-}" ]]; then
        printf 'aws:amz:%s:s3' "${RGW_REGION}"
    else
        # When RGW_REGION is unset, let curl derive provider details from the endpoint.
        printf 'aws:amz'
    fi
}

rgw_curl() {
    local __body_var="$1" __status_var="$2" method="$3" path="$4"
    shift 4

    # Pre-set caller variables so they are never unbound even if we return early.
    printf -v "${__body_var}" '%s' ""
    printf -v "${__status_var}" '%s' "000"

    local base_url="${RGW_ADMIN_URL%/}"
    # Strip trailing /admin if present so we don't double it.
    base_url="${base_url%/admin}"
    local url
    if [[ "${path}" == /* ]]; then
        url="${base_url}/admin${path}"
    else
        url="${base_url}/admin/${path}"
    fi

    local query=""
    local pair key value
    for pair in "$@"; do
        key="${pair%%=*}"
        value="${pair#*=}"
        [[ -n "${query}" ]] && query+="&"
        query+="${key}=$(rgw_urlencode "${value}")"
    done
    [[ -n "${query}" ]] && url="${url}?${query}"

    local body_file
    body_file=$(mktemp)

    local -a curl_args=(
        --silent
        --show-error
        --location
        --request "${method}"
        --connect-timeout 10
        --max-time "${RGW_CURL_TIMEOUT}"
        --output "${body_file}"
        --write-out '%{http_code}'
        --aws-sigv4 "$(rgw_sigv4_spec)"
    )

    [[ "${OS_INSECURE:-false}" == "true" ]] && curl_args+=(--insecure)
    [[ -n "${OS_CACERT:-}" ]] && curl_args+=(--cacert "${OS_CACERT}")
    [[ -n "${OS_CERT:-}" ]] && curl_args+=(--cert "${OS_CERT}")
    [[ -n "${OS_KEY:-}" ]] && curl_args+=(--key "${OS_KEY}")

    local had_xtrace=0
    if [[ "$-" == *x* ]]; then
        had_xtrace=1
        set +x
    fi

    rgw_log "DEBUG: ${method} ${path} -> ${url}"
    local _rc_http_code _rc
    # The key pair goes in through a -K config on a process-substitution fd:
    # argv (`--user KEY:SECRET`) is readable by any local user via /proc.
    _rc_http_code=$(curl "${curl_args[@]}" \
        -K <(printf 'user = "%s"\n' \
            "$(curl_config_quote "${RGW_ACCESS_KEY}:${RGW_SECRET_KEY}")") \
        "${url}" 2>&1)
    _rc=$?
    RGW_LAST_CURL_RC="${_rc}"

    (( had_xtrace )) && set -x

    if [[ "${_rc}" -ne 0 ]]; then
        rm -f "${body_file}"
        rgw_log "RGW request failed: ${method} ${path} (curl exit ${_rc}: ${_rc_http_code})"
        return 1
    fi

    local _body
    _body=$(cat "${body_file}")
    rm -f "${body_file}"
    printf -v "${__body_var}" '%s' "${_body}"
    printf -v "${__status_var}" '%s' "${_rc_http_code}"
    rgw_log "DEBUG: ${method} ${path} -> HTTP ${_rc_http_code} (${#_body} bytes)"
}

# Project ID of an implicit-tenant uid ("<id>$<id>"); the rule itself lives
# in _rgw_implicit_pairs. A multi-line value is rejected outright so no single
# line of it can pass.
rgw_implicit_project_id() {
    [[ "$1" != *$'\n'* ]] || return 1
    local project_id
    project_id=$(_rgw_implicit_pairs <<< "$1" | cut -f2)
    [[ -n "${project_id}" ]] || return 1
    printf '%s\n' "${project_id}"
}

rgw_list_implicit_users() {
    local marker=""
    rgw_log "DEBUG: listing implicit-tenant users from RGW"
    while :; do
        local -a query_args=("list=true" "format=json" "max-entries=${RGW_PAGE_SIZE}")
        [[ -n "${marker}" ]] && query_args+=("marker=${marker}")

        local body="" http_code="000"
        if ! rgw_curl body http_code GET "/user" "${query_args[@]}"; then
            rgw_log "DEBUG: rgw_curl failed for user listing (http_code=${http_code})"
            return 1
        fi

        if [[ "${http_code}" != "200" ]]; then
            rgw_log "RGW user listing returned HTTP ${http_code}"
            return 1
        fi

        echo "${body}" | jq -r '
            def items:
                if type == "array" then .
                elif .keys? then .keys
                elif .users? then .users
                else []
                end;
            items[]
            | if type == "string" then . else (.uid // .user_id // .id // empty) end
            # \A..\z, not ^..$: in jq (Oniguruma) `$` also matches before a
            # trailing newline, so "a$a\n" would print as the uid "a$a".
            | select(test("\\A([A-Za-z0-9-]+)\\$\\1\\z"))
        ' || return 1

        local truncated next_marker
        truncated=$(echo "${body}" | jq -r 'if (.truncated // false) then "true" else "false" end') || return 1
        [[ "${truncated}" == "true" ]] || break

        next_marker=$(echo "${body}" | jq -r '
            .marker // .next_marker // (
                ((.keys // .users // [])[-1] // empty)
                | if type == "string" then . else (.uid // .user_id // .id // empty) end
            ) // empty
        ') || return 1

        if [[ -z "${next_marker}" ]]; then
            rgw_log "RGW user listing was truncated without a continuation marker"
            return 1
        fi
        marker="${next_marker}"
    done
}

rgw_list_user_buckets() {
    local uid="$1"
    local merged_file page_file tmp_file
    merged_file=$(mktemp)
    printf '[]\n' > "${merged_file}"

    local marker=""
    while :; do
        local -a query_args=(
            "uid=${uid}"
            "stats=true"
            "format=json"
            "max-entries=${RGW_PAGE_SIZE}"
        )
        [[ -n "${marker}" ]] && query_args+=("marker=${marker}")

        local body="" http_code="000"
        if ! rgw_curl body http_code GET "/bucket" "${query_args[@]}"; then
            rgw_log "DEBUG: rgw_curl failed for bucket listing uid=${uid} (http_code=${http_code})"
            rm -f "${merged_file}"
            return 1
        fi

        case "${http_code}" in
            200) ;;
            404)
                cat "${merged_file}"
                rm -f "${merged_file}"
                return 0
                ;;
            *)
                rgw_log "RGW bucket listing for ${uid} returned HTTP ${http_code}"
                rm -f "${merged_file}"
                return 1
                ;;
        esac

        page_file=$(mktemp)
        echo "${body}" | jq -c '
            def items:
                if type == "array" then .
                elif .buckets? then .buckets
                elif .bucket? then [.] 
                else []
                end;
            items
            | map({
                name: (.bucket // .name // .bucket_name // empty),
                tenant: (.tenant // .bucket_tenant // empty),
                num_objects: (.usage."rgw.main".num_objects // .usage.main.num_objects // .stats.num_objects // 0)
            } | select(.name != ""))
        ' > "${page_file}" || {
            rm -f "${merged_file}" "${page_file}"
            return 1
        }

        tmp_file=$(mktemp)
        jq -s '.[0] + .[1]' "${merged_file}" "${page_file}" > "${tmp_file}" || {
            rm -f "${merged_file}" "${page_file}" "${tmp_file}"
            return 1
        }
        mv "${tmp_file}" "${merged_file}"
        rm -f "${page_file}"

        # Plain arrays (e.g. [] or [{...}]) have no pagination wrapper — done.
        local body_type
        body_type=$(echo "${body}" | jq -r 'type') || break
        [[ "${body_type}" != "array" ]] || break

        local truncated next_marker
        truncated=$(echo "${body}" | jq -r 'if (.truncated // false) then "true" else "false" end') || {
            rm -f "${merged_file}"
            return 1
        }
        [[ "${truncated}" == "true" ]] || break

        next_marker=$(echo "${body}" | jq -r '
            .marker // .next_marker // (
                ((.buckets // .bucket // [])[-1] // empty)
                | (.bucket // .name // .bucket_name // empty)
            ) // empty
        ') || {
            rm -f "${merged_file}"
            return 1
        }

        if [[ -z "${next_marker}" ]]; then
            rgw_log "RGW bucket listing for ${uid} was truncated without a continuation marker"
            rm -f "${merged_file}"
            return 1
        fi
        marker="${next_marker}"
    done

    cat "${merged_file}"
    rm -f "${merged_file}"
}

# Bucket names owned by uid, one per line: GET /admin/bucket?uid= WITHOUT
# stats. With stats=true, Reef/Quincy RGWBucketAdminOp::info ignores a failed
# bucket_stats() and simply omits that bucket, so the stats listing can be
# short; without stats it dumps every name from the user's own bucket list
# (paginated server-side). Returns 0 (possibly empty), 2 user absent (404),
# 1 failure or an unexpected body.
rgw_list_user_bucket_names() {
    local uid="$1" body="" http_code="000"
    rgw_curl body http_code GET "/bucket" "uid=${uid}" "format=json" || return 1
    case "${http_code}" in
        200) ;;
        404) return 2 ;;
        *)
            rgw_log "RGW bucket name listing for ${uid} returned HTTP ${http_code}"
            return 1
            ;;
    esac
    jq -r '
        if type != "array" then error("unexpected body") else .[] end
        | if type == "string" then . else (.bucket // .name // error("unexpected entry")) end
    ' <<< "${body}" 2>/dev/null || return 1  # never let a jq code read as 2 (absent)
}

# 0 the user exists, 1 it does not (HTTP 404), 2 could not tell.
rgw_user_exists() {
    local body="" http_code="000"
    rgw_curl body http_code GET "/user" "uid=$1" "format=json" || return 2
    case "${http_code}" in
        200) return 0 ;;
        404) return 1 ;;
        *) return 2 ;;
    esac
}

# Delete an RGW user. With purge_data=true RGW also removes every bucket (and
# its objects) the user owns, in the same request.
#
# Buckets are deliberately NOT deleted one by one via DELETE /admin/bucket.
# Implicit-tenant users' buckets live in tenant <project_id>, and before Ceph
# Squid (19) RGWOp_Bucket_Remove ignores any tenant argument and looks the
# bucket name up in the EMPTY tenant -- so on Reef/Quincy a name-only delete
# would hit a different (global-tenant) bucket of the same name, or 404.
# Removing the user with purge-data walks the user's own bucket listing
# (tenant-correct, ownership-scoped) on every release. Without purge_data,
# RGW refuses to remove a user that still owns buckets ("must specify purge
# data to remove user with buckets"), which purge_rgw.sh relies on to
# fail closed for empty unknown-owner orphans that gained a bucket after the
# Phase 1 snapshot. Admin ops reference (Remove User / Remove Bucket):
# https://docs.ceph.com/en/latest/radosgw/adminops/
#
# purge-data removes every object of every bucket inside ONE request, so it
# gets RGW_PURGE_TIMEOUT (default 600 s) instead of the 30 s per-request
# RGW_CURL_TIMEOUT that suits listings.
#
# Returns 0 deleted, 2 already absent (HTTP 404: e.g. purged by a concurrent
# run -- the desired end state, but nothing was deleted, so callers must not
# count it), 3 client-side timeout (curl exit 28: RGW may still be deleting),
# 1 any other failure.
rgw_delete_user() {
    local uid="$1" purge_data="${2:-false}"
    local body="" http_code="000"
    local -a args=("uid=${uid}" "format=json")
    # Dynamically scoped: rgw_curl reads RGW_CURL_TIMEOUT.
    local RGW_CURL_TIMEOUT="${RGW_CURL_TIMEOUT}"
    if [[ "${purge_data}" == "true" ]]; then
        args+=("purge-data=true")
        RGW_CURL_TIMEOUT="${RGW_PURGE_TIMEOUT}"
    fi
    if ! rgw_curl body http_code DELETE "/user" "${args[@]}"; then
        [[ "${RGW_LAST_CURL_RC}" == 28 ]] && return 3
        return 1
    fi
    case "${http_code}" in
        200|204) return 0 ;;
        404)
            rgw_log "RGW user ${uid} was already absent (HTTP 404)"
            return 2
            ;;
        *)
            rgw_log "RGW user deletion for ${uid} returned HTTP ${http_code}"
            return 1
            ;;
    esac
}

rgw_count_buckets() {
    echo "$1" | jq 'length' 2>/dev/null || echo 0
}

rgw_count_objects() {
    echo "$1" | jq '[.[].num_objects // 0] | add // 0' 2>/dev/null || echo 0
}

rgw_classify_owner() {
    local uid="$1"
    local project_id
    project_id=$(rgw_implicit_project_id "${uid}" 2>/dev/null) || { echo "unknown_owner"; return; }
    if rgw_is_rally_owned "${project_id}"; then
        echo "rally_owned"
    else
        echo "unknown_owner"
    fi
}

# Per-project existence checks for project IDs on stdin, all over ONE SDK
# session (keystone_projects.py --check). Prints "<id> exists|gone|error";
# an ID that gets no line is inconclusive for the caller.
rgw_check_keystone_projects() {
    python3 "${RGW_HELPERS_DIR}/keystone_projects.py" --check
}

rgw_is_rally_owned() {
    local project_id="$1"
    [[ -f "${RGW_LEDGER_FILE}" ]] || return 1

    awk -v project_id="${project_id}" '
        ($1 == project_id) || ($2 == project_id) { found = 1; exit 0 }
        END { exit found ? 0 : 1 }
    ' "${RGW_LEDGER_FILE}"
}

RGW_HELPERS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Every Keystone project ID, one per line. Via keystone_projects.py rather than
# `openstack project list`: Keystone's list_limit silently truncates the list
# (no pagination for projects) and the CLI hides the "truncated" flag, so live
# projects would look deleted -- and their RGW users orphaned. The script
# exits nonzero on truncation, which sends callers to the per-project lookups.
rgw_list_keystone_project_ids() {
    python3 "${RGW_HELPERS_DIR}/keystone_projects.py"
}

# Pre-fetch all Keystone project IDs into a temp file for the bulk anti-join
# in rgw_find_orphaned_users, which falls back to per-project lookups if this
# fails.
_RGW_KEYSTONE_PROJECTS_FILE=""

rgw_prefetch_keystone_projects() {
    _RGW_KEYSTONE_PROJECTS_FILE=$(mktemp)
    # An empty "successful" listing is implausible (we authenticate inside a
    # project) and would make every user look orphaned, so it counts as a
    # failure and the authoritative per-project lookups take over.
    if rgw_list_keystone_project_ids > "${_RGW_KEYSTONE_PROJECTS_FILE}" \
            && [[ -s "${_RGW_KEYSTONE_PROJECTS_FILE}" ]]; then
        return 0
    fi
    rm -f "${_RGW_KEYSTONE_PROJECTS_FILE}"
    _RGW_KEYSTONE_PROJECTS_FILE=""
    return 1
}

rgw_cleanup_keystone_cache() {
    [[ -n "${_RGW_KEYSTONE_PROJECTS_FILE}" ]] && rm -f "${_RGW_KEYSTONE_PROJECTS_FILE}"
    _RGW_KEYSTONE_PROJECTS_FILE=""
}

# "<uid>\t<project_id>" for every implicit-tenant uid on stdin -- the one
# definition of an implicit-tenant uid (rgw_implicit_project_id wraps it):
# split on the FIRST '$', both halves equal, no further '$', non-empty,
# ^[A-Za-z0-9-]+$ (C locale so ranges stay ASCII). One awk pass, not a
# subshell per uid.
_rgw_implicit_pairs() {
    LC_ALL=C awk '
        {
            uid = $0
            pos = index(uid, "$")
            if (pos == 0) next
            first = substr(uid, 1, pos - 1)
            second = substr(uid, pos + 1)
            if (first == "" || first != second || index(second, "$") > 0) next
            if (first !~ /^[A-Za-z0-9-]+$/) next
            print uid "\t" first
        }
    '
}

# Print implicit-tenant uids whose Keystone project is CONFIRMED gone.
# RGW_LAST_FIND_ERRORS counts inconclusive lookups (fail-closed callers skip
# deletion when it is nonzero).
rgw_find_orphaned_users() {
    RGW_LAST_FIND_ERRORS=0

    local users_file pairs_file rc=0
    users_file=$(mktemp)
    pairs_file=$(mktemp)
    if ! rgw_list_implicit_users > "${users_file}" \
            || ! _rgw_implicit_pairs < "${users_file}" > "${pairs_file}"; then
        RGW_LAST_FIND_ERRORS=1
        rm -f "${users_file}" "${pairs_file}"
        return 1
    fi
    rm -f "${users_file}"

    if rgw_prefetch_keystone_projects; then
        # Anti-join: whole-line project membership (a per-uid subshell + grep
        # took ~40s at 5k users).
        LC_ALL=C awk -F'\t' '
            FILENAME == ARGV[1] { projects[$0] = 1; next }
            !($2 in projects) { print $1 }
        ' "${_RGW_KEYSTONE_PROJECTS_FILE}" "${pairs_file}" || rc=$?
        rm -f "${pairs_file}"
        rgw_cleanup_keystone_cache
        if [[ "${rc}" -ne 0 ]]; then
            RGW_LAST_FIND_ERRORS=1
            return 1
        fi
        return 0
    fi

    # Bulk list failed or was truncated: check each distinct project over one
    # SDK session. Only an explicit "gone" (HTTP 404) makes an orphan; an
    # "error", a missing answer, or a failed checker is inconclusive and
    # counted, so callers fail closed.
    rgw_log "Keystone bulk project list failed or was truncated; falling back to per-project lookups"
    local status_file
    status_file=$(mktemp)
    if [[ -s "${pairs_file}" ]]; then
        cut -f2 "${pairs_file}" | LC_ALL=C sort -u | rgw_check_keystone_projects > "${status_file}" || true
    fi
    local result
    result=$(LC_ALL=C awk -F'\t' '
        FILENAME == ARGV[1] { split($0, f, " "); status[f[1]] = f[2]; next }
        {
            s = ($2 in status) ? status[$2] : "error"
            if (s == "gone") print "ORPHAN\t" $1
            else if (s != "exists") errors++
        }
        END { print "ERRORS\t" errors + 0 }
    ' "${status_file}" "${pairs_file}") || result=$'ERRORS\t1'
    rm -f "${status_file}" "${pairs_file}"
    RGW_LAST_FIND_ERRORS=$(awk -F'\t' '$1 == "ERRORS" { print $2 }' <<< "${result}")
    RGW_LAST_FIND_ERRORS="${RGW_LAST_FIND_ERRORS:-1}"
    awk -F'\t' '$1 == "ORPHAN" { print $2 }' <<< "${result}"
}

# --------------------------------------------------------------------------
# Auto-purge rally-owned RGW orphans (called from run_tests.sh::main)
# --------------------------------------------------------------------------
# Fail-closed: skipped entirely when the scan failed or had any inconclusive
# Keystone lookup. Only users whose project ID is in the provenance ledger
# (rgw_classify_owner == rally_owned) are deleted, with their buckets via
# purge-data (see rgw_delete_user). Unknown-owner orphans are never touched;
# purge_orphans.sh is the manual tool for those.
auto_purge_rgw() {
    rgw_available || return 0

    rgw_info "Auto-purging rally-owned RGW orphans..."

    local orphans_file
    orphans_file=$(mktemp)
    if ! rgw_find_orphaned_users > "${orphans_file}"; then
        rgw_info "WARNING: RGW orphan scan failed; skipping auto-purge"
        rm -f "${orphans_file}"
        return 0
    fi

    if [[ "${RGW_LAST_FIND_ERRORS}" -gt 0 ]]; then
        rgw_info "WARNING: RGW scan had ${RGW_LAST_FIND_ERRORS} error(s); skipping auto-purge (fail-closed)"
        rm -f "${orphans_file}"
        return 0
    fi

    local purged=0 failed=0 skipped=0 uid rc
    while IFS= read -r uid; do
        [[ -n "${uid}" ]] || continue
        if [[ "$(rgw_classify_owner "${uid}")" != "rally_owned" ]]; then
            skipped=$((skipped + 1))
            continue
        fi

        if rgw_delete_user "${uid}" true; then rc=0; else rc=$?; fi
        case "${rc}" in
            0)
                rgw_info "  RGW user ${uid}: deleted (with its buckets)"
                purged=$((purged + 1))
                ;;
            2) rgw_info "  RGW user ${uid}: already absent; nothing deleted" ;;
            *)
                rgw_info "  RGW user ${uid}: deletion FAILED"
                failed=$((failed + 1))
                ;;
        esac
    done < "${orphans_file}"

    rm -f "${orphans_file}"

    rgw_info "RGW auto-purge complete: purged=${purged} failed=${failed} skipped_unknown=${skipped}"
}
