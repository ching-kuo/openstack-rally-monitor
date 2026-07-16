# Implementation 0001: Entrypoint configuration validation

## Changes

1. Add `scripts/entrypoint_config.sh` with:
   - a reusable bounded-decimal parser;
   - port normalization;
   - schedule generation with a single-line stdout contract.
2. Source the module immediately after the entrypoint logger is defined.
3. Validate and replace the four raw environment-derived values before any
   directory, database, process, or cron mutation.
4. Remove the former inline schedule helper.
5. Add `scripts/test_entrypoint_config.py` contract tests for the extracted
   module and a source-level assertion that the entrypoint validates before its
   initialization section.
6. Update `env.sample` and the README configuration table with explicit bounds
   and rounding behavior.

## Test-first evidence

The new focused suite is run before `entrypoint_config.sh` exists. It must fail
during collection/execution because the expected module and validation calls
are absent. After implementation, the same suite must pass, followed by the
complete Python suite, ShellCheck, and `git diff --check`.

Observed red phase: `27 failed, 1 passed`. The one passing assertion confirmed
that shell-like test input did not create its marker even while the module was
absent. Observed green phase after the final overflow regression was `29 passed`
in the focused suite. The complete-suite count is recorded in `result.md` after
the implementation commit so it cannot drift from the final reviewed tree.

## Security review

The parser uses a regular-expression allowlist before Bash arithmetic. Base-10
conversion happens only after validation. Downstream `su -c` strings receive
normalized digits rather than raw environment content.

## Rollback

Reverting the implementation commit restores the prior parsing behavior. No
persistent data format or deployment migration is involved.
