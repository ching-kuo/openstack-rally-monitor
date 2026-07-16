# Specification 0004: Accurate scheduling and CI documentation

## Scheduling contract

Describe `RALLY_SCHEDULE_INTERVAL` and `HEALTH_CHECK_INTERVAL` as requested cron
intervals. Preserve the existing accepted range and rounding behavior, then add
the missing field-boundary rule:

- `*/N` minute steps restart at each hour, so a value that does not divide 60
  has one shorter hour-rollover gap
- `*/H` hour steps restart each day, so an effective hour value that does not
  divide 24 has one shorter midnight-rollover gap

State that the configuration does not provide a stateful elapsed-time delay.
Put the operator-facing explanation in `README.md` and `env.sample`, and align
the original scheduling specification with the implemented cron semantics.

## Evidence contract

Replace the hard-coded ShellCheck file count with the exact `scripts/*.sh`
surface. Keep the historical issue statement unchanged because it describes
the pre-improvement baseline, when ten production shell scripts existed.

Update `CLAUDE.md` so the CI section lists ShellCheck before pytest and says
that either failure blocks the image build. Remove its fixed pytest count and
direct readers to the current command output instead.

## Compatibility

No code, workflow, configuration value, or generated cron expression changes.
