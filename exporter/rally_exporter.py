#!/usr/bin/env python3
"""
Rally OpenStack Prometheus Exporter
====================================
Exposes Rally test results as Prometheus metrics.
Reads JSON results from the /results directory and serves metrics on :9101.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, jsonify
from prometheus_client import (
    Counter,
    CollectorRegistry,
    Gauge,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
RESULTS_DIR = os.environ.get("RESULTS_DIR", "/results")
EXPORTER_PORT = int(os.environ.get("EXPORTER_PORT", 9101))
READY_MAX_AGE_MINUTES = int(os.environ.get("READY_MAX_AGE_MINUTES", 480))

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Prometheus Metrics Registry
# ---------------------------------------------------------------------------
registry = CollectorRegistry()

rally_task_success = Gauge(
    "rally_task_success",
    "Whether the Rally task passed (1) or failed (0)",
    ["service", "scenario"],
    registry=registry,
)

rally_task_duration_seconds = Gauge(
    "rally_task_duration_seconds",
    "Duration of the Rally task in seconds",
    ["service", "scenario"],
    registry=registry,
)

rally_task_iterations_total = Gauge(
    "rally_task_iterations_total",
    "Total number of iterations in the Rally task",
    ["service", "scenario"],
    registry=registry,
)

rally_task_failures_total = Gauge(
    "rally_task_failures_total",
    "Number of failed iterations in the Rally task",
    ["service", "scenario"],
    registry=registry,
)

rally_task_sla_passed = Gauge(
    "rally_task_sla_passed",
    "Whether the SLA check passed (1) or failed (0)",
    ["service", "scenario"],
    registry=registry,
)

rally_service_status = Gauge(
    "rally_service_status",
    "Overall service status: 1=passed, 0=failed, -1=pending/skipped",
    ["service"],
    registry=registry,
)

rally_cleanup_failure = Gauge(
    "rally_cleanup_failure",
    "Whether scenario-created (s_rally_*) cleanup failed (1) or succeeded (0)",
    ["service"],
    registry=registry,
)

rally_orphaned_resources = Gauge(
    "rally_orphaned_resources",
    "Number of scenario-created (s_rally_*) orphaned resources",
    ["service", "resource_type"],
    registry=registry,
)

rally_context_cleanup_warning = Gauge(
    "rally_context_cleanup_warning",
    "Whether context-created (c_rally_*) resources were left behind (1) or not (0)",
    ["service"],
    registry=registry,
)

rally_context_orphaned_resources = Gauge(
    "rally_context_orphaned_resources",
    "Number of context-created (c_rally_*) orphaned resources",
    ["service", "resource_type"],
    registry=registry,
)

rally_rgw_orphaned_users = Gauge(
    "rally_rgw_orphaned_users",
    "Number of orphaned RGW implicit-tenant users detected during cleanup scans",
    registry=registry,
)

rally_rgw_orphaned_buckets = Gauge(
    "rally_rgw_orphaned_buckets",
    "Number of buckets owned by orphaned RGW implicit-tenant users",
    registry=registry,
)

rally_rgw_unknown_owner_orphans = Gauge(
    "rally_rgw_unknown_owner_orphans",
    "Number of orphaned RGW implicit-tenant users not present in the Rally provenance ledger",
    registry=registry,
)

rally_rgw_scan_ok = Gauge(
    "rally_rgw_scan_ok",
    "Whether RGW orphan scanning is healthy: 1=ok/skipped, 0=error",
    registry=registry,
)

rally_last_run_timestamp = Gauge(
    "rally_last_run_timestamp",
    "Epoch timestamp of the last Rally test run",
    registry=registry,
)

rally_overall_success = Gauge(
    "rally_overall_success",
    "Whether all services passed (1) or any failed (0)",
    registry=registry,
)

rally_run_duration_seconds = Gauge(
    "rally_run_duration_seconds",
    "Total duration of the last full Rally test suite run in seconds",
    registry=registry,
)

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
        "the loss. RallyApiSignalLost alerts on this 0 state."
    ),
    registry=registry,
)

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

rally_exporter_errors_total = Counter(
    "rally_exporter_errors_total",
    "Total number of errors reading or parsing result files",
    ["file"],
    registry=registry,
)

rally_data_valid = Gauge(
    "rally_data_valid",
    "Whether valid test result data is available (1) or not (0)",
    registry=registry,
)


# ---------------------------------------------------------------------------
# File Cache State
# Avoids redundant disk reads when data has not changed between scrapes.
# CACHE_MAX_AGE_SECONDS is a safety fallback: force re-read even if mtime
# appears unchanged (e.g., on filesystems with low-resolution timestamps).
# ---------------------------------------------------------------------------
CACHE_MAX_AGE_SECONDS: float = 60.0

_summary_mtime: float = -1.0
_summary_data: dict = {}
_summary_cache_time: float = 0.0

_cleanup_mtime: float = -1.0
_cleanup_data: dict = {}
_cleanup_cache_time: float = 0.0

_health_mtime: float = -1.0
_health_data: dict = {}
_health_cache_time: float = 0.0

_announce_mtime: float = -1.0
_announce_data: dict = {}
_announce_cache_time: float = 0.0

# Tracks the timestamp of the last summary we processed into labeled metrics.
# Only when this changes do we clear and rebuild per-service/scenario gauges.
_last_processed_ts: str = ""
_last_applied_cleanup: dict = {}
_last_applied_health: dict = {}


# ---------------------------------------------------------------------------
# Data Loading
# ---------------------------------------------------------------------------
def _load_cached_json(
    filename: str, cached_mtime: float, cached_data: dict, cached_time: float
) -> tuple:
    """Load a JSON file with simple mtime-based caching."""
    path = os.path.join(RESULTS_DIR, filename)
    try:
        mtime = os.path.getmtime(path)
        now = time.time()
        if mtime == cached_mtime and (now - cached_time) < CACHE_MAX_AGE_SECONDS:
            return cached_data, cached_mtime, cached_time

        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data, mtime, now
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        rally_exporter_errors_total.labels(file=filename).inc()
        return None, cached_mtime, cached_time


def load_latest_summary() -> dict:
    """Load the latest summary JSON file, using mtime-based caching."""
    global _summary_mtime, _summary_data, _summary_cache_time
    data, _summary_mtime, _summary_cache_time = _load_cached_json(
        "latest_summary.json",
        _summary_mtime,
        _summary_data,
        _summary_cache_time,
    )
    if data is None:
        return {"timestamp": "none", "services": {}}
    _summary_data = data
    return data


def load_cleanup_metrics() -> dict:
    """Load cleanup metrics JSON file, using mtime-based caching."""
    global _cleanup_mtime, _cleanup_data, _cleanup_cache_time
    data, _cleanup_mtime, _cleanup_cache_time = _load_cached_json(
        "cleanup_metrics.json",
        _cleanup_mtime,
        _cleanup_data,
        _cleanup_cache_time,
    )
    if data is None:
        return {
            "cleanup_failed": 0,
            "orphaned_resources": {},
            "context_orphaned_resources": {},
            "details": {},
            "context_details": {},
            "rgw_scan_status": "skipped",
            "rgw_orphaned_users": 0,
            "rgw_orphaned_buckets": 0,
            "rgw_unknown_owner_orphans": 0,
        }
    _cleanup_data = data
    return data


def load_health() -> dict:
    """Load API health-check JSON file, using mtime-based caching.

    health.json is written by scripts/health_check.sh on its own ~15-minute
    cadence, independent of the Rally summary. The entrypoint always seeds it,
    so a missing/corrupt file is treated like the other always-present files:
    the error counter is incremented and a benign default is returned.
    """
    global _health_mtime, _health_data, _health_cache_time
    data, _health_mtime, _health_cache_time = _load_cached_json(
        "health.json",
        _health_mtime,
        _health_data,
        _health_cache_time,
    )
    if data is None:
        return {"overall": "unknown", "services": {}}
    _health_data = data
    return data


def load_announcements() -> dict:
    """Load operator announcement state, using mtime-based caching.

    Unlike the always-seeded summary/cleanup/health files,
    announcement-state.json legitimately does not exist until the first
    `announce.sh post`. A missing file is therefore the normal pre-first-post
    state and must NOT increment rally_exporter_errors_total — it returns the
    empty default silently. Corrupt JSON (or other read errors) on an existing
    file still increments the counter via _load_cached_json.
    """
    global _announce_mtime, _announce_data, _announce_cache_time
    path = os.path.join(RESULTS_DIR, "announcement-state.json")
    if not os.path.exists(path):
        # Absence is expected pre-first-post; do not count it as an error.
        _announce_mtime = -1.0
        _announce_data = {}
        return {"announcements": []}
    data, _announce_mtime, _announce_cache_time = _load_cached_json(
        "announcement-state.json",
        _announce_mtime,
        _announce_data,
        _announce_cache_time,
    )
    if data is None:
        return {"announcements": []}
    _announce_data = data
    return data


def parse_timestamp(ts: str) -> float:
    """Convert Rally timestamp string to epoch."""
    try:
        dt = datetime.strptime(ts, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Metrics Update
# ---------------------------------------------------------------------------
_SVC_MAP: dict = {
    "servers": "nova",
    "networks": "neutron",
    "routers": "neutron",
    "security_groups": "neutron",
    "volumes": "cinder",
    "images": "glance",
    "users": "keystone",
    "projects": "keystone",
}


def _apply_cleanup_metrics(cleanup: dict) -> None:
    """Apply cleanup orphan metrics from cleanup_metrics.json.

    Called on every scrape regardless of whether the summary timestamp changed,
    because cleanup_metrics.json has its own independent update cycle (written
    after each test run by cleanup_monitor.sh).
    """
    global _last_applied_cleanup
    if cleanup is _last_applied_cleanup:
        return
    _last_applied_cleanup = cleanup

    # cleanup_metrics.json is a full snapshot, so clear prior labels first to
    # avoid leaking stale orphan counts when the file is missing or malformed.
    for metric in [
        rally_cleanup_failure,
        rally_orphaned_resources,
        rally_context_cleanup_warning,
        rally_context_orphaned_resources,
    ]:
        metric.clear()

    # Scenario-created (s_rally_*) orphan metrics — warning/critical severity
    orphaned = cleanup.get("orphaned_resources", {})
    for service, count in orphaned.items():
        rally_cleanup_failure.labels(service=service).set(1 if count > 0 else 0)
        rally_orphaned_resources.labels(service=service, resource_type="total").set(count)

    for resource_type, count in cleanup.get("details", {}).items():
        svc = _SVC_MAP.get(resource_type, "unknown")
        rally_orphaned_resources.labels(service=svc, resource_type=resource_type).set(count)

    # Context-created (c_rally_*) orphan metrics — info severity
    context_orphaned = cleanup.get("context_orphaned_resources", {})
    for service, count in context_orphaned.items():
        rally_context_cleanup_warning.labels(service=service).set(1 if count > 0 else 0)
        rally_context_orphaned_resources.labels(service=service, resource_type="total").set(count)

    for resource_type, count in cleanup.get("context_details", {}).items():
        svc = _SVC_MAP.get(resource_type, "unknown")
        rally_context_orphaned_resources.labels(service=svc, resource_type=resource_type).set(count)

    rgw_scan_status = cleanup.get("rgw_scan_status", "skipped")
    rally_rgw_orphaned_users.set(cleanup.get("rgw_orphaned_users", 0))
    rally_rgw_orphaned_buckets.set(cleanup.get("rgw_orphaned_buckets", 0))
    rally_rgw_unknown_owner_orphans.set(cleanup.get("rgw_unknown_owner_orphans", 0))
    rally_rgw_scan_ok.set(0 if rgw_scan_status == "error" else 1)


def _apply_health_metrics(health: dict) -> None:
    """Apply API health-check metrics from health.json.

    Called on every scrape regardless of whether the Rally summary changed,
    because health.json updates on its own ~15-minute cycle (written by
    health_check.sh). Mirrors _apply_cleanup_metrics: identity-check to skip
    redundant work, then clear-before-set so a service that drops out of a
    later health.json does not leave a stale labeled series behind.
    """
    global _last_applied_health
    if health is _last_applied_health:
        return
    _last_applied_health = health

    # health.json is a full snapshot; clear prior per-service labels first so
    # stale services do not linger when the file shrinks or goes missing.
    rally_api_up.clear()
    rally_api_latency_milliseconds.clear()

    services = health.get("services", {})
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

    # Overall: "up" and "degraded" both map to 1 (reachable); everything else
    # maps to 0 (fail closed). An explicit "down", an "unknown"/missing overall
    # (e.g. the seed file), or any unrecognized value all set 0. This is
    # deliberate: per-service rally_api_up labels are cleared above and only
    # re-set for services present in a valid health.json, so a corrupt or
    # missing file leaves NO per-service series to fire `== 0` alerts. If this
    # unlabeled gauge stayed at its prior 1 on bad data, a broken health
    # pipeline would read as "all healthy". Failing closed means "1 = the most
    # recent health data reports overall reachability; 0 = down OR no valid
    # signal" — RallyApiSignalLost in rally_alerts.yml covers the 0 case.
    overall = health.get("overall")
    if overall in ("up", "degraded"):
        rally_api_overall_up.set(1)
    else:
        rally_api_overall_up.set(0)


# Operator-announcement types. Enum-only: never derive labels from body/id text.
# Mirrors TYPE_LABELS in dashboard/app.js and VALID_TYPES_RE in announce.sh.
_ANNOUNCEMENT_TYPES = ("incident", "maintenance", "scheduled")


def _parse_iso8601_ms(value) -> float:
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


def _is_announcement_active(rec: dict, now_ms: float) -> bool:
    """Return whether an announcement record is currently active.

    KEEP IN SYNC with dashboard/app.js::isAnnouncementActive — both encode the
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
    if rec.get("type") not in _ANNOUNCEMENT_TYPES:
        return False
    start_ms = _parse_iso8601_ms(rec.get("effective_from"))
    if start_ms == start_ms and start_ms > now_ms:  # not-NaN and future
        return False
    end_ms = _parse_iso8601_ms(rec.get("expires_at"))
    if end_ms == end_ms and end_ms <= now_ms:  # not-NaN and past/now
        return False
    return True


def _apply_announcement_metrics(state: dict) -> None:
    """Apply maintenance/announcement gauges from announcement-state.json.

    Emits all three type labels every scrape (0 when none) so series never
    appear or disappear, and sets rally_maintenance_mode to 1 iff any
    maintenance record is currently active. Label cardinality is enum-only;
    body/id text never becomes a label.
    """
    counts = {t: 0 for t in _ANNOUNCEMENT_TYPES}
    now_ms = time.time() * 1000.0

    announcements = state.get("announcements", []) if isinstance(state, dict) else []
    if isinstance(announcements, list):
        for rec in announcements:
            if _is_announcement_active(rec, now_ms):
                counts[rec["type"]] += 1

    for atype in _ANNOUNCEMENT_TYPES:
        rally_announcement_active.labels(type=atype).set(counts[atype])
    rally_maintenance_mode.set(1 if counts["maintenance"] > 0 else 0)


def update_metrics():
    """Read latest results and update all Prometheus metrics.

    Cleanup metrics (rally_cleanup_failure, rally_orphaned_resources, etc.) are
    updated on every scrape because cleanup_metrics.json changes independently of
    the summary timestamp (written after each run by cleanup_monitor.sh).

    Summary-derived labeled metrics (per-service, per-scenario) are only cleared
    and rebuilt when the summary timestamp changes, to avoid transient metric gaps.

    rally_data_valid and rally_overall_success are always updated to reflect the
    current validity of the data.
    """
    global _last_processed_ts

    summary = load_latest_summary()
    cleanup = load_cleanup_metrics()
    health = load_health()
    announcements = load_announcements()

    services = summary.get("services", {})
    current_ts = summary.get("timestamp", "")
    is_valid = bool(services) and current_ts not in ("none", "waiting_for_first_run", "")

    # Cleanup, health, and announcement metrics are always applied — each comes
    # from a separate file with its own independent update cycle (cleanup and
    # health refresh on the health-check/run cadences, announcements whenever an
    # operator runs announce.sh). Apply them before the validity check so these
    # signals stay current even when the Rally summary is missing or stale.
    _apply_cleanup_metrics(cleanup)
    _apply_health_metrics(health)
    _apply_announcement_metrics(announcements)

    if not is_valid:
        # No usable data: signal invalidity without disturbing labeled metrics.
        rally_data_valid.set(0)
        rally_overall_success.set(0)
        return

    rally_data_valid.set(1)

    # run_duration_seconds is written to the summary file after the test run
    # completes, potentially with the same timestamp as the initial write.
    # Update it on every valid scrape so corrections are never missed.
    run_duration = summary.get("run_duration_seconds", 0)
    if run_duration > 0:
        rally_run_duration_seconds.set(run_duration)

    # If the summary timestamp hasn't changed, summary-derived labeled metrics
    # are already up-to-date. Skip the clear+rebuild to avoid transient gaps.
    if current_ts == _last_processed_ts:
        return

    _last_processed_ts = current_ts

    # New data: clear only summary-derived labeled metrics, then rebuild.
    for metric in [
        rally_task_success,
        rally_task_duration_seconds,
        rally_task_iterations_total,
        rally_task_failures_total,
        rally_task_sla_passed,
        rally_service_status,
    ]:
        metric.clear()

    # Update last run timestamp
    ts = parse_timestamp(current_ts)
    if ts > 0:
        rally_last_run_timestamp.set(ts)

    # Per-service metrics
    all_passed = True
    for service, data in services.items():
        status = data.get("status", "pending")

        if status == "passed":
            rally_service_status.labels(service=service).set(1)
        elif status == "failed":
            rally_service_status.labels(service=service).set(0)
            all_passed = False
        else:
            rally_service_status.labels(service=service).set(-1)

        # Per-scenario metrics
        for scenario in data.get("scenarios", []):
            name = scenario.get("name", "unknown")
            rally_task_duration_seconds.labels(
                service=service, scenario=name
            ).set(scenario.get("duration", 0))

            rally_task_iterations_total.labels(
                service=service, scenario=name
            ).set(scenario.get("iterations", 0))

            failures = scenario.get("failures", 0)
            rally_task_failures_total.labels(
                service=service, scenario=name
            ).set(failures)

            passed = 1 if failures == 0 else 0
            rally_task_success.labels(
                service=service, scenario=name
            ).set(passed)
            if not passed:
                all_passed = False

            sla = 1 if scenario.get("sla", False) else 0
            rally_task_sla_passed.labels(
                service=service, scenario=name
            ).set(sla)

    rally_overall_success.set(1 if all_passed else 0)


# ---------------------------------------------------------------------------
# Flask Routes
# ---------------------------------------------------------------------------
@app.route("/metrics")
def metrics():
    """Prometheus metrics endpoint."""
    update_metrics()
    return Response(
        generate_latest(registry),
        mimetype=CONTENT_TYPE_LATEST,
    )


@app.route("/health")
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok", "timestamp": time.time()})


@app.route("/ready")
def ready():
    """Readiness check endpoint."""
    summary = load_latest_summary()
    reasons = []

    timestamp = summary.get("timestamp", "")
    if timestamp in ("waiting_for_first_run", "none", ""):
        reasons.append("timestamp_missing")
        age_minutes = None
    else:
        try:
            parsed = datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - parsed
            age_minutes = age.total_seconds() / 60.0
            if age >= timedelta(minutes=READY_MAX_AGE_MINUTES):
                reasons.append("timestamp_too_old")
        except (ValueError, TypeError):
            age_minutes = None
            reasons.append("timestamp_invalid")

    services = summary.get("services", {})
    if not any(
        data.get("status", "pending") != "pending"
        for data in services.values()
        if isinstance(data, dict)
    ):
        reasons.append("all_services_pending")

    if reasons:
        return jsonify({"ready": False, "reasons": reasons}), 503

    return jsonify(
        {
            "ready": True,
            "timestamp": timestamp,
            "age_minutes": round(age_minutes, 2),
        }
    ), 200


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Rally Exporter starting on port {EXPORTER_PORT}...")
    app.run(host="0.0.0.0", port=EXPORTER_PORT, debug=False)
