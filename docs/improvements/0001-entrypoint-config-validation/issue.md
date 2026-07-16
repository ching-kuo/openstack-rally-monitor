# Issue 0001: Entrypoint accepts unsafe or corrupting numeric configuration

## What

`scripts/entrypoint.sh` does not validate its schedule intervals or listening
ports before using them. Its schedule helper also writes a rounding warning to
standard output, the same stream used to return the generated cron expression.

For example, `RALLY_SCHEDULE_INTERVAL=61` makes the command substitution capture
both the warning and `0 */2 * * *`. The warning is then written into
`/etc/cron.d/rally-tests` as if it were cron syntax, so the configured jobs may
not load. Non-decimal interval text is passed to Bash arithmetic evaluation,
and port text is interpolated into `su -c` command strings.

## Why it matters

- A documented configuration input can silently disable all scheduled tests.
- A typo such as `15m` produces an indirect shell error instead of an actionable
  configuration error.
- Treating environment text as arithmetic or shell syntax expands the attack
  surface of a root-running container entrypoint.
- Invalid ports fail only after child processes start, complicating diagnosis.

## Acceptance criteria

1. Schedule intervals accept decimal integers from 1 through 1440 only.
2. Invalid intervals fail before cron or child processes are started.
3. Warnings are emitted on standard error and never contaminate the returned
   cron expression.
4. A 24-hour interval emits an unambiguous daily expression.
5. Exporter and dashboard ports accept decimal integers from 1 through 65535
   only and are normalized before command construction.
6. Automated tests cover valid boundaries, rounding, malformed values, and
   shell-like input.

## Scope

This issue changes entrypoint configuration parsing only. It does not change the
documented rounding policy for intervals of at least one hour that are not exact
hour multiples.
