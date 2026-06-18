"""Tests for scripts/backfill_reports.sh.

The migration re-renders historical <ts>/<service>.html from the persisted
<service>.json so pre-upgrade AngularJS reports stop rendering broken under the
strict CSP. Covered here: an old report is regenerated into the new self-
contained format, an already-migrated report is left untouched (idempotent),
non-run directories and non-service files are ignored, and a malformed JSON
still yields a valid empty-state report (best-effort, never crashes the boot).
"""
import json
import shutil
import subprocess
from pathlib import Path

# macOS ships bash 3.2 at /bin/bash; the scripts need bash >= 4 (mapfile).
BASH = shutil.which("bash") or "/bin/bash"

SCRIPT = Path(__file__).resolve().parent / "backfill_reports.sh"

NEW_MARKER = 'id="rally-report-data"'
OLD_REPORT = (
    "<!DOCTYPE html><html ng-app><head>"
    '<script src="https://ajax.googleapis.com/ajax/libs/angularjs/1.3.3/angular.min.js"></script>'
    "</head><body>{{n.cls}}</body></html>"
)

SAMPLE_RESULTS = [
    {
        "key": {"name": "NovaServers.boot", "kw": {"runner": {"type": "constant"}}},
        "result": [{"duration": 1.0, "atomic_actions": {"nova.boot": 1.0}, "error": []}],
        "sla": [{"criterion": "failure_rate", "success": True, "detail": "ok"}],
        "hooks": [],
        "load_duration": 1.0,
        "full_duration": 2.0,
    }
]


def run_backfill(results_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env={"RESULTS_DIR": str(results_dir), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )


def _make_run(results_dir: Path, ts: str, service: str, *, html: str | None, results=SAMPLE_RESULTS):
    run_dir = results_dir / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"{service}.json").write_text(json.dumps(results))
    if html is not None:
        (run_dir / f"{service}.html").write_text(html)
    return run_dir


def test_regenerates_old_report_into_self_contained_format(tmp_path):
    run_dir = _make_run(tmp_path, "20260615T160001Z", "nova", html=OLD_REPORT)

    result = run_backfill(tmp_path)

    assert result.returncode == 0, result.stderr
    rendered = (run_dir / "nova.html").read_text()
    assert NEW_MARKER in rendered
    assert "googleapis.com" not in rendered
    assert "{{n.cls}}" not in rendered
    # The persisted JSON content survives the round-trip.
    assert "NovaServers" in rendered
    assert "regenerated 1 of 1" in result.stdout


def test_is_idempotent_and_leaves_migrated_reports_untouched(tmp_path):
    run_dir = _make_run(tmp_path, "20260615T160001Z", "nova", html=OLD_REPORT)

    run_backfill(tmp_path)
    migrated = (run_dir / "nova.html").read_text()
    result = run_backfill(tmp_path)

    assert (run_dir / "nova.html").read_text() == migrated  # byte-identical, not re-rendered
    assert "regenerated 0 of 1" in result.stdout


def test_renders_when_html_is_missing(tmp_path):
    run_dir = _make_run(tmp_path, "20260615T160001Z", "cinder", html=None)

    result = run_backfill(tmp_path)

    assert result.returncode == 0
    assert NEW_MARKER in (run_dir / "cinder.html").read_text()
    assert "regenerated 1 of 1" in result.stdout


def test_ignores_non_run_directories_and_branding(tmp_path):
    # A custom-theme dir under branding/ must never be scanned for reports.
    (tmp_path / "branding").mkdir()
    (tmp_path / "branding" / "tokens.json").write_text("{}")
    (tmp_path / "not-a-timestamp").mkdir()
    (tmp_path / "not-a-timestamp" / "nova.json").write_text("[]")

    result = run_backfill(tmp_path)

    assert result.returncode == 0
    assert "regenerated 0 of 0" in result.stdout
    assert not (tmp_path / "branding" / "tokens.html").exists()
    assert not (tmp_path / "not-a-timestamp" / "nova.html").exists()


def test_malformed_json_yields_empty_state_report(tmp_path):
    run_dir = tmp_path / "20260615T160001Z"
    run_dir.mkdir()
    (run_dir / "nova.json").write_text("this is not json (crashed task)")
    (run_dir / "nova.html").write_text(OLD_REPORT)

    result = run_backfill(tmp_path)

    assert result.returncode == 0
    rendered = (run_dir / "nova.html").read_text()
    assert NEW_MARKER in rendered  # broken old report replaced by a valid empty state
    assert "regenerated 1 of 1" in result.stdout
