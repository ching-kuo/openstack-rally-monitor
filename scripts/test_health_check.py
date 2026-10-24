"""Behavior tests for the health_check.sh cron wrapper."""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# macOS ships bash 3.2 at /bin/bash; the scripts need bash >= 4.
BASH = shutil.which("bash") or "/bin/bash"
SCRIPT = Path(__file__).resolve().parent / "health_check.sh"
BASE_PATH = "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"


def test_timed_out_check_is_stamped_at_completion(tmp_path: Path) -> None:
    """The all-down fallback must carry the time it was published, not the
    time the check started. The exporter's staleness cut-off assumes
    completion stamps: with a start stamp, back-to-back 300s timeouts at a
    1-minute interval looked stale between publishes, dropped rally_api_up=0,
    and kept resetting RallyApiDown's for: 20m."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Simulate a checker killed by `timeout` after running for a while.
    seen_timeout = tmp_path / "timeout_arg"
    shims = {
        "timeout": f'echo "$1" > "{seen_timeout}"\nsleep 2\nexit 124\n',
        # flock is util-linux (absent on macOS); the lock is not under test.
        "flock": "exit 0\n",
    }
    for name, body in shims.items():
        shim = bin_dir / name
        shim.write_text("#!/usr/bin/env bash\n" + body)
        shim.chmod(0o755)

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
