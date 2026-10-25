"""Shared builders for the exporter test modules (not a test file)."""
import time

from prometheus_client import generate_latest

from metrics_common import registry


def make_summary(timestamp="20240101T120000Z", services=None, run_duration=10.0):
    """Build a minimal valid summary dict."""
    if services is None:
        services = {
            "nova": {
                "status": "passed",
                "duration": 10.0,
                "total_iterations": 2,
                "failed_iterations": 0,
                "sla_passed": True,
                "scenarios": [
                    {
                        "name": "CreateDeleteServer",
                        "duration": 10.0,
                        "iterations": 2,
                        "failures": 0,
                        "sla": True,
                    }
                ],
            }
        }
    return {"timestamp": timestamp, "services": services, "run_duration_seconds": run_duration}


def make_cleanup(s_nova=0, c_nova=0, rgw_status="skipped", rgw_users=0, rgw_buckets=0, rgw_unknown=0,
                 openstack_status="ok"):
    """Build a minimal cleanup_metrics dict."""
    return {
        "timestamp": "20240101T120000Z",
        "openstack_scan_status": openstack_status,
        "openstack_scan_errors": 0 if openstack_status == "ok" else 1,
        "cleanup_failed": 1 if s_nova > 0 else 0,
        "context_cleanup_warning": 1 if c_nova > 0 else 0,
        "rgw_scan_status": rgw_status,
        "rgw_orphaned_users": rgw_users,
        "rgw_orphaned_buckets": rgw_buckets,
        "rgw_unknown_owner_orphans": rgw_unknown,
        "orphaned_resources": {"nova": s_nova},
        "context_orphaned_resources": {"nova": c_nova},
        "details": {"servers": s_nova},
        "context_details": {"servers": c_nova},
    }


def make_health(overall="up", services=None):
    """Build a minimal health.json dict."""
    if services is None:
        services = {
            "keystone": {"status": "up", "latency_ms": 120, "checked_at": "2024-01-01T12:00:00Z"},
            "nova": {"status": "down", "latency_ms": 5000, "checked_at": "2024-01-01T12:00:00Z"},
        }
    # Stamped "now" so the snapshot is fresh; staleness has dedicated tests.
    return {"timestamp": iso_offset(0), "overall": overall, "services": services}


def make_announcement(rec_type="incident", effective_from=None, expires_at=None,
                      rec_id="incident-1", body="x"):
    """Build a single announcement record (only enum-relevant fields matter)."""
    rec = {"id": rec_id, "type": rec_type, "body": body, "created_at": "2024-01-01T00:00:00Z"}
    if effective_from is not None:
        rec["effective_from"] = effective_from
    if expires_at is not None:
        rec["expires_at"] = expires_at
    return rec


def iso_offset(seconds):
    """ISO 8601 UTC timestamp `seconds` from now (negative = past)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds))


def metrics_output():
    """Return current registry output as a string."""
    return generate_latest(registry).decode()
