"""Tests for scripts/notify.sh — webhook notification on status transitions.

notify.sh is invoked as `notify.sh <passed|failed>` from run_tests.sh after
record_smoke_result. It compares the given status against
${RESULTS_DIR}/.last_notified_status and POSTs a JSON payload to
NOTIFY_WEBHOOK_URL only on a transition.

Tests drive the script via subprocess with RESULTS_DIR pointed at a tmp_path
and a fake `curl` shim prepended to PATH. The shim records its argv and stdin
to files so we can assert on the payload without making real HTTP requests, and
its exit code is controllable to simulate send failures.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

# macOS ships bash 3.2 at /bin/bash; the scripts need bash >= 4 (mapfile).
BASH = shutil.which("bash") or "/bin/bash"

SCRIPT = Path(__file__).resolve().parent / "notify.sh"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def make_curl_shim(bin_dir: Path, exit_code: int = 0) -> tuple[Path, Path]:
    """Write a fake `curl` into bin_dir that records argv + stdin and exits
    with exit_code. Returns the (argv_file, stdin_file) paths it writes to."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    argv_file = bin_dir / "curl_argv.txt"
    stdin_file = bin_dir / "curl_stdin.txt"
    shim = bin_dir / "curl"
    shim.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" > "{argv_file}"\n'
        f'cat > "{stdin_file}"\n'
        f"exit {exit_code}\n"
    )
    shim.chmod(0o755)
    return argv_file, stdin_file


def write_summary(tmp_path: Path, summary: dict) -> None:
    (tmp_path / "latest_summary.json").write_text(json.dumps(summary))


def run_notify(
    tmp_path: Path,
    status: str,
    *,
    bin_dir: Path | None = None,
    webhook_url: str | None = None,
    notify_format: str | None = None,
    dashboard_url: str | None = None,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": (f"{bin_dir}:" if bin_dir else "")
        + "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
        "RESULTS_DIR": str(tmp_path),
    }
    if webhook_url is not None:
        env["NOTIFY_WEBHOOK_URL"] = webhook_url
    if notify_format is not None:
        env["NOTIFY_FORMAT"] = notify_format
    if dashboard_url is not None:
        env["NOTIFY_DASHBOARD_URL"] = dashboard_url
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [BASH, str(SCRIPT), status],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def read_state(tmp_path: Path) -> str | None:
    path = tmp_path / ".last_notified_status"
    if not path.exists():
        return None
    return path.read_text().strip()


SECRET_URL = "https://hooks.example.com/services/SECRET-TOKEN-abc123"


# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------

class TestNoOp:
    def test_unset_url_is_silent_noop(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        argv_file, _ = make_curl_shim(bin_dir)
        write_summary(tmp_path, {"services": {"nova": {"status": "failed"}}})
        # No NOTIFY_WEBHOOK_URL passed.
        result = run_notify(tmp_path, "failed", bin_dir=bin_dir)
        assert result.returncode == 0, result.stderr
        assert not argv_file.exists(), "curl must not be invoked without a URL"
        assert read_state(tmp_path) is None

    def test_first_run_green_does_not_notify(self, tmp_path: Path) -> None:
        """Missing state baselines to 'passed', so a first-ever green run is
        passed->passed: no transition, no notification."""
        bin_dir = tmp_path / "bin"
        argv_file, _ = make_curl_shim(bin_dir)
        write_summary(tmp_path, {"services": {"nova": {"status": "passed"}}})
        result = run_notify(tmp_path, "passed", bin_dir=bin_dir, webhook_url=SECRET_URL)
        assert result.returncode == 0, result.stderr
        assert not argv_file.exists()
        # No send => state not written.
        assert read_state(tmp_path) is None

    def test_repeat_failed_does_not_notify(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        argv_file, _ = make_curl_shim(bin_dir)
        (tmp_path / ".last_notified_status").write_text("failed\n")
        write_summary(tmp_path, {"services": {"nova": {"status": "failed"}}})
        result = run_notify(tmp_path, "failed", bin_dir=bin_dir, webhook_url=SECRET_URL)
        assert result.returncode == 0, result.stderr
        assert not argv_file.exists(), "no transition => no curl"
        assert read_state(tmp_path) == "failed"

    def test_invalid_status_argument_rejected(self, tmp_path: Path) -> None:
        result = run_notify(tmp_path, "bogus", webhook_url=SECRET_URL)
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# Transition cases
# ---------------------------------------------------------------------------

class TestTransitions:
    def test_first_run_failed_notifies(self, tmp_path: Path) -> None:
        """Missing state baselines to 'passed', so a first-ever failed run is a
        passed->failed transition and must notify."""
        bin_dir = tmp_path / "bin"
        argv_file, stdin_file = make_curl_shim(bin_dir)
        write_summary(tmp_path, {"services": {"nova": {"status": "failed"}}})
        result = run_notify(tmp_path, "failed", bin_dir=bin_dir, webhook_url=SECRET_URL)
        assert result.returncode == 0, result.stderr
        assert argv_file.exists(), "curl must fire on first failed run"
        assert read_state(tmp_path) == "failed"

    def test_recovery_failed_to_passed_notifies(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        argv_file, stdin_file = make_curl_shim(bin_dir)
        (tmp_path / ".last_notified_status").write_text("failed\n")
        write_summary(tmp_path, {"services": {"nova": {"status": "passed"}}})
        result = run_notify(tmp_path, "passed", bin_dir=bin_dir, webhook_url=SECRET_URL)
        assert result.returncode == 0, result.stderr
        payload = json.loads(stdin_file.read_text())
        assert payload["status"] == "passed"
        assert payload["previous_status"] == "failed"
        assert read_state(tmp_path) == "passed"


# ---------------------------------------------------------------------------
# Payload contract
# ---------------------------------------------------------------------------

class TestPayload:
    def test_generic_payload_is_valid_json_with_expected_fields(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        _, stdin_file = make_curl_shim(bin_dir)
        write_summary(tmp_path, {
            "timestamp": "20260612T000000Z",
            "services": {
                "keystone": {"status": "passed"},
                "nova": {"status": "failed"},
                "swift": {"status": "skipped"},
            },
        })
        run_notify(
            tmp_path, "failed", bin_dir=bin_dir, webhook_url=SECRET_URL,
            dashboard_url="https://dash.example",
        )
        payload = json.loads(stdin_file.read_text())
        assert payload["event"] == "rally_status_change"
        assert payload["status"] == "failed"
        assert payload["previous_status"] == "passed"
        assert payload["error"] is None
        assert payload["dashboard_url"] == "https://dash.example"
        # failed_services = every non-passed service (failed + skipped).
        assert set(payload["failed_services"]) == {"nova", "swift"}
        # timestamp is ISO-8601 UTC-ish.
        assert payload["timestamp"].endswith("Z")

    def test_dashboard_url_omitted_when_unset(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        _, stdin_file = make_curl_shim(bin_dir)
        write_summary(tmp_path, {"services": {"nova": {"status": "failed"}}})
        run_notify(tmp_path, "failed", bin_dir=bin_dir, webhook_url=SECRET_URL)
        payload = json.loads(stdin_file.read_text())
        assert "dashboard_url" not in payload

    def test_deployment_failure_shape_carries_error(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        _, stdin_file = make_curl_shim(bin_dir)
        write_summary(tmp_path, {
            "timestamp": "20260612T000000Z",
            "error": "deployment_setup_failed",
            "services": {},
        })
        run_notify(tmp_path, "failed", bin_dir=bin_dir, webhook_url=SECRET_URL)
        payload = json.loads(stdin_file.read_text())
        assert payload["error"] == "deployment_setup_failed"
        assert payload["failed_services"] == []

    def test_missing_summary_file_still_sends_safe_payload(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        argv_file, stdin_file = make_curl_shim(bin_dir)
        # No summary file written.
        result = run_notify(tmp_path, "failed", bin_dir=bin_dir, webhook_url=SECRET_URL)
        assert result.returncode == 0, result.stderr
        assert argv_file.exists()
        payload = json.loads(stdin_file.read_text())
        assert payload["failed_services"] == []
        assert payload["error"] is None

    def test_slack_format_wraps_text(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        _, stdin_file = make_curl_shim(bin_dir)
        write_summary(tmp_path, {
            "services": {"nova": {"status": "failed"}, "keystone": {"status": "passed"}},
        })
        run_notify(
            tmp_path, "failed", bin_dir=bin_dir, webhook_url=SECRET_URL,
            notify_format="slack",
        )
        payload = json.loads(stdin_file.read_text())
        assert list(payload.keys()) == ["text"]
        assert isinstance(payload["text"], str)
        assert "nova" in payload["text"]

    def test_discord_format_wraps_content(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        _, stdin_file = make_curl_shim(bin_dir)
        write_summary(tmp_path, {"services": {"nova": {"status": "failed"}}})
        run_notify(
            tmp_path, "failed", bin_dir=bin_dir, webhook_url=SECRET_URL,
            notify_format="discord",
        )
        payload = json.loads(stdin_file.read_text())
        assert list(payload.keys()) == ["content"]
        assert isinstance(payload["content"], str)

    def test_slack_recovery_message_is_human_readable(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        _, stdin_file = make_curl_shim(bin_dir)
        (tmp_path / ".last_notified_status").write_text("failed\n")
        write_summary(tmp_path, {"services": {"nova": {"status": "passed"}}})
        run_notify(
            tmp_path, "passed", bin_dir=bin_dir, webhook_url=SECRET_URL,
            notify_format="slack",
        )
        payload = json.loads(stdin_file.read_text())
        assert "recovered" in payload["text"].lower()


# ---------------------------------------------------------------------------
# Failure handling + state file robustness
# ---------------------------------------------------------------------------

class TestFailureHandling:
    def test_curl_failure_leaves_state_unchanged(self, tmp_path: Path) -> None:
        """A non-2xx / network failure (curl exit != 0) must NOT update the
        state file, so the next run retries the same transition."""
        bin_dir = tmp_path / "bin"
        make_curl_shim(bin_dir, exit_code=22)  # 22 = curl HTTP error
        (tmp_path / ".last_notified_status").write_text("passed\n")
        write_summary(tmp_path, {"services": {"nova": {"status": "failed"}}})
        result = run_notify(tmp_path, "failed", bin_dir=bin_dir, webhook_url=SECRET_URL)
        # The run itself is unaffected (exit 0); only a warning is logged.
        assert result.returncode == 0, result.stderr
        assert "WARNING" in result.stdout
        # State preserved => retry next run.
        assert read_state(tmp_path) == "passed"
        # No leftover tmp file.
        assert list(tmp_path.glob(".last_notified_status.tmp*")) == []

    def test_curl_failure_does_not_create_state_on_first_run(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        make_curl_shim(bin_dir, exit_code=7)  # 7 = connection failed
        write_summary(tmp_path, {"services": {"nova": {"status": "failed"}}})
        result = run_notify(tmp_path, "failed", bin_dir=bin_dir, webhook_url=SECRET_URL)
        assert result.returncode == 0
        assert read_state(tmp_path) is None  # no baseline written on failure

    def test_state_written_atomically_no_tmp_leftover(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        make_curl_shim(bin_dir, exit_code=0)
        write_summary(tmp_path, {"services": {"nova": {"status": "failed"}}})
        run_notify(tmp_path, "failed", bin_dir=bin_dir, webhook_url=SECRET_URL)
        assert read_state(tmp_path) == "failed"
        assert list(tmp_path.glob(".last_notified_status.tmp*")) == []

    def test_url_never_logged(self, tmp_path: Path) -> None:
        """The webhook URL may embed a secret token; it must never appear in
        notify.sh's own log output (curl gets it as an argv, but our logs
        must not echo it)."""
        bin_dir = tmp_path / "bin"
        make_curl_shim(bin_dir, exit_code=0)
        write_summary(tmp_path, {"services": {"nova": {"status": "failed"}}})
        result = run_notify(tmp_path, "failed", bin_dir=bin_dir, webhook_url=SECRET_URL)
        assert "SECRET-TOKEN" not in result.stdout
        assert "SECRET-TOKEN" not in result.stderr

    def test_whitespace_in_state_file_is_tolerated(self, tmp_path: Path) -> None:
        """A state file with stray whitespace/newlines should still be parsed
        as a clean status (no spurious transition)."""
        bin_dir = tmp_path / "bin"
        argv_file, _ = make_curl_shim(bin_dir)
        (tmp_path / ".last_notified_status").write_text("  failed \n")
        write_summary(tmp_path, {"services": {"nova": {"status": "failed"}}})
        run_notify(tmp_path, "failed", bin_dir=bin_dir, webhook_url=SECRET_URL)
        assert not argv_file.exists(), "failed==failed after trimming => no send"
