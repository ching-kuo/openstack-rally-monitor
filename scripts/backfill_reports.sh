#!/usr/bin/env bash
# ==============================================================================
# Backfill historical Rally HTML reports into the self-contained format
# ==============================================================================
# Before scripts/render_report.py, run_tests.sh produced each
# <RUN_DIR>/<service>.html with `rally task report`, whose AngularJS page loaded
# d3/nvd3/AngularJS from public CDNs. Under the strict Content-Security-Policy
# dashboard/serve.py now serves for EVERY response, those CDN scripts are
# blocked, so any PRE-UPGRADE report renders as broken raw {{...}} placeholders.
#
# This one-shot migration walks every run directory under RESULTS_DIR and
# re-renders <service>.html from the persisted <service>.json (which run_tests.sh
# has always written alongside the report) using the same render_report.py the
# live pipeline uses. Properties:
#   * idempotent   -- a report already in the new format (data-block marker) is
#                     skipped, so steady-state cost is one grep per report;
#   * best-effort  -- a malformed/missing JSON yields a valid empty-state report
#                     (render_report.py exits 0), never a crash;
#   * self-healing -- entrypoint.sh runs it on every boot, so deploying the new
#                     image fixes historical reports immediately instead of
#                     waiting out RALLY_RESULTS_RETENTION_DAYS.
#
# Run-directory and service-name shapes mirror serve.py's RUN_REPORT_RE and the
# RALLY_SERVICES allowlist, so we never descend into branding/ or emit a report
# the server would refuse to serve.
set -uo pipefail

RESULTS_DIR="${RESULTS_DIR:-/results}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Compact UTC rally timestamp (YYYYMMDDTHHMMSSZ); matches run_tests.sh TIMESTAMP
# and serve.py RUN_REPORT_RE. Anchored so branding/ and other dirs are ignored.
readonly TS_RE='^[0-9]{8}T[0-9]{6}Z$'
# Lowercase service token; same allowlist as parse_rally_services.
readonly SVC_RE='^[a-z0-9_-]+$'
# Marker that identifies an already-migrated, self-contained report.
readonly NEW_MARKER='id="rally-report-data"'

backfilled=0
scanned=0
for dir in "${RESULTS_DIR}"/*/; do
    ts="$(basename "${dir}")"
    [[ "${ts}" =~ ${TS_RE} ]] || continue
    for json in "${dir}"*.json; do
        [[ -f "${json}" ]] || continue
        svc="$(basename "${json}" .json)"
        [[ "${svc}" =~ ${SVC_RE} ]] || continue
        html="${dir}${svc}.html"
        scanned=$((scanned + 1))
        # Skip reports already rendered in the self-contained format.
        if [[ -f "${html}" ]] && grep -q "${NEW_MARKER}" "${html}" 2>/dev/null; then
            continue
        fi
        if python3 "${SCRIPT_DIR}/render_report.py" "${json}" "${html}" \
                --service "${svc}" --timestamp "${ts}" 2>/dev/null; then
            backfilled=$((backfilled + 1))
        fi
    done
done

echo "backfill_reports: regenerated ${backfilled} of ${scanned} report(s) under ${RESULTS_DIR}"
