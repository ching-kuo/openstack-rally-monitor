"""Operator-announcement gauges, read from announcement-state.json."""

import time

from prometheus_client import Gauge

from metrics_common import parse_iso8601_ms, registry

rally_announcement_active = Gauge(
    "rally_announcement_active",
    (
        "Number of currently-active operator announcements of each type. "
        "All three type labels (incident, maintenance, scheduled) are emitted "
        "every scrape, 0 when none, so the series never appear or disappear."
    ),
    ["type"],
    registry=registry,
)

rally_maintenance_mode = Gauge(
    "rally_maintenance_mode",
    "Whether any maintenance-type announcement is currently active (1) or not (0)",
    registry=registry,
)

# Operator-announcement types. Enum-only: never derive labels from body/id text.
# Mirrors TYPE_LABELS in dashboard/js/announcements.js and VALID_TYPES_RE in announce.sh.
ANNOUNCEMENT_TYPES = ("incident", "maintenance", "scheduled")


def is_announcement_active(rec: dict, now_ms: float) -> bool:
    """Return whether an announcement record is currently active.

    KEEP IN SYNC with dashboard/js/announcements.js::isAnnouncementActive — both encode the
    same active-ness semantics (the repo uses this convention for
    ALL_GREEN_PREDICATE across announce.sh/run_tests.sh):
      - unknown type            -> not active
      - effective_from in the future (parseable) -> not active
      - expires_at at/<= now (parseable)         -> not active
      - records missing both bounds (incidents)  -> active until cleared
    Unparseable bounds are ignored (treated as absent), matching the JS
    Number.isFinite guard.
    """
    if not isinstance(rec, dict):
        return False
    if rec.get("type") not in ANNOUNCEMENT_TYPES:
        return False
    start_ms = parse_iso8601_ms(rec.get("effective_from"))
    if start_ms == start_ms and start_ms > now_ms:  # not-NaN and future
        return False
    end_ms = parse_iso8601_ms(rec.get("expires_at"))
    if end_ms == end_ms and end_ms <= now_ms:  # not-NaN and past/now
        return False
    return True


def apply_announcement_metrics(state: dict) -> None:
    """Apply maintenance/announcement gauges from announcement-state.json.

    Emits all three type labels every scrape (0 when none) so series never
    appear or disappear, and sets rally_maintenance_mode to 1 iff any
    maintenance record is currently active. Label cardinality is enum-only;
    body/id text never becomes a label.
    """
    counts = {t: 0 for t in ANNOUNCEMENT_TYPES}
    now_ms = time.time() * 1000.0

    announcements = state.get("announcements", []) if isinstance(state, dict) else []
    if isinstance(announcements, list):
        for rec in announcements:
            if is_announcement_active(rec, now_ms):
                counts[rec["type"]] += 1

    for atype in ANNOUNCEMENT_TYPES:
        rally_announcement_active.labels(type=atype).set(counts[atype])
    rally_maintenance_mode.set(1 if counts["maintenance"] > 0 else 0)
