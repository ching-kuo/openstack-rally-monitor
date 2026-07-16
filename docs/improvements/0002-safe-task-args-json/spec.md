# Specification 0002: JSON-safe Rally task arguments

## Output contract

The generated file is a JSON object with this exact logical shape:

```json
{
  "env": {
    "RALLY_NOVA_FLAVOR": "<string>",
    "RALLY_NOVA_IMAGE": "<string>",
    "OS_AUTH_URL": "<string>"
  }
}
```

`RALLY_NOVA_FLAVOR` defaults to `m1.tiny`, `RALLY_NOVA_IMAGE` defaults to
`cirros-0.6.2-x86_64-disk`, and `OS_AUTH_URL` defaults to the empty string.
Values remain strings and must round-trip byte-for-byte through UTF-8 JSON.

## Function contract

Add `write_task_args TARGET` to `scripts/run_tests.sh`.

- `TARGET` is required.
- `jq -n --arg` performs all string encoding.
- Output is first written to `TARGET.tmp` in the same directory.
- A successful `jq` invocation is followed by `mv TARGET.tmp TARGET`.
- A failed `jq` invocation removes the temporary file, preserves any existing
  target, and returns non-zero.

The main run path calls this function before service iteration. Existing
`set -e` behavior makes generation failure abort the run rather than handing
Rally malformed or stale arguments.

## Compatibility

`jq` is already a runtime dependency and is used throughout the script. No new
package or configuration is introduced.
