"""Behavior tests for the health_check.sh cron wrapper."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from shell_testlib import BASE_PATH, BASH, write_shim

SCRIPT = Path(__file__).resolve().parent / "health_check.sh"


NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
GOOD_DOC = {
    "timestamp": NOW,
    "overall": "up",
    "services": {"keystone": {"status": "up", "latency_ms": 5, "checked_at": NOW}},
}


def run_check(tmp_path: Path, checker: str, *, flock_rc: int = 0,
              **env: str) -> subprocess.CompletedProcess[str]:
    """Run health_check.sh with `checker` as the body of the `timeout` shim,
    which stands in for `timeout N python3 api_health_check.py`. flock is a
    shim too (util-linux, absent on macOS); its exit code picks contention."""
    bin_dir = tmp_path / "bin"
    write_shim(bin_dir, "timeout", checker)
    write_shim(bin_dir, "flock", f"exit {flock_rc}\n")
    return subprocess.run(
        [BASH, str(SCRIPT)],
        env={
            "PATH": f"{bin_dir}:{BASE_PATH}",
            "RESULTS_DIR": str(tmp_path),
            "HEALTH_LOCKFILE": str(tmp_path / "health.lock"),
            "RALLY_SERVICES": "keystone,nova",
            **env,
        },
        capture_output=True, text=True, check=False,
    )


def emit(text: str, rc: int = 0) -> str:
    """A checker body that prints `text` and exits `rc`."""
    return f"cat <<'DOC'\n{text}\nDOC\nexit {rc}\n"


def test_timed_out_check_is_stamped_at_completion(tmp_path: Path) -> None:
    """The all-down fallback must carry the time it was published, not the
    time the check started. The exporter's staleness cut-off assumes
    completion stamps: with a start stamp, back-to-back 300s timeouts at a
    1-minute interval looked stale between publishes, dropped rally_api_up=0,
    and kept resetting RallyApiDown's for: 20m."""
    # Simulate a checker killed by `timeout` after running for a while.
    seen_timeout = tmp_path / "timeout_arg"
    started = datetime.now(timezone.utc).replace(microsecond=0)
    result = run_check(tmp_path, f'echo "$1" > "{seen_timeout}"\nsleep 2\nexit 124\n',
                       HEALTH_CHECK_TIMEOUT="42")
    assert result.returncode == 0, result.stderr

    health = json.loads((tmp_path / "health.json").read_text())
    assert health["overall"] == "down"
    assert set(health["services"]) == {"keystone", "nova"}
    stamped = datetime.strptime(health["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    assert (stamped - started).total_seconds() >= 2
    assert all(s["checked_at"] == health["timestamp"] for s in health["services"].values())
    # The kill deadline comes from HEALTH_CHECK_TIMEOUT, shared with the exporter.
    assert seen_timeout.read_text().strip() == "42"


def test_malformed_numeric_env_does_not_freeze_history(tmp_path: Path) -> None:
    """A unit-suffixed UPTIME_WINDOW_DAYS ("30d") or a zero interval used to
    abort the bash arithmetic AFTER health.json was written, so the history
    silently stopped growing. Invalid values now fall back to the defaults."""
    result = run_check(tmp_path, "exit 124\n", RALLY_SERVICES="keystone",
                       UPTIME_WINDOW_DAYS="30d", HEALTH_CHECK_INTERVAL="0")
    assert result.returncode == 0, result.stderr
    raw = (tmp_path / "health_history.json").read_text()
    history = json.loads(raw)
    assert len(history["checks"]) == 1
    assert history["uptime"]["window_days"] == 30
    # Compact (jq -c): the dashboard re-fetches this multi-MB file.
    assert raw.count("\n") == 1
    assert "UPTIME_WINDOW_DAYS" in result.stderr


def test_held_lock_exits_without_touching_health(tmp_path: Path) -> None:
    (tmp_path / "health.json").write_text('{"previous": true}')
    result = run_check(tmp_path, emit(json.dumps(GOOD_DOC)), flock_rc=1)
    assert result.returncode == 0
    assert "already in progress" in result.stderr
    assert (tmp_path / "health.json").read_text() == '{"previous": true}'
    assert not (tmp_path / "health_history.json").exists()


def test_valid_checker_output_is_published_and_appended(tmp_path: Path) -> None:
    result = run_check(tmp_path, emit(json.dumps(GOOD_DOC)))
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "health.json").read_text()) == GOOD_DOC
    assert not (tmp_path / "health.json.tmp").exists()
    history = json.loads((tmp_path / "health_history.json").read_text())
    assert [c["overall"] for c in history["checks"]] == ["up"]


@pytest.mark.parametrize("checker", [
    # Stdout pollution: a stray banner before the document fails `jq empty`.
    pytest.param(emit("Loading plugins...\n" + json.dumps(GOOD_DOC)), id="invalid-json"),
    # An internal error (e.g. openstacksdk missing) even with valid output.
    pytest.param(emit(json.dumps(GOOD_DOC), rc=1), id="checker-failed"),
])
def test_unusable_checker_output_publishes_all_down(tmp_path: Path, checker: str) -> None:
    result = run_check(tmp_path, checker)
    assert result.returncode == 0, result.stderr
    assert "writing all-down health document" in result.stderr
    health = json.loads((tmp_path / "health.json").read_text())
    assert health["overall"] == "down"
    assert set(health["services"]) == {"keystone", "nova"}
    assert not (tmp_path / "health.json.tmp").exists()


@pytest.mark.parametrize(("days", "interval", "cap"), [
    pytest.param("1", "15", 672, id="floored-at-672"),   # 1*24*60/15 = 96
    pytest.param("8", "15", 768, id="window-over-interval"),
])
def test_history_cap(tmp_path: Path, days: str, interval: str, cap: int) -> None:
    """The cap is UPTIME_WINDOW_DAYS*24*60/HEALTH_CHECK_INTERVAL, floored at
    672 (7 days of 15-minute checks) so the 7-day timeline never shrinks."""
    old = [{"timestamp": NOW, "overall": "down"}] * 900
    (tmp_path / "health_history.json").write_text(json.dumps({"checks": old}))
    result = run_check(tmp_path, emit(json.dumps(GOOD_DOC)),
                       UPTIME_WINDOW_DAYS=days, HEALTH_CHECK_INTERVAL=interval)
    assert result.returncode == 0, result.stderr
    checks = json.loads((tmp_path / "health_history.json").read_text())["checks"]
    assert len(checks) == cap
    assert checks[-1]["overall"] == "up"  # the newest check is kept
