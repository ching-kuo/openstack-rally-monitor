"""Run the promtool unit tests for prometheus/rally_alerts.yml.

Skipped when promtool is not on PATH (it is not installed in CI); run locally
with `promtool test rules prometheus/rally_alerts_test.yml`.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

PROMETHEUS_DIR = Path(__file__).resolve().parent.parent / "prometheus"
PROMTOOL = shutil.which("promtool")

pytestmark = pytest.mark.skipif(PROMTOOL is None, reason="promtool not installed")


def run_promtool(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PROMTOOL, *args], cwd=PROMETHEUS_DIR, capture_output=True, text=True, check=False
    )


def test_rules_are_valid():
    result = run_promtool("check", "rules", "rally_alerts.yml")
    assert result.returncode == 0, result.stdout + result.stderr


def test_rule_unit_tests_pass():
    result = run_promtool("test", "rules", "rally_alerts_test.yml")
    assert result.returncode == 0, result.stdout + result.stderr
