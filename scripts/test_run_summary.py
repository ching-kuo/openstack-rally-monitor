"""Contract tests for scripts/run_tests.sh::build_summary.

build_summary is the heart of pass/fail classification: it parses each
per-service Rally task-results JSON file (${RUN_DIR}/<service>.json) and emits
${RUN_DIR}/summary.json + ${SUMMARY_FILE} with per-service status, duration,
total_iterations, failed_iterations, sla_passed, and a scenarios[] detail array.
That summary feeds the dashboard, the exporter, the uptime ledger, and the
announcement auto-clear, yet had zero coverage.

These tests source run_tests.sh in a bash -c harness (its `BASH_SOURCE == $0`
main guard makes sourcing side-effect-free) with RESULTS_DIR / RUN_DIR /
TIMESTAMP / SUMMARY_FILE pointed at a tmp dir, drop realistic Rally fixtures
into RUN_DIR, invoke build_summary, and assert on the produced summary.json.
Mirrors the harness pattern in test_uptime_ledger.py::record.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

# macOS ships bash 3.2 at /bin/bash; the scripts need bash >= 4 (mapfile).
BASH = shutil.which("bash") or "/bin/bash"

import pytest

SCRIPT = Path(__file__).resolve().parent / "run_tests.sh"

# build_summary iterates this hardcoded service list (run_tests.sh SERVICES).
# Services without a result file are reported as "skipped".
SERVICES = ("keystone", "nova", "neutron", "glance", "cinder", "swift")

TIMESTAMP = "20260612T120000Z"


# ---------------------------------------------------------------------------
# Rally task-results fixtures (shape of `rally task results <uuid>` JSON:
# an array of scenario objects).
# ---------------------------------------------------------------------------

def scenario(
    name: str,
    *,
    full_duration: float = 5.0,
    iterations: int = 2,
    errors: int = 0,
    runner_times: int | None = None,
    sla_success: list[bool] | None = None,
):
    """Build one Rally scenario result object.

    - `iterations` real iteration records are emitted; the first `errors` of
      them carry a non-empty `.error` array (Rally's failed-iteration shape).
    - When `iterations == 0`, the iteration count must fall back to
      `.key.kw.runner.times` (set via `runner_times`).
    - `sla_success` controls the `.sla[]` success booleans (default one True).
    """
    result = []
    for i in range(iterations):
        rec: dict = {"duration": 1.0, "timestamp": 1.0}
        if i < errors:
            rec["error"] = [
                "ExceptionClass",
                "human readable failure message",
                "traceback...",
            ]
        else:
            rec["error"] = []
        result.append(rec)

    if sla_success is None:
        sla_success = [True]
    sla = [
        {"criterion": "failure_rate", "success": ok, "detail": "..."}
        for ok in sla_success
    ]

    return {
        "key": {
            "name": name,
            "kw": {"runner": {"type": "constant", "times": runner_times if runner_times is not None else iterations}},
        },
        "full_duration": full_duration,
        "load_duration": full_duration - 1.0,
        "result": result,
        "sla": sla,
    }


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def build_summary(tmp_path: Path, service_files: dict[str, object]):
    """Drop fixture files into RUN_DIR and run build_summary against tmp_path.

    `service_files` maps service name -> file content. A dict/list value is
    JSON-encoded; a str value is written verbatim (for the garbage-result
    case); a service omitted from the mapping has no result file (-> skipped).
    """
    run_dir = tmp_path / TIMESTAMP
    run_dir.mkdir(parents=True, exist_ok=True)
    # build_summary appends to RUN_LOG if it exists; create it like main() does.
    (run_dir / "run.log").touch()

    for service, content in service_files.items():
        path = run_dir / f"{service}.json"
        if isinstance(content, str):
            path.write_text(content)
        else:
            path.write_text(json.dumps(content))

    summary_file = tmp_path / "latest_summary.json"
    # run_tests.sh derives RUN_DIR/SUMMARY_FILE/RUN_LOG from a freshly computed
    # TIMESTAMP at source time, so they must be re-assigned AFTER sourcing (the
    # same post-source override that test_uptime_ledger.py uses for TIMESTAMP).
    overrides = (
        f'RUN_DIR="{run_dir}"; TIMESTAMP="{TIMESTAMP}"; '
        f'SUMMARY_FILE="{summary_file}"; RUN_LOG="{run_dir / "run.log"}"; '
    )
    result = subprocess.run(
        [
            BASH,
            "-c",
            f'source "{SCRIPT}" && {overrides} build_summary',
        ],
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
            "RESULTS_DIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"build_summary failed:\n{result.stderr}\n{result.stdout}"
    summary = json.loads(summary_file.read_text())
    # The per-run summary.json and latest_summary.json must be identical.
    run_summary = json.loads((run_dir / "summary.json").read_text())
    assert run_summary == summary
    return summary


def svc(summary: dict, name: str) -> dict:
    return summary["services"][name]


# ---------------------------------------------------------------------------
# Top-level shape
# ---------------------------------------------------------------------------

def test_summary_has_timestamp_and_all_services(tmp_path: Path) -> None:
    summary = build_summary(tmp_path, {})
    assert summary["timestamp"] == TIMESTAMP
    assert set(summary["services"].keys()) == set(SERVICES)


# ---------------------------------------------------------------------------
# Passing scenario
# ---------------------------------------------------------------------------

def test_passing_scenario_reports_passed(tmp_path: Path) -> None:
    summary = build_summary(tmp_path, {
        "nova": [scenario("NovaServers.boot_and_delete", full_duration=12.5, iterations=3)],
    })
    nova = svc(summary, "nova")
    assert nova["status"] == "passed"
    assert nova["duration"] == 12.5
    assert nova["total_iterations"] == 3
    assert nova["failed_iterations"] == 0
    assert nova["sla_passed"] is True
    assert len(nova["scenarios"]) == 1
    detail = nova["scenarios"][0]
    assert detail["name"] == "NovaServers.boot_and_delete"
    assert detail["duration"] == 12.5
    assert detail["iterations"] == 3
    assert detail["failures"] == 0
    assert detail["sla"] is True
    # A passing scenario carries an empty first_error (the dashboard hides the
    # error line when this is empty).
    assert detail["first_error"] == ""


def test_multiple_scenarios_aggregate(tmp_path: Path) -> None:
    summary = build_summary(tmp_path, {
        "keystone": [
            scenario("Authenticate.keystone", full_duration=2.0, iterations=5),
            scenario("KeystoneBasic.create_user", full_duration=3.0, iterations=4),
        ],
    })
    ks = svc(summary, "keystone")
    assert ks["status"] == "passed"
    assert ks["duration"] == 5.0  # 2.0 + 3.0
    assert ks["total_iterations"] == 9  # 5 + 4
    assert ks["failed_iterations"] == 0
    assert len(ks["scenarios"]) == 2


# ---------------------------------------------------------------------------
# Failed iterations
# ---------------------------------------------------------------------------

def test_failed_iterations_mark_service_failed(tmp_path: Path) -> None:
    summary = build_summary(tmp_path, {
        "cinder": [scenario("CinderVolumes.create_and_delete", iterations=4, errors=2)],
    })
    cinder = svc(summary, "cinder")
    assert cinder["status"] == "failed"
    assert cinder["total_iterations"] == 4
    assert cinder["failed_iterations"] == 2
    # SLA can still be reported true; the failure count alone fails the service.
    assert cinder["sla_passed"] is True
    assert cinder["scenarios"][0]["failures"] == 2
    # first_error surfaces the MESSAGE element (.error[1]) of the first failed
    # iteration so the dashboard modal can show WHY it failed. The fixture's
    # error array is [type, message, traceback].
    assert cinder["scenarios"][0]["first_error"] == "human readable failure message"


# ---------------------------------------------------------------------------
# first_error extraction (the failure-cause excerpt the modal renders)
# ---------------------------------------------------------------------------

def test_first_error_uses_first_failed_iteration_message(tmp_path: Path) -> None:
    """When several iterations fail, first_error is the message of the FIRST
    one (index 0 of the failed subset), not a later one."""
    scn = scenario("NovaServers.boot", iterations=3, errors=0)
    # Hand-craft three iterations: pass, fail("first boom"), fail("second boom").
    scn["result"] = [
        {"duration": 1.0, "timestamp": 1.0, "error": []},
        {"duration": 1.0, "timestamp": 1.0,
         "error": ["TimeoutException", "first boom", "trace A"]},
        {"duration": 1.0, "timestamp": 1.0,
         "error": ["ValueError", "second boom", "trace B"]},
    ]
    summary = build_summary(tmp_path, {"nova": [scn]})
    nova = svc(summary, "nova")
    assert nova["scenarios"][0]["failures"] == 2
    assert nova["scenarios"][0]["first_error"] == "first boom"


def test_first_error_falls_back_to_type_when_message_missing(tmp_path: Path) -> None:
    """Older/edge Rally error shapes may carry a single element. The jq takes
    .error[1] then falls back to .error[0] so the excerpt is never empty for a
    genuinely failed iteration."""
    scn = scenario("CinderVolumes.create", iterations=1, errors=0)
    scn["result"] = [
        {"duration": 1.0, "timestamp": 1.0, "error": ["LoneErrorString"]},
    ]
    summary = build_summary(tmp_path, {"cinder": [scn]})
    cinder = svc(summary, "cinder")
    assert cinder["scenarios"][0]["failures"] == 1
    assert cinder["scenarios"][0]["first_error"] == "LoneErrorString"


def test_first_error_truncated_to_300_chars(tmp_path: Path) -> None:
    """Long failure messages are truncated server-side so summaries stay small;
    the dashboard relies on this bound rather than truncating client-side."""
    long_msg = "E" * 500
    scn = scenario("GlanceImages.create", iterations=1, errors=0)
    scn["result"] = [
        {"duration": 1.0, "timestamp": 1.0,
         "error": ["BigException", long_msg, "trace"]},
    ]
    summary = build_summary(tmp_path, {"glance": [scn]})
    first_error = svc(summary, "glance")["scenarios"][0]["first_error"]
    assert len(first_error) == 300
    assert first_error == "E" * 300


# ---------------------------------------------------------------------------
# SLA failure
# ---------------------------------------------------------------------------

def test_sla_failure_marks_service_failed(tmp_path: Path) -> None:
    summary = build_summary(tmp_path, {
        "neutron": [scenario("NeutronNetworks.create", iterations=3, errors=0,
                              sla_success=[False])],
    })
    neutron = svc(summary, "neutron")
    assert neutron["status"] == "failed"
    assert neutron["failed_iterations"] == 0  # no iteration errors
    assert neutron["sla_passed"] is False
    assert neutron["scenarios"][0]["sla"] is False


def test_sla_passed_requires_all_sla_checks_to_pass(tmp_path: Path) -> None:
    # A scenario with mixed SLA results: one passes, one fails -> sla False.
    summary = build_summary(tmp_path, {
        "neutron": [scenario("NeutronNetworks.create", iterations=2,
                              sla_success=[True, False])],
    })
    assert svc(summary, "neutron")["sla_passed"] is False
    assert svc(summary, "neutron")["status"] == "failed"


# ---------------------------------------------------------------------------
# Zero-result fallback to runner.times
# ---------------------------------------------------------------------------

def test_zero_result_falls_back_to_runner_times(tmp_path: Path) -> None:
    """A scenario whose `.result` array is empty derives its iteration count
    from `.key.kw.runner.times` instead. That fallback count feeds both the
    per-scenario detail AND the aggregate total_iterations, so a clean
    (no-error, SLA-passing) zero-result scenario is reported passed with the
    configured iteration count rather than being mislabeled as a no-op."""
    summary = build_summary(tmp_path, {
        "glance": [scenario("GlanceImages.create", iterations=0, runner_times=7)],
    })
    glance = svc(summary, "glance")
    assert glance["scenarios"][0]["iterations"] == 7  # fell back to runner.times
    assert glance["total_iterations"] == 7            # aggregate uses the fallback
    assert glance["failed_iterations"] == 0
    assert glance["sla_passed"] is True
    assert glance["status"] == "passed"


def test_zero_iterations_total_forces_failed(tmp_path: Path) -> None:
    """When neither real results nor runner.times yield any iterations, the
    aggregate total is 0 and the service is forced to 'failed' — a run that
    executed nothing is never green (the `total_iterations -eq 0` guard)."""
    summary = build_summary(tmp_path, {
        "glance": [scenario("GlanceImages.create", iterations=0, runner_times=0)],
    })
    glance = svc(summary, "glance")
    assert glance["total_iterations"] == 0
    assert glance["status"] == "failed"


# ---------------------------------------------------------------------------
# Garbage / crashed-task result file
# ---------------------------------------------------------------------------

def test_non_array_garbage_result_marks_failed(tmp_path: Path) -> None:
    """A crashed `rally task results` can leave plain text (a stack trace or an
    error line) instead of a JSON array. build_summary must classify this as
    failed, not crash."""
    summary = build_summary(tmp_path, {
        "swift": "Traceback (most recent call last):\n  ValueError: boom\n",
    })
    swift = svc(summary, "swift")
    assert swift["status"] == "failed"
    assert swift["total_iterations"] == 0
    assert swift["failed_iterations"] == 0
    assert swift["scenarios"] == []


def test_valid_json_but_not_array_marks_failed(tmp_path: Path) -> None:
    # A JSON object (not an array) is also invalid Rally results output.
    summary = build_summary(tmp_path, {
        "swift": {"error": "task failed to start"},
    })
    assert svc(summary, "swift")["status"] == "failed"


def test_empty_array_result_marks_failed(tmp_path: Path) -> None:
    # An empty array parses, but yields zero iterations -> failed.
    summary = build_summary(tmp_path, {"swift": []})
    swift = svc(summary, "swift")
    assert swift["status"] == "failed"
    assert swift["total_iterations"] == 0
    assert swift["scenarios"] == []


# ---------------------------------------------------------------------------
# Missing result file -> skipped
# ---------------------------------------------------------------------------

def test_missing_result_file_is_skipped(tmp_path: Path) -> None:
    # Only nova has a result file; the other five are skipped.
    summary = build_summary(tmp_path, {
        "nova": [scenario("NovaServers.boot_and_delete")],
    })
    assert svc(summary, "nova")["status"] == "passed"
    for other in ("keystone", "neutron", "glance", "cinder", "swift"):
        s = svc(summary, other)
        assert s["status"] == "skipped"
        assert s["duration"] == 0
        assert s["total_iterations"] == 0
        assert s["failed_iterations"] == 0
        assert s["sla_passed"] is True
        assert s["scenarios"] == []


# ---------------------------------------------------------------------------
# Mixed run (the realistic case): some pass, some fail, some skip
# ---------------------------------------------------------------------------

def test_mixed_run_classifies_each_service_independently(tmp_path: Path) -> None:
    summary = build_summary(tmp_path, {
        "keystone": [scenario("Authenticate.keystone", iterations=5)],            # pass
        "nova": [scenario("NovaServers.boot", iterations=4, errors=1)],           # fail (iter error)
        "neutron": [scenario("NeutronNetworks.create", sla_success=[False])],     # fail (sla)
        "cinder": "garbage not json",                                             # fail (crash)
        # glance + swift omitted -> skipped
    })
    assert svc(summary, "keystone")["status"] == "passed"
    assert svc(summary, "nova")["status"] == "failed"
    assert svc(summary, "neutron")["status"] == "failed"
    assert svc(summary, "cinder")["status"] == "failed"
    assert svc(summary, "glance")["status"] == "skipped"
    assert svc(summary, "swift")["status"] == "skipped"


# ---------------------------------------------------------------------------
# RALLY_SERVICES parsing (parse_rally_services)
# ---------------------------------------------------------------------------
# The monitored service set is configurable via RALLY_SERVICES. run_tests.sh's
# parse_rally_services normalizes it (trim/lowercase/drop-empties/dedupe, order
# preserved) and the result becomes the SERVICES array that build_summary
# iterates. The same normalization rules are mirrored in api_health_check.py's
# parse_rally_services (covered in test_api_health_check.py) -- keep in sync.

DEFAULT_SERVICES = "keystone,nova,neutron,glance,cinder,swift"


def parse_rally_services(raw: str | None) -> list[str]:
    """Source run_tests.sh and invoke its parse_rally_services with `raw`.

    Returns the normalized service list (the function echoes one name per
    line). `raw=None` exercises the unset path (the arg is passed empty, which
    the function treats as "fall back to default", matching how main() invokes
    it with ${RALLY_SERVICES:-}).
    """
    arg = "" if raw is None else raw
    result = subprocess.run(
        [
            BASH,
            "-c",
            # Quote the argument exactly as given so whitespace/empties survive.
            f'source "{SCRIPT}" && parse_rally_services "$1"',
            "_",
            arg,
        ],
        env={"PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"parse_rally_services failed:\n{result.stderr}"
    return [line for line in result.stdout.splitlines() if line != ""]


def test_parse_services_default_when_unset() -> None:
    assert parse_rally_services(None) == DEFAULT_SERVICES.split(",")


@pytest.mark.parametrize("raw", ["", "   ", ",", " , , ", ",,,"])
def test_parse_services_empty_or_blank_falls_back_to_default(raw: str) -> None:
    # Empty / whitespace-only / separator-only inputs normalize to nothing and
    # must fall back to the six-service default rather than yielding an empty
    # SERVICES array (which would make build_summary emit no services).
    assert parse_rally_services(raw) == DEFAULT_SERVICES.split(",")


def test_parse_services_trims_lowercases_dedupes_preserving_order() -> None:
    # Mixed case, surrounding + interior whitespace, an empty segment, and a
    # duplicate. Order is first-seen; no keystone hoisting here (that is the
    # health checker's concern -- the smoke set reflects operator order verbatim).
    assert parse_rally_services("  Nova , KEYSTONE , nova ,, Cinder ") == [
        "nova",
        "keystone",
        "cinder",
    ]


def test_parse_services_strips_internal_whitespace() -> None:
    # Whitespace inside a token is stripped (awk gsub over [[:space:]]).
    assert parse_rally_services("no va, cind er") == ["nova", "cinder"]


def test_parse_services_keeps_unknown_names() -> None:
    # Parsing is registry-agnostic: an unknown service (e.g. octavia) is kept;
    # run_service_tests logs a SKIP later when no scenario file exists for it.
    assert parse_rally_services("octavia,nova") == ["octavia", "nova"]


def test_parse_services_preserves_operator_order() -> None:
    # A non-default ordering is preserved exactly (dashboard re-sorts client
    # side; the script must not impose its own order).
    assert parse_rally_services("swift,nova,keystone") == ["swift", "nova", "keystone"]


# ---------------------------------------------------------------------------
# Configured subset flows through build_summary
# ---------------------------------------------------------------------------

def test_build_summary_honours_trimmed_rally_services(tmp_path: Path) -> None:
    """With RALLY_SERVICES trimmed to a subset, build_summary's SERVICES array
    (and thus the summary keys) contains exactly that subset -- a Swift-less
    cloud no longer carries a permanent swift column. Verifies the parser ->
    SERVICES -> build_summary wiring end to end."""
    run_dir = tmp_path / TIMESTAMP
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.log").touch()
    (run_dir / "nova.json").write_text(
        json.dumps([scenario("NovaServers.boot_and_delete", iterations=2)])
    )

    summary_file = tmp_path / "latest_summary.json"
    overrides = (
        f'RUN_DIR="{run_dir}"; TIMESTAMP="{TIMESTAMP}"; '
        f'SUMMARY_FILE="{summary_file}"; RUN_LOG="{run_dir / "run.log"}"; '
    )
    result = subprocess.run(
        [
            BASH,
            "-c",
            f'source "{SCRIPT}" && {overrides} build_summary',
        ],
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
            "RESULTS_DIR": str(tmp_path),
            # Trim the set: only keystone + nova are monitored.
            "RALLY_SERVICES": "keystone,nova",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"build_summary failed:\n{result.stderr}\n{result.stdout}"
    summary = json.loads(summary_file.read_text())
    # Exactly the configured services -- swift/cinder/etc. are absent.
    assert set(summary["services"].keys()) == {"keystone", "nova"}
    assert summary["services"]["nova"]["status"] == "passed"
    # keystone had no result file -> skipped (honest signal for a missing scenario).
    assert summary["services"]["keystone"]["status"] == "skipped"
