"""Tests for scripts/rally_project_tracker.py, the per-task provenance poller.

Unit tests drive Tracker/run with a fake connection. The integration tests
run run_tests.sh::run_service_tests with a fake ``openstack`` package on
PYTHONPATH (so the real tracker process runs, minus the SDK) and assert on
the ledger it leaves behind, including that rgw_helpers.sh still reads it.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rally_project_tracker as tracker_mod  # noqa: E402

BASH = shutil.which("bash") or "/bin/bash"
SCRIPTS = Path(__file__).resolve().parent
TIMESTAMP = "20260612T120000Z"
BASE_PATH = "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"
# Same shape the bash tracker wrote: "<UTC ts> <project_id> <service>".
LEDGER_LINE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ (\S+) (\S+)$")


class FakeConn:
    """identity.get("/projects") answers from a list of snapshots, one per
    call (the last one repeats). An Exception entry is raised instead; a
    dict entry is returned as the raw response body."""

    def __init__(self, *snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0
        self.identity = SimpleNamespace(get=self._get)

    def _get(self, path):
        # Only Rally context projects are requested (server-side filter).
        assert path == "/projects?name__startswith=c_rally_", path
        snap = self.snapshots[min(self.calls, len(self.snapshots) - 1)]
        self.calls += 1
        if isinstance(snap, Exception):
            raise snap
        body = snap if isinstance(snap, dict) else {
            "projects": [{"id": i, "name": n} for i, n in snap], "links": {"next": None},
        }
        return SimpleNamespace(status_code=200, json=lambda: body)


def no_sleep(_seconds):
    pass


def make_tracker(conn, tmp_path: Path, logs: list):
    ledger = tmp_path / "rally_project_ids.log"
    t = tracker_mod.Tracker(conn, "nova", str(ledger), logs.append,
                            now_fn=lambda: "2026-06-12T12:00:00Z")
    return t, ledger


def test_snapshot_projects_are_never_recorded(tmp_path: Path) -> None:
    conn = FakeConn(
        [("old", "c_rally_old"), ("admin", "admin")],
        [("old", "c_rally_old"), ("new", "c_rally_new"), ("s", "s_rally_x")],
    )
    logs: list = []
    t, ledger = make_tracker(conn, tmp_path, logs)
    assert t.snapshot()
    assert t.poll()
    assert t.poll()  # same projects again: no duplicate line
    assert ledger.read_text() == "2026-06-12T12:00:00Z new nova\n"
    assert logs == ["  nova: recorded Rally project new"]


def test_null_project_name_does_not_fail_the_poll(tmp_path: Path) -> None:
    conn = FakeConn([], [("x", None), ("new", "c_rally_new")])
    t, ledger = make_tracker(conn, tmp_path, [])
    t.snapshot()
    assert t.poll()
    assert ledger.read_text().split() == ["2026-06-12T12:00:00Z", "new", "nova"]


def test_snapshot_is_retried(tmp_path: Path) -> None:
    conn = FakeConn(RuntimeError("auth"), [("old", "c_rally_old")],
                    [("old", "c_rally_old"), ("new", "c_rally_new")])
    logs: list = []
    t, ledger = make_tracker(conn, tmp_path, logs)
    assert t.snapshot(sleep_fn=no_sleep)
    assert "attempt 1/3" in logs[0]
    assert t.poll()
    assert ledger.read_text() == "2026-06-12T12:00:00Z new nova\n"


def test_snapshot_that_keeps_failing_reports_failure(tmp_path: Path) -> None:
    conn = FakeConn(RuntimeError("auth"))
    logs: list = []
    t, _ = make_tracker(conn, tmp_path, logs)
    assert not t.snapshot(sleep_fn=no_sleep)
    assert conn.calls == 3
    assert len(logs) == 3


def test_malformed_record_fails_snapshot_and_poll(tmp_path: Path) -> None:
    # A record without a usable ID would silently drop out of the baseline,
    # and the project could later be attributed to this task.
    t, ledger = make_tracker(FakeConn([("a", "c_rally_a"), (None, "c_rally_noid")]), tmp_path, [])
    assert not t.snapshot(sleep_fn=no_sleep)
    assert not t.poll()
    assert not ledger.exists()


def test_truncated_listing_is_a_failure_not_a_partial_view(tmp_path: Path) -> None:
    # A truncated snapshot would drop pre-existing projects from the
    # seen-set; a truncated poll could hide projects. Both must fail.
    truncated = {"projects": [{"id": "a", "name": "c_rally_a"}], "truncated": True}
    t, ledger = make_tracker(FakeConn(truncated), tmp_path, [])
    assert not t.snapshot(sleep_fn=no_sleep)
    assert not t.poll()
    assert not ledger.exists()


def test_run_warns_once_and_final_poll_records_late_project(tmp_path: Path) -> None:
    # Two failing polls (one warning), then the stop signal; the final poll
    # still records a project created just before the task exited.
    conn = FakeConn(RuntimeError("x"), RuntimeError("y"), [("late", "c_rally_late")])
    logs: list = []
    t, ledger = make_tracker(conn, tmp_path, logs)
    ticks = iter([True, True, False])
    tracker_mod.run(t, lambda: False, 0, lambda: next(ticks), sleep_fn=no_sleep)
    warnings = [m for m in logs if "failed to poll" in m]
    assert len(warnings) == 1
    assert ledger.read_text() == "2026-06-12T12:00:00Z late nova\n"


def test_short_lived_project_seen_mid_task_is_recorded(tmp_path: Path) -> None:
    # A context project that exists only between two polls must still land
    # in the ledger -- the reason the cadence was not lengthened.
    conn = FakeConn([], [], [("blip", "c_rally_blip")], [], [])
    t, ledger = make_tracker(conn, tmp_path, [])
    t.snapshot()
    ticks = iter([True, True, True, False])
    tracker_mod.run(t, lambda: False, 0, lambda: next(ticks), sleep_fn=no_sleep)
    assert ledger.read_text() == "2026-06-12T12:00:00Z blip nova\n"


def test_stop_event_ends_the_loop(tmp_path: Path) -> None:
    conn = FakeConn([])
    t, _ = make_tracker(conn, tmp_path, [])
    tracker_mod.run(t, lambda: True, 60, lambda: True, sleep_fn=no_sleep)
    assert conn.calls == 1  # only the final poll


def test_stop_flag_interrupts_the_interval_sleep(tmp_path: Path) -> None:
    # The handler only sets a flag; the sleep is sliced so a stop request is
    # honoured within one slice, not after the full interval.
    conn = FakeConn([])
    t, _ = make_tracker(conn, tmp_path, [])
    slept: list = []
    flag = {"stop": False}

    def sleep(seconds):
        slept.append(seconds)
        flag["stop"] = True

    tracker_mod.run(t, lambda: flag["stop"], 5.0, lambda: True, sleep_fn=sleep)
    assert slept == [tracker_mod.STOP_CHECK_SECONDS]
    assert conn.calls == 2  # one poll, one final poll


def test_unwritable_ledger_is_retried_next_poll(tmp_path: Path) -> None:
    conn = FakeConn([], [("p", "c_rally_p")])
    t, ledger = make_tracker(conn, tmp_path, [])
    t.snapshot()
    t.ledger_path = str(tmp_path / "missing-dir" / "ledger.log")
    assert not t.poll()
    t.ledger_path = str(ledger)
    assert t.poll()
    assert " p nova" in ledger.read_text()


# ---------------------------------------------------------------------------
# Integration: run_service_tests drives the real tracker process
# ---------------------------------------------------------------------------

FAKE_OPENSTACK = '''
import json, os
from types import SimpleNamespace

def connect(**kwargs):
    path = os.environ["FAKE_PROJECTS_FILE"]
    def get(url):
        with open(path) as handle:
            body = {"projects": json.load(handle)}
        return SimpleNamespace(status_code=200, json=lambda: body)
    return SimpleNamespace(identity=SimpleNamespace(get=get))
'''


def write_shim(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


def run_service(tmp_path: Path, rally_start_body: str, with_sdk: bool = True,
                sdk_source: str | None = None):
    projects = tmp_path / "projects.json"
    projects.write_text(json.dumps([{"id": "pre", "name": "c_rally_pre"}]))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_shim(bin_dir, "rally", f"""
case "$1 $2" in
  "task start") {rally_start_body} ;;
  *) exit 0 ;;
esac
""")
    config = tmp_path / "config"
    (config / "scenarios").mkdir(parents=True)
    (config / "scenarios" / "nova.yaml").write_text("---\n")
    run_dir = tmp_path / TIMESTAMP
    run_dir.mkdir()
    (run_dir / "run.log").touch()
    env = {
        "PATH": f"{bin_dir}:{BASE_PATH}",
        "RESULTS_DIR": str(tmp_path),
        "RALLY_CONFIG_DIR": str(config),
        "FAKE_PROJECTS_FILE": str(projects),
    }
    # Without the SDK, a package that fails to import stands in for it, so
    # the test does not depend on what the system python3 has installed.
    sdk = tmp_path / "sdk" / "openstack"
    sdk.mkdir(parents=True)
    if sdk_source is None:
        sdk_source = FAKE_OPENSTACK if with_sdk else 'raise ImportError("no openstacksdk")\n'
    (sdk / "__init__.py").write_text(sdk_source)
    env["PYTHONPATH"] = str(sdk.parent)
    overrides = (
        f'RUN_DIR="{run_dir}"; TIMESTAMP="{TIMESTAMP}"; RUN_LOG="{run_dir / "run.log"}"; '
    )
    result = subprocess.run(
        [BASH, "-c", f'source "{SCRIPTS / "run_tests.sh"}" && {overrides} '
                     f'run_service_tests nova "$RUN_DIR/task_args.json"'],
        env=env, capture_output=True, text=True, check=False, timeout=60,
    )
    return result, tmp_path / "rally_project_ids.log"


def test_run_service_tests_records_task_projects_only(tmp_path: Path) -> None:
    # The task creates a context project and exits; the pre-existing one is
    # in the snapshot and must not be attributed to nova.
    create = (
        f'echo \'[{{"id":"pre","name":"c_rally_pre"}},'
        f'{{"id":"made","name":"c_rally_made"}}]\' > "$FAKE_PROJECTS_FILE"; '
        'echo "Task aborted"; exit 1'
    )
    result, ledger = run_service(tmp_path, create)
    assert result.returncode == 0, result.stderr
    lines = ledger.read_text().splitlines()
    assert len(lines) == 1, lines
    match = LEDGER_LINE.match(lines[0])
    assert match and match.groups() == ("made", "nova")
    assert "nova: recorded Rally project made" in result.stdout
    assert "recorded Rally project made" in (tmp_path / TIMESTAMP / "run.log").read_text()

    # The RGW gate still reads the ledger the tracker wrote.
    owned = subprocess.run(
        [BASH, "-c", f'source "{SCRIPTS / "rgw_helpers.sh"}"; '
                     f'RGW_LEDGER_FILE="{ledger}"; rgw_is_rally_owned made && echo yes'],
        capture_output=True, text=True, check=False,
    )
    assert owned.stdout.strip() == "yes"


def test_run_service_tests_survives_missing_sdk(tmp_path: Path) -> None:
    result, ledger = run_service(tmp_path, 'echo "Task aborted"; exit 1', with_sdk=False)
    assert result.returncode == 0, result.stderr
    assert "provenance tracker could not connect" in result.stdout
    assert not ledger.exists()
    # The task still ran and was reported.
    assert (tmp_path / TIMESTAMP / "nova.start_failed").exists()


def test_main_records_nothing_when_snapshot_keeps_failing(tmp_path: Path, monkeypatch) -> None:
    """Fail closed: without a snapshot, pre-existing c_rally_* projects would
    be attributed to this task and authorize purging their RGW data."""
    conn = FakeConn(*([RuntimeError("keystone down")] * 3), [("pre", "c_rally_pre")])
    monkeypatch.setattr(tracker_mod, "build_connection", lambda: conn)
    monkeypatch.setattr(tracker_mod.time, "sleep", no_sleep)
    # Keep main() from installing its SIGTERM handler in the pytest process.
    monkeypatch.setattr(tracker_mod.signal, "signal", lambda *_args: None)
    ledger = tmp_path / "ledger.log"
    ready = tmp_path / "ready"
    rc = tracker_mod.main(["--service", "nova", "--ledger", str(ledger),
                           "--ready-file", str(ready)])
    assert rc == 1
    assert ready.exists()
    assert not ledger.exists()
    assert conn.calls == 3  # no polling after the abandoned snapshot


def test_tracker_dying_before_ready_is_logged_with_exit_code(tmp_path: Path) -> None:
    # SystemExit escapes the tracker's `except Exception`: it dies before
    # signalling ready, which must not pass silently.
    result, ledger = run_service(tmp_path, 'echo "Task aborted"; exit 1',
                                 sdk_source="import sys\nsys.exit(7)\n")
    assert result.returncode == 0, result.stderr
    assert "provenance tracker exited (code 7) before its snapshot" in result.stdout
    assert not ledger.exists()


def _shell(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    run_dir = tmp_path / TIMESTAMP
    run_dir.mkdir(exist_ok=True)
    (run_dir / "run.log").touch()
    return subprocess.run(
        [BASH, "-c", f'source "{SCRIPTS / "run_tests.sh"}" && RUN_LOG="{run_dir / "run.log"}"; {body}'],
        env={"PATH": BASE_PATH, "RESULTS_DIR": str(tmp_path)},
        capture_output=True, text=True, check=False, timeout=30,
    )


def test_stop_never_signals_a_pid_that_is_not_our_running_child(tmp_path: Path) -> None:
    """After the tracker exited, its PID may be recycled: stop must not
    signal it. _tracker_running consults bash's job table, not kill -0."""
    result = _shell(tmp_path, """
        kill() { echo "KILL $*"; }
        command sleep 0.1 & TRACKER_PID=$!
        command sleep 0.5          # the child exits and bash reaps it
        stop_project_tracker
        echo "PID=[${TRACKER_PID}]"
        TRACKER_PID=1              # e.g. a recycled PID we never started
        _tracker_running && echo RUNNING || echo NOT-RUNNING
        stop_project_tracker
    """)
    assert result.returncode == 0, result.stderr
    assert "KILL" not in result.stdout
    assert "PID=[]" in result.stdout
    assert "NOT-RUNNING" in result.stdout


def test_stop_terminates_a_running_tracker_and_clears_its_pid(tmp_path: Path) -> None:
    result = _shell(tmp_path, """
        command sleep 30 & TRACKER_PID=$!
        stop_project_tracker
        echo "PID=[${TRACKER_PID}]"
        _tracker_running && echo RUNNING || echo NOT-RUNNING
    """)
    assert result.returncode == 0, result.stderr
    assert "PID=[]" in result.stdout and "NOT-RUNNING" in result.stdout


def test_newline_in_project_id_never_reaches_the_ledger(tmp_path: Path) -> None:
    """A newline-suffixed ID would split into a second ledger line that
    authorizes purging a different project."""
    conn = FakeConn([], [("victim\n2026-01-01T00:00:00Z other nova", "c_rally_x"),
                         ("ok\n", "c_rally_y"), ("good", "c_rally_z")])
    t, ledger = make_tracker(conn, tmp_path, [])
    t.snapshot()
    assert t.poll()
    assert ledger.read_text() == "2026-06-12T12:00:00Z good nova\n"


def test_ledger_write_rejects_whitespace_in_id_or_service(tmp_path: Path) -> None:
    logs: list = []
    t, ledger = make_tracker(FakeConn([]), tmp_path, logs)
    assert not t.record("bad id")
    t.service = "nova\nx"
    assert not t.record("good")
    assert not ledger.exists()
