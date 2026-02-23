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
from pathlib import Path

from flask import Flask, Response, jsonify
from prometheus_client import (
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
    "Whether cleanup failed (1) or succeeded (0)",
    ["service"],
    registry=registry,
)

rally_orphaned_resources = Gauge(
    "rally_orphaned_resources",
    "Number of orphaned resources detected",
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
        return {"timestamp": "none", "services": {}}


def load_cleanup_metrics() -> dict:
    """Load cleanup metrics JSON file."""
    metrics_file = os.path.join(RESULTS_DIR, "cleanup_metrics.json")
    try:
        with open(metrics_file, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"cleanup_failed": 0, "orphaned_resources": {}, "details": {}}



def parse_timestamp(ts: str) -> float:
    """Convert Rally timestamp string to epoch."""
    try:
        from datetime import datetime
        dt = datetime.strptime(ts, "%Y%m%dT%H%M%SZ")
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Metrics Update
# ---------------------------------------------------------------------------
def update_metrics():
    """Read latest results and update all Prometheus metrics."""
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

    # Cleanup metrics
    cleanup_failed = cleanup.get("cleanup_failed", 0)
    orphaned = cleanup.get("orphaned_resources", {})
    for service, count in orphaned.items():
        rally_cleanup_failure.labels(service=service).set(
            1 if count > 0 else 0
        )
        rally_orphaned_resources.labels(
            service=service, resource_type="total"
        ).set(count)

    details = cleanup.get("details", {})
    for resource_type, count in details.items():
        # Map resource types to services
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
        svc = svc_map.get(resource_type, "unknown")
        rally_orphaned_resources.labels(
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Rally Exporter starting on port {EXPORTER_PORT}...")
    app.run(host="0.0.0.0", port=EXPORTER_PORT, debug=False)
