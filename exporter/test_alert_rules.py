"""Run the promtool unit tests for prometheus/rally_alerts.yml.

CI installs a pinned, checksum-verified promtool (build-push.yml), and with the
CI environment variable set (to anything but "", "0" or "false") a missing
promtool fails instead of skipping, so the alert rules can never go untested
there. Locally the suite is skipped when promtool is not on PATH; run it with
`promtool test rules prometheus/rally_alerts_test.yml`.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROMETHEUS_DIR = Path(__file__).resolve().parent.parent / "prometheus"
PROMTOOL = shutil.which("promtool")
IN_CI = os.environ.get("CI", "").strip().lower() not in ("", "0", "false")

pytestmark = pytest.mark.skipif(
    PROMTOOL is None and not IN_CI, reason="promtool not installed"
)


def run_promtool(*args: str) -> subprocess.CompletedProcess:
    assert PROMTOOL is not None, "promtool must be on PATH in CI"
    return subprocess.run(
        [PROMTOOL, *args], cwd=PROMETHEUS_DIR, capture_output=True, text=True, check=False
    )


def test_rules_are_valid():
    result = run_promtool("check", "rules", "rally_alerts.yml")
    assert result.returncode == 0, result.stdout + result.stderr


def test_rule_unit_tests_pass():
    result = run_promtool("test", "rules", "rally_alerts_test.yml")
    assert result.returncode == 0, result.stdout + result.stderr
