"""Unit tests for rally_exporter.py."""
import json
import os
import time

import pytest

import metrics_common as mc
import rally_exporter as exporter
from exporter_testlib import make_cleanup, make_summary, metrics_output


# ---------------------------------------------------------------------------
# parse_timestamp
# ---------------------------------------------------------------------------

class TestParseTimestamp:
    def test_known_epoch(self):
        # 2024-01-01T12:00:00Z = 1704110400
        assert exporter.parse_timestamp("20240101T120000Z") == pytest.approx(1704110400.0)

    @pytest.mark.parametrize(
        "invalid_value",
        ["none", "", "waiting_for_first_run", "not-a-date", None],
    )
    def test_invalid_values_return_zero(self, invalid_value):
        assert exporter.parse_timestamp(invalid_value) == 0.0


# ---------------------------------------------------------------------------
# load_latest_summary
# ---------------------------------------------------------------------------

class TestLoadLatestSummary:
    @pytest.mark.parametrize("contents", [None, "not valid json {{"])
    def test_missing_or_corrupt_file_returns_default(self, results_dir, contents):
        if contents is not None:
            (results_dir / "latest_summary.json").write_text(contents)
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
    @pytest.mark.parametrize("contents", [None, "{bad json"])
    def test_missing_or_corrupt_file_returns_default(self, results_dir, contents):
        if contents is not None:
            (results_dir / "cleanup_metrics.json").write_text(contents)
        result = exporter.load_cleanup_metrics()
        assert result["cleanup_failed"] == 0
        assert result["rgw_scan_status"] == "skipped"
        assert result["rgw_orphaned_users"] == 0

    def test_valid_file_returns_data(self, results_dir):
        cleanup = make_cleanup(s_nova=3)
        (results_dir / "cleanup_metrics.json").write_text(json.dumps(cleanup))
        result = exporter.load_cleanup_metrics()
        assert result["orphaned_resources"]["nova"] == 3


# ---------------------------------------------------------------------------
# update_metrics — CQ-08 regression tests
# ---------------------------------------------------------------------------

class TestUpdateMetrics:
    @pytest.mark.parametrize(
        "summary",
        [
            pytest.param(None, id="missing_file"),
            pytest.param({"timestamp": "20240101T120000Z", "services": {}}, id="empty_services"),
            pytest.param(
                {
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
                },
                id="waiting_for_first_run",
            ),
        ],
    )
    def test_invalid_summary_sets_data_invalid(self, results_dir, summary):
        """CQ-08: missing, empty-services, or seed/waiting summaries must not report success."""
        if summary is not None:
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

    def test_cleanup_labels_clear_when_cleanup_file_disappears(self, results_dir):
        """Cleanup labels should not linger after the cleanup snapshot disappears."""
        summary_path = results_dir / "latest_summary.json"
        cleanup_path = results_dir / "cleanup_metrics.json"

        summary_path.write_text(json.dumps(make_summary()))
        cleanup_path.write_text(json.dumps(make_cleanup(s_nova=2, c_nova=1)))
        exporter.update_metrics()

        output = metrics_output()
        assert 'rally_cleanup_failure{service="nova"} 1.0' in output
        assert 'rally_context_cleanup_warning{service="nova"} 1.0' in output

        cleanup_path.unlink()
        exporter._cleanup_mtime = -1.0
        exporter.update_metrics()

        output = metrics_output()
        assert 'rally_cleanup_failure{service="nova"}' not in output
        assert 'rally_orphaned_resources{service="nova",resource_type="servers"}' not in output
        assert 'rally_context_cleanup_warning{service="nova"}' not in output
        assert 'rally_context_orphaned_resources{service="nova",resource_type="servers"}' not in output

    def test_rgw_metrics_follow_cleanup_file(self, results_dir):
        summary_path = results_dir / "latest_summary.json"
        cleanup_path = results_dir / "cleanup_metrics.json"

        summary_path.write_text(json.dumps(make_summary()))
        cleanup_path.write_text(json.dumps(make_cleanup(rgw_status="ok", rgw_users=2, rgw_buckets=5, rgw_unknown=1)))
        exporter.update_metrics()

        output = metrics_output()
        assert "rally_rgw_orphaned_users 2.0" in output
        assert "rally_rgw_orphaned_buckets 5.0" in output
        assert "rally_rgw_unknown_owner_orphans 1.0" in output
        assert "rally_rgw_scan_ok 1.0" in output

    def test_rgw_scan_error_sets_scan_ok_zero(self, results_dir):
        summary_path = results_dir / "latest_summary.json"
        cleanup_path = results_dir / "cleanup_metrics.json"

        summary_path.write_text(json.dumps(make_summary()))
        cleanup_path.write_text(json.dumps(make_cleanup(rgw_status="error", rgw_users=1, rgw_buckets=2)))
        exporter.update_metrics()

        output = metrics_output()
        assert "rally_rgw_scan_ok 0.0" in output

    def test_missing_or_corrupt_cleanup_file_is_not_a_healthy_scan(self, results_dir):
        (results_dir / "latest_summary.json").write_text(json.dumps(make_summary()))
        exporter.update_metrics()  # no cleanup_metrics.json at all
        assert "rally_cleanup_scan_ok 0.0" in metrics_output()

        cleanup_path = results_dir / "cleanup_metrics.json"
        cleanup_path.write_text(json.dumps(make_cleanup()))
        exporter.update_metrics()
        assert "rally_cleanup_scan_ok 1.0" in metrics_output()

        cleanup_path.write_text("{not json")
        os.utime(cleanup_path, (time.time() + 5, time.time() + 5))
        exporter.update_metrics()
        assert "rally_cleanup_scan_ok 0.0" in metrics_output()

    def test_openstack_scan_error_sets_cleanup_scan_ok_zero(self, results_dir):
        (results_dir / "latest_summary.json").write_text(json.dumps(make_summary()))
        cleanup_path = results_dir / "cleanup_metrics.json"
        cleanup_path.write_text(json.dumps(make_cleanup(openstack_status="error")))
        exporter.update_metrics()
        assert "rally_cleanup_scan_ok 0.0" in metrics_output()

        # Recovery flips it back; a pre-field file (no key) also reads as ok.
        legacy = make_cleanup()
        del legacy["openstack_scan_status"]
        cleanup_path.write_text(json.dumps(legacy))
        os.utime(cleanup_path, (time.time() + 5, time.time() + 5))
        exporter.update_metrics()
        assert "rally_cleanup_scan_ok 1.0" in metrics_output()


class TestCronSpacing:
    """KEEP IN SYNC with entrypoint_config.sh::make_cron_schedule."""

    @pytest.mark.parametrize(
        "raw, expected_minutes",
        [
            ("15", 15),
            ("45", 45),
            ("015", 15),
            ("60", 60),
            ("61", 120),
            ("90", 120),
            ("240", 240),
            ("1439", 1440),
            ("1440", 1440),
            (None, 240),
            ("", 240),
            ("4h", 240),
            ("0", 240),
            ("1441", 240),
        ],
    )
    def test_spacing(self, raw, expected_minutes):
        assert mc.cron_spacing_seconds(raw, 240) == expected_minutes * 60


class TestRunSetupFailure:
    """A run that aborts before any test ({services: {}, error}) is a real,
    failed run -- not "no data"."""

    @staticmethod
    def failure_summary(timestamp="20240101T160000Z"):
        return {
            "timestamp": timestamp,
            "error": "deployment_setup_failed",
            "error_detail": "401 Unauthorized",
            "services": {},
        }

    def test_failure_summary_is_valid_but_failed(self, results_dir):
        path = results_dir / "latest_summary.json"
        path.write_text(json.dumps(make_summary(timestamp="20240101T120000Z")))
        exporter.update_metrics()
        assert 'rally_service_status{service="nova"} 1.0' in metrics_output()

        path.write_text(json.dumps(self.failure_summary()))
        exporter._summary_mtime = -1.0  # force cache miss
        exporter.update_metrics()
        output = metrics_output()
        assert "rally_data_valid 1.0" in output
        assert "rally_overall_success 0.0" in output
        assert "rally_run_setup_ok 0.0" in output
        # The last green per-service series must not survive the failed run.
        assert "rally_service_status{" not in output
        assert "rally_task_success{" not in output
        # The failed run is the newest run: staleness alerts must not fire.
        assert "rally_last_run_timestamp 1.7041248e+09" in output

    def test_failure_summary_resets_duration_and_end_time(self, results_dir):
        """An aborted run has no duration: the previous run's must not linger,
        and its end time is its start (RallyStaleResults measures from here)."""
        sample = exporter.registry.get_sample_value
        path = results_dir / "latest_summary.json"
        path.write_text(json.dumps(make_summary(timestamp="20240101T120000Z", run_duration=600)))
        exporter.update_metrics()
        assert sample("rally_run_duration_seconds") == 600
        assert sample("rally_last_run_end_timestamp") == 1704110400 + 600

        path.write_text(json.dumps(self.failure_summary()))  # 20240101T160000Z
        exporter._summary_mtime = -1.0
        exporter.update_metrics()
        assert sample("rally_run_duration_seconds") == 0
        assert sample("rally_last_run_end_timestamp") == 1704124800

    def test_missing_duration_resets_duration_with_end_time(self, results_dir):
        """A run killed after build_summary has no run_duration_seconds. Its
        end time is its start, so the duration that RallyStaleResults adds to
        the threshold must be 0 too, not the previous run's."""
        sample = exporter.registry.get_sample_value
        path = results_dir / "latest_summary.json"
        path.write_text(json.dumps(make_summary(timestamp="20240101T120000Z", run_duration=600)))
        exporter.update_metrics()

        killed = make_summary(timestamp="20240101T160000Z")
        del killed["run_duration_seconds"]
        path.write_text(json.dumps(killed))
        exporter._summary_mtime = -1.0
        exporter.update_metrics()
        assert sample("rally_run_duration_seconds") == 0
        assert sample("rally_last_run_end_timestamp") == 1704124800

    def test_normal_summary_sets_run_setup_ok(self, results_dir):
        (results_dir / "latest_summary.json").write_text(json.dumps(make_summary()))
        exporter.update_metrics()
        assert "rally_run_setup_ok 1.0" in metrics_output()

    def test_service_failed_to_start_fails_overall(self, results_dir):
        """A service whose Rally task never started is reported failed by
        build_summary (with an error field) and must clear overall success."""
        summary = make_summary(services={
            "nova": {
                "status": "failed",
                "error": "task_failed_to_start",
                "duration": 0,
                "total_iterations": 0,
                "failed_iterations": 0,
                "sla_passed": True,
                "scenarios": [],
            }
        })
        (results_dir / "latest_summary.json").write_text(json.dumps(summary))
        exporter.update_metrics()
        output = metrics_output()
        assert 'rally_service_status{service="nova"} 0.0' in output
        assert "rally_overall_success 0.0" in output

    def test_recovers_after_transient_invalid_read(self, results_dir):
        """A transiently unreadable summary followed by the same valid summary
        must restore overall success and the labeled series (it used to stay 0
        until the next run because the timestamp had already been processed)."""
        path = results_dir / "latest_summary.json"
        path.write_text(json.dumps(make_summary()))
        exporter.update_metrics()
        path.unlink()
        exporter.update_metrics()
        assert "rally_overall_success 0.0" in metrics_output()

        path.write_text(json.dumps(make_summary()))
        exporter._summary_mtime = -1.0
        exporter.update_metrics()
        output = metrics_output()
        assert "rally_overall_success 1.0" in output
        assert 'rally_service_status{service="nova"} 1.0' in output


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
