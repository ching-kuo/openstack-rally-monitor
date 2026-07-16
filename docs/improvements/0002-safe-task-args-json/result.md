# Result 0002: JSON-safe Rally task arguments

## Implementation commit

`2407b9695afab256501ab7dbb87ecdc51a423787` (`fix: encode Rally task arguments safely`)

## What changed

- Replaced raw JSON heredoc interpolation with a `write_task_args` helper that
  passes values to a constant jq program through `--arg`.
- Preserved the existing flavor, image, and authentication URL defaults and the
  exact output object shape.
- Added same-directory temporary output and atomic rename.
- Added cleanup and target-preservation behavior for generation failure.
- Added four focused tests for defaults, special-text round trips, deliberate
  jq failure, and integration from `main`.

## Why the result is better

Operator-provided OpenStack names and URLs can now contain JSON-significant
characters without breaking every service task. An interrupted or failed
generation cannot expose a partial file or overwrite a previously complete
target. The data is never evaluated as jq source.

## Validation result

| Check | Result |
|---|---|
| Red phase | `3 failed, 1 passed` before `write_task_args` existed |
| Focused tests | `4 passed` |
| Full tests | `336 passed, 1 skipped` in 39.90 seconds |
| ShellCheck | Changed file passed with the three pre-existing `SC2155`/`SC2015` findings excluded |
| Whitespace/error check | `git diff --check` passed |
| Knowledge-graph pre-commit review | Risk score `0.40`; no affected flows detected |

The graph hook does not infer Bash functions called from Python subprocesses and
therefore listed `write_task_args` and `main` as gaps. The focused suite sources
`run_tests.sh`, invokes the production function, parses its output independently
with Python, and checks that `main` delegates to it.

## Review notes

- No dependency was added; the runtime image already requires jq.
- No scenario or persistent data format changed.
- Existing untracked local files remained unstaged and unmodified.
