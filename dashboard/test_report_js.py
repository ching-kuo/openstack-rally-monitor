"""Node-driven contract tests for pure helpers in dashboard/report.js.

report.js calls initReport() at load time (touching document), so it cannot be
required directly under node. We extract the self-contained pure helpers by
their source (node_testlib.extract_function) and evaluate them in isolation.
This pins:

  - scenarioStatus: a scenario is "passed" only with iterations>0, zero errors,
    and sla_passed != false (mirrors the all-green intent in build_summary).
  - groupScenarios: scenarios group by class in first-seen order, carrying their
    original index so the nav can route to the right scenario.
  - formatSeconds / atomicBars: numeric formatting and chart-data shaping.

If node is unavailable the tests skip rather than fail.
"""
import json
from pathlib import Path

from node_testlib import assert_cases, extract_function, requires_node, run_script

REPORT_JS = Path(__file__).resolve().parent / "report.js"

pytestmark = requires_node


def _extract(*func_names: str) -> str:
    return "\n".join(extract_function(REPORT_JS, name) for name in func_names)


def _eval(func_name: str, call: str) -> str:
    return run_script(f"{_extract(func_name)}\nprocess.stdout.write(String({call}));")


def _eval_json(func_names: str | tuple[str, ...], call: str):
    names = (func_names,) if isinstance(func_names, str) else func_names
    return json.loads(
        run_script(f"{_extract(*names)}\nprocess.stdout.write(JSON.stringify({call}));")
    )


# --- scenarioStatus ---------------------------------------------------------

SCENARIO_STATUS_CASES = [
    ("passed", "{iterations:2,errors:0,sla_passed:true}", "passed"),
    ("failed-on-errors", "{iterations:2,errors:1,sla_passed:true}", "failed"),
    ("failed-on-sla", "{iterations:2,errors:0,sla_passed:false}", "failed"),
    ("failed-on-zero-iterations", "{iterations:0,errors:0,sla_passed:true}", "failed"),
    ("failed-on-nullish", "null", "failed"),
]


def test_scenario_status():
    """All cases in one node run; the ids keep a failure readable."""
    args = ",".join(arg for _, arg, _ in SCENARIO_STATUS_CASES)
    out = _eval_json("scenarioStatus", f"[{args}].map((s) => scenarioStatus(s))")
    assert_cases([(case_id, expected) for case_id, _, expected in SCENARIO_STATUS_CASES], out)


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
    out = _eval_json(
        ("parseHash", "selectionHash"),
        "[{view:'overview'},{view:'input'},{view:'scenario',idx:3}]"
        ".map((s) => parseHash(selectionHash(s), 5))",
    )
    assert out == [{"view": "overview"}, {"view": "input"}, {"view": "scenario", "idx": 3}]


def _initial(scenarios_js: str):
    return _eval_json(("scenarioStatus", "initialSelection"),
                      f"initialSelection({scenarios_js})")


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
    out = _eval_json(
        ("scenarioStatus", "parseHash", "initialSelection", "routeFor"),
        "((sc) => [routeFor('', sc), routeFor('#input', sc), routeFor('#scenario=9', sc)])"
        "([{iterations:1,errors:0},{iterations:1,errors:1}])",
    )
    assert out == [
        {"view": "scenario", "idx": 1},
        {"view": "input"},
        {"view": "scenario", "idx": 1},
    ]
