"""Exporter gauges for a run that aborts before any test (rally_exporter.py)."""
import json

import rally_exporter as exporter
from exporter_testlib import make_summary, metrics_output


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
