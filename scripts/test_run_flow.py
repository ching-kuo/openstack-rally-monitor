"""Behavior tests for run_tests.sh steps around build_summary: starting a
service task (run_service_tests), the early-abort path (abort_run), and the
history.json publish (publish_dashboard_files).

Like test_run_summary.py, these source run_tests.sh in a bash -c harness (its
main guard makes sourcing side-effect-free). External commands (`rally`,
`openstack`) are PATH shims written into tmp_path.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from shell_testlib import write_shim

# macOS ships bash 3.2 at /bin/bash; the scripts need bash >= 4 (mapfile).
BASH = shutil.which("bash") or "/bin/bash"
SCRIPT = Path(__file__).resolve().parent / "run_tests.sh"
TIMESTAMP = "20260612T120000Z"
BASE_PATH = "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"
UUID = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"


def run_harness(tmp_path: Path, command: str, extra_env: dict | None = None,
                bin_dir: Path | None = None) -> subprocess.CompletedProcess:
    run_dir = tmp_path / TIMESTAMP
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.log").touch()
    # RUN_DIR/SUMMARY_FILE/RUN_LOG derive from a fresh TIMESTAMP at source
    # time, so pin them after sourcing (same as test_run_summary.py).
    overrides = (
        f'RUN_DIR="{run_dir}"; TIMESTAMP="{TIMESTAMP}"; '
        f'SUMMARY_FILE="{tmp_path / "latest_summary.json"}"; '
        f'RUN_LOG="{run_dir / "run.log"}"; '
    )
    path = f"{bin_dir}:{BASE_PATH}" if bin_dir else BASE_PATH
    env = {"PATH": path, "RESULTS_DIR": str(tmp_path), **(extra_env or {})}
    return subprocess.run(
        [BASH, "-c", f'source "{SCRIPT}" && {overrides} {command}'],
        env=env, capture_output=True, text=True, errors="replace", check=False,
    )


# ---------------------------------------------------------------------------
# run_service_tests
# ---------------------------------------------------------------------------

def service_env(tmp_path: Path, start_output: str) -> tuple[Path, dict]:
    """Shim rally/openstack so `rally task start` prints start_output."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "rally_calls.log"
    write_shim(bin_dir, "rally", f"""
echo "$*" >> "{calls}"
case "$1 $2" in
  "task start") printf '%s\\n' {json.dumps(start_output)}; exit 1 ;;
  "task list") exit 0 ;;
  "task results") echo '[]' ;;
  "task status") echo "Task $3: finished" ;;
esac
""")
    write_shim(bin_dir, "openstack", "echo '[]'\n")
    config = tmp_path / "config"
    (config / "scenarios").mkdir(parents=True)
    (config / "scenarios" / "nova.yaml").write_text("---\n")
    return bin_dir, {"RALLY_CONFIG_DIR": str(config)}


# The provenance tracker polls every 5s while the task runs; shorten that.
SERVICE_CMD = (
    'sleep() { command sleep 0.1; }; '
    'run_service_tests nova "$RUN_DIR/task_args.json"'
)


def test_no_task_uuid_writes_start_failed_marker(tmp_path: Path) -> None:
    """Rally output mentioning "Task <word>" is not a UUID. The loose
    `Task [a-f0-9-]+` match took "Task aborted" as task "ab"; the strict
    pattern finds nothing, the tag lookup finds nothing, and the service is
    marked as never started."""
    bin_dir, env = service_env(tmp_path, "Task aborted: config is invalid")
    result = run_harness(tmp_path, SERVICE_CMD, env, bin_dir)
    assert result.returncode == 0, result.stderr
    marker = tmp_path / TIMESTAMP / "nova.start_failed"
    assert marker.read_text().strip() == "Task aborted: config is invalid"
    calls = (tmp_path / "rally_calls.log").read_text()
    assert "task results" not in calls
    assert not (tmp_path / TIMESTAMP / "nova.json").exists()
    # The dashboard's "Full Rally report" link must not 404: an empty-state
    # report carrying the reason is rendered instead.
    report = (tmp_path / TIMESTAMP / "nova.html").read_text()
    assert "Task aborted: config is invalid" in report
    assert '"error_code":"task_failed_to_start"' in report


def test_full_task_uuid_is_used(tmp_path: Path) -> None:
    bin_dir, env = service_env(tmp_path, f"Task {UUID}: started")
    result = run_harness(tmp_path, SERVICE_CMD, env, bin_dir)
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "rally_calls.log").read_text()
    assert f"task results {UUID}" in calls
    assert not (tmp_path / TIMESTAMP / "nova.start_failed").exists()


# ---------------------------------------------------------------------------
# abort_run (deployment setup / task-args failure)
# ---------------------------------------------------------------------------

def test_abort_run_publishes_failure_and_exits_nonzero(tmp_path: Path) -> None:
    result = run_harness(
        tmp_path,
        'abort_run task_args_write_failed "could not write task_args.json"',
        # Wide window: the fixed TIMESTAMP must not be pruned from the ledger.
        {"UPTIME_WINDOW_DAYS": "36500"},
    )
    assert result.returncode == 1, result.stderr
    summary = json.loads((tmp_path / "latest_summary.json").read_text())
    assert summary["error"] == "task_args_write_failed"
    assert summary["error_detail"] == "could not write task_args.json"
    assert summary["services"] == {}
    ledger = json.loads((tmp_path / "smoke_history.json").read_text())
    assert [r["status"] for r in ledger["runs"]] == ["failed"]
    results = json.loads((tmp_path / "results.json").read_text())
    assert results["summary"]["error"] == "task_args_write_failed"


# ---------------------------------------------------------------------------
# publish_dashboard_files: history.json
# ---------------------------------------------------------------------------

def test_history_stays_one_document_when_xargs_splits(tmp_path: Path) -> None:
    """With enough run directories xargs splits the file list across several
    commands. `xargs jq -s` then printed several {runs: ...} documents into
    history.json (invalid JSON for the dashboard). A shim forcing two files
    per command reproduces the split deterministically."""
    real_xargs = shutil.which("xargs")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_shim(bin_dir, "xargs", f'exec "{real_xargs}" -n 2 "$@"\n')

    stamps = [f"202606{day:02d}T000000Z" for day in range(1, 6)]
    for stamp in stamps:
        (tmp_path / stamp).mkdir()
        (tmp_path / stamp / "summary.json").write_text(
            json.dumps({"timestamp": stamp, "services": {}}, indent=2)
        )
    (tmp_path / "latest_summary.json").write_text(json.dumps({"timestamp": stamps[-1]}))

    result = run_harness(tmp_path, "publish_dashboard_files",
                         {"UPTIME_WINDOW_DAYS": "30"}, bin_dir)
    assert result.returncode == 0, result.stderr
    history = json.loads((tmp_path / "history.json").read_text())
    # The harness's own (summary-less) run dir is ignored; order is by name.
    assert [r["timestamp"] for r in history["runs"]] == stamps


def test_history_empty_when_no_runs(tmp_path: Path) -> None:
    (tmp_path / "latest_summary.json").write_text(json.dumps({"timestamp": TIMESTAMP}))
    result = run_harness(tmp_path, "publish_dashboard_files", {"UPTIME_WINDOW_DAYS": "30"})
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "history.json").read_text()) == {"runs": []}


# ---------------------------------------------------------------------------
# log_tail: the reason text stored in summaries and reports
# ---------------------------------------------------------------------------

def test_log_tail_is_utf8_safe_and_bounded(tmp_path: Path) -> None:
    """A byte cut (`head -c 500`) could split a multi-byte character and
    hand render_report.py an argument it could not encode, leaving no report.
    log_tail cuts by characters and replaces invalid bytes."""
    log = tmp_path / "task.log"
    log.write_bytes(b"line one\n" + "é".encode() * 400 + b"\nbad \xff byte\n")
    result = run_harness(tmp_path, f'log_tail "{log}"')
    assert result.returncode == 0, result.stderr
    text = result.stdout.rstrip("\n")
    assert len(text) <= 500
    assert "\n" not in text
    assert text.endswith("bad � byte")


def test_log_tail_of_missing_file_is_empty(tmp_path: Path) -> None:
    result = run_harness(tmp_path, f'log_tail "{tmp_path / "nope.log"}"')
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_start_failure_with_invalid_utf8_still_renders_report(tmp_path: Path) -> None:
    bin_dir, env = service_env(tmp_path, "placeholder")
    # Replace the start output with raw non-UTF-8 bytes.
    (bin_dir / "rally").write_text((bin_dir / "rally").read_text().replace(
        "printf '%s\\n' \"placeholder\"", "printf 'Task broke \\xff\\xfe\\n'"))
    result = run_harness(tmp_path, SERVICE_CMD, env, bin_dir)
    assert result.returncode == 0, result.stderr
    report = (tmp_path / TIMESTAMP / "nova.html").read_text()
    assert "Task broke" in report


# ---------------------------------------------------------------------------
# Failure handling at the end of a run
# ---------------------------------------------------------------------------

def test_finish_run_publishes_even_when_pruning_fails(tmp_path: Path) -> None:
    (tmp_path / "latest_summary.json").write_text(json.dumps({"timestamp": TIMESTAMP, "services": {}}))
    result = run_harness(
        tmp_path,
        'prune_old_results() { return 1; }; RUN_START_EPOCH=$(date +%s); finish_run',
        {"UPTIME_WINDOW_DAYS": "30"},
    )
    assert result.returncode == 0, result.stderr
    assert "pruning failed" in result.stdout
    results = json.loads((tmp_path / "results.json").read_text())
    assert "run_duration_seconds" in results["summary"]


def test_abort_run_updates_latest_summary_when_run_dir_unwritable(tmp_path: Path) -> None:
    run_dir = tmp_path / TIMESTAMP
    run_dir.mkdir()
    (run_dir / "run.log").touch()
    run_dir.chmod(0o500)
    try:
        result = run_harness(
            tmp_path,
            'abort_run task_args_write_failed "could not write task_args.json"',
            {"UPTIME_WINDOW_DAYS": "36500"},
        )
    finally:
        run_dir.chmod(0o700)
    assert result.returncode == 1, result.stderr
    summary = json.loads((tmp_path / "latest_summary.json").read_text())
    assert summary["error"] == "task_args_write_failed"
    assert f"could not write {run_dir / 'summary.json'}" in result.stdout
    results = json.loads((tmp_path / "results.json").read_text())
    assert results["summary"]["error"] == "task_args_write_failed"


def test_abort_run_never_copies_previous_summary_into_run_dir(tmp_path: Path) -> None:
    """If latest_summary.json cannot be replaced, the per-run summary must
    still be the abort payload -- copying latest_summary.json would duplicate
    the previous run in history.json and hide the setup failure."""
    (tmp_path / "latest_summary.json").write_text(
        json.dumps({"timestamp": "20260611T000000Z", "services": {"nova": {"status": "passed"}}})
    )
    (tmp_path / "latest_summary.json.tmp").mkdir()  # makes the replacement fail
    result = run_harness(
        tmp_path,
        'abort_run deployment_setup_failed "401 Unauthorized"',
        {"UPTIME_WINDOW_DAYS": "36500"},
    )
    assert result.returncode == 1, result.stderr
    assert "could not write" in result.stdout
    run_summary = json.loads((tmp_path / TIMESTAMP / "summary.json").read_text())
    assert run_summary["timestamp"] == TIMESTAMP
    assert run_summary["error"] == "deployment_setup_failed"
    assert run_summary["error_detail"] == "401 Unauthorized"


def test_abort_run_records_failed_even_if_latest_summary_is_stale(tmp_path: Path) -> None:
    """The abort path is a failed run by definition. If latest_summary.json
    could not be replaced it still holds a green previous run, which must not
    be classified into the uptime ledger (or notify.sh) as "passed"."""
    (tmp_path / "latest_summary.json").write_text(
        json.dumps({"timestamp": "20260611T000000Z", "services": {"nova": {"status": "passed"}}})
    )
    (tmp_path / "latest_summary.json.tmp").mkdir()  # makes the replacement fail
    result = run_harness(
        tmp_path,
        'abort_run deployment_setup_failed "401"; ',
        {"UPTIME_WINDOW_DAYS": "36500"},
    )
    assert result.returncode == 1, result.stderr
    ledger = json.loads((tmp_path / "smoke_history.json").read_text())
    assert [r["status"] for r in ledger["runs"]] == ["failed"]
    assert "status=failed" in result.stdout


# ---------------------------------------------------------------------------
# prune_rally_project_ledger: its own PROVENANCE_RETENTION_DAYS window
# ---------------------------------------------------------------------------

def test_provenance_ledger_pruned_on_its_own_window(tmp_path: Path) -> None:
    """Entries older than PROVENANCE_RETENTION_DAYS go; newer ones and legacy
    one-field lines stay. The window must be PROVENANCE_RETENTION_DAYS, not
    RALLY_RESULTS_RETENTION_DAYS (the ledger authorizes RGW auto-purge)."""
    ledger = tmp_path / "rally_project_ids.log"
    ledger.write_text(
        "2026-01-01T00:00:00Z old-proj nova\n"
        "legacy-proj\n"
        "2026-05-01T00:00:00Z new-proj swift\n"
    )
    # GNU `date -d` is not portable to the test host; pin the cutoff and
    # record which window was asked for.
    date_shim = (
        'date() { if [[ "$*" == *"days ago"* ]]; then echo "$*" >> "$RESULTS_DIR/date.log"; '
        'echo 2026-03-01T00:00:00Z; else command date "$@"; fi; }; '
    )
    result = run_harness(
        tmp_path, date_shim + "prune_rally_project_ledger",
        {"PROVENANCE_RETENTION_DAYS": "45", "RALLY_RESULTS_RETENTION_DAYS": "7"},
    )
    assert result.returncode == 0, result.stderr
    assert ledger.read_text() == "legacy-proj\n2026-05-01T00:00:00Z new-proj swift\n"
    assert "45 days ago" in (tmp_path / "date.log").read_text()


def test_cleanup_metrics_seed_is_shared_with_boot_init(tmp_path: Path) -> None:
    """One seed (os_helpers.sh::seed_cleanup_metrics) for run_lib.sh and the
    boot-time init_state.sh; it must read as a healthy, not-yet-scanned state."""
    result = run_harness(tmp_path, "ensure_cleanup_metrics_file")
    assert result.returncode == 0, result.stderr
    seed = json.loads((tmp_path / "cleanup_metrics.json").read_text())
    assert seed["timestamp"] == "waiting_for_first_run"
    assert seed["openstack_scan_status"] == "ok"
    assert seed["rgw_scan_status"] == "skipped"
    init_state = (SCRIPT.parent / "init_state.sh").read_text()
    assert "seed_cleanup_metrics" in init_state
    assert '"rgw_orphaned_objects"' not in init_state


def test_prune_failure_is_logged_not_swallowed(tmp_path: Path) -> None:
    """A run directory rally cannot fully delete (left root-owned by a root
    run) used to fail silently under `2>/dev/null || true`; it must be named,
    and checked BEFORE deleting: a partial rm -rf removes the deletable files,
    bumps the directory's mtime out of the -mtime window and so silences the
    warning on every later run."""
    old = tmp_path / "20200101T000000Z"
    stuck = old / "sub"
    stuck.mkdir(parents=True)
    (old / "nova.json").write_text("{}")
    (stuck / "nova.html").write_text("x")
    stuck.chmod(0o555)
    os.utime(old, (946684800, 946684800))
    try:
        result = run_harness(
            tmp_path,
            'date() { if [[ "$*" == *"days ago"* ]]; then echo 2000-01-01T00:00:00Z; '
            'else command date "$@"; fi; }; prune_old_results',
            {"RALLY_RESULTS_RETENTION_DAYS": "1"},
        )
    finally:
        stuck.chmod(0o755)
    assert result.returncode == 0, result.stderr
    assert f"WARNING: could not prune {old}" in result.stdout
    assert (old / "nova.json").exists()
    assert old.stat().st_mtime == 946684800
