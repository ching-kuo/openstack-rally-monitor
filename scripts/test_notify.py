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
import subprocess
from pathlib import Path
from typing import Callable, NamedTuple

import pytest

from shell_testlib import BASE_PATH, BASH, write_curl_config_shim

SCRIPT = Path(__file__).resolve().parent / "notify.sh"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

class CurlShim(NamedTuple):
    bin_dir: Path
    argv: Path    # curl's argv (the webhook URL never travels here)
    config: Path  # its -K config, which carries the URL
    stdin: Path   # the posted payload


@pytest.fixture
def make_curl(tmp_path: Path) -> Callable[..., CurlShim]:
    """Write a fake `curl` into tmp_path/bin that records argv, its -K config
    and stdin, and exits with exit_code."""
    def _make(exit_code: int = 0) -> CurlShim:
        bin_dir = tmp_path / "bin"
        shim = CurlShim(bin_dir, bin_dir / "curl_argv.txt",
                        bin_dir / "curl_config.txt", bin_dir / "curl_stdin.txt")
        write_curl_config_shim(bin_dir, shim.argv, shim.config,
                               f'cat > "{shim.stdin}"\nexit {exit_code}\n')
        return shim
    return _make


@pytest.fixture
def curl(make_curl: Callable[..., CurlShim]) -> CurlShim:
    """A fake curl that succeeds."""
    return make_curl()


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
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{bin_dir}:{BASE_PATH}" if bin_dir else BASE_PATH,
        "RESULTS_DIR": str(tmp_path),
    }
    if webhook_url is not None:
        env["NOTIFY_WEBHOOK_URL"] = webhook_url
    if notify_format is not None:
        env["NOTIFY_FORMAT"] = notify_format
    if dashboard_url is not None:
        env["NOTIFY_DASHBOARD_URL"] = dashboard_url
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


def payload(shim: CurlShim) -> dict:
    return json.loads(shim.stdin.read_text())


SECRET_URL = "https://hooks.example.com/services/SECRET-TOKEN-abc123"
FAILED_NOVA = {"services": {"nova": {"status": "failed"}}}


# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------

class TestNoOp:
    def test_unset_url_is_silent_noop(self, tmp_path: Path, curl: CurlShim) -> None:
        write_summary(tmp_path, FAILED_NOVA)
        # No NOTIFY_WEBHOOK_URL passed.
        result = run_notify(tmp_path, "failed", bin_dir=curl.bin_dir)
        assert result.returncode == 0, result.stderr
        assert not curl.argv.exists(), "curl must not be invoked without a URL"
        assert read_state(tmp_path) is None

    @pytest.mark.parametrize(("state", "status", "expected_state"), [
        # Missing state baselines to 'passed', so a first-ever green run is
        # passed->passed: no transition, and no state is written.
        pytest.param(None, "passed", None, id="first-run-green"),
        pytest.param("failed\n", "failed", "failed", id="repeat-failed"),
        # Stray whitespace is trimmed: failed == failed, no spurious send.
        pytest.param("  failed \n", "failed", "failed", id="whitespace-in-state-file"),
    ])
    def test_no_transition_does_not_notify(
        self, tmp_path: Path, curl: CurlShim, state: str | None, status: str,
        expected_state: str | None,
    ) -> None:
        if state is not None:
            (tmp_path / ".last_notified_status").write_text(state)
        write_summary(tmp_path, {"services": {"nova": {"status": status}}})
        result = run_notify(tmp_path, status, bin_dir=curl.bin_dir, webhook_url=SECRET_URL)
        assert result.returncode == 0, result.stderr
        assert not curl.argv.exists(), "no transition => no curl"
        assert read_state(tmp_path) == expected_state

    def test_invalid_status_argument_rejected(self, tmp_path: Path) -> None:
        result = run_notify(tmp_path, "bogus", webhook_url=SECRET_URL)
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# Transition cases
# ---------------------------------------------------------------------------

class TestTransitions:
    def test_first_run_failed_notifies(self, tmp_path: Path, curl: CurlShim) -> None:
        """Missing state baselines to 'passed', so a first-ever failed run is a
        passed->failed transition and must notify. The new state is written
        atomically (no tmp leftover)."""
        write_summary(tmp_path, FAILED_NOVA)
        result = run_notify(tmp_path, "failed", bin_dir=curl.bin_dir, webhook_url=SECRET_URL)
        assert result.returncode == 0, result.stderr
        assert curl.argv.exists(), "curl must fire on first failed run"
        assert read_state(tmp_path) == "failed"
        assert list(tmp_path.glob(".last_notified_status.tmp*")) == []

    def test_recovery_failed_to_passed_notifies(self, tmp_path: Path, curl: CurlShim) -> None:
        (tmp_path / ".last_notified_status").write_text("failed\n")
        write_summary(tmp_path, {"services": {"nova": {"status": "passed"}}})
        result = run_notify(tmp_path, "passed", bin_dir=curl.bin_dir, webhook_url=SECRET_URL)
        assert result.returncode == 0, result.stderr
        sent = payload(curl)
        assert sent["status"] == "passed"
        assert sent["previous_status"] == "failed"
        assert read_state(tmp_path) == "passed"


# ---------------------------------------------------------------------------
# Payload contract
# ---------------------------------------------------------------------------

class TestPayload:
    def test_generic_payload_is_valid_json_with_expected_fields(
        self, tmp_path: Path, curl: CurlShim,
    ) -> None:
        write_summary(tmp_path, {
            "timestamp": "20260612T000000Z",
            "services": {
                "keystone": {"status": "passed"},
                "nova": {"status": "failed"},
                "swift": {"status": "skipped"},
            },
        })
        run_notify(
            tmp_path, "failed", bin_dir=curl.bin_dir, webhook_url=SECRET_URL,
            dashboard_url="https://dash.example",
        )
        sent = payload(curl)
        assert sent["event"] == "rally_status_change"
        assert sent["status"] == "failed"
        assert sent["previous_status"] == "passed"
        assert sent["error"] is None
        assert sent["dashboard_url"] == "https://dash.example"
        # failed_services = every non-passed service (failed + skipped).
        assert set(sent["failed_services"]) == {"nova", "swift"}
        # timestamp is ISO-8601 UTC-ish.
        assert sent["timestamp"].endswith("Z")

    def test_dashboard_url_omitted_when_unset(self, tmp_path: Path, curl: CurlShim) -> None:
        write_summary(tmp_path, {"services": {"nova": {"status": "failed"}}})
        run_notify(tmp_path, "failed", bin_dir=curl.bin_dir, webhook_url=SECRET_URL)
        sent = payload(curl)
        assert "dashboard_url" not in sent

    def test_deployment_failure_shape_carries_error(self, tmp_path: Path, curl: CurlShim) -> None:
        write_summary(tmp_path, {
            "timestamp": "20260612T000000Z",
            "error": "deployment_setup_failed",
            "services": {},
        })
        run_notify(tmp_path, "failed", bin_dir=curl.bin_dir, webhook_url=SECRET_URL)
        sent = payload(curl)
        assert sent["error"] == "deployment_setup_failed"
        assert sent["failed_services"] == []

    def test_missing_summary_file_still_sends_safe_payload(
        self, tmp_path: Path, curl: CurlShim,
    ) -> None:
        # No summary file written.
        result = run_notify(tmp_path, "failed", bin_dir=curl.bin_dir, webhook_url=SECRET_URL)
        assert result.returncode == 0, result.stderr
        assert curl.argv.exists()
        sent = payload(curl)
        assert sent["failed_services"] == []
        assert sent["error"] is None

    def test_slack_format_wraps_text(self, tmp_path: Path, curl: CurlShim) -> None:
        write_summary(tmp_path, {
            "services": {"nova": {"status": "failed"}, "keystone": {"status": "passed"}},
        })
        run_notify(
            tmp_path, "failed", bin_dir=curl.bin_dir, webhook_url=SECRET_URL,
            notify_format="slack",
        )
        sent = payload(curl)
        assert list(sent.keys()) == ["text"]
        assert isinstance(sent["text"], str)
        assert "nova" in sent["text"]

    def test_discord_format_wraps_content(self, tmp_path: Path, curl: CurlShim) -> None:
        write_summary(tmp_path, {"services": {"nova": {"status": "failed"}}})
        run_notify(
            tmp_path, "failed", bin_dir=curl.bin_dir, webhook_url=SECRET_URL,
            notify_format="discord",
        )
        sent = payload(curl)
        assert list(sent.keys()) == ["content"]
        assert isinstance(sent["content"], str)

    def test_slack_recovery_message_is_human_readable(
        self, tmp_path: Path, curl: CurlShim,
    ) -> None:
        (tmp_path / ".last_notified_status").write_text("failed\n")
        write_summary(tmp_path, {"services": {"nova": {"status": "passed"}}})
        run_notify(
            tmp_path, "passed", bin_dir=curl.bin_dir, webhook_url=SECRET_URL,
            notify_format="slack",
        )
        sent = payload(curl)
        assert "recovered" in sent["text"].lower()


# ---------------------------------------------------------------------------
# Failure handling + state file robustness
# ---------------------------------------------------------------------------

class TestFailureHandling:
    @pytest.mark.parametrize(("exit_code", "state", "expected_state"), [
        pytest.param(22, "passed\n", "passed", id="http-error-keeps-baseline"),  # 22 = curl HTTP error
        pytest.param(7, None, None, id="connect-failure-on-first-run"),          # 7 = connection failed
    ])
    def test_curl_failure_leaves_state_unchanged(
        self, tmp_path: Path, make_curl: Callable[..., CurlShim],
        exit_code: int, state: str | None, expected_state: str | None,
    ) -> None:
        """A non-2xx / network failure (curl exit != 0) must NOT update (or
        create) the state file, so the next run retries the same transition."""
        shim = make_curl(exit_code)
        if state is not None:
            (tmp_path / ".last_notified_status").write_text(state)
        write_summary(tmp_path, FAILED_NOVA)
        result = run_notify(tmp_path, "failed", bin_dir=shim.bin_dir, webhook_url=SECRET_URL)
        # The run itself is unaffected (exit 0); only a warning is logged.
        assert result.returncode == 0, result.stderr
        assert "WARNING" in result.stdout
        assert read_state(tmp_path) == expected_state
        assert list(tmp_path.glob(".last_notified_status.tmp*")) == []

    def test_url_never_logged_nor_in_curl_argv(self, tmp_path: Path, curl: CurlShim) -> None:
        """The webhook URL may embed a secret token: it must never appear in
        notify.sh's own log output, nor in curl's argv (world-readable via
        /proc/<pid>/cmdline); it reaches curl only through its -K config,
        quoted so it cannot end the config string."""
        write_summary(tmp_path, FAILED_NOVA)
        url = 'https://hooks.example.com/x?token=SECRET-TOKEN&q="a\\b"'
        result = run_notify(tmp_path, "failed", bin_dir=curl.bin_dir, webhook_url=url)
        assert result.returncode == 0, result.stderr
        assert "SECRET-TOKEN" not in result.stdout
        assert "SECRET-TOKEN" not in result.stderr
        assert "SECRET-TOKEN" not in curl.argv.read_text()
        assert curl.config.read_text() == (
            'url = "https://hooks.example.com/x?token=SECRET-TOKEN&q=\\"a\\\\b\\""\n'
        )
        assert read_state(tmp_path) == "failed"
