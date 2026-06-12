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


def make_cleanup(s_nova=0, c_nova=0, rgw_status="skipped", rgw_users=0, rgw_buckets=0, rgw_unknown=0):
    """Build a minimal cleanup_metrics dict."""
    return {
        "timestamp": "20240101T120000Z",
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
    return {"timestamp": "2024-01-01T12:00:00Z", "overall": overall, "services": services}


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
    exporter._health_mtime = -1.0
    exporter._health_data = {}
    exporter._health_cache_time = 0.0
    exporter._announce_mtime = -1.0
    exporter._announce_data = {}
    exporter._announce_cache_time = 0.0
    exporter._last_processed_ts = ""
    exporter._last_applied_cleanup = {}
    exporter._last_applied_health = {}
    # Reset scalar metrics
    exporter.rally_data_valid.set(0)
    exporter.rally_overall_success.set(0)
    exporter.rally_last_run_timestamp.set(0)
    exporter.rally_run_duration_seconds.set(0)
    exporter.rally_maintenance_mode.set(0)
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
        exporter.rally_api_up,
        exporter.rally_api_latency_milliseconds,
        exporter.rally_announcement_active,
    ]:
        m.clear()
    exporter.rally_rgw_orphaned_users.set(0)
    exporter.rally_rgw_orphaned_buckets.set(0)
    exporter.rally_rgw_unknown_owner_orphans.set(0)
    exporter.rally_rgw_scan_ok.set(1)
    # rally_api_overall_up is unlabeled: .clear() is unsupported, so reset to a
    # known baseline between tests. The gauge now FAILS CLOSED -- unknown/missing
    # overall sets 0 -- and several tests pre-set it to 1 to prove the flip.
    exporter.rally_api_overall_up.set(0)
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


# ---------------------------------------------------------------------------
# load_health
# ---------------------------------------------------------------------------

class TestLoadHealth:
    @pytest.mark.parametrize("contents", [None, "{not json"])
    def test_missing_or_corrupt_file_returns_default(self, results_dir, contents):
        if contents is not None:
            (results_dir / "health.json").write_text(contents)
        result = exporter.load_health()
        assert result == {"overall": "unknown", "services": {}}

    def test_missing_or_corrupt_file_increments_error_counter(self, results_dir):
        """health.json is always seeded, so absence/corruption is a real error."""
        before = exporter.rally_exporter_errors_total.labels(file="health.json")._value.get()
        exporter.load_health()  # no file present
        after = exporter.rally_exporter_errors_total.labels(file="health.json")._value.get()
        assert after == before + 1

    def test_valid_file_returns_data(self, results_dir):
        (results_dir / "health.json").write_text(json.dumps(make_health()))
        result = exporter.load_health()
        assert result["overall"] == "up"
        assert result["services"]["keystone"]["status"] == "up"


# ---------------------------------------------------------------------------
# API health metrics — update_metrics / _apply_health_metrics
# ---------------------------------------------------------------------------

class TestHealthMetrics:
    def test_per_service_and_overall_set(self, results_dir):
        (results_dir / "latest_summary.json").write_text(json.dumps(make_summary()))
        (results_dir / "health.json").write_text(json.dumps(make_health()))
        exporter.update_metrics()
        output = metrics_output()
        assert 'rally_api_up{service="keystone"} 1.0' in output
        assert 'rally_api_up{service="nova"} 0.0' in output
        assert 'rally_api_latency_milliseconds{service="keystone"} 120.0' in output
        assert 'rally_api_latency_milliseconds{service="nova"} 5000.0' in output
        assert "rally_api_overall_up 1.0" in output

    def test_overall_down_sets_zero(self, results_dir):
        (results_dir / "health.json").write_text(json.dumps(make_health(overall="down")))
        exporter.update_metrics()
        assert "rally_api_overall_up 0.0" in metrics_output()

    def test_degraded_service_counts_as_up(self, results_dir):
        """degraded means reachable-but-slow: rally_api_up is 1 (slowness shows
        only in rally_api_latency_milliseconds)."""
        (results_dir / "health.json").write_text(json.dumps(make_health(
            overall="degraded",
            services={
                "keystone": {"status": "up", "latency_ms": 120, "checked_at": "2024-01-01T12:00:00Z"},
                "nova": {"status": "degraded", "latency_ms": 6200, "checked_at": "2024-01-01T12:00:00Z"},
                "cinder": {"status": "down", "latency_ms": 30000, "checked_at": "2024-01-01T12:00:00Z"},
            },
        )))
        exporter.update_metrics()
        output = metrics_output()
        assert 'rally_api_up{service="keystone"} 1.0' in output
        assert 'rally_api_up{service="nova"} 1.0' in output  # degraded -> up
        assert 'rally_api_up{service="cinder"} 0.0' in output  # down -> 0
        # The slow service is still visible via its latency gauge.
        assert 'rally_api_latency_milliseconds{service="nova"} 6200.0' in output

    def test_overall_degraded_sets_one(self, results_dir):
        """overall degraded -> rally_api_overall_up 1 (reachable)."""
        (results_dir / "health.json").write_text(json.dumps(make_health(overall="degraded")))
        exporter.update_metrics()
        assert "rally_api_overall_up 1.0" in metrics_output()

    def test_overall_unknown_fails_closed_to_zero(self, results_dir):
        """A seed/unknown overall FAILS CLOSED to 0, even after a prior healthy
        signal. Otherwise a corrupt/missing health.json would leave this gauge
        stuck at 1 while the per-service rally_api_up series are cleared (absent
        series cannot fire `== 0` alerts), so a broken pipeline would read as
        all-healthy."""
        exporter.rally_api_overall_up.set(1)  # simulate a prior "up" signal
        (results_dir / "health.json").write_text(
            json.dumps(make_health(overall="unknown"))
        )
        exporter.update_metrics()
        assert "rally_api_overall_up 0.0" in metrics_output()

    def test_overall_missing_fails_closed_to_zero(self, results_dir):
        exporter.rally_api_overall_up.set(1)
        (results_dir / "health.json").write_text(
            json.dumps({"timestamp": "2024-01-01T12:00:00Z", "services": {}})
        )
        exporter.update_metrics()
        assert "rally_api_overall_up 0.0" in metrics_output()

    def test_overall_healthy_then_corrupt_flips_to_zero(self, results_dir):
        """Regression: a healthy scrape (overall up -> 1) followed by a
        corrupt/missing-overall health.json must flip the gauge to 0, not leave
        it stuck at 1. This is the exact stale-signal bug the fail-closed change
        fixes."""
        (results_dir / "health.json").write_text(json.dumps(make_health(overall="up")))
        exporter.update_metrics()
        assert "rally_api_overall_up 1.0" in metrics_output()
        # Now the file loses a valid overall (e.g. truncated/corrupt write).
        (results_dir / "health.json").write_text(
            json.dumps({"timestamp": "2024-01-01T12:05:00Z", "services": {}})
        )
        exporter.update_metrics()
        assert "rally_api_overall_up 0.0" in metrics_output()

    def test_health_updates_when_summary_invalid(self, results_dir):
        """Health metrics apply even with no/invalid summary (own update cycle)."""
        (results_dir / "health.json").write_text(json.dumps(make_health()))
        exporter.update_metrics()
        output = metrics_output()
        assert "rally_data_valid 0.0" in output
        assert 'rally_api_up{service="keystone"} 1.0' in output

    def test_health_labels_clear_when_service_drops(self, results_dir):
        """A service that disappears from health.json must not leave a stale series."""
        health_path = results_dir / "health.json"
        health_path.write_text(json.dumps(make_health()))
        exporter.update_metrics()
        assert 'rally_api_up{service="nova"}' in metrics_output()

        # New health snapshot without nova
        health_path.write_text(json.dumps(make_health(services={
            "keystone": {"status": "up", "latency_ms": 100, "checked_at": "2024-01-01T12:15:00Z"},
        })))
        exporter._health_mtime = -1.0  # force cache miss
        exporter.update_metrics()
        output = metrics_output()
        assert 'rally_api_up{service="nova"}' not in output
        assert 'rally_api_up{service="keystone"} 1.0' in output

    def test_missing_health_file_fails_closed_to_zero(self, results_dir):
        """With no health.json, load_health returns overall 'unknown', which now
        fails closed to 0 (was the stale-at-1 bug): a missing health pipeline
        must not read as healthy."""
        exporter.rally_api_overall_up.set(1)
        exporter.update_metrics()
        assert "rally_api_overall_up 0.0" in metrics_output()


# ---------------------------------------------------------------------------
# load_announcements
# ---------------------------------------------------------------------------

class TestLoadAnnouncements:
    def test_absent_file_returns_default_no_error(self, results_dir):
        """Absence is the normal pre-first-post state: no error counter bump."""
        before = exporter.rally_exporter_errors_total.labels(
            file="announcement-state.json"
        )._value.get()
        result = exporter.load_announcements()
        after = exporter.rally_exporter_errors_total.labels(
            file="announcement-state.json"
        )._value.get()
        assert result == {"announcements": []}
        assert after == before  # NOT incremented

    def test_corrupt_file_returns_default_and_increments_error(self, results_dir):
        (results_dir / "announcement-state.json").write_text("{bad json")
        before = exporter.rally_exporter_errors_total.labels(
            file="announcement-state.json"
        )._value.get()
        result = exporter.load_announcements()
        after = exporter.rally_exporter_errors_total.labels(
            file="announcement-state.json"
        )._value.get()
        assert result == {"announcements": []}
        assert after == before + 1

    def test_valid_file_returns_data(self, results_dir):
        state = {"announcements": [make_announcement()]}
        (results_dir / "announcement-state.json").write_text(json.dumps(state))
        result = exporter.load_announcements()
        assert len(result["announcements"]) == 1


# ---------------------------------------------------------------------------
# _is_announcement_active
# ---------------------------------------------------------------------------

class TestIsAnnouncementActive:
    def test_incident_no_bounds_is_active(self):
        rec = make_announcement(rec_type="incident")
        assert exporter._is_announcement_active(rec, time.time() * 1000.0) is True

    def test_unknown_type_not_active(self):
        rec = make_announcement(rec_type="bogus")
        assert exporter._is_announcement_active(rec, time.time() * 1000.0) is False

    def test_future_effective_from_not_active(self):
        rec = make_announcement(rec_type="scheduled", effective_from=iso_offset(3600),
                                expires_at=iso_offset(7200))
        assert exporter._is_announcement_active(rec, time.time() * 1000.0) is False

    def test_past_expires_at_not_active(self):
        rec = make_announcement(rec_type="maintenance", expires_at=iso_offset(-60))
        assert exporter._is_announcement_active(rec, time.time() * 1000.0) is False

    def test_active_window_is_active(self):
        rec = make_announcement(rec_type="scheduled", effective_from=iso_offset(-60),
                                expires_at=iso_offset(3600))
        assert exporter._is_announcement_active(rec, time.time() * 1000.0) is True

    def test_unparseable_bounds_ignored(self):
        rec = make_announcement(rec_type="incident", effective_from="not-a-date",
                                expires_at="garbage")
        assert exporter._is_announcement_active(rec, time.time() * 1000.0) is True


# ---------------------------------------------------------------------------
# Announcement metrics — update_metrics / _apply_announcement_metrics
# ---------------------------------------------------------------------------

class TestAnnouncementMetrics:
    def test_absent_file_emits_zeros_no_error(self, results_dir):
        before = exporter.rally_exporter_errors_total.labels(
            file="announcement-state.json"
        )._value.get()
        exporter.update_metrics()
        after = exporter.rally_exporter_errors_total.labels(
            file="announcement-state.json"
        )._value.get()
        output = metrics_output()
        assert 'rally_announcement_active{type="incident"} 0.0' in output
        assert 'rally_announcement_active{type="maintenance"} 0.0' in output
        assert 'rally_announcement_active{type="scheduled"} 0.0' in output
        assert "rally_maintenance_mode 0.0" in output
        assert after == before  # absence is not an error

    def test_active_incident_counted(self, results_dir):
        state = {"announcements": [make_announcement(rec_type="incident")]}
        (results_dir / "announcement-state.json").write_text(json.dumps(state))
        exporter.update_metrics()
        output = metrics_output()
        assert 'rally_announcement_active{type="incident"} 1.0' in output
        assert "rally_maintenance_mode 0.0" in output

    def test_future_effective_from_not_counted(self, results_dir):
        state = {"announcements": [make_announcement(
            rec_type="scheduled", effective_from=iso_offset(3600), expires_at=iso_offset(7200)
        )]}
        (results_dir / "announcement-state.json").write_text(json.dumps(state))
        exporter.update_metrics()
        assert 'rally_announcement_active{type="scheduled"} 0.0' in metrics_output()

    def test_past_expires_at_not_counted(self, results_dir):
        state = {"announcements": [make_announcement(
            rec_type="maintenance", expires_at=iso_offset(-60)
        )]}
        (results_dir / "announcement-state.json").write_text(json.dumps(state))
        exporter.update_metrics()
        output = metrics_output()
        assert 'rally_announcement_active{type="maintenance"} 0.0' in output
        assert "rally_maintenance_mode 0.0" in output

    def test_active_maintenance_sets_mode(self, results_dir):
        state = {"announcements": [make_announcement(
            rec_type="maintenance", expires_at=iso_offset(3600)
        )]}
        (results_dir / "announcement-state.json").write_text(json.dumps(state))
        exporter.update_metrics()
        output = metrics_output()
        assert 'rally_announcement_active{type="maintenance"} 1.0' in output
        assert "rally_maintenance_mode 1.0" in output

    def test_corrupt_file_emits_zeros(self, results_dir):
        (results_dir / "announcement-state.json").write_text("{corrupt")
        exporter.update_metrics()
        output = metrics_output()
        assert 'rally_announcement_active{type="incident"} 0.0' in output
        assert 'rally_announcement_active{type="maintenance"} 0.0' in output
        assert 'rally_announcement_active{type="scheduled"} 0.0' in output
        assert "rally_maintenance_mode 0.0" in output


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
