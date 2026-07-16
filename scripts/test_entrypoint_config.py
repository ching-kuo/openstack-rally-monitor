"""Contract tests for numeric configuration used by scripts/entrypoint.sh."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


BASH = shutil.which("bash") or "/bin/bash"
SCRIPTS = Path(__file__).resolve().parent
CONFIG = SCRIPTS / "entrypoint_config.sh"
ENTRYPOINT = SCRIPTS / "entrypoint.sh"


def run_function(function: str, value: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            BASH,
            "-c",
            f'source "$1"; {function} "$2"',
            "entrypoint-config-test",
            str(CONFIG),
            value,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("minutes", "expression"),
    [
        ("1", "*/1 * * * *"),
        ("15", "*/15 * * * *"),
        ("60", "0 */1 * * *"),
        ("120", "0 */2 * * *"),
        ("1440", "0 0 * * *"),
        ("0060", "0 */1 * * *"),
    ],
)
def test_make_cron_schedule_returns_one_expression(minutes: str, expression: str) -> None:
    result = run_function("make_cron_schedule", minutes)

    assert result.returncode == 0
    assert result.stdout == f"{expression}\n"
    assert result.stderr == ""


def test_non_hour_interval_warns_on_stderr_without_corrupting_expression() -> None:
    result = run_function("make_cron_schedule", "61")

    assert result.returncode == 0
    assert result.stdout == "0 */2 * * *\n"
    assert "Rounding up to 120m" in result.stderr


def test_rounding_to_twenty_four_hours_uses_daily_expression() -> None:
    result = run_function("make_cron_schedule", "1439")

    assert result.returncode == 0
    assert result.stdout == "0 0 * * *\n"
    assert "Rounding up to 1440m" in result.stderr


@pytest.mark.parametrize(
    "value",
    ["", "0", "1441", "-1", "+1", "1.5", "15m", " 15 ", "9" * 100],
)
def test_make_cron_schedule_rejects_invalid_values(value: str) -> None:
    result = run_function("make_cron_schedule", value)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "must be a decimal integer from 1 through 1440" in result.stderr


def test_make_cron_schedule_rejects_shell_like_arithmetic_input(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    value = f'1+arr[$(touch "{marker}")]'

    result = run_function("make_cron_schedule", value)

    assert result.returncode != 0
    assert not marker.exists()


@pytest.mark.parametrize(
    ("value", "normalized"),
    [("1", "1"), ("8080", "8080"), ("65535", "65535"), ("00080", "80")],
)
def test_normalize_port_accepts_and_normalizes_decimal(value: str, normalized: str) -> None:
    result = run_function("normalize_port", value)

    assert result.returncode == 0
    assert result.stdout == f"{normalized}\n"
    assert result.stderr == ""


@pytest.mark.parametrize("value", ["", "0", "65536", "-1", "80/tcp", "8080; id"])
def test_normalize_port_rejects_invalid_values(value: str) -> None:
    result = run_function("normalize_port", value)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "must be a decimal integer from 1 through 65535" in result.stderr


def test_entrypoint_validates_before_initialization() -> None:
    source = ENTRYPOINT.read_text()
    initialization = source.index("# Initialize")

    assert source.index('source "${SCRIPT_DIR}/entrypoint_config.sh"') < initialization
    assert source.index('SCHEDULE_INTERVAL=$(normalize_interval') < initialization
    assert source.index('HEALTH_CHECK_INTERVAL=$(normalize_interval') < initialization
    assert source.index('EXPORTER_PORT=$(normalize_port') < initialization
    assert source.index('DASHBOARD_PORT=$(normalize_port') < initialization
