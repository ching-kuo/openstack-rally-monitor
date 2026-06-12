# Build the all-down health document published when api_health_check.py fails
# or emits invalid JSON. Invoked by health_check.sh as:
#   jq -n -f health_fallback_filter.jq --arg ts <ts> --arg raw <RALLY_SERVICES> --arg default <list>
#
# The service set honors RALLY_SERVICES like the checker itself (keystone
# always present), mirroring api_health_check.py::parse_rally_services and
# run_tests.sh::parse_rally_services -- keep the three in sync. Normalization
# here is the subset that matters for this error path: split on commas, strip
# whitespace, lowercase, drop empties, then drop any token failing the
# ^[a-z0-9_-]+$ allowlist (path-traversal hardening, same as the other two
# parsers). Object construction dedupes keys.
#
# The allowlist filter runs BEFORE choosing raw vs default: an all-invalid
# RALLY_SERVICES must fall back to the default set, matching the other two
# parsers -- otherwise the fallback document would underreport the outage as
# keystone-only.

def normalize:
  split(",")
  | map(gsub("\\s"; "") | ascii_downcase)
  | map(select(length > 0 and test("^[a-z0-9_-]+$")));

($ts) as $t
| (($raw | normalize) as $named
    | if ($named | length) > 0 then $named else ($default | normalize) end) as $names
| {
    timestamp: $t,
    overall: "down",
    services: (
        ["keystone"] + $names
        | map({(.): {status: "down", latency_ms: 0, checked_at: $t}})
        | add
    )
  }
