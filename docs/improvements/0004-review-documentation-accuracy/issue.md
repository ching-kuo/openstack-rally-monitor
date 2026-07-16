# Issue 0004: Review evidence and scheduling guidance are stale

## What

Independent review found three documentation defects after the first three
improvement cycles:

- interval settings are described as minutes "between" runs even though cron
  step fields reset at hour and day boundaries
- the ShellCheck result hard-codes a script count that no longer matches the
  scanned glob
- `CLAUDE.md` omits the ShellCheck CI gate and carries an obsolete test count

## Why it matters

Operators can mistake cron step expressions for uniform elapsed-time delays,
which makes non-divisor settings such as 25 minutes or 9 hours run with a
shorter rollover gap. Stale validation and CI descriptions also make the
project's evidence harder to audit even when the implementation is correct.

## Acceptance criteria

1. User-facing configuration guidance describes the settings as cron schedule
   requests rather than guaranteed elapsed delays.
2. Hour and day rollover behavior is stated in the README, sample environment,
   and scheduling specification.
3. The ShellCheck result describes the scanned glob without a fragile count.
4. `CLAUDE.md` documents both enforced CI checks and contains no fixed pytest
   count.
5. Runtime behavior is unchanged.

## Scope

This is a documentation-only review follow-up. It does not restrict accepted
intervals, add runtime warnings, or replace cron with stateful scheduling.
