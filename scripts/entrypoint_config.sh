#!/usr/bin/env bash
# Numeric configuration parsing shared by the container entrypoint and tests.

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

normalize_port() {
    _normalize_bounded_decimal "${1-}" 1 65535 "${2:-Port}"
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
