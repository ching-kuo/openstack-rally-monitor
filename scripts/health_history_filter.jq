# Appends the current health check to the rolling history, caps it at $max
# entries, and recomputes the uptime figures over the past $days days.
# Inputs (via jq args): $cur (slurped current check), $max (int), $days (int).
# Used by health_check.sh via `jq -f`; covered by test_uptime_ledger.py.
((now - ($days * 86400)) | strftime("%Y-%m-%dT%H:%M:%SZ")) as $cutoff
| .checks = ((.checks + $cur)[-$max:])
| (.checks | map(select(.timestamp >= $cutoff))) as $window
| ([$window[] | select(.overall == "up")] | length) as $up
| .uptime = {
    window_days: $days,
    checks_total: ($window | length),
    checks_up: $up,
    percent: (if ($window | length) > 0
              then (10000 * $up / ($window | length) | round / 100)
              else null end)
}
