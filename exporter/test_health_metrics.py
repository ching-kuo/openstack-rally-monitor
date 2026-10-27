"""Tests for health_metrics.py (API health gauges from health.json)."""
import json

import pytest

import health_metrics as hm
import metrics_common as mc
import rally_exporter as exporter
from exporter_testlib import error_count, iso_offset, make_health, make_summary, metrics_output

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
        before = error_count("health.json")
        exporter.load_health()  # no file present
        assert error_count("health.json") == before + 1

    # A valid file is covered through update_metrics by
    # TestHealthMetrics::test_per_service_and_overall_set.


# ---------------------------------------------------------------------------
# API health metrics — update_metrics / health_metrics.apply_health_metrics
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

    @pytest.mark.parametrize(
        ("overall", "expected"),
        [
            pytest.param("up", 1, id="up"),
            pytest.param("degraded", 1, id="degraded-is-reachable"),
            pytest.param("down", 0, id="down"),
            pytest.param("unknown", 0, id="unknown-fails-closed"),
            pytest.param("bogus", 0, id="unrecognized-fails-closed"),
            pytest.param(None, 0, id="missing-overall-fails-closed"),
            pytest.param("no-file", 0, id="missing-file-fails-closed"),
        ],
    )
    def test_overall_up_table(self, results_dir, overall, expected):
        """rally_api_overall_up FAILS CLOSED: only up/degraded read 1. Each case
        starts from the opposite value, so a gauge that kept a stale 1 (a broken
        pipeline whose per-service series are cleared, so no `== 0` alert can
        fire) or a stale 0 fails the test. A seed/unknown or missing overall
        and a missing health.json (load_health returns 'unknown') all read 0."""
        hm.rally_api_overall_up.set(1 - expected)
        if overall != "no-file":
            health = make_health(overall=overall)
            if overall is None:
                del health["overall"]
            (results_dir / "health.json").write_text(json.dumps(health))
        exporter.update_metrics()
        assert f"rally_api_overall_up {float(expected)}" in metrics_output()

    def test_overall_healthy_then_corrupt_flips_to_zero(self, results_dir):
        """Regression: a healthy scrape (overall up -> 1) followed by a
        missing-overall health.json must flip the gauge to 0, not leave it
        stuck at 1. This is the exact stale-signal bug the fail-closed change
        fixes. (Both snapshots are fresh, so staleness is not what flips it.)"""
        path = results_dir / "health.json"
        path.write_text(json.dumps(make_health(overall="up")))
        exporter.update_metrics()
        assert "rally_api_overall_up 1.0" in metrics_output()
        # Now the file loses a valid overall (e.g. truncated/corrupt write).
        broken = make_health(overall="up")
        del broken["overall"]
        path.write_text(json.dumps(broken))
        exporter._health_mtime = -1.0  # force a re-read
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


class TestHealthStaleness:
    """A health.json that stops updating must not keep reporting healthy."""

    def test_fresh_snapshot_exports_check_timestamp(self, results_dir):
        health = make_health(overall="up")
        health["timestamp"] = "2099-01-01T00:00:00Z"  # future: never stale
        (results_dir / "health.json").write_text(json.dumps(health))
        exporter.update_metrics()
        output = metrics_output()
        assert "rally_api_last_check_timestamp 4.0709088e+09" in output
        assert "rally_api_overall_up 1.0" in output

    def test_stale_snapshot_fails_closed_and_drops_series(self, results_dir, monkeypatch):
        monkeypatch.setattr(hm, "HEALTH_STALE_AFTER_SECONDS", 2700)
        health = make_health(overall="up")
        health["timestamp"] = iso_offset(-2701)
        (results_dir / "health.json").write_text(json.dumps(health))
        hm.rally_api_overall_up.set(1)
        exporter.update_metrics()
        output = metrics_output()
        assert "rally_api_overall_up 0.0" in output
        assert 'rally_api_up{service="keystone"}' not in output
        assert "rally_api_latency_milliseconds{" not in output
        # The age is still visible so an alert can say how stale it is.
        assert "rally_api_last_check_timestamp 0.0" not in output

    def test_unchanged_file_goes_stale_between_scrapes(self, results_dir, monkeypatch):
        """Regression: the identity cache skipped re-evaluation, so a health.json
        that simply stopped being rewritten kept rally_api_overall_up at 1
        forever (cron dead, checker hung)."""
        health = make_health(overall="up")
        health["timestamp"] = iso_offset(-600)
        (results_dir / "health.json").write_text(json.dumps(health))
        monkeypatch.setattr(hm, "HEALTH_STALE_AFTER_SECONDS", 2700)
        exporter.update_metrics()
        assert "rally_api_overall_up 1.0" in metrics_output()

        # Same file, same cached object -- only the clock (here: threshold) moved.
        monkeypatch.setattr(hm, "HEALTH_STALE_AFTER_SECONDS", 300)
        exporter.update_metrics()
        output = metrics_output()
        assert "rally_api_overall_up 0.0" in output
        assert 'rally_api_up{service="keystone"}' not in output

    @pytest.mark.parametrize("timestamp", ["waiting", None, "not-a-date"])
    def test_unparseable_timestamp_fails_closed(self, results_dir, timestamp):
        health = make_health(overall="up")
        health["timestamp"] = timestamp
        (results_dir / "health.json").write_text(json.dumps(health))
        hm.rally_api_overall_up.set(1)
        exporter.update_metrics()
        output = metrics_output()
        assert "rally_api_overall_up 0.0" in output
        assert "rally_api_last_check_timestamp 0.0" in output

    def test_threshold_gauges_exported_at_import(self):
        output = metrics_output()
        assert f"rally_api_stale_after_seconds {float(hm.HEALTH_STALE_AFTER_SECONDS)}" in output
        assert f"rally_schedule_interval_seconds {float(mc.SCHEDULE_INTERVAL_SECONDS)}" in output

    @pytest.mark.parametrize(
        "interval, expected",
        [
            (900, 2700),    # default 15m: three intervals (45m)
            (60, 420),      # 1m: floor = interval + 300s checker timeout + 60s
            (3600, 10800),  # hourly: three intervals
        ],
    )
    def test_stale_threshold_never_below_one_slow_check(self, interval, expected):
        """A single check killed at its 300s timeout must not read as stale."""
        assert hm.stale_after_seconds(interval) == expected


class TestConsecutiveTimeouts:
    def test_down_series_survive_back_to_back_timed_out_checks(self, results_dir, monkeypatch):
        """At a 1-minute interval each check can run the full 300s timeout and
        the next starts at the following tick. Between those completion-stamped
        publishes the all-down document is up to ~5 minutes old; it must stay
        current so rally_api_up=0 persists and RallyApiDown can fire."""
        monkeypatch.setattr(hm, "HEALTH_STALE_AFTER_SECONDS", hm.stale_after_seconds(60))
        health = make_health(overall="down", services={
            "keystone": {"status": "down", "latency_ms": 0, "checked_at": iso_offset(-361)},
        })
        # Previous timeout published at completion; the next is due 300s + 60s later.
        health["timestamp"] = iso_offset(-361)
        (results_dir / "health.json").write_text(json.dumps(health))
        exporter.update_metrics()
        assert 'rally_api_up{service="keystone"} 0.0' in metrics_output()


class TestBrokenHealthFile:
    """A health.json that breaks after a good check must still let
    RallyApiHealthStale fire. That alert ignores a 0 timestamp (fresh boot), so
    the gauge keeps the last good check time instead of dropping to 0."""

    def test_corrupt_file_keeps_last_good_check_time(self, results_dir):
        path = results_dir / "health.json"
        health = make_health(overall="up")
        health["timestamp"] = "2099-01-01T00:00:00Z"
        path.write_text(json.dumps(health))
        exporter.update_metrics()

        path.write_text("{not json")
        hm.rally_api_overall_up.set(1)
        exporter._health_mtime = -1.0  # force a re-read
        exporter.update_metrics()
        output = metrics_output()
        assert "rally_api_last_check_timestamp 4.0709088e+09" in output
        assert "rally_api_overall_up 0.0" in output
        assert 'rally_api_up{service="keystone"}' not in output

    @pytest.mark.parametrize("contents", [None, '{"timestamp":"waiting","overall":"unknown","services":{}}'])
    def test_never_checked_stays_zero(self, results_dir, contents):
        """Seed file or no file and no good check yet: 0, so a fresh container
        does not raise RallyApiHealthStale."""
        if contents is not None:
            (results_dir / "health.json").write_text(contents)
        exporter.update_metrics()
        assert "rally_api_last_check_timestamp 0.0" in metrics_output()


@pytest.mark.parametrize(
    "raw, expected",
    [(None, 300), ("120", 120), ("0120", 120), ("", 300), ("abc", 300), ("0", 300), ("3601", 300)],
)
def test_checker_timeout_comes_from_shared_env(raw, expected):
    """HEALTH_CHECK_TIMEOUT is the single setting for health_check.sh's kill
    deadline and the staleness floor; invalid values fall back like the
    entrypoint default (which refuses to start on them anyway)."""
    assert hm.parse_check_timeout(raw) == expected
