"""API health-check gauges, read from health.json (written by health_check.sh)."""

import os
import time

from prometheus_client import Gauge

from metrics_common import HEALTH_INTERVAL_SECONDS, parse_iso8601_ms, registry

def parse_check_timeout(raw: str | None, default: int = 300) -> int:
    """Parse HEALTH_CHECK_TIMEOUT (seconds, 1..3600).

    entrypoint.sh normalizes and exports it for both this exporter and
    health_check.sh (which kills the checker after it), so both read the same
    value; the default only covers a run without the entrypoint. KEEP IN SYNC:
    the 300 default and 1..3600 bounds with entrypoint.sh /
    entrypoint_config.sh::normalize_timeout and health_check.sh's fallback.
    """
    try:
        seconds = int(raw) if raw else default
    except ValueError:
        return default
    return seconds if 1 <= seconds <= 3600 else default


HEALTH_CHECK_TIMEOUT_SECONDS = parse_check_timeout(os.environ.get("HEALTH_CHECK_TIMEOUT"))


def stale_after_seconds(interval_seconds: int) -> int:
    """Age after which health.json counts as stale for a check interval.

    A health.json older than this means the health pipeline has stopped (cron
    died, a check hung, the volume went read-only), so its last values must not
    be reported as current. Three intervals normally; the floor covers short
    intervals, where one slow check (killed at the timeout) would otherwise
    read as stale. The timestamp is written when a check finishes, so the next
    one lands at most an interval plus the timeout later; the extra minute
    absorbs cron jitter. KEEP IN SYNC with scripts/init_state.sh, which
    writes the same value as dashboard_config.json's health_stale_after_seconds.
    """
    return max(3 * interval_seconds, interval_seconds + HEALTH_CHECK_TIMEOUT_SECONDS + 60)


HEALTH_STALE_AFTER_SECONDS = stale_after_seconds(HEALTH_INTERVAL_SECONDS)

rally_api_up = Gauge(
    "rally_api_up",
    (
        "Whether the API health check for a service reported reachable (1) or "
        'down (0). "degraded" (the service answered but exceeded '
        "HEALTH_LATENCY_WARN_MS) counts as up (1) — this gauge measures "
        "reachability, not speed. Slowness shows up in "
        "rally_api_latency_milliseconds, not here."
    ),
    ["service"],
    registry=registry,
)

rally_api_latency_milliseconds = Gauge(
    "rally_api_latency_milliseconds",
    (
        "Latency in milliseconds of the last API health check for a service. "
        "This is where a degraded (slow-but-reachable) service is visible; "
        "rally_api_up stays 1 for degraded, so alert on this gauge for "
        "latency-based warnings."
    ),
    ["service"],
    registry=registry,
)

rally_api_overall_up = Gauge(
    "rally_api_overall_up",
    (
        "Whether the most recent API health data reports overall reachability "
        '(1) or not (0). "up" and "degraded" both map to 1 (degraded means '
        "every service answered, just slowly — see "
        "rally_api_latency_milliseconds). Everything else maps to 0 and the "
        'gauge FAILS CLOSED: an explicit "down", an "unknown"/missing overall '
        "(e.g. the seed health.json on a fresh volume, or a corrupt/missing "
        "file), and any unrecognized value all set 0. Semantics: 1 = healthy "
        "signal; 0 = down OR no valid signal. This pairs with per-service "
        "rally_api_up (genuinely absent until a service is first checked, so "
        "absent series cannot fire `== 0`): when the health pipeline breaks, "
        "those per-service series vanish and only this gauge can still signal "
        "the loss. A stale health.json (older than "
        "rally_api_stale_after_seconds) also sets 0. RallyApiSignalLost "
        "alerts on this 0 state."
    ),
    registry=registry,
)

rally_api_last_check_timestamp = Gauge(
    "rally_api_last_check_timestamp",
    (
        "Epoch timestamp of the last health check this exporter read; "
        "kept while health.json is missing or corrupt, 0 until a check with a "
        "parseable timestamp has been seen"
    ),
    registry=registry,
)

rally_api_stale_after_seconds = Gauge(
    "rally_api_stale_after_seconds",
    (
        "Age in seconds after which health.json counts as stale: three "
        "health-check intervals (as cron spaces HEALTH_CHECK_INTERVAL), but "
        "never less than one interval plus the checker timeout. A stale file "
        "drops the per-service rally_api_* series and sets "
        "rally_api_overall_up to 0."
    ),
    registry=registry,
)

rally_api_stale_after_seconds.set(HEALTH_STALE_AFTER_SECONDS)

# Identity of the last applied health dict and whether it was stale then; the
# applier skips work only when both are unchanged.
_last_applied_health: dict = {}
_last_health_stale: bool = False
# Last parseable check time seen by this process. A missing or corrupt
# health.json has no timestamp of its own; rally_api_last_check_timestamp keeps
# reporting this so RallyApiHealthStale (which ignores 0 = never checked, the
# fresh-boot case) still fires once it ages. After an exporter restart onto a
# broken file it is 0 again and RallyApiSignalLost is the remaining signal.
_last_good_check_epoch: float = 0.0


def _health_check_epoch(health: dict) -> float:
    """Return health.json's top-level timestamp as epoch seconds (0 if unusable)."""
    ms = parse_iso8601_ms(health.get("timestamp"))
    return ms / 1000.0 if ms == ms else 0.0  # NaN -> 0


def apply_health_metrics(health: dict) -> None:
    """Apply API health-check metrics from health.json.

    Called on every scrape regardless of whether the Rally summary changed,
    because health.json updates on its own ~15-minute cycle (written by
    health_check.sh). Mirrors rally_exporter._apply_cleanup_metrics: identity-check to skip
    redundant work, then clear-before-set so a service that drops out of a
    later health.json does not leave a stale labeled series behind.

    Staleness is re-evaluated on every scrape, because an unchanged file ages:
    when health.json is older than HEALTH_STALE_AFTER_SECONDS (or has no
    parseable timestamp) the pipeline has stopped, so its last values are not
    reported as current — per-service series are dropped and the overall gauge
    fails closed to 0, exactly as for a corrupt file.
    """
    global _last_applied_health, _last_health_stale, _last_good_check_epoch
    checked_at = _health_check_epoch(health)
    if checked_at > 0:
        _last_good_check_epoch = checked_at
    stale = checked_at <= 0 or (time.time() - checked_at) > HEALTH_STALE_AFTER_SECONDS
    if health is _last_applied_health and stale == _last_health_stale:
        return
    _last_applied_health = health
    _last_health_stale = stale

    rally_api_last_check_timestamp.set(_last_good_check_epoch)

    # health.json is a full snapshot; clear prior per-service labels first so
    # stale services do not linger when the file shrinks or goes missing.
    rally_api_up.clear()
    rally_api_latency_milliseconds.clear()

    services = {} if stale else health.get("services", {})
    if isinstance(services, dict):
        for service, info in services.items():
            if not isinstance(info, dict):
                continue
            status = info.get("status")
            # "degraded" (reachable but slow) counts as up: this gauge measures
            # reachability. Only an explicit "down" is 0. Slowness is visible in
            # rally_api_latency_milliseconds below.
            rally_api_up.labels(service=service).set(0 if status == "down" else 1)
            latency = info.get("latency_ms")
            if isinstance(latency, (int, float)) and not isinstance(latency, bool):
                rally_api_latency_milliseconds.labels(service=service).set(latency)

    # Fail closed: only a fresh "up"/"degraded" is 1 (see the
    # rally_api_overall_up help text and the docstring above).
    overall = health.get("overall")
    if not stale and overall in ("up", "degraded"):
        rally_api_overall_up.set(1)
    else:
        rally_api_overall_up.set(0)
