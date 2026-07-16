# Result 0001: Entrypoint configuration validation

## Implementation commit

`135ce2cb18e2674d9dff64f824750969765e60e3` (`fix: validate entrypoint numeric configuration`)

## What changed

- Extracted entrypoint numeric parsing and cron generation into a directly
  testable shell module.
- Fixed non-whole-hour warnings so they use standard error and cannot become
  invalid lines in `/etc/cron.d/rally-tests`.
- Added strict base-10 bounds and normalization for both intervals and both
  listening ports before startup side effects.
- Made 24-hour and rounded-to-24-hour schedules emit `0 0 * * *`.
- Documented the accepted values and retained rounding policy.
- Added 29 focused regression cases, including boundary, leading-zero,
  overflow-length, malformed, and shell-like values.

## Why the result is better

The previously valid-looking `61` minute configuration no longer corrupts the
generated crontab. Configuration mistakes now fail immediately with the name,
accepted range, and rejected value. Environment content cannot reach arithmetic
evaluation or the port-bearing `su -c` strings until it has been reduced to a
bounded decimal integer.

## Validation result

| Check | Result |
|---|---|
| Red phase | `27 failed, 1 passed` before the implementation module existed |
| Focused tests | `29 passed` |
| Full tests | `332 passed, 1 skipped` in 41.84 seconds |
| ShellCheck | Passed for `entrypoint.sh` and `entrypoint_config.sh` with sourced-file following enabled |
| Whitespace/error check | `git diff --check` passed |
| Knowledge-graph pre-commit review | Risk score `0.50`; no affected flows detected |

The graph hook reported shell functions as test gaps because it does not infer
the subprocess `source` relationship. `scripts/test_entrypoint_config.py`
directly sources the module and invokes each public function; the focused test
result above is the authoritative coverage evidence for this shell boundary.

## Review notes

- No dependency or persistent-data change was introduced.
- Existing defaults remain unchanged.
- The existing policy that rounds non-whole-hour intervals upward remains
  unchanged; only its output isolation and 24-hour representation changed.
- Untracked local editor, agent, and pre-existing planning files were not
  staged or modified.
