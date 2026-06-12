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

If node is unavailable the tests skip rather than fail (CI images that omit
node still get a green run; the bash-side publish test covers the data path).
"""
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
