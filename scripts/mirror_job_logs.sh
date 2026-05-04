#!/usr/bin/env bash
# ==============================================================================
# Cron Log Mirror - duplicate scheduled job output to file and Docker logs
# ==============================================================================
set -euo pipefail

usage() {
    echo "Usage: $0 <log-file> <command> [args...]" >&2
    exit 64
}

[[ $# -ge 2 ]] || usage

LOG_FILE="$1"
shift

# Docker only captures PID 1 stdout/stderr, so cron jobs mirror their combined
# output there explicitly while still appending to the long-term log file.
DOCKER_LOG_OUTPUT="${DOCKER_LOG_OUTPUT:-/proc/1/fd/1}"

mkdir -p "$(dirname "${LOG_FILE}")"

set +e
"$@" 2>&1 | tee -a "${LOG_FILE}" > "${DOCKER_LOG_OUTPUT}"
PIPE_RC=( "${PIPESTATUS[@]}" )
set -e

CMD_RC=${PIPE_RC[0]}
TEE_RC=${PIPE_RC[1]}

if [[ "${CMD_RC}" -ne 0 ]]; then
    exit "${CMD_RC}"
fi

exit "${TEE_RC}"
