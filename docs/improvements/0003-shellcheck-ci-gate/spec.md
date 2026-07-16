# Specification 0003: Warning-free shell CI gate

## Static-analysis command

CI and local validation use:

```bash
shellcheck -x -P scripts scripts/*.sh
```

`-x` follows sourced files and `-P scripts` resolves the repository's dynamic
`SCRIPT_DIR` imports. No warning exclusions or inline suppressions are allowed
for the clean baseline.

The current `ubuntu-latest` GitHub-hosted image is Ubuntu 24.04 and lists
ShellCheck 0.9.0 as a preinstalled apt package. The workflow uses that runner
tool directly, avoiding a new third-party action or install step.

## Code behavior

- `log` declares `msg` separately, then assigns the timestamped value.
- `write_run_state_running` and `write_run_state_idle` group the `printf` and
  `mv` operations inside an explicit `if ! { ...; }; then` block.
- On either write or rename failure, the temporary file is removed.
- Both run-state helpers remain best-effort and return success after cleanup so
  state telemetry cannot fail the Rally run or its EXIT trap.

## CI placement

Add a `Lint shell scripts` step to the existing `test` job after checkout and
before Python setup. The image build remains dependent on the complete test job,
so shell findings block image publication.
