#!/usr/bin/env bash
# ==============================================================================
# Shared helpers (source-only)
# ==============================================================================
# Sourced by run_tests.sh, cleanup_monitor.sh, purge_orphans.sh and
# entrypoint.sh so the service-set parsing, the run-lock path, the
# cleanup_metrics.json seed and the OpenStack list -> validate -> filter path
# (os_list_rally_resources) each exist exactly once.

# The monitored service set is configurable via RALLY_SERVICES (comma-separated).
# parse_rally_services normalizes it (trim/lowercase/drop-empties/dedupe, then
# drop any token not matching the ^[a-z0-9_-]+$ allowlist, order preserved);
# the same parsing rules are mirrored in api_health_check.py's
# parse_rally_services and health_fallback_filter.jq (health_check.sh's
# all-down fallback) -- keep the three in sync. The allowlist is
# path-traversal hardening: service names index rally/scenarios/<name>.yaml and
# runs/<ts>/<name>.html, so a token like "../etc" must never survive. A
# configured service with no rally/scenarios/<name>.yaml still logs a SKIP in
# run_tests.sh::run_service_tests (the operator's signal for a typo'd name);
# build_summary then reports it "skipped".
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

# The Rally run lock: held by run_tests.sh for a whole run and by
# purge_orphans.sh --confirm for a whole purge; entrypoint.sh pre-creates it
# at boot, root-owned with mode 0660 so the rally user writes it through
# GID 0 (CAP_CHOWN is dropped, so it cannot be chowned to rally). One setting for all three (override only for tests
# or a non-standard layout; it is passed to cron via /rally/rally_env).
RALLY_RUN_LOCK_FILE="${RALLY_RUN_LOCK_FILE:-/rally/logs/rally-run.lock}"

# 0 when <service> is monitored: in the normalized RALLY_SERVICES set
# (keystone always). Callers use it only to judge a missing catalog endpoint;
# every resource type is scanned regardless, because one service's scenarios
# leave orphans in another's (nova contexts create c_rally networks/routers).
os_service_monitored() {
    [[ "$1" == "keystone" ]] && return 0
    grep -Fxq -- "$1" <<< "$(parse_rally_services "${RALLY_SERVICES:-}")"
}

# Run `<cmd...> -f json` and store "<id>\t<name>" for every s_rally*/c_rally*
# entry in the variable named $1. Returns 0 on success, 2 when the service has
# no endpoint in the catalog, 1 when the call fails or its output is not a
# JSON array (so a failure never reads as "0 orphans"). A 2 is "not
# applicable" only for a service outside RALLY_SERVICES; for a monitored one
# it usually means a wrong OS_INTERFACE/OS_REGION_NAME and callers count it
# as an error (os_service_monitored). OS_LIST_LAST_ERROR holds the first line
# of stderr from the call.
OS_LIST_LAST_ERROR=""
os_list_rally_resources() {
    local __out_var="$1"; shift
    local __err_file __json __rows __rc=0
    __err_file=$(mktemp)
    __json=$("$@" -f json 2>"${__err_file}") || __rc=$?
    # shellcheck disable=SC2034  # read by the sourcing scripts
    OS_LIST_LAST_ERROR=$(head -1 "${__err_file}")
    if [[ "${__rc}" -ne 0 ]] \
            && grep -Eiq 'endpoint for .* not found|EndpointNotFound|Could not find requested endpoint' "${__err_file}"; then
        rm -f "${__err_file}"
        return 2
    fi
    rm -f "${__err_file}"
    [[ "${__rc}" -eq 0 ]] || return 1
    __rows=$(_os_rally_rows "${__json}") || return 1
    printf -v "${__out_var}" '%s' "${__rows}"
}

# One jq pass that validates the shape (non-array output fails) and prints
# "<id>\t<name>" per s_rally*/c_rally* entry. A null/missing Name counts as ""
# (no match) rather than erroring the whole program, which used to zero the
# count for its resource type. A matching entry whose ID is not a non-empty
# ^[A-Za-z0-9_-]+$ string fails the WHOLE listing: an empty ID would make
# bash `read` (IFS=tab collapses a leading tab) shift the name into the ID
# column, and a partial snapshot must never pass as complete. @tsv escapes
# tabs/newlines inside names, so the name column cannot leak either.
_os_rally_rows() {
    jq -r '
        if type != "array" then error("not a JSON array") else . end
        | .[]
        | ((.Name // "") | tostring) as $name
        | select($name | test("^[sc]_rally"))
        # \A..\z: jq `$` would also accept "id\n".
        | if (.ID | type) == "string" and (.ID | test("\\A[A-Za-z0-9_-]+\\z"))
          then [.ID, $name] | @tsv
          else error("invalid ID for \($name)") end
    ' <<< "$1" 2>/dev/null
}

# Write the "no scan yet" cleanup_metrics.json seed to $1, atomically, unless
# the file exists. Returns 0 when it seeded, 1 when the file was present. One
# definition for entrypoint.sh (boot) and run_lib.sh (publish safety net). A
# seed is a healthy "nothing scanned yet" state; the exporter treats a
# missing or corrupt file as a failed scan.
seed_cleanup_metrics() {
    local file="$1"
    [[ -f "${file}" ]] && return 1
    cat > "${file}.tmp" <<'EOF_CLEANUP_SEED'
{
    "timestamp": "waiting_for_first_run",
    "cleanup_failed": 0,
    "context_cleanup_warning": 0,
    "openstack_scan_status": "ok",
    "openstack_scan_errors": 0,
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
EOF_CLEANUP_SEED
    mv "${file}.tmp" "${file}"
}
