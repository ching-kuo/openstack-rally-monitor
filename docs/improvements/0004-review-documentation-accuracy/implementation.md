# Implementation 0004: Review documentation accuracy

## What changes

1. Reword the two interval variables in `README.md`, `env.sample`, and
   `CLAUDE.md` as cron schedule requests.
2. Add one concise explanation of shorter hour/day rollover gaps to the README,
   sample environment, and specification 0001.
3. Make result 0003's ShellCheck evidence count-independent by naming the glob.
4. Add the ShellCheck command to the `CLAUDE.md` CI description and remove its
   stale fixed test count.

## Why this approach

The review classified the behavior as pre-existing and the defect as a
documentation mismatch. Documenting the exact cron semantics resolves the
operator ambiguity without expanding scope into scheduler redesign. Referring
to commands and globs instead of snapshot counts prevents the same drift from
recurring.

## Validation

- search the changed documentation for the stale phrases and fixed counts
- inspect the focused diff against the independent review findings
- run `git diff --check`
- run the full pytest suite to ensure the documentation-only follow-up did not
  disturb the previously validated branch state

The implementation commit is recorded in `result.md` after validation.
