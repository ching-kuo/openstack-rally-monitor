"""One case table for the three RALLY_SERVICES parsers (KEEP IN SYNC):

- os_helpers.sh::parse_rally_services (bash; drives run_tests.sh's SERVICES),
- api_health_check.py::parse_rally_services (the health checker),
- health_fallback_filter.jq (the all-down document health_check.sh publishes
  when the checker fails).

All three split on commas, strip all whitespace, lowercase, drop empties,
dedupe preserving first-seen order, drop tokens failing ^[a-z0-9_-]+$
(path-traversal hardening) and fall back to the default set when nothing
survives. The one intended difference: the health parsers (Python and jq)
always put keystone first, because the health check authenticates regardless;
the bash parser keeps the operator's order verbatim. Each case states the
bash result and the health parsers' result is derived by hoisting keystone.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import api_health_check as hc
from shell_testlib import BASE_PATH, BASH

SCRIPTS = Path(__file__).resolve().parent
OS_HELPERS = SCRIPTS / "os_helpers.sh"
RUN_TESTS = SCRIPTS / "run_tests.sh"
FALLBACK_FILTER = SCRIPTS / "health_fallback_filter.jq"
DEFAULT = ["keystone", "nova", "neutron", "glance", "cinder", "swift"]

# (raw RALLY_SERVICES or None for unset, bash parser result)
CASES = [
    pytest.param(None, DEFAULT, id="unset"),
    pytest.param("", DEFAULT, id="empty"),
    pytest.param("   ", DEFAULT, id="blank"),
    pytest.param(",", DEFAULT, id="lone-comma"),
    pytest.param(" , , ", DEFAULT, id="commas-and-spaces"),
    pytest.param(",,,", DEFAULT, id="commas-only"),
    pytest.param("  Nova , KEYSTONE , nova ,, Cinder , GLANCE ",
                 ["nova", "keystone", "cinder", "glance"],
                 id="trim-lowercase-dedupe-keep-order"),
    pytest.param("no va, cind er", ["nova", "cinder"], id="internal-whitespace"),
    pytest.param("no va", ["nova"], id="internal-space-kept-after-strip"),
    pytest.param("octavia,nova", ["octavia", "nova"], id="unknown-name-kept"),
    pytest.param("swift,nova,keystone", ["swift", "nova", "keystone"],
                 id="operator-order-keystone-last"),
    pytest.param("nova,neutron", ["nova", "neutron"], id="keystone-absent"),
    pytest.param("nova,keystone,neutron", ["nova", "keystone", "neutron"],
                 id="keystone-in-middle"),
    pytest.param("glance", ["glance"], id="single-service"),
    pytest.param("nova,../health,glance", ["nova", "glance"], id="path-traversal-dropped"),
    pytest.param("nova;rm,glance", ["glance"], id="shell-metachar-dropped"),
    pytest.param(" Nova , ../x, SWIFT , nova ", ["nova", "swift"],
                 id="mixed-valid-invalid-and-duplicate"),
    pytest.param("../a,b/c,../../d", DEFAULT, id="all-invalid-falls-back"),
    pytest.param("../etc, b/c, ;rm", DEFAULT, id="all-invalid-with-spaces-falls-back"),
]


def hoist_keystone(names: list[str]) -> list[str]:
    """The health parsers' view of a bash result: keystone first, once."""
    return ["keystone"] + [n for n in names if n != "keystone"]


def parse_bash(raw: str | None) -> list[str]:
    result = subprocess.run(
        [BASH, "-c", f'source "{OS_HELPERS}" && parse_rally_services "$1"', "_", raw or ""],
        env={"PATH": BASE_PATH},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return [line for line in result.stdout.splitlines() if line]


def run_fallback_filter(raw: str | None) -> dict:
    """Apply health_fallback_filter.jq the way health_check.sh does."""
    result = subprocess.run(
        ["jq", "-n", "-f", str(FALLBACK_FILTER),
         "--arg", "ts", "2026-06-12T00:00:00Z",
         "--arg", "raw", raw or "",
         "--arg", "default", ",".join(DEFAULT)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_bash_parser(raw: str | None, expected: list[str]) -> None:
    assert parse_bash(raw) == expected


def test_run_tests_sources_the_normalized_set(tmp_path: Path) -> None:
    """End to end: run_tests.sh builds its SERVICES array from RALLY_SERVICES
    through the bash parser when sourced."""
    result = subprocess.run(
        [BASH, "-c", f'source "{RUN_TESTS}" && printf "%s\\n" "${{SERVICES[@]}}"'],
        env={"PATH": BASE_PATH, "RESULTS_DIR": str(tmp_path),
             "RALLY_SERVICES": " Nova ,../etc,KEYSTONE"},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["nova", "keystone"]


@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_python_parser(raw: str | None, expected: list[str]) -> None:
    assert list(hc.parse_rally_services(raw)) == hoist_keystone(expected)


@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_jq_fallback_parser(raw: str | None, expected: list[str]) -> None:
    assert list(run_fallback_filter(raw)["services"]) == hoist_keystone(expected)


def test_python_parser_warns_on_dropped_token(capsys: pytest.CaptureFixture[str]) -> None:
    hc.parse_rally_services("nova,../health,glance")
    assert "dropping invalid service name" in capsys.readouterr().err


def test_python_parser_custom_default() -> None:
    assert hc.parse_rally_services(None, default=("keystone", "nova")) == ("keystone", "nova")


def test_fallback_document_reports_every_service_down() -> None:
    doc = run_fallback_filter("")
    assert doc["overall"] == "down"
    assert all(v["status"] == "down" and v["latency_ms"] == 0
               for v in doc["services"].values())
