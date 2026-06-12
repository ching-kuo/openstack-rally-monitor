# Appends the current health check to the rolling history, caps it at $max
# entries, and recomputes the uptime figures over the past $days days.
# Inputs (via jq args): $cur (slurped current check), $max (int), $days (int).
# Used by health_check.sh via `jq -f`; covered by test_uptime_ledger.py.
#
# Uptime measures REACHABILITY, not speed: a check counts as "up" whenever its
# overall status is anything other than "down" (i.e. "up" OR "degraded"). A
# degraded check means every service answered but at least one was slow
# (> HEALTH_LATENCY_WARN_MS) — the API was still reachable, so it must not
# depress availability. Slowness surfaces on the dashboard (amber cells) and in
# the rally_api_latency_milliseconds gauge, not here. KEEP IN SYNC with
# rally_exporter.py::_apply_health_metrics, which likewise treats degraded as up.
((now - ($days * 86400)) | strftime("%Y-%m-%dT%H:%M:%SZ")) as $cutoff
| .checks = ((.checks + $cur)[-$max:])
| (.checks | map(select(.timestamp >= $cutoff))) as $window
| ([$window[] | select(.overall != "down")] | length) as $up
| .uptime = {
    window_days: $days,
    checks_total: ($window | length),
    checks_up: $up,
    percent: (if ($window | length) > 0
              then (10000 * $up / ($window | length) | round / 100)
              else null end)
}
