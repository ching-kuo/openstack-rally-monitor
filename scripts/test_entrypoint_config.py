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


@pytest.mark.parametrize(
    ("minutes", "seconds"),
    [("1", "60"), ("15", "900"), ("60", "3600"), ("61", "7200"), ("90", "7200"),
     ("1439", "86400"), ("1440", "86400")],
)
def test_cron_spacing_seconds_is_the_longest_cron_gap(minutes: str, seconds: str) -> None:
    """Same values as exporter metrics_common.cron_spacing_seconds: below an
    hour the step itself, from an hour up the rounded-up whole hours."""
    result = run_function("cron_spacing_seconds", minutes)

    assert result.returncode == 0
    assert result.stdout == f"{seconds}\n"
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


@pytest.mark.parametrize(("raw", "expected"), [("300", "300\n"), ("1", "1\n"), ("3600", "3600\n"), ("0090", "90\n")])
def test_normalize_timeout_accepts_seconds(raw: str, expected: str) -> None:
    result = run_function("normalize_timeout", raw)
    assert result.returncode == 0
    assert result.stdout == expected


@pytest.mark.parametrize("raw", ["0", "3601", "5m", "", "-1"])
def test_normalize_timeout_rejects_out_of_range(raw: str) -> None:
    result = run_function("normalize_timeout", raw)
    assert result.returncode != 0
    assert result.stdout == ""


@pytest.mark.parametrize(("raw", "expected"), [("7", "7\n"), ("030", "30\n"), ("36500", "36500\n")])
def test_normalize_days_accepts_decimal_days(raw: str, expected: str) -> None:
    result = run_function("normalize_days", raw)
    assert result.returncode == 0
    assert result.stdout == expected


@pytest.mark.parametrize("raw", ["30d", "", "-1", "1e3"])
def test_normalize_days_rejects_malformed(raw: str) -> None:
    result = run_function("normalize_days", raw)
    assert result.returncode != 0
    assert result.stdout == ""


def run_snippet(snippet: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, "-c", f'source "$1"; {snippet}', "entrypoint-config-test", str(CONFIG)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **env},
        check=False,
    )


def test_or_default_falls_back_with_a_warning() -> None:
    result = run_snippet('or_default normalize_days "30d" 30 UPTIME_WINDOW_DAYS', {})
    assert result.returncode == 0
    assert result.stdout == "30\n"
    assert "UPTIME_WINDOW_DAYS" in result.stderr and "using 30" in result.stderr


def test_or_default_passes_valid_values_through() -> None:
    result = run_snippet('or_default normalize_days "014" 30 UPTIME_WINDOW_DAYS', {})
    assert result.returncode == 0
    assert result.stdout == "14\n"
    assert result.stderr == ""


CREDS = {
    "OS_AUTH_URL": "https://keystone.example.net:5000/v3",
    "OS_USERNAME": "monitor",
    "OS_PASSWORD": "s3cret-value",
    "OS_PROJECT_NAME": "monitoring",
}


def test_check_os_credentials_accepts_complete_set() -> None:
    result = run_snippet("check_os_credentials", CREDS)
    assert result.returncode == 0
    assert result.stderr == ""


def test_check_os_credentials_names_every_missing_variable() -> None:
    env = {**CREDS, "OS_PASSWORD": "", "OS_PROJECT_NAME": ""}
    result = run_snippet("check_os_credentials", env)
    assert result.returncode != 0
    assert "OS_PASSWORD" in result.stderr and "OS_PROJECT_NAME" in result.stderr
    assert "OS_USERNAME" not in result.stderr


def test_check_os_credentials_warns_on_env_sample_placeholders_without_leaking() -> None:
    env = {
        **CREDS,
        "OS_AUTH_URL": "https://your-openstack-api:5000/v3",
        "OS_PASSWORD": "changeme",
    }
    result = run_snippet("check_os_credentials", env)
    assert result.returncode == 0
    assert "WARNING" in result.stderr
    assert "OS_AUTH_URL" in result.stderr and "OS_PASSWORD" in result.stderr
    assert "changeme" not in result.stderr


def test_entrypoint_checks_credentials_and_days_before_initialization() -> None:
    source = ENTRYPOINT.read_text()
    initialization = source.index("# Initialize")
    assert source.index("check_os_credentials") < initialization
    for name, fn in (("UPTIME_WINDOW_DAYS", "normalize_days"),
                     ("RALLY_RESULTS_RETENTION_DAYS", "normalize_retention_days"),
                     ("PROVENANCE_RETENTION_DAYS", "normalize_provenance_days")):
        # or_default: a day count is never fatal (warn + default/clamp).
        assert source.index(f'{name}=$(or_default {fn}') < initialization


def test_retention_days_accept_zero() -> None:
    """RALLY_RESULTS_RETENTION_DAYS=0 (prune everything older than a day)
    worked before validation existed; it must not start a crash loop."""
    result = run_function("normalize_retention_days", "0")
    assert result.returncode == 0
    assert result.stdout == "0\n"


def test_provenance_zero_is_clamped_to_one_day_with_a_warning() -> None:
    """0 would erase the RGW auto-purge authorization ledger on every run, so
    it is raised to 1 (loudly) rather than refused (it was accepted before)."""
    result = run_function("normalize_provenance_days", "0")
    assert result.returncode == 0
    assert result.stdout == "1\n"
    assert "PROVENANCE_RETENTION_DAYS" in result.stderr


@pytest.mark.parametrize(
    ("fn", "raw", "expected"),
    [
        # 0 used to run (an empty window: null uptime); keep running, loudly.
        ("normalize_days", "0", "1\n"),
        # Huge values used to run too; clamp instead of crash-looping.
        ("normalize_days", "99999", "36500\n"),
        ("normalize_days", "123456789012345678901234567890", "36500\n"),
        ("normalize_retention_days", "99999", "36500\n"),
        ("normalize_provenance_days", "99999", "36500\n"),
    ],
)
def test_out_of_range_day_counts_are_clamped_with_a_warning(fn: str, raw: str, expected: str) -> None:
    result = run_function(fn, raw)
    assert result.returncode == 0
    assert result.stdout == expected
    assert "WARNING" in result.stderr


def test_or_default_day_count_is_never_fatal() -> None:
    result = run_snippet('or_default normalize_days "abc" 30 UPTIME_WINDOW_DAYS', {})
    assert result.returncode == 0
    assert result.stdout == "30\n"


def test_entrypoint_dashboard_links_never_dereference_their_destination() -> None:
    """Root creates the /dashboard links. Plain `ln -sf` follows a destination
    that is already a symlink to a directory and creates the new link INSIDE
    it, i.e. root writing into the rally-writable /results. `-T` (GNU; the
    image is Debian) treats the destination as a plain name, `-n` never
    dereferences it."""
    links = [
        line.strip()
        for line in ENTRYPOINT.read_text().splitlines()
        if line.strip().startswith("ln ")
    ]
    assert len(links) >= 8
    for line in links:
        flags = line.split()[1]
        assert flags.startswith("-") and {"s", "n", "T"} <= set(flags[1:]), line


def _gnu_ln() -> str | None:
    for name in ("ln", "gln"):
        path = shutil.which(name)
        if path and "GNU" in subprocess.run(
            [path, "--version"], capture_output=True, text=True, check=False
        ).stdout:
            return path
    return None


@pytest.mark.skipif(_gnu_ln() is None, reason="GNU ln (as in the Debian image) not available")
def test_ln_sfnT_replaces_a_link_to_a_directory_instead_of_writing_into_it(
    tmp_path: Path,
) -> None:
    """The flag semantics the entrypoint relies on (runs in CI, ubuntu)."""
    squatted = tmp_path / "results" / "results.json"
    squatted.mkdir(parents=True)  # rally turned the target into a directory
    link = tmp_path / "results.json"
    link.symlink_to(squatted)  # the link from the previous boot

    result = subprocess.run(
        [_gnu_ln(), "-sfnT", str(squatted), str(link)], capture_output=True, check=False
    )
    assert result.returncode == 0
    assert link.is_symlink()
    assert list(squatted.iterdir()) == []
