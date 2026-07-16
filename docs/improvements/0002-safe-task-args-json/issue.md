# Issue 0002: Raw environment interpolation can corrupt Rally task arguments

## What

`scripts/run_tests.sh` creates each run's `task_args.json` with an interpolated
shell heredoc. `RALLY_NOVA_FLAVOR`, `RALLY_NOVA_IMAGE`, and `OS_AUTH_URL` are
placed between JSON quotes without JSON encoding.

A value containing a quote, backslash, control character, or newline therefore
produces malformed JSON. For example, a flavor named `tiny "quoted"` produces
an invalid document before Rally starts any scenario.

## Why it matters

- OpenStack resource names are operator-controlled strings and may contain
  characters that are meaningful to JSON.
- Authentication URLs can contain escaped path or query content.
- One malformed value breaks the shared argument file, affecting every
  configured service in the run.
- The current direct write can leave a partially written file if generation is
  interrupted.

## Acceptance criteria

1. All three values are encoded by a JSON-aware tool rather than interpolated
   into JSON syntax.
2. Existing defaults remain unchanged when flavor or image variables are unset.
3. Quotes, backslashes, newlines, and Unicode round-trip exactly.
4. The final file is published atomically only after generation succeeds.
5. Failure leaves any existing target file untouched and returns non-zero.
6. Focused tests exercise defaults, hostile/special text, and failed generation.

## Scope

Only generation of `task_args.json` changes. Rally scenario definitions and the
meaning of the three environment keys do not change.
