#!/usr/bin/env bash
# Append scheduled job output to a retained log file. entrypoint.sh tails the
# file as a PID 1 child to forward into `docker logs`.
set -euo pipefail

usage() {
    echo "Usage: $0 <log-file> <command> [args...]" >&2
    exit 64
}

[[ $# -ge 2 ]] || usage

LOG_FILE="$1"
shift

mkdir -p "$(dirname "${LOG_FILE}")"

"$@" >>"${LOG_FILE}" 2>&1
