# Result 0003: Warning-free shell CI gate

## Implementation commit

`93b0d838f6d160640e11212396a8258bfe057b42` (`ci: enforce shell static analysis`)

## What changed

- Removed the repository's one `SC2155` warning and two `SC2015` findings with
  explicit, behavior-preserving shell control flow.
- Added a ShellCheck step to the existing test job, before Python setup.
- Configured analysis to follow sourced files across the entire `scripts/`
  shell surface with no exclusions.
- Corrected the workflow comment's obsolete test-suite count.

## Why the result is better

All production shell entry points and helpers now share an enforced static
baseline. A ShellCheck regression blocks the test job, and because image build
and publication depend on that job, a known shell defect cannot be released by
the normal workflow.

## Validation result

| Check | Result |
|---|---|
| Red phase | One `SC2155` warning and two `SC2015` findings; non-zero exit |
| ShellCheck green phase | All ten shell scripts passed with `-x -P scripts` and no exclusions |
| Bash syntax | `bash -n scripts/*.sh` passed |
| Workflow validation | actionlint passed |
| Full tests | `336 passed, 1 skipped` in 39.83 seconds |
| JavaScript syntax audit | `node --check` passed for `app.js` and `report.js` |
| Whitespace/error check | `git diff --check` passed |
| Knowledge-graph pre-commit review | Risk score `0.40`; no affected flows detected |

The graph hook lists the three mechanically changed shell functions as test
gaps because the primary proof for this improvement is whole-repository static
analysis. The full behavioral suite passed unchanged, and the CI gate executes
the same ShellCheck command used locally.

## Review notes

- The GitHub-hosted `ubuntu-latest` inventory was checked before relying on its
  preinstalled ShellCheck package.
- No third-party action, package installation, runtime dependency, or deployment
  configuration was added.
- Existing untracked local files remained unstaged and unmodified.
