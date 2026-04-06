#!/usr/bin/env bash
# ==============================================================================
# Shared RadosGW admin helpers
# ==============================================================================
# Source-only helper library used by cleanup_monitor.sh and purge_orphans.sh.
# Talks to the RGW admin REST API via curl + AWS SigV4 so no Ceph CLI is needed.

RGW_RESULTS_DIR="${RESULTS_DIR:-/results}"
RGW_LEDGER_FILE="${RGW_LEDGER_FILE:-${RGW_RESULTS_DIR}/rally_project_ids.log}"
RGW_CURL_TIMEOUT="${RGW_CURL_TIMEOUT:-30}"
RGW_PAGE_SIZE="${RGW_PAGE_SIZE:-1000}"

RGW_LAST_FIND_ERRORS=0

rgw_log() {
    # Always write to stderr — this library's stdout is frequently redirected
    # into temp files by callers (rgw_find_orphaned_users, rgw_list_implicit_users).
    if declare -F log >/dev/null 2>&1; then
        log "$@" >&2
    else
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [rgw] $*" >&2
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
    local url
    if [[ "${path}" == /* ]]; then
        url="${base_url}${path}"
    else
        url="${base_url}/${path}"
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
        --user "${RGW_ACCESS_KEY}:${RGW_SECRET_KEY}"
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
    _rc_http_code=$(curl "${curl_args[@]}" "${url}" 2>&1)
    _rc=$?

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

rgw_implicit_project_id() {
    local uid="$1"
    if [[ "${uid}" != *'$'* ]]; then
        return 1
    fi

    local first="${uid%%\$*}"
    local second="${uid#*\$}"
    if [[ -n "${first}" && "${first}" == "${second}" && "${second}" != *'$'* && "${first}" =~ ^[A-Za-z0-9-]+$ ]]; then
        printf '%s\n' "${first}"
        return 0
    fi
    return 1
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
            | select(test("^([A-Za-z0-9-]+)\\$\\1$"))
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

rgw_delete_bucket() {
    local bucket_name="$1"
    local body="" http_code="000"
    rgw_curl body http_code DELETE "/bucket" "bucket=${bucket_name}" "purge-objects=true" "format=json" || return 1
    case "${http_code}" in
        200|204|404) return 0 ;;
        *)
            rgw_log "RGW bucket deletion for ${bucket_name} returned HTTP ${http_code}"
            return 1
            ;;
    esac
}

rgw_delete_user() {
    local uid="$1"
    local body="" http_code="000"
    rgw_curl body http_code DELETE "/user" "uid=${uid}" "format=json" || return 1
    case "${http_code}" in
        200|204|404) return 0 ;;
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
    if [[ -n "${project_id}" ]] && rgw_is_rally_owned "${project_id}"; then
        echo "rally_owned"
    else
        echo "unknown_owner"
    fi
}

rgw_check_keystone_project() {
    local project_id="$1"
    local output
    if output=$(openstack project show "${project_id}" -f value -c id 2>&1); then
        return 0
    fi

    if echo "${output}" | grep -Eiq 'No project with a name or ID|No project found|Could not find resource|does not exist|Resource not found|HttpException.*404|HTTP 404'; then
        return 1
    fi

    rgw_log "Keystone lookup failed for project ${project_id}: $(echo "${output}" | tr '\n' ' ')"
    return 2
}

rgw_is_rally_owned() {
    local project_id="$1"
    [[ -f "${RGW_LEDGER_FILE}" ]] || return 1

    awk -v project_id="${project_id}" '
        ($1 == project_id) || ($2 == project_id) { found = 1; exit 0 }
        END { exit found ? 0 : 1 }
    ' "${RGW_LEDGER_FILE}"
}

# Pre-fetch all Keystone project IDs into a temp file for bulk membership checks.
# Falls back to per-project lookups if the bulk list fails.
_RGW_KEYSTONE_PROJECTS_FILE=""

rgw_prefetch_keystone_projects() {
    _RGW_KEYSTONE_PROJECTS_FILE=$(mktemp)
    if openstack project list -f value -c ID > "${_RGW_KEYSTONE_PROJECTS_FILE}" 2>/dev/null; then
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

# Returns: 0 = exists, 1 = authoritative 404, 2 = inconclusive
rgw_check_keystone_project_cached() {
    local project_id="$1"
    if [[ -n "${_RGW_KEYSTONE_PROJECTS_FILE}" ]]; then
        if grep -Fxq "${project_id}" "${_RGW_KEYSTONE_PROJECTS_FILE}"; then
            return 0
        fi
        return 1
    fi
    rgw_check_keystone_project "${project_id}"
}

rgw_find_orphaned_users() {
    RGW_LAST_FIND_ERRORS=0

    local users_file
    users_file=$(mktemp)
    if ! rgw_list_implicit_users > "${users_file}"; then
        RGW_LAST_FIND_ERRORS=1
        rm -f "${users_file}"
        return 1
    fi

    local bulk_ok=0
    if rgw_prefetch_keystone_projects; then
        bulk_ok=1
    else
        rgw_log "Keystone bulk project list failed; falling back to per-project lookups"
    fi

    local uid project_id lookup_rc
    while IFS= read -r uid; do
        [[ -n "${uid}" ]] || continue
        project_id=$(rgw_implicit_project_id "${uid}") || continue

        if rgw_check_keystone_project_cached "${project_id}"; then
            lookup_rc=0
        else
            lookup_rc=$?
        fi
        case "${lookup_rc}" in
            0) ;;
            1) printf '%s\n' "${uid}" ;;
            2) RGW_LAST_FIND_ERRORS=$((RGW_LAST_FIND_ERRORS + 1)) ;;
        esac
    done < "${users_file}"

    rm -f "${users_file}"
    rgw_cleanup_keystone_cache
}
