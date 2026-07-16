# Implementation 0002: JSON-safe Rally task arguments

## Changes

1. Add a small `write_task_args` function next to the service-runner functions.
2. Pass each environment value with `jq --arg` and construct the fixed object in
   the jq program.
3. Write beside the destination and rename only after successful encoding.
4. Remove the temporary file on generation failure without touching an existing
   destination.
5. Replace the inline heredoc in `main` with the helper call.
6. Add focused subprocess tests that source `run_tests.sh`, call the helper, and
   decode the result with Python's JSON parser.

## Test-first method

The focused suite is introduced before the helper. The red phase must show the
function is missing and the source still contains the raw heredoc. The green
phase must pass before the complete repository suite and ShellCheck run.

Observed red phase: `3 failed, 1 passed`; the passing case only established that
the absent helper had not overwritten an existing file. Observed focused green
phase: `4 passed`, including a deliberate failing-`jq` executable to exercise
the cleanup path rather than an unrelated missing-command failure.

## Security and reliability review

The jq program is constant; environment content is data supplied through
`--arg`, never executable jq syntax. Shell arguments and paths are quoted. The
same-directory temporary file keeps rename atomic on the target filesystem.

## Rollback

Revert the implementation commit. No persistent schema or migration is added;
the generated JSON shape is unchanged.
