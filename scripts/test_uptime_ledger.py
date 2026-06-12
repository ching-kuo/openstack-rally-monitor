"""Tests for the uptime computations: the smoke-test ledger in run_tests.sh
and the health-history filter (health_history_filter.jq) used by
health_check.sh."""
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

# macOS ships bash 3.2 at /bin/bash; the scripts need bash >= 4 (mapfile).
BASH = shutil.which("bash") or "/bin/bash"

SCRIPT = Path(__file__).resolve().parent / "run_tests.sh"
HEALTH_FILTER = Path(__file__).resolve().parent / "health_history_filter.jq"


def make_summary(statuses=None, error=None):
    """Build a latest_summary.json-shaped dict."""
    summary = {"timestamp": "20260603T000000Z", "services": {}}
    for name, status in (statuses or {}).items():
        summary["services"][name] = {"status": status}
    if error is not None:
        summary["error"] = error
    return summary


def record(tmp_path, summary, timestamp="20260603T000000Z", window_days=30):
    """Source run_tests.sh and invoke record_smoke_result against tmp_path."""
    (tmp_path / "latest_summary.json").write_text(json.dumps(summary))
    result = subprocess.run(
        [
            BASH,
            "-c",
            f'source "{SCRIPT}" && TIMESTAMP="{timestamp}" record_smoke_result',
        ],
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
            "RESULTS_DIR": str(tmp_path),
            "UPTIME_WINDOW_DAYS": str(window_days),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result


def read_ledger(tmp_path):
    return json.loads((tmp_path / "smoke_history.json").read_text())


def recent_ts(days_ago):
    """Compact UTC timestamp `days_ago` days in the past."""
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def test_all_passed_run_records_passed_with_full_uptime(tmp_path):
    summary = make_summary({"keystone": "passed", "nova": "passed"})
    result = record(tmp_path, summary, timestamp=recent_ts(0))

    assert result.returncode == 0, result.stderr
    ledger = read_ledger(tmp_path)
    assert [r["status"] for r in ledger["runs"]] == ["passed"]
    assert ledger["uptime"] == {
        "window_days": 30,
        "runs_total": 1,
        "runs_passed": 1,
        "percent": 100,
    }


def test_failed_service_records_failed(tmp_path):
    summary = make_summary({"keystone": "passed", "nova": "failed"})
    record(tmp_path, summary, timestamp=recent_ts(0))

    ledger = read_ledger(tmp_path)
    assert ledger["runs"][0]["status"] == "failed"
    assert ledger["uptime"]["percent"] == 0


def test_deployment_failure_shape_records_failed(tmp_path):
    summary = make_summary({}, error="deployment_setup_failed")
    record(tmp_path, summary, timestamp=recent_ts(0))

    ledger = read_ledger(tmp_path)
    assert ledger["runs"][0]["status"] == "failed"


def test_skipped_service_counts_as_failed(tmp_path):
    summary = make_summary({"keystone": "passed", "swift": "skipped"})
    record(tmp_path, summary, timestamp=recent_ts(0))

    assert read_ledger(tmp_path)["runs"][0]["status"] == "failed"


def test_uptime_percent_rounds_to_two_decimals(tmp_path):
    # 2 passed out of 3 = 66.666...% -> 66.67
    ledger = {
        "runs": [
            {"timestamp": recent_ts(2), "status": "passed"},
            {"timestamp": recent_ts(1), "status": "failed"},
        ]
    }
    (tmp_path / "smoke_history.json").write_text(json.dumps(ledger))
    record(tmp_path, make_summary({"keystone": "passed"}), timestamp=recent_ts(0))

    uptime = read_ledger(tmp_path)["uptime"]
    assert uptime["runs_total"] == 3
    assert uptime["runs_passed"] == 2
    assert uptime["percent"] == 66.67


def test_entries_older_than_window_are_pruned(tmp_path):
    ledger = {
        "runs": [
            {"timestamp": recent_ts(40), "status": "failed"},
            {"timestamp": recent_ts(5), "status": "passed"},
        ]
    }
    (tmp_path / "smoke_history.json").write_text(json.dumps(ledger))
    record(tmp_path, make_summary({"keystone": "passed"}), timestamp=recent_ts(0))

    ledger = read_ledger(tmp_path)
    timestamps = [r["timestamp"] for r in ledger["runs"]]
    assert len(timestamps) == 2  # 40-day-old entry dropped
    assert all(ts >= recent_ts(30) for ts in timestamps)
    assert ledger["uptime"] == {
        "window_days": 30,
        "runs_total": 2,
        "runs_passed": 2,
        "percent": 100,
    }


def test_window_days_is_configurable(tmp_path):
    ledger = {"runs": [{"timestamp": recent_ts(10), "status": "failed"}]}
    (tmp_path / "smoke_history.json").write_text(json.dumps(ledger))
    record(
        tmp_path,
        make_summary({"keystone": "passed"}),
        timestamp=recent_ts(0),
        window_days=7,
    )

    ledger = read_ledger(tmp_path)
    assert len(ledger["runs"]) == 1  # 10-day-old entry outside the 7-day window
    assert ledger["uptime"]["window_days"] == 7
    assert ledger["uptime"]["percent"] == 100


def test_existing_results_json_uptime_is_refreshed(tmp_path):
    # record_smoke_result() syncs .uptime directly into an existing
    # results.json (belt-and-suspenders alongside the publish that the
    # deployment-failure path now also runs; standalone callers rely on this).
    (tmp_path / "results.json").write_text(
        json.dumps({"summary": {}, "cleanup": {}, "uptime": None})
    )
    record(tmp_path, make_summary({}, error="deployment_setup_failed"),
           timestamp=recent_ts(0))

    results = json.loads((tmp_path / "results.json").read_text())
    assert results["uptime"]["percent"] == 0
    assert results["uptime"]["runs_total"] == 1
    assert results["summary"] == {}  # other keys untouched


def test_missing_results_json_is_not_created(tmp_path):
    record(tmp_path, make_summary({"keystone": "passed"}), timestamp=recent_ts(0))

    assert not (tmp_path / "results.json").exists()


def publish(tmp_path, summary, timestamp="20260603T000000Z"):
    """Source run_tests.sh and run publish_dashboard_files against tmp_path.

    Mirrors the deployment-failure path: latest_summary.json holds the failure
    shape, smoke ledger and cleanup metrics are auto-seeded by the function.
    """
    (tmp_path / "latest_summary.json").write_text(json.dumps(summary))
    return subprocess.run(
        [
            BASH,
            "-c",
            f'source "{SCRIPT}" && TIMESTAMP="{timestamp}" publish_dashboard_files',
        ],
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
            "RESULTS_DIR": str(tmp_path),
            "UPTIME_WINDOW_DAYS": "30",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_deployment_failure_path_publishes_error_shape(tmp_path):
    # The deployment-failure path calls publish_dashboard_files after
    # record_smoke_result, so results.json must carry the failure summary
    # (empty services + .error) instead of a stale green run. The dashboard's
    # getRunStatus treats this shape as failed (covered by the node assertion
    # in test_dashboard_getrunstatus.py).
    summary = make_summary({}, error="deployment_setup_failed")
    result = publish(tmp_path, summary, timestamp=recent_ts(0))

    assert result.returncode == 0, result.stderr
    results = json.loads((tmp_path / "results.json").read_text())
    assert results["summary"]["error"] == "deployment_setup_failed"
    assert results["summary"]["services"] == {}


# ---------------------------------------------------------------------------
# Health history filter (the exact jq program health_check.sh runs via -f)
# ---------------------------------------------------------------------------

def iso_ts(days_ago):
    """ISO UTC timestamp `days_ago` days in the past (health-check format)."""
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def run_health_filter(tmp_path, history, current, max_entries=2880, days=30):
    """Apply health_history_filter.jq the same way health_check.sh does."""
    history_file = tmp_path / "health_history.json"
    health_file = tmp_path / "health.json"
    history_file.write_text(json.dumps(history))
    health_file.write_text(json.dumps(current))
    result = subprocess.run(
        [
            "jq",
            "--slurpfile", "cur", str(health_file),
            "--argjson", "max", str(max_entries),
            "--argjson", "days", str(days),
            "-f", str(HEALTH_FILTER),
            str(history_file),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_health_filter_appends_and_computes_uptime(tmp_path):
    history = {
        "checks": [
            {"timestamp": iso_ts(2), "overall": "down"},
            {"timestamp": iso_ts(1), "overall": "up"},
        ]
    }
    current = {"timestamp": iso_ts(0), "overall": "up", "services": {}}
    out = run_health_filter(tmp_path, history, current)

    assert len(out["checks"]) == 3
    assert out["uptime"] == {
        "window_days": 30,
        "checks_total": 3,
        "checks_up": 2,
        "percent": 66.67,
    }


def test_health_filter_counts_degraded_as_up(tmp_path):
    # Uptime measures reachability: a "degraded" check (reachable but slow)
    # counts as up. Here 2 of 3 are reachable (up + degraded), 1 is down.
    history = {
        "checks": [
            {"timestamp": iso_ts(2), "overall": "down"},
            {"timestamp": iso_ts(1), "overall": "degraded"},
        ]
    }
    current = {"timestamp": iso_ts(0), "overall": "up", "services": {}}
    out = run_health_filter(tmp_path, history, current)

    assert out["uptime"]["checks_total"] == 3
    assert out["uptime"]["checks_up"] == 2  # up + degraded, not the down
    assert out["uptime"]["percent"] == 66.67


def test_health_filter_all_degraded_is_full_uptime(tmp_path):
    # Every check degraded -> 100% uptime (the API was always reachable).
    history = {"checks": [{"timestamp": iso_ts(1), "overall": "degraded"}]}
    current = {"timestamp": iso_ts(0), "overall": "degraded", "services": {}}
    out = run_health_filter(tmp_path, history, current)

    assert out["uptime"]["checks_total"] == 2
    assert out["uptime"]["checks_up"] == 2
    assert out["uptime"]["percent"] == 100


def test_health_filter_excludes_checks_outside_window(tmp_path):
    history = {"checks": [{"timestamp": iso_ts(45), "overall": "down"}]}
    current = {"timestamp": iso_ts(0), "overall": "up", "services": {}}
    out = run_health_filter(tmp_path, history, current)

    # The old entry stays in storage (count cap governs that) but is
    # excluded from the uptime window.
    assert len(out["checks"]) == 2
    assert out["uptime"]["checks_total"] == 1
    assert out["uptime"]["percent"] == 100


def test_health_filter_caps_stored_entries(tmp_path):
    history = {
        "checks": [
            {"timestamp": iso_ts(0), "overall": "up"} for _ in range(5)
        ]
    }
    current = {"timestamp": iso_ts(0), "overall": "up", "services": {}}
    out = run_health_filter(tmp_path, history, current, max_entries=3)

    assert len(out["checks"]) == 3


def test_health_filter_null_percent_when_window_empty(tmp_path):
    history = {"checks": []}
    current = {"timestamp": iso_ts(45), "overall": "up", "services": {}}
    out = run_health_filter(tmp_path, history, current)

    assert out["uptime"]["checks_total"] == 0
    assert out["uptime"]["percent"] is None


def test_unreadable_summary_records_failed_run(tmp_path):
    (tmp_path / "latest_summary.json").write_text("not json")
    result = subprocess.run(
        [
            BASH,
            "-c",
            f'source "{SCRIPT}" && TIMESTAMP="{recent_ts(0)}" record_smoke_result',
        ],
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
            "RESULTS_DIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    # read_ledger parsing the file also proves it is still valid JSON
    ledger = read_ledger(tmp_path)
    assert ledger["runs"][0]["status"] == "failed"
