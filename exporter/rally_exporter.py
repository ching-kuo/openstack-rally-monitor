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
from pathlib import Path

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

rally_exporter_errors_total = Counter(
    "rally_exporter_errors_total",
    "Total number of errors reading or parsing result files",
    ["file"],
    registry=registry,
)


# ---------------------------------------------------------------------------
# Data Loading
# ---------------------------------------------------------------------------
def load_latest_summary() -> dict:
    """Load the latest summary JSON file."""
    summary_file = os.path.join(RESULTS_DIR, "latest_summary.json")
    try:
        with open(summary_file, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        rally_exporter_errors_total.labels(file="latest_summary.json").inc()
        return {"timestamp": "none", "services": {}}


def load_cleanup_metrics() -> dict:
    """Load cleanup metrics JSON file."""
    metrics_file = os.path.join(RESULTS_DIR, "cleanup_metrics.json")
    try:
        with open(metrics_file, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        rally_exporter_errors_total.labels(file="cleanup_metrics.json").inc()
        return {"cleanup_failed": 0, "orphaned_resources": {}, "details": {}}



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
def update_metrics():
    """Read latest results and update all Prometheus metrics."""
    for metric in [
        rally_task_success,
        rally_task_duration_seconds,
        rally_task_iterations_total,
        rally_task_failures_total,
        rally_task_sla_passed,
        rally_service_status,
        rally_cleanup_failure,
        rally_orphaned_resources,
        rally_context_cleanup_warning,
        rally_context_orphaned_resources,
    ]:
        metric.clear()

    summary = load_latest_summary()
    cleanup = load_cleanup_metrics()

    # Update last run timestamp
    ts = parse_timestamp(summary.get("timestamp", ""))
    if ts > 0:
        rally_last_run_timestamp.set(ts)

    # Track overall success
    all_passed = True

    # Per-service metrics
    services = summary.get("services", {})
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
    run_duration = summary.get("run_duration_seconds", 0)
    if run_duration > 0:
        rally_run_duration_seconds.set(run_duration)

    svc_map = {
        "servers": "nova",
        "networks": "neutron",
        "routers": "neutron",
        "security_groups": "neutron",
        "volumes": "cinder",
        "images": "glance",
        "users": "keystone",
        "projects": "keystone",
    }

    # Scenario-created (s_rally_*) orphan metrics — warning/critical severity
    orphaned = cleanup.get("orphaned_resources", {})
    for service, count in orphaned.items():
        rally_cleanup_failure.labels(service=service).set(
            1 if count > 0 else 0
        )
        rally_orphaned_resources.labels(
            service=service, resource_type="total"
        ).set(count)

    for resource_type, count in cleanup.get("details", {}).items():
        svc = svc_map.get(resource_type, "unknown")
        rally_orphaned_resources.labels(
            service=svc, resource_type=resource_type
        ).set(count)

    # Context-created (c_rally_*) orphan metrics — info severity
    context_orphaned = cleanup.get("context_orphaned_resources", {})
    for service, count in context_orphaned.items():
        rally_context_cleanup_warning.labels(service=service).set(
            1 if count > 0 else 0
        )
        rally_context_orphaned_resources.labels(
            service=service, resource_type="total"
        ).set(count)

    for resource_type, count in cleanup.get("context_details", {}).items():
        svc = svc_map.get(resource_type, "unknown")
        rally_context_orphaned_resources.labels(
            service=svc, resource_type=resource_type
        ).set(count)


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
