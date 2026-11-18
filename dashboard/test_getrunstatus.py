"""Node-driven contract tests for the dashboard modules' run/service helpers.

Each test imports the functions it needs straight from dashboard/js/ (see
node_testlib.py). This pins:

  - getRunStatus: the deployment-failure shape ({services:{}, error:...}),
    empty-services, and all-skipped runs must classify as "failed" (NOT pass a
    vacuous check and render "All Healthy"), while "skipped" is neutral when at
    least one service passes (shared table: testdata/run_status_cases.json).
  - orderedServiceUnion: the duration chart derives its series from the union
    of all runs (not just the oldest), so a newly-added service appears
    immediately.
  - cleanupBadge: a blind OpenStack orphan scan (openstack_scan_status
    "error") must not render the badge as "OK".

If node is unavailable the tests skip rather than fail (CI images that omit
node still get a green run; the bash-side publish test covers the data path).
"""
import json
import re
from pathlib import Path

import pytest

from node_testlib import FAKE_DOM, JS_DIR, imports, requires_node, run_json, run_module

RUN_STATUS_CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "testdata" / "run_status_cases.json").read_text()
)["cases"]

pytestmark = requires_node


def _call(module: str, name: str, args_js: str) -> str:
    return run_module(
        imports(module, name) + f"process.stdout.write(String({name}({args_js})));"
    )


def _call_json(module: str, name: str, args_js: str):
    return run_json(
        imports(module, name) + f"process.stdout.write(JSON.stringify({name}({args_js})));"
    )


def _eval_getrunstatus(run_obj_js: str) -> str:
    return _call("status", "getRunStatus", run_obj_js)


@pytest.mark.parametrize(
    ("run_js", "expected"),
    [
        # The deployment-failure shape and empty services must not pass the
        # vacuous every(...passed) check.
        ('{"timestamp":"x","services":{},"error":"deployment_setup_failed"}', "failed"),
        ('{"timestamp":"x","services":{}}', "failed"),
        ('{"services":{"nova":{"status":"passed"}}}', "passed"),
        ('{"services":{"nova":{"status":"passed"},"cinder":{"status":"failed"}}}', "failed"),
        # An .error present alongside passing services still classifies failed.
        ('{"services":{"nova":{"status":"passed"}},"error":"boom"}', "failed"),
    ],
    ids=["deployment-failure", "empty-services", "all-passed", "any-failed", "error-overrides"],
)
def test_getrunstatus(run_js, expected):
    assert _eval_getrunstatus(run_js) == expected


@pytest.mark.parametrize(
    "case",
    RUN_STATUS_CASES,
    ids=[c["id"] for c in RUN_STATUS_CASES],
)
def test_getrunstatus_shared_cases(case):
    status = _eval_getrunstatus(json.dumps(case["summary"]))
    assert status == case["dashboard_status"]
    assert (status == "passed") == case["passed"]


def _eval_union(items_js: str) -> list:
    return _call_json("status", "orderedServiceUnion", items_js)


def test_ordered_union_spans_all_runs_not_just_first():
    # Oldest run lacks "swift"; a later run adds it. The union must include it
    # (this is the duration-chart fix: series derived from the union, not run[0]).
    items = (
        '[{"services":{"keystone":{},"nova":{}}},'
        '{"services":{"keystone":{},"nova":{},"swift":{}}}]'
    )
    assert _eval_union(items) == ["keystone", "nova", "swift"]


def test_ordered_union_preferred_order_then_unknowns():
    items = '[{"services":{"octavia":{},"nova":{}}}]'
    # Preferred (nova) first in canonical order; unknown (octavia) trails.
    assert _eval_union(items) == ["nova", "octavia"]


# ---------------------------------------------------------------------------
# serviceErrorText: service-level failure reason shown in the modal
# ---------------------------------------------------------------------------

def _eval_service_error(svc_js: str) -> str:
    return _call("status", "serviceErrorText", svc_js)


def test_service_error_text_task_failed_to_start_uses_detail():
    text = _eval_service_error(
        '{"status":"failed","error":"task_failed_to_start","error_detail":"Task config is invalid"}'
    )
    assert text == "Task failed to start: Task config is invalid"


def test_service_error_text_falls_back_to_error_code():
    assert _eval_service_error('{"status":"failed","error":"task_failed_to_start"}') == (
        "Task failed to start: task_failed_to_start"
    )


def test_service_error_text_empty_without_error():
    assert _eval_service_error('{"status":"passed"}') == ""
    assert _eval_service_error("null") == ""


def _cleanup_badge(cleanup_js: str) -> dict:
    return _call_json("status", "cleanupBadge", cleanup_js)


def test_cleanup_badge_flags_a_blind_openstack_scan():
    # A failed listing reads as 0 orphans; the badge must not say OK.
    assert _cleanup_badge(
        '{"cleanup_failed":0,"details":{},"openstack_scan_status":"error"}'
    ) == {"text": "SCAN ERROR", "cls": "error"}
    assert _cleanup_badge('{"cleanup_failed":0,"details":{}}') == {"text": "OK", "cls": "ok"}
    assert _cleanup_badge(
        '{"cleanup_failed":1,"details":{},"openstack_scan_status":"error"}'
    ) == {"text": "ISSUES", "cls": "error"}


# ---------------------------------------------------------------------------
# Status whitelist + relative data paths
# ---------------------------------------------------------------------------

def test_known_status_maps_unrecognized_values_to_unknown():
    out = run_module(
        imports("status", "knownStatus")
        + "const A=['passed','failed'];"
        "process.stdout.write([knownStatus('passed',A), knownStatus('x\" onclick=\"y',A),"
        " knownStatus(undefined,A)].join('|'));"
    )
    assert out == "passed|unknown|unknown"


def test_service_cards_never_interpolate_raw_status():
    """A hostile status must not reach a class name or the chip text."""
    hostile = '"><img src=x onerror=alert(1)>'
    js = (
        FAKE_DOM
        + imports("services", "renderServiceCards")
        + f"const bad = {json.dumps(hostile)};"
        "renderServiceCards({timestamp: 't', services: {nova: {status: bad}}},"
        " {runs: [{services: {nova: {status: bad}}}]},"
        " {services: {nova: {status: bad, latency_ms: 1}}});"
        "const card = els.servicesGrid.children[0];"
        "process.stdout.write(card.className + '\\n' + card.innerHTML);"
    )
    out = run_module(js)
    assert "<img" not in out
    assert out.splitlines()[0] == "service-card status-unknown"
    assert '<span class="status-chip unknown">unknown</span>' in out


def test_data_fetches_use_relative_paths():
    """Absolute /x.json paths break behind a sub-path reverse proxy."""
    for module in JS_DIR.glob("*.js"):
        assert re.findall(r"""fetch(?:Json)?\(\s*["'`]/""", module.read_text()) == [], module


def _health_detail(status: str, services_js: str) -> str:
    return _call("health", "healthCellDetail", f"{json.dumps(status)}, {services_js}")


def test_health_cell_detail_unknown_is_not_all_up():
    assert _health_detail("unknown", "{}") == "Status unknown"
    assert _health_detail("up", "{}") == "All services up"
    assert _health_detail("down", '{"nova":{"status":"down"}}') == "Down: nova"


def test_format_duration_tolerates_non_numbers():
    """A string/garbage duration used to throw in .toFixed and abort the whole
    card loop (or the modal)."""
    out = run_module(
        imports("format", "formatDuration")
        + "process.stdout.write([formatDuration('12.5'), formatDuration('x'),"
        " formatDuration(undefined), formatDuration(0.25), formatDuration(90)].join('|'));"
    )
    assert out == "12.5s|—|—|250ms|1.5m"
