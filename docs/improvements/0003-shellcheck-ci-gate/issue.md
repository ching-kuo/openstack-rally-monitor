# Issue 0003: Shell defects are not gated in CI

## What

The repository contains ten production shell scripts, but the only CI test step
runs pytest. A full ShellCheck scan currently exits non-zero with three findings
in `scripts/run_tests.sh`:

- `SC2155`: `log` combines declaration with command substitution, masking the
  substitution status.
- two `SC2015` findings: run-state atomic writes use `A && B || C`, whose fallback
  can run after a partially successful chain and is easy to misread during
  maintenance.

## Why it matters

Shell scripts orchestrate credentials, cron, Rally tasks, cleanup, and published
monitoring state. Several high-impact failures found in this improvement cycle
were shell boundary defects. Without a CI gate, ShellCheck-clean local work can
regress silently.

## Acceptance criteria

1. `shellcheck -x -P scripts scripts/*.sh` passes without exclusions.
2. The three current findings are resolved without changing intended behavior.
3. The existing GitHub Actions test job runs that exact command before pytest.
4. Sourced helper files are followed during analysis.
5. Full tests, Bash syntax checks, and workflow syntax validation pass locally.

## Scope

This issue adds static enforcement and makes the minimum control-flow cleanup
needed for a clean baseline. It does not reformat or refactor unrelated shell
code.
