#!/usr/bin/env bash
# Append scheduled job output to a retained log file. entrypoint.sh tails the
# file as a PID 1 child to forward into `docker logs`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Run as root it would create a root-owned log file cron (rally) can no longer
# append to, and run the wrapped job as root.
source "${SCRIPT_DIR}/os_helpers.sh"
reexec_as_rally "$@"

usage() {
    echo "Usage: $0 <log-file> <command> [args...]" >&2
    exit 64
}

[[ $# -ge 2 ]] || usage

LOG_FILE="$1"
shift

mkdir -p "$(dirname "${LOG_FILE}")"

"$@" >>"${LOG_FILE}" 2>&1
