# Specification 0001: Fail-fast numeric entrypoint configuration

## Inputs

| Variable | Accepted values | Default | Normalized form |
|---|---:|---:|---:|
| `RALLY_SCHEDULE_INTERVAL` | decimal integer `1..1440` | `240` | base-10 integer |
| `HEALTH_CHECK_INTERVAL` | decimal integer `1..1440` | `15` | base-10 integer |
| `EXPORTER_PORT` | decimal integer `1..65535` | `9101` | base-10 integer |
| `DASHBOARD_PORT` | decimal integer `1..65535` | `8080` | base-10 integer |

Leading zeroes are allowed and normalized as decimal, so `0060` becomes `60`
rather than being interpreted as octal. Signs, whitespace, suffixes, arithmetic
expressions, and shell metacharacters are rejected.

## Schedule behavior

- `1..59`: `*/N * * * *`
- exact whole hours below 24 hours: `0 */H * * *`
- non-whole-hour values from 60 through 1439: round up to the next whole hour,
  emit one warning on standard error, then use the whole-hour expression
- values that normalize or round to 24 hours: `0 0 * * *`

These are cron field steps, not stateful elapsed-time delays. A minute step that
does not divide 60 has one shorter gap at the next hour, and an effective
whole-hour step that does not divide 24 has one shorter gap at midnight.

The helper's standard output contract is exactly one cron expression followed
by a newline. Diagnostics must use standard error.

## Failure behavior

Invalid configuration prints an entrypoint-prefixed error naming the variable
and rejected value, then returns non-zero. Because the entrypoint runs with
`set -e`, startup stops before initialization has external side effects.

## Design

Move pure configuration functions into `scripts/entrypoint_config.sh`. The
entrypoint sources this file, validates and normalizes all four values near the
top, and then uses only normalized values. The separate module allows direct,
fast subprocess tests without starting Rally, cron, or network listeners.

No dependency is added.
