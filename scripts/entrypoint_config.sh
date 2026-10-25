#!/usr/bin/env bash
# Configuration validation shared by the container entrypoint (fail fast),
# health_check.sh and run_tests.sh (or_default fallbacks), and tests.

_entrypoint_config_log() {
    local level="$1"
    shift
    if declare -F log >/dev/null 2>&1; then
        log "${level}: $*" >&2
    else
        printf '[entrypoint] %s: %s\n' "${level}" "$*" >&2
    fi
}

_normalize_bounded_decimal() {
    local raw="${1-}"
    local minimum="$2"
    local maximum="$3"
    local label="$4"
    local normalized number

    if [[ ! "${raw}" =~ ^[0-9]+$ ]]; then
        _entrypoint_config_log ERROR \
            "${label} must be a decimal integer from ${minimum} through ${maximum}, got '${raw}'"
        return 1
    fi

    normalized="${raw}"
    while [[ "${normalized}" == 0* && ${#normalized} -gt 1 ]]; do
        normalized="${normalized#0}"
    done

    # Bound the digit count before arithmetic so an extremely long environment
    # value cannot overflow Bash's integer representation and wrap into range.
    if (( ${#normalized} > ${#maximum} )); then
        _entrypoint_config_log ERROR \
            "${label} must be a decimal integer from ${minimum} through ${maximum}, got '${raw}'"
        return 1
    fi

    number=$((10#${normalized}))
    if (( number < minimum || number > maximum )); then
        _entrypoint_config_log ERROR \
            "${label} must be a decimal integer from ${minimum} through ${maximum}, got '${raw}'"
        return 1
    fi

    printf '%d\n' "${number}"
}

normalize_interval() {
    _normalize_bounded_decimal "${1-}" 1 1440 "${2:-Schedule interval}"
}

# Seconds; shared by health_check.sh (kill deadline) and the exporter
# (staleness floor) through the exported HEALTH_CHECK_TIMEOUT.
normalize_timeout() {
    _normalize_bounded_decimal "${1-}" 1 3600 "${2:-Timeout}"
}

normalize_port() {
    _normalize_bounded_decimal "${1-}" 1 65535 "${2:-Port}"
}

# Day counts never stop the container on a number that used to run: an
# out-of-range value is clamped into <min>..36500 with a warning (only a
# non-number fails, and callers wrap that in or_default -> default).
# Usage: _clamp_days <raw> <min> <label>
_clamp_days() {
    local raw="${1-}" minimum="$2" label="$3" maximum=36500 normalized
    if [[ ! "${raw}" =~ ^[0-9]+$ ]]; then
        _entrypoint_config_log ERROR "${label} must be a decimal number of days, got '${raw}'"
        return 1
    fi
    normalized="${raw}"
    while [[ "${normalized}" == 0* && ${#normalized} -gt 1 ]]; do
        normalized="${normalized#0}"
    done
    # Digit count first, so a huge value cannot overflow bash arithmetic.
    if (( ${#normalized} > ${#maximum} )) || (( 10#${normalized} > maximum )); then
        _entrypoint_config_log WARNING "${label}=${raw} exceeds ${maximum}; using ${maximum}"
        normalized="${maximum}"
    elif (( 10#${normalized} < minimum )); then
        _entrypoint_config_log WARNING "${label}=${raw} is below ${minimum}; using ${minimum}"
        normalized="${minimum}"
    fi
    printf '%d\n' "$((10#${normalized}))"
}

# UPTIME_WINDOW_DAYS: 0 used to run with an empty window (null uptime).
normalize_days() {
    _clamp_days "${1-}" 1 "${2:-Days}"
}

# RALLY_RESULTS_RETENTION_DAYS: 0 is meaningful (find -mtime +0 prunes run
# directories older than a day).
normalize_retention_days() {
    _clamp_days "${1-}" 0 "${2:-Retention days}"
}

# PROVENANCE_RETENTION_DAYS: 0 would erase the RGW auto-purge authorization
# ledger on every run, so it is raised to 1 (with the reason in the warning).
normalize_provenance_days() {
    local label="${2:-PROVENANCE_RETENTION_DAYS}"
    if [[ "${1-}" =~ ^0+$ ]]; then
        _entrypoint_config_log WARNING \
            "${label}=0 would erase the RGW provenance ledger every run; using 1"
        printf '1\n'
        return 0
    fi
    _clamp_days "${1-}" 1 "${label}"
}

# Usage: or_default <normalizer> <raw> <default> <label>
# An unset/empty <raw> means <default> (no warning), so callers pass "${VAR-}".
# For scripts that cron or `docker exec` may start without the entrypoint's
# fail-fast validation: a bad value falls back to <default> with a warning
# instead of aborting mid-run (e.g. after health.json was already written).
or_default() {
    local normalizer="$1" default="$3" label="$4"
    local raw="${2:-${default}}"
    # The normalizer's stderr stays visible (its warnings matter); only its
    # stdout is the value.
    if ! "${normalizer}" "${raw}" "${label}"; then
        _entrypoint_config_log WARNING "${label}='${raw}' is invalid; using ${default}"
        printf '%s\n' "${default}"
    fi
}

# Fail fast on missing OpenStack credentials instead of starting a "healthy"
# container whose first run fails as deployment_setup_failed. Values are never
# printed: only variable names appear in the messages.
check_os_credentials() {
    local name missing=()
    for name in OS_AUTH_URL OS_USERNAME OS_PASSWORD OS_PROJECT_NAME; do
        [[ -n "${!name:-}" ]] || missing+=("${name}")
    done
    if (( ${#missing[@]} > 0 )); then
        _entrypoint_config_log ERROR "missing required OpenStack settings: ${missing[*]} (see env.sample)"
        return 1
    fi
    # The env.sample placeholders: still runnable, but surely a forgotten edit.
    [[ "${OS_AUTH_URL}" != *your-openstack-api* ]] \
        || _entrypoint_config_log WARNING "OS_AUTH_URL is still the env.sample placeholder"
    [[ "${OS_PASSWORD}" != "changeme" ]] \
        || _entrypoint_config_log WARNING "OS_PASSWORD is still the env.sample placeholder"
    return 0
}

# Convert a validated minute interval to a cron expression. Intervals of at
# least one hour that are not whole hours retain the historical round-up policy.
# Standard output is reserved exclusively for the expression.
make_cron_schedule() {
    local minutes rounded hours

    minutes=$(normalize_interval "${1-}" "Schedule interval") || return 1
    if (( minutes < 60 )); then
        printf '*/%d * * * *\n' "${minutes}"
        return 0
    fi

    rounded="${minutes}"
    if (( minutes % 60 != 0 )); then
        rounded=$(( ((minutes + 59) / 60) * 60 ))
        _entrypoint_config_log WARNING \
            "Interval ${minutes}m is not a multiple of 60. Rounding up to ${rounded}m for cron."
    fi

    if (( rounded == 1440 )); then
        printf '0 0 * * *\n'
        return 0
    fi

    hours=$(( rounded / 60 ))
    printf '0 */%d * * *\n' "${hours}"
}
