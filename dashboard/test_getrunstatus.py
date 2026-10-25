"""Node-driven contract tests for pure helpers in dashboard/app.js.

app.js calls startDashboard() at load time (touching `document`/`fetch`), so it
cannot be `require`d directly under node. Instead we extract the two pure
functions under test by their source markers, evaluate them in an isolated node
context, and assert on their return values. This pins:

  - getRunStatus: the deployment-failure shape ({services:{}, error:...}) and
    the empty-services shape must classify as "failed", NOT pass the vacuous
    `every(...passed)` check and render "All Healthy".
  - orderedServiceUnion: the duration chart now derives its series from the
    union of all runs (not just the oldest), so a newly-added service appears
    immediately.
  - renderCleanup (with a stub `document`): a blind OpenStack orphan scan
    (openstack_scan_status "error") must not render the badge as "OK".

If node is unavailable the tests skip rather than fail (CI images that omit
node still get a green run; the bash-side publish test covers the data path).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parent / "app.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not available")


def _extract(func_name: str) -> str:
    """Return the source of a top-level `function <name>(...) { ... }` block.

    Brace-matches from the function keyword so nested braces are handled. The
    helpers under test are self-contained (no closure over module state), so the
    extracted text is directly evaluable.
    """
    src = APP_JS.read_text()
    marker = f"function {func_name}("
    start = src.index(marker)
    depth = 0
    i = src.index("{", start)
    body_start = i
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
    result = subprocess.run(
        [NODE, "-e", snippet],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"node failed:\n{result.stderr}"
    return result.stdout.strip()


def _eval_getrunstatus(run_obj_js: str) -> str:
    fn = _extract("getRunStatus")
    return _run_node(f"{fn}\nprocess.stdout.write(getRunStatus({run_obj_js}));")


def test_getrunstatus_deployment_failure_shape_is_failed():
    status = _eval_getrunstatus(
        '{"timestamp":"x","services":{},"error":"deployment_setup_failed"}'
    )
    assert status == "failed"


def test_getrunstatus_empty_services_is_failed():
    assert _eval_getrunstatus('{"timestamp":"x","services":{}}') == "failed"


def test_getrunstatus_all_passed_is_passed():
    assert (
        _eval_getrunstatus('{"services":{"nova":{"status":"passed"}}}') == "passed"
    )


def test_getrunstatus_any_failed_is_failed():
    assert (
        _eval_getrunstatus(
            '{"services":{"nova":{"status":"passed"},"cinder":{"status":"failed"}}}'
        )
        == "failed"
    )


def test_getrunstatus_error_overrides_passing_services():
    # An .error present alongside passing services still classifies failed.
    assert (
        _eval_getrunstatus(
            '{"services":{"nova":{"status":"passed"}},"error":"boom"}'
        )
        == "failed"
    )


def _eval_union(items_js: str) -> list:
    fn = _extract("orderedServiceUnion")
    preferred = (
        'const PREFERRED_SERVICE_ORDER='
        '["keystone","nova","neutron","glance","cinder","swift"];'
    )
    out = _run_node(
        f"{preferred}\n{fn}\n"
        f"process.stdout.write(JSON.stringify(orderedServiceUnion({items_js})));"
    )
    import json

    return json.loads(out)


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
    fn = _extract("serviceErrorText")
    return _run_node(f"{fn}\nprocess.stdout.write(serviceErrorText({svc_js}));")


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


def _cleanup_badge(cleanup_js: str) -> str:
    fn = _extract("renderCleanup")
    stub = (
        "const els = {cleanupGrid: {}, cleanupBadge: {}};"
        "const document = {getElementById: (id) => els[id]};"
    )
    return _run_node(
        f"{stub}\n{fn}\nrenderCleanup({cleanup_js});"
        "process.stdout.write(els.cleanupBadge.textContent + '|' + els.cleanupBadge.className);"
    )


def test_cleanup_badge_flags_a_blind_openstack_scan():
    # A failed listing reads as 0 orphans; the badge must not say OK.
    assert _cleanup_badge(
        '{"cleanup_failed":0,"details":{},"openstack_scan_status":"error"}'
    ) == "SCAN ERROR|section-badge error"
    assert _cleanup_badge('{"cleanup_failed":0,"details":{}}') == "OK|section-badge ok"
    assert _cleanup_badge(
        '{"cleanup_failed":1,"details":{},"openstack_scan_status":"error"}'
    ) == "ISSUES|section-badge error"


# ---------------------------------------------------------------------------
# Status whitelist + relative data paths
# ---------------------------------------------------------------------------

def test_known_status_maps_unrecognized_values_to_unknown():
    fn = _extract("knownStatus")
    out = _run_node(
        f"{fn}\nconst A=['passed','failed'];"
        "process.stdout.write([knownStatus('passed',A), knownStatus('x\" onclick=\"y',A),"
        " knownStatus(undefined,A)].join('|'));"
    )
    assert out == "passed|unknown|unknown"


def test_service_cards_never_interpolate_raw_status():
    """A hostile status must not reach a class name or the chip text."""
    hostile = '"><img src=x onerror=alert(1)>'
    stub = (
        "const cards = [];"
        "const grid = {appendChild: (c) => cards.push(c)};"
        "const document = {getElementById: () => grid,"
        " createElement: () => ({className: '', innerHTML: ''})};"
        "const escapeHtml = (v) => String(v ?? '').replace(/[&<>\"']/g, (c) => `&#${c.charCodeAt(0)};`);"
        "const formatDuration = () => '1s';"
        "const SERVICE_ICONS = {}; const SERVICE_DESCRIPTIONS = {};"
    )
    # The real whitelists, so the test follows app.js if a status is added.
    src = APP_JS.read_text()
    stub += "".join(
        re.search(rf"^const {name} = .*;$", src, re.M).group(0)
        for name in ("SERVICE_STATUSES", "HEALTH_STATUSES")
    )
    js = (
        f"{stub}\n{_extract('knownStatus')}\n{_extract('renderServiceCards')}\n"
        f"const bad = {json.dumps(hostile)};"
        "renderServiceCards({timestamp: 't', services: {nova: {status: bad}}},"
        " {runs: [{services: {nova: {status: bad}}}]},"
        " {services: {nova: {status: bad, latency_ms: 1}}});"
        "process.stdout.write(cards[0].className + '\\n' + cards[0].innerHTML);"
    )
    out = _run_node(js)
    assert "<img" not in out
    assert out.splitlines()[0] == "service-card status-unknown"
    assert '<span class="status-chip unknown">unknown</span>' in out


def test_data_fetches_use_relative_paths():
    """Absolute /x.json paths break behind a sub-path reverse proxy."""
    assert re.findall(r"""fetch\(\s*["'`]/""", APP_JS.read_text()) == []


def _health_detail(status: str, services_js: str) -> str:
    fn = _extract("healthCellDetail")
    stub = "const escapeHtml = (v) => String(v ?? '');"
    return _run_node(
        f"{stub}\n{fn}\nprocess.stdout.write(healthCellDetail({json.dumps(status)}, {services_js}));"
    )


def test_health_cell_detail_unknown_is_not_all_up():
    assert _health_detail("unknown", "{}") == "Status unknown"
    assert _health_detail("up", "{}") == "All services up"
    assert _health_detail("down", '{"nova":{"status":"down"}}') == "Down: nova"


def test_format_duration_tolerates_non_numbers():
    """A string/garbage duration used to throw in .toFixed and abort the whole
    card loop (or the modal)."""
    fn = _extract("formatDuration")
    out = _run_node(
        f"{fn}\nprocess.stdout.write([formatDuration('12.5'), formatDuration('x'),"
        " formatDuration(undefined), formatDuration(0.25), formatDuration(90)].join('|'));"
    )
    assert out == "12.5s|—|—|250ms|1.5m"
