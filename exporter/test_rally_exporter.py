"""Unit tests for rally_exporter.py."""
import json
import os
import time

import pytest
from prometheus_client import generate_latest

import rally_exporter as exporter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def make_cleanup(s_nova=0, c_nova=0):
    """Build a minimal cleanup_metrics dict."""
    return {
        "timestamp": "20240101T120000Z",
        "cleanup_failed": 1 if s_nova > 0 else 0,
        "context_cleanup_warning": 1 if c_nova > 0 else 0,
        "orphaned_resources": {"nova": s_nova},
        "context_orphaned_resources": {"nova": c_nova},
        "details": {"servers": s_nova},
        "context_details": {"servers": c_nova},
    }


def metrics_output():
    """Return current registry output as a string."""
    return generate_latest(exporter.registry).decode()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset module-level cache and metric state between tests."""
    exporter._summary_mtime = -1.0
    exporter._summary_data = {}
    exporter._summary_cache_time = 0.0
    exporter._cleanup_mtime = -1.0
    exporter._cleanup_data = {}
    exporter._cleanup_cache_time = 0.0
    exporter._last_processed_ts = ""
    # Reset scalar metrics
    exporter.rally_data_valid.set(0)
    exporter.rally_overall_success.set(0)
    exporter.rally_last_run_timestamp.set(0)
    exporter.rally_run_duration_seconds.set(0)
    # Clear labeled metrics
    for m in [
        exporter.rally_task_success,
        exporter.rally_task_duration_seconds,
        exporter.rally_task_iterations_total,
        exporter.rally_task_failures_total,
        exporter.rally_task_sla_passed,
        exporter.rally_service_status,
        exporter.rally_cleanup_failure,
        exporter.rally_orphaned_resources,
        exporter.rally_context_cleanup_warning,
        exporter.rally_context_orphaned_resources,
    ]:
        m.clear()
    yield


@pytest.fixture
def results_dir(tmp_path, monkeypatch):
    """Temporary results directory with RESULTS_DIR patched."""
    monkeypatch.setattr(exporter, "RESULTS_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def client():
    """Flask test client."""
    exporter.app.config["TESTING"] = True
    with exporter.app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# parse_timestamp
# ---------------------------------------------------------------------------

class TestParseTimestamp:
    def test_valid_format(self):
        ts = exporter.parse_timestamp("20240101T120000Z")
        assert ts > 0

    def test_known_epoch(self):
        # 2024-01-01T12:00:00Z = 1704110400
        assert exporter.parse_timestamp("20240101T120000Z") == pytest.approx(1704110400.0)

    def test_none_string_returns_zero(self):
        assert exporter.parse_timestamp("none") == 0.0

    def test_empty_string_returns_zero(self):
        assert exporter.parse_timestamp("") == 0.0

    def test_waiting_string_returns_zero(self):
        assert exporter.parse_timestamp("waiting_for_first_run") == 0.0

    def test_invalid_format_returns_zero(self):
        assert exporter.parse_timestamp("not-a-date") == 0.0

    def test_none_value_returns_zero(self):
        assert exporter.parse_timestamp(None) == 0.0


# ---------------------------------------------------------------------------
# load_latest_summary
# ---------------------------------------------------------------------------

class TestLoadLatestSummary:
    def test_missing_file_returns_default(self, results_dir):
        result = exporter.load_latest_summary()
        assert result == {"timestamp": "none", "services": {}}

    def test_corrupt_json_returns_default(self, results_dir):
        (results_dir / "latest_summary.json").write_text("not valid json {{")
        result = exporter.load_latest_summary()
        assert result == {"timestamp": "none", "services": {}}

    def test_valid_file_returns_data(self, results_dir):
        summary = make_summary()
        (results_dir / "latest_summary.json").write_text(json.dumps(summary))
        result = exporter.load_latest_summary()
        assert result["timestamp"] == "20240101T120000Z"
        assert "nova" in result["services"]

    def test_caches_on_same_mtime(self, results_dir):
        """File is not re-read when mtime is unchanged and cache is fresh."""
        summary = make_summary()
        path = results_dir / "latest_summary.json"
        path.write_text(json.dumps(summary))
        # First call loads and caches
        exporter.load_latest_summary()
        # Overwrite file content but preserve mtime to simulate no-change
        saved_mtime = path.stat().st_mtime
        path.write_text(json.dumps(make_summary(timestamp="20991231T235959Z")))
        os.utime(path, (saved_mtime, saved_mtime))
        result = exporter.load_latest_summary()
        assert result["timestamp"] == "20240101T120000Z"  # original cached value

    def test_reloads_when_mtime_changes(self, results_dir):
        """File is re-read when mtime changes."""
        path = results_dir / "latest_summary.json"
        path.write_text(json.dumps(make_summary(timestamp="20240101T120000Z")))
        exporter.load_latest_summary()
        # Write new content and let mtime advance naturally
        time.sleep(0.01)
        path.write_text(json.dumps(make_summary(timestamp="20240202T120000Z")))
        result = exporter.load_latest_summary()
        assert result["timestamp"] == "20240202T120000Z"


# ---------------------------------------------------------------------------
# load_cleanup_metrics
# ---------------------------------------------------------------------------

class TestLoadCleanupMetrics:
    def test_missing_file_returns_default(self, results_dir):
        result = exporter.load_cleanup_metrics()
        assert result == {"cleanup_failed": 0, "orphaned_resources": {}, "details": {}}

    def test_corrupt_json_returns_default(self, results_dir):
        (results_dir / "cleanup_metrics.json").write_text("{bad json")
        result = exporter.load_cleanup_metrics()
        assert result["cleanup_failed"] == 0

    def test_valid_file_returns_data(self, results_dir):
        cleanup = make_cleanup(s_nova=3)
        (results_dir / "cleanup_metrics.json").write_text(json.dumps(cleanup))
        result = exporter.load_cleanup_metrics()
        assert result["orphaned_resources"]["nova"] == 3


# ---------------------------------------------------------------------------
# update_metrics — CQ-08 regression tests
# ---------------------------------------------------------------------------

class TestUpdateMetrics:
    def test_missing_file_sets_data_invalid(self, results_dir):
        """CQ-08: Missing summary file must not report success."""
        exporter.update_metrics()
        output = metrics_output()
        assert "rally_data_valid 0.0" in output
        assert "rally_overall_success 0.0" in output

    def test_empty_services_sets_data_invalid(self, results_dir):
        """CQ-08: Summary with empty services must not report success."""
        summary = {"timestamp": "20240101T120000Z", "services": {}}
        (results_dir / "latest_summary.json").write_text(json.dumps(summary))
        exporter.update_metrics()
        output = metrics_output()
        assert "rally_data_valid 0.0" in output
        assert "rally_overall_success 0.0" in output

    def test_waiting_for_first_run_sets_data_invalid(self, results_dir):
        """CQ-08: Seed/waiting summary must not report success."""
        summary = {
            "timestamp": "waiting_for_first_run",
            "services": {
                "nova": {
                    "status": "pending",
                    "duration": 0,
                    "total_iterations": 0,
                    "failed_iterations": 0,
                    "sla_passed": True,
                    "scenarios": [],
                }
            },
        }
        (results_dir / "latest_summary.json").write_text(json.dumps(summary))
        exporter.update_metrics()
        output = metrics_output()
        assert "rally_data_valid 0.0" in output
        assert "rally_overall_success 0.0" in output

    def test_valid_passing_data_reports_success(self, results_dir):
        """Valid summary with all services passing → success=1, data_valid=1."""
        (results_dir / "latest_summary.json").write_text(json.dumps(make_summary()))
        exporter.update_metrics()
        output = metrics_output()
        assert "rally_data_valid 1.0" in output
        assert "rally_overall_success 1.0" in output

    def test_failed_service_sets_overall_failure(self, results_dir):
        summary = make_summary(services={
            "nova": {
                "status": "failed",
                "duration": 5.0,
                "total_iterations": 2,
                "failed_iterations": 1,
                "sla_passed": False,
                "scenarios": [
                    {"name": "CreateDeleteServer", "duration": 5.0, "iterations": 2, "failures": 1, "sla": False}
                ],
            }
        })
        (results_dir / "latest_summary.json").write_text(json.dumps(summary))
        exporter.update_metrics()
        output = metrics_output()
        assert "rally_overall_success 0.0" in output
        assert "rally_data_valid 1.0" in output

    def test_labeled_metrics_retained_on_same_timestamp(self, results_dir):
        """CQ-04: Labeled metrics are not cleared when timestamp is unchanged."""
        path = results_dir / "latest_summary.json"
        path.write_text(json.dumps(make_summary()))
        exporter.update_metrics()
        # Confirm service metric is set
        output1 = metrics_output()
        assert 'rally_service_status{service="nova"} 1.0' in output1
        # Second scrape with same timestamp — metrics should stay
        exporter.update_metrics()
        output2 = metrics_output()
        assert 'rally_service_status{service="nova"} 1.0' in output2

    def test_labeled_metrics_rebuilt_on_new_timestamp(self, results_dir):
        """CQ-04: Labeled metrics are rebuilt when timestamp changes."""
        path = results_dir / "latest_summary.json"
        path.write_text(json.dumps(make_summary(timestamp="20240101T120000Z")))
        exporter.update_metrics()
        assert 'rally_service_status{service="nova"} 1.0' in metrics_output()
        # New run with nova failing — reset file cache so the new content is read
        path.write_text(json.dumps(make_summary(
            timestamp="20240101T160000Z",
            services={
                "nova": {
                    "status": "failed",
                    "duration": 5.0,
                    "total_iterations": 2,
                    "failed_iterations": 1,
                    "sla_passed": False,
                    "scenarios": [
                        {"name": "CreateDeleteServer", "duration": 5.0, "iterations": 2, "failures": 1, "sla": False}
                    ],
                }
            }
        )))
        exporter._summary_mtime = -1.0  # force cache miss on next load
        exporter.update_metrics()
        output = metrics_output()
        assert 'rally_service_status{service="nova"} 0.0' in output

    def test_cleanup_metrics_update_on_same_summary_timestamp(self, results_dir):
        """Cleanup metrics update even when summary timestamp is unchanged."""
        summary_path = results_dir / "latest_summary.json"
        cleanup_path = results_dir / "cleanup_metrics.json"

        summary_path.write_text(json.dumps(make_summary()))
        cleanup_path.write_text(json.dumps(make_cleanup(s_nova=0)))
        exporter.update_metrics()
        assert 'rally_cleanup_failure{service="nova"} 0.0' in metrics_output()

        # Write new cleanup data but keep same summary file (mtime preserved)
        cleanup_path.write_text(json.dumps(make_cleanup(s_nova=3)))
        exporter._cleanup_mtime = -1.0  # force cache miss on cleanup file
        exporter.update_metrics()
        # Cleanup metric must reflect new value even though summary timestamp unchanged
        assert 'rally_cleanup_failure{service="nova"} 1.0' in metrics_output()

    def test_cleanup_metrics_update_when_summary_invalid(self, results_dir):
        """Cleanup metrics update even when summary file is missing/invalid."""
        cleanup_path = results_dir / "cleanup_metrics.json"
        cleanup_path.write_text(json.dumps(make_cleanup(s_nova=2)))
        # No summary file — update_metrics would otherwise return early
        exporter.update_metrics()
        output = metrics_output()
        assert "rally_data_valid 0.0" in output
        assert 'rally_cleanup_failure{service="nova"} 1.0' in output


# ---------------------------------------------------------------------------
# Flask endpoints
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    def test_returns_200_with_ok(self, client, results_dir):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert "timestamp" in data


class TestReadyEndpoint:
    def test_not_ready_when_no_results(self, client, results_dir):
        resp = client.get("/ready")
        assert resp.status_code == 503
        data = resp.get_json()
        assert data["ready"] is False

    def test_not_ready_when_all_pending(self, client, results_dir):
        summary = make_summary(services={
            "nova": {"status": "pending", "duration": 0, "total_iterations": 0,
                     "failed_iterations": 0, "sla_passed": True, "scenarios": []}
        })
        (results_dir / "latest_summary.json").write_text(json.dumps(summary))
        resp = client.get("/ready")
        assert resp.status_code == 503

    def test_ready_with_recent_valid_results(self, client, results_dir):
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        summary = make_summary(timestamp=ts)
        (results_dir / "latest_summary.json").write_text(json.dumps(summary))
        resp = client.get("/ready")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ready"] is True


class TestMetricsEndpoint:
    def test_returns_200_with_prometheus_content_type(self, client, results_dir):
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert b"text/plain" in resp.content_type.encode()

    def test_contains_expected_metric_names(self, client, results_dir):
        resp = client.get("/metrics")
        body = resp.data
        assert b"rally_data_valid" in body
        assert b"rally_overall_success" in body
        assert b"rally_last_run_timestamp" in body
        assert b"rally_exporter_errors_total" in body
