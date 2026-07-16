# Result 0004: Accurate cron cadence and CI evidence

## Implementation commit

`29fc1daa7fd6da08b867f6d4f10e0c4d82efb547`
(`docs: clarify cron cadence and CI evidence`)

## What changed

- Reframed both scheduling variables as requested cron intervals rather than
  guaranteed elapsed-time delays.
- Documented the shorter rollover gap for minute steps that do not divide 60
  and effective hour steps that do not divide 24.
- Replaced result 0003's obsolete script count with the exact scanned glob.
- Updated `CLAUDE.md` to describe the enforced ShellCheck and pytest gates and
  removed its stale fixed test count.

## Why the result is better

Operators can now predict the actual cron cadence for every accepted value,
including non-divisors, without inferring uniform spacing that cron does not
provide. Review evidence and contributor guidance refer to durable commands
and file surfaces instead of counts that drift as the repository grows.

## Validation result

| Check | Result |
|---|---|
| Independent review | Three P3 documentation findings; no P0/P1/P2 findings |
| Stale-phrase search | No obsolete interval wording, ten-script result, or `270 passed` count found |
| Full tests | `336 passed, 1 skipped` in 42.61 seconds |
| Whitespace/error check | `git diff --check` passed |
| Knowledge-graph pre-commit review | Eight documentation files; risk score `0.00`, no affected flows or test gaps |

## Review notes

- The historical ten-script statement in issue 0003 remains unchanged because
  it correctly records the pre-improvement baseline.
- Runtime scheduling, accepted configuration values, generated cron
  expressions, and CI workflow behavior are unchanged.
- GitHub CI was not run because the main branch remains local and unpushed.
- Existing untracked local files remained unstaged and unmodified.
