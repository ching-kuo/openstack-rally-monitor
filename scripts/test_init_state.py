"""Tests for scripts/init_state.sh, the boot-time state seeding that
entrypoint.sh runs as the rally user (never as root: root writing into the
rally-writable /results and /rally/logs would follow planted symlinks)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

# macOS ships bash 3.2 at /bin/bash; the scripts need bash >= 4.
BASH = shutil.which("bash") or "/bin/bash"
SCRIPT = Path(__file__).resolve().parent / "init_state.sh"


def run_init(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    logs = tmp_path / "logs"
    env = {
        "PATH": os.environ["PATH"],
        "RESULTS_DIR": str(tmp_path / "results"),
        "RALLY_RUN_LOCK_FILE": str(logs / "rally-run.lock"),
        "HEALTH_LOCKFILE": str(logs / "rally-health.lock"),
        "CLEANUP_LOCK_FILE": str(logs / "rally-cleanup.lock"),
        **extra,
    }
    return subprocess.run(
        [BASH, str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )


def test_first_boot_seeds_state_locks_and_logs(tmp_path: Path) -> None:
    result = run_init(tmp_path, RALLY_SERVICES="nova, Keystone,../etc")
    assert result.returncode == 0, result.stderr

    results = tmp_path / "results"
    summary = json.loads((results / "latest_summary.json").read_text())
    assert summary["timestamp"] == "waiting_for_first_run"
    assert list(summary["services"]) == ["nova", "keystone"]
    assert json.loads((results / "cleanup_metrics.json").read_text())["rgw_scan_status"] == "skipped"
    combined = json.loads((results / "results.json").read_text())
    assert combined["summary"] == summary
    assert json.loads((results / "history.json").read_text()) == {"runs": []}
    assert json.loads((results / "health.json").read_text())["overall"] == "unknown"
    assert json.loads((results / "health_history.json").read_text()) == {"checks": []}
    assert json.loads((results / "run_state.json").read_text())["state"] == "idle"
    assert (results / "branding").is_dir()

    logs = tmp_path / "logs"
    for name in ("rally-run.lock", "rally-health.lock", "rally-cleanup.lock"):
        assert (logs / name).is_file(), name
    assert (results / ".announce.lock").is_file()


def test_reboot_keeps_state_but_resets_run_state(tmp_path: Path) -> None:
    assert run_init(tmp_path).returncode == 0
    results = tmp_path / "results"
    (results / "latest_summary.json").write_text('{"timestamp": "20260101T000000Z", "services": {}}')
    (results / "run_state.json").write_text('{"state": "running"}')

    result = run_init(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "20260101T000000Z" in (results / "latest_summary.json").read_text()
    assert json.loads((results / "run_state.json").read_text())["state"] == "idle"


def test_unwritable_lock_left_by_a_root_run_is_replaced(tmp_path: Path) -> None:
    """A lock created by an earlier root `docker exec` (0644 root) made every
    later rally cron run fail to open it. The rally user owns the directory, so
    boot replaces such a lock with a fresh, writable one."""
    logs = tmp_path / "logs"
    logs.mkdir()
    stale = logs / "rally-health.lock"
    stale.write_text("")
    stale.chmod(0o444)

    result = run_init(tmp_path)
    assert result.returncode == 0, result.stderr
    assert os.access(stale, os.W_OK)


def test_unwritable_state_file_is_replaced_with_its_content(tmp_path: Path) -> None:
    """State files a root run left behind (root-owned 0644) cannot be
    rewritten by rally's in-place appends; rally owns /results, so boot swaps
    in a rally-owned copy (copy + mv) without losing the content."""
    results = tmp_path / "results"
    results.mkdir()
    ledger = results / "rally_project_ids.log"
    ledger.write_text("2026-01-01T00:00:00Z abc nova\n")
    ledger.chmod(0o444)

    result = run_init(tmp_path)
    assert result.returncode == 0, result.stderr
    assert os.access(ledger, os.W_OK)
    assert ledger.read_text() == "2026-01-01T00:00:00Z abc nova\n"
    assert f"Replaced unwritable state file {ledger}" in result.stdout


def test_unwritable_run_dir_is_reported_with_the_fix(tmp_path: Path) -> None:
    """A run directory rally cannot modify cannot be pruned or repaired from
    inside the container; it must be named loudly, with the remedy."""
    results = tmp_path / "results"
    run_dir = results / "20260101T000000Z"
    run_dir.mkdir(parents=True)
    (run_dir / "nova.json").write_text("{}")
    run_dir.chmod(0o555)
    try:
        result = run_init(tmp_path)
    finally:
        run_dir.chmod(0o755)
    assert result.returncode == 0, result.stderr
    assert f"WARNING: {run_dir} is not writable" in result.stdout
    assert "chown -R" in result.stdout
