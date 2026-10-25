"""Node-driven contract tests for pure helpers in dashboard/report.js.

report.js calls initReport() at load time (touching document), so it cannot be
required directly under node. We extract the self-contained pure helpers by
their source markers and evaluate them in isolation -- the same technique as
test_getrunstatus.py. This pins:

  - scenarioStatus: a scenario is "passed" only with iterations>0, zero errors,
    and sla_passed != false (mirrors the all-green intent in build_summary).
  - groupScenarios: scenarios group by class in first-seen order, carrying their
    original index so the nav can route to the right scenario.
  - formatSeconds / atomicBars: numeric formatting and chart-data shaping.

If node is unavailable the tests skip rather than fail.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPORT_JS = Path(__file__).resolve().parent / "report.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not available")


def _extract(func_name: str) -> str:
    src = REPORT_JS.read_text()
    marker = f"function {func_name}("
    start = src.index(marker)
    depth = 0
    i = src.index("{", start)
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    raise AssertionError(f"could not brace-match {func_name}")


def _run_node(snippet: str) -> str:
    result = subprocess.run([NODE, "-e", snippet], capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"node failed:\n{result.stderr}"
    return result.stdout.strip()


def _eval(func_name: str, call: str) -> str:
    fn = _extract(func_name)
    return _run_node(f"{fn}\nprocess.stdout.write(String({call}));")


def _eval_json(func_name: str, call: str):
    fn = _extract(func_name)
    return json.loads(_run_node(f"{fn}\nprocess.stdout.write(JSON.stringify({call}));"))


# --- scenarioStatus ---------------------------------------------------------

def test_scenario_status_passed():
    assert _eval("scenarioStatus", 'scenarioStatus({iterations:2,errors:0,sla_passed:true})') == "passed"


def test_scenario_status_failed_on_errors():
    assert _eval("scenarioStatus", 'scenarioStatus({iterations:2,errors:1,sla_passed:true})') == "failed"


def test_scenario_status_failed_on_sla():
    assert _eval("scenarioStatus", 'scenarioStatus({iterations:2,errors:0,sla_passed:false})') == "failed"


def test_scenario_status_failed_on_zero_iterations():
    assert _eval("scenarioStatus", 'scenarioStatus({iterations:0,errors:0,sla_passed:true})') == "failed"


def test_scenario_status_failed_on_nullish():
    assert _eval("scenarioStatus", "scenarioStatus(null)") == "failed"


# --- groupScenarios ---------------------------------------------------------

def test_group_scenarios_orders_and_indexes():
    scenarios = (
        '[{"cls":"NovaFlavors","method":"list_flavors"},'
        '{"cls":"NovaServers","method":"boot_and_delete"},'
        '{"cls":"NovaFlavors","method":"create_flavor"}]'
    )
    groups = _eval_json("groupScenarios", f"groupScenarios({scenarios})")
    assert [g["cls"] for g in groups] == ["NovaFlavors", "NovaServers"]
    # First-seen class order; scenarios keep their original payload index.
    flavors = groups[0]["scenarios"]
    assert [s["method"] for s in flavors] == ["list_flavors", "create_flavor"]
    assert [s["_idx"] for s in flavors] == [0, 2]
    assert groups[1]["scenarios"][0]["_idx"] == 1


def test_group_scenarios_defaults_missing_class():
    groups = _eval_json("groupScenarios", 'groupScenarios([{"method":"x"}])')
    assert groups[0]["cls"] == "unknown"


# --- formatSeconds / atomicBars --------------------------------------------

def test_format_seconds():
    assert _eval("formatSeconds", "formatSeconds(2.2614)") == "2.261 s"
    assert _eval("formatSeconds", 'formatSeconds("nope")') == "0.000 s"


def test_atomic_bars_shapes_chart_data():
    atomics = '[{"name":"nova.boot","avg":20.5},{"name":"nova.delete","avg":9}]'
    out = _eval_json("atomicBars", f"atomicBars({atomics})")
    assert out["labels"] == ["nova.boot", "nova.delete"]
    assert out["data"] == [20.5, 9]


# --- errorHeading -----------------------------------------------------------

def test_error_heading_is_generic_with_code():
    assert _eval("errorHeading", 'errorHeading({error_code: "task_failed_to_start", error: "x"})') == (
        "Service error: task_failed_to_start"
    )


def test_error_heading_without_code():
    assert _eval("errorHeading", 'errorHeading({error: "x"})') == "Service error"


# --- routing: parseHash / selectionHash / initialSelection -----------------

def test_parse_hash_routes_known_views():
    out = _eval_json(
        "parseHash",
        '["#overview","#input","#scenario=1","#scenario=2","#scenario=x","","#junk"]'
        ".map((h) => parseHash(h, 2))",
    )
    assert out == [
        {"view": "overview"},
        {"view": "input"},
        {"view": "scenario", "idx": 1},
        None,  # out of range for 2 scenarios
        None,
        None,
        None,
    ]


def test_selection_hash_round_trips_through_parse_hash():
    fns = _extract("parseHash") + "\n" + _extract("selectionHash")
    out = json.loads(_run_node(
        f"{fns}\nconst sels=[{{view:'overview'}},{{view:'input'}},{{view:'scenario',idx:3}}];"
        "process.stdout.write(JSON.stringify(sels.map((s) => parseHash(selectionHash(s), 5))));"
    ))
    assert out == [{"view": "overview"}, {"view": "input"}, {"view": "scenario", "idx": 3}]


def _initial(scenarios_js: str):
    fns = _extract("scenarioStatus") + "\n" + _extract("initialSelection")
    return json.loads(_run_node(
        f"{fns}\nprocess.stdout.write(JSON.stringify(initialSelection({scenarios_js})));"
    ))


def test_initial_selection_opens_first_failed_scenario():
    scenarios = (
        "[{iterations:1,errors:0,sla_passed:true},"
        "{iterations:1,errors:2,sla_passed:true},"
        "{iterations:1,errors:0,sla_passed:false}]"
    )
    assert _initial(scenarios) == {"view": "scenario", "idx": 1}


def test_initial_selection_is_overview_when_all_passed_or_empty():
    assert _initial("[{iterations:1,errors:0,sla_passed:true}]") == {"view": "overview"}
    assert _initial("[]") == {"view": "overview"}


def test_route_for_falls_back_to_initial_selection():
    """Initial load and hashchange share one fallback, so Back to the original
    (hashless) entry shows the same view the page opened on."""
    fns = "\n".join(_extract(n) for n in ("scenarioStatus", "parseHash", "initialSelection", "routeFor"))
    out = json.loads(_run_node(
        f"{fns}\nconst sc=[{{iterations:1,errors:0}},{{iterations:1,errors:1}}];"
        "process.stdout.write(JSON.stringify([routeFor('', sc), routeFor('#input', sc),"
        " routeFor('#scenario=9', sc)]));"
    ))
    assert out == [
        {"view": "scenario", "idx": 1},
        {"view": "input"},
        {"view": "scenario", "idx": 1},
    ]
