"""Behavior tests for run_tests.sh::main: the order of its steps on the normal
and the deployment-failure paths, and run_state.json (running while the run
holds the lock, idle again from the EXIT trap).

main is run for real after sourcing run_tests.sh; each step it calls is
replaced by a recorder that appends its name to calls.log. The absolute-path
calls (/scripts/notify.sh, /scripts/announce.sh) are recorded the same way,
since bash lets a function name contain slashes. abort_run and finish_run
stay real, so their own ordering is exercised too.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from shell_testlib import BASE_PATH, BASH, write_shim

SCRIPT = Path(__file__).resolve().parent / "run_tests.sh"
TIMESTAMP = "20260612T120000Z"

RECORDERS = r"""
rec() { echo "$*" >> "$CALLS"; }
log_environment() { :; }
setup_deployment() {
    rec "setup_deployment state=$(jq -r .state "$RUN_STATE_FILE")"
    return "${SETUP_RC:-0}"
}
write_task_args() { rec write_task_args "$@"; return "${WRITE_ARGS_RC:-0}"; }
run_service_tests() { rec run_service_tests "$@"; }
build_summary() { rec build_summary; echo '{}' > "$SUMMARY_FILE"; }
record_smoke_result() { rec record_smoke_result "$@"; RUN_SMOKE_STATUS="${1:-passed}"; }
publish_dashboard_files() { rec publish_dashboard_files; }
auto_purge_rgw() { rec auto_purge_rgw; }
check_cleanup() { rec check_cleanup; }
attach_cleanup_to_summary() { rec attach_cleanup_to_summary; }
prune_old_results() { rec prune_old_results; }
/scripts/notify.sh() { rec "notify $*"; }
/scripts/announce.sh() { rec "announce $1"; }
"""


def run_main(tmp_path: Path, *, setup_rc: int = 0, write_args_rc: int = 0,
             flock_rc: int = 0) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run main with the recorders; return the process and the call list."""
    bin_dir = tmp_path / "bin"
    # flock is a shim: macOS has none, and its exit code picks lock contention.
    write_shim(bin_dir, "flock", f"exit {flock_rc}\n")
    run_dir = tmp_path / TIMESTAMP
    overrides = (
        f'RUN_DIR="{run_dir}"; TIMESTAMP="{TIMESTAMP}"; '
        f'RUN_LOG="{run_dir / "run.log"}"; '
    )
    result = subprocess.run(
        [BASH, "-c", f'source "{SCRIPT}" && {overrides} {RECORDERS}\nmain'],
        env={
            "PATH": f"{bin_dir}:{BASE_PATH}",
            "RESULTS_DIR": str(tmp_path),
            "RALLY_SERVICES": "nova,cinder",
            "RALLY_RUN_LOCK_FILE": str(tmp_path / "rally-run.lock"),
            "CALLS": str(tmp_path / "calls.log"),
            "SETUP_RC": str(setup_rc),
            "WRITE_ARGS_RC": str(write_args_rc),
        },
        capture_output=True, text=True, check=False,
    )
    calls_file = tmp_path / "calls.log"
    calls = calls_file.read_text().splitlines() if calls_file.exists() else []
    return result, calls


def task_args(tmp_path: Path) -> Path:
    """The one task-args file main writes and hands to every service run."""
    return tmp_path / TIMESTAMP / "task_args.json"


def run_state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "run_state.json").read_text())


def test_normal_run_order(tmp_path: Path) -> None:
    """task args are written once into the run directory and that same file
    goes to every service run; notify follows the ledger; incidents are
    auto-cleared before anything is published; cleanup metrics are written
    after the RGW auto-purge (so they show the post-purge state); finish_run
    prunes before it publishes."""
    result, calls = run_main(tmp_path)
    assert result.returncode == 0, result.stderr
    args = task_args(tmp_path)
    assert calls == [
        "setup_deployment state=running",
        f"write_task_args {args}",
        f"run_service_tests nova {args}",
        f"run_service_tests cinder {args}",
        "build_summary",
        "record_smoke_result",
        "notify passed",
        "announce auto-clear-if-all-green",
        "auto_purge_rgw",
        "check_cleanup",
        "attach_cleanup_to_summary",
        "prune_old_results",
        "publish_dashboard_files",
    ]
    assert run_state(tmp_path)["state"] == "idle"  # EXIT trap
    summary = json.loads((tmp_path / "latest_summary.json").read_text())
    assert isinstance(summary["run_duration_seconds"], int)


@pytest.mark.parametrize(("kwargs", "steps", "error"), [
    pytest.param({"setup_rc": 1}, ["setup_deployment state=running"],
                 "deployment_setup_failed", id="deployment-setup"),
    pytest.param({"write_args_rc": 1},
                 ["setup_deployment state=running", "write_task_args {args}"],
                 "task_args_write_failed", id="task-args"),
])
def test_abort_still_records_publishes_and_notifies(
    tmp_path: Path, kwargs: dict, steps: list[str], error: str,
) -> None:
    """Both abort paths count the run as failed, publish it and notify, but
    never auto-clear incidents (announce is not called)."""
    result, calls = run_main(tmp_path, **kwargs)
    assert result.returncode == 1
    steps = [step.format(args=task_args(tmp_path)) for step in steps]
    assert calls == steps + [
        "record_smoke_result failed",
        "publish_dashboard_files",
        "notify failed",
    ]
    assert run_state(tmp_path)["state"] == "idle"  # the trap fires on exit 1 too
    summary = json.loads((tmp_path / "latest_summary.json").read_text())
    assert summary["error"] == error


def test_lock_contention_skips_without_touching_run_state(tmp_path: Path) -> None:
    """The run that loses the flock exits before installing the EXIT trap, so
    the winning run's "running" state is not reset to idle."""
    (tmp_path / "run_state.json").write_text('{"state": "running"}')
    result, calls = run_main(tmp_path, flock_rc=1)
    assert result.returncode == 0, result.stderr
    assert "SKIPPED" in result.stdout
    assert calls == []
    assert run_state(tmp_path) == {"state": "running"}
