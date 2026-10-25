"""Behavior tests for the health_check.sh cron wrapper."""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from shell_testlib import write_shim

# macOS ships bash 3.2 at /bin/bash; the scripts need bash >= 4.
BASH = shutil.which("bash") or "/bin/bash"
SCRIPT = Path(__file__).resolve().parent / "health_check.sh"
BASE_PATH = "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"


def write_shims(tmp_path: Path, shims: dict[str, str]) -> Path:
    """Create tmp_path/bin with one executable bash shim per name -> body."""
    bin_dir = tmp_path / "bin"
    for name, body in shims.items():
        write_shim(bin_dir, name, body)
    return bin_dir


def test_timed_out_check_is_stamped_at_completion(tmp_path: Path) -> None:
    """The all-down fallback must carry the time it was published, not the
    time the check started. The exporter's staleness cut-off assumes
    completion stamps: with a start stamp, back-to-back 300s timeouts at a
    1-minute interval looked stale between publishes, dropped rally_api_up=0,
    and kept resetting RallyApiDown's for: 20m."""
    # Simulate a checker killed by `timeout` after running for a while.
    seen_timeout = tmp_path / "timeout_arg"
    bin_dir = write_shims(tmp_path, {
        "timeout": f'echo "$1" > "{seen_timeout}"\nsleep 2\nexit 124\n',
        # flock is util-linux (absent on macOS); the lock is not under test.
        "flock": "exit 0\n",
    })

    started = datetime.now(timezone.utc).replace(microsecond=0)
    result = subprocess.run(
        [BASH, str(SCRIPT)],
        env={
            "PATH": f"{bin_dir}:{BASE_PATH}",
            "RESULTS_DIR": str(tmp_path),
            "HEALTH_LOCKFILE": str(tmp_path / "health.lock"),
            "RALLY_SERVICES": "keystone,nova",
            "HEALTH_CHECK_TIMEOUT": "42",
        },
        capture_output=True, text=True, check=False,
    )
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
    bin_dir = write_shims(tmp_path, {"timeout": "exit 124\n", "flock": "exit 0\n"})

    result = subprocess.run(
        [BASH, str(SCRIPT)],
        env={
            "PATH": f"{bin_dir}:{BASE_PATH}",
            "RESULTS_DIR": str(tmp_path),
            "HEALTH_LOCKFILE": str(tmp_path / "health.lock"),
            "RALLY_SERVICES": "keystone",
            "UPTIME_WINDOW_DAYS": "30d",
            "HEALTH_CHECK_INTERVAL": "0",
        },
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    history = json.loads((tmp_path / "health_history.json").read_text())
    assert len(history["checks"]) == 1
    assert history["uptime"]["window_days"] == 30
    assert "UPTIME_WINDOW_DAYS" in result.stderr
