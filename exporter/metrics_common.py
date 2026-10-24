"""Shared pieces of the Rally exporter: the metrics registry, cron spacing,
and ISO 8601 parsing.

Split out of rally_exporter.py so health_metrics.py and
announcement_metrics.py can register their gauges on the same registry
without importing the Flask app module.
"""

import os
from datetime import datetime, timezone

from prometheus_client import CollectorRegistry

registry = CollectorRegistry()


def cron_spacing_seconds(raw: str | None, default_minutes: int) -> int:
    """Return the longest gap, in seconds, between runs of a minute interval.

    KEEP IN SYNC with scripts/entrypoint_config.sh::make_cron_schedule, which
    turns the same interval into the cron entry: below 60 minutes it is a
    `*/N` minute step (longest gap N), from 60 up it rounds UP to whole hours
    (longest gap = the rounded value). Invalid or out-of-range input falls back
    to the default, matching the entrypoint, which refuses to start on a bad
    value and therefore never runs with one.
    """
    try:
        minutes = int(raw) if raw is not None else default_minutes
    except ValueError:
        minutes = default_minutes
    if not 1 <= minutes <= 1440:
        minutes = default_minutes
    if minutes >= 60:
        minutes = min(-(-minutes // 60) * 60, 1440)
    return minutes * 60


# Effective spacing of the two cron jobs. The exporter inherits both variables
# from entrypoint.sh, which refuses to start on an invalid value, so the
# defaults here only cover an unset variable.
SCHEDULE_INTERVAL_SECONDS = cron_spacing_seconds(os.environ.get("RALLY_SCHEDULE_INTERVAL"), 240)
HEALTH_INTERVAL_SECONDS = cron_spacing_seconds(os.environ.get("HEALTH_CHECK_INTERVAL"), 15)


def parse_iso8601_ms(value) -> float:
    """Parse an ISO 8601 UTC timestamp to epoch milliseconds.

    Returns float("nan") when the value is missing or unparseable, matching the
    JS side where Date.parse(...) yields NaN and Number.isFinite(NaN) is false.
    """
    if not isinstance(value, str):
        return float("nan")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return float("nan")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0
