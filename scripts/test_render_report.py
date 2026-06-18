"""Unit tests for scripts/render_report.py.

Covers the normalization from the rally `old-json-results` shape into the
render-ready payload, the percentile/atomic/error aggregation, the HTML shell
(relative asset paths, strict-CSP-friendly: no CDN, no sandbox, no inline
executable script), and the <script type=application/json> escaping that keeps
hostile result content from breaking out of the data block.
"""
import json
import re

import render_report as rr

SAMPLE = [
    {
        "key": {
            "name": "NovaFlavors.list_flavors",
            "description": "List flavors.",
            "pos": 0,
            "kw": {"runner": {"type": "constant", "times": 5}, "context": {}},
        },
        "result": [
            {"duration": 2.0, "atomic_actions": {"nova.list_flavors": 1.9}, "error": []},
            {"duration": 2.5, "atomic_actions": {"nova.list_flavors": 2.4}, "error": []},
        ],
        "sla": [{"criterion": "failure_rate", "success": True, "detail": "ok"}],
        "hooks": [],
        "load_duration": 2.261,
        "full_duration": 9.648,
    },
    {
        "key": {
            "name": "NovaServers.boot_and_delete_server",
            "description": "Boot and delete a server.",
            "pos": 1,
            "kw": {"runner": {"type": "constant"}},
        },
        "result": [
            {"duration": 30.1, "atomic_actions": {"nova.boot_server": 20.0, "nova.delete_server": 9.0}, "error": []},
            {
                "duration": 32.0,
                "atomic_actions": {"nova.boot_server": 22.0, "nova.delete_server": 8.0},
                "error": ["TimeoutException", "Server failed to boot", "Traceback ..."],
            },
        ],
        "sla": [{"criterion": "failure_rate", "success": False, "detail": "50% > 0%"}],
        "hooks": [{"x": 1}],
        "load_duration": 63.892,
        "full_duration": 96.543,
    },
]


def _data_block(html: str) -> dict:
    """Extract and parse the embedded <script type=application/json> payload."""
    m = re.search(r'id="rally-report-data">(.*?)</script>', html, re.S)
    assert m, "data block not found"
    return json.loads(m.group(1))


# --- Aggregation helpers ----------------------------------------------------

def test_percentile_empty_and_single():
    assert rr._percentile([], 95) == 0.0
    assert rr._percentile([4.2], 50) == 4.2
    assert rr._percentile([4.2], 95) == 4.2


def test_duration_stats():
    stats = rr._duration_stats([2.0, 2.5])
    assert stats["min"] == 2.0
    assert stats["max"] == 2.5
    assert stats["avg"] == 2.25
    assert rr._duration_stats([]) == {
        "min": 0.0, "max": 0.0, "avg": 0.0, "median": 0.0, "p90": 0.0, "p95": 0.0
    }


def test_atomic_breakdown_preserves_order_and_aggregates():
    atomics = rr._atomic_breakdown(
        [
            {"atomic_actions": {"a": 1.0, "b": 2.0}},
            {"atomic_actions": {"a": 3.0, "b": 4.0}},
        ]
    )
    assert [a["name"] for a in atomics] == ["a", "b"]
    a, b = atomics
    assert a["count"] == 2 and a["avg"] == 2.0 and a["min"] == 1.0 and a["max"] == 3.0
    assert b["avg"] == 3.0


def test_error_samples_dedupe_and_count():
    samples = rr._error_samples(
        [
            {"error": ["E", "boom", "tb1"]},
            {"error": ["E", "boom", "tb1"]},
            {"error": []},
            {"error": ["F", "other"]},
        ]
    )
    assert len(samples) == 2
    assert samples[0]["type"] == "E" and samples[0]["count"] == 2
    assert samples[1]["type"] == "F" and samples[1]["message"] == "other"
    assert samples[1]["traceback"] == ""  # missing element tolerated


# --- Payload normalization --------------------------------------------------

def test_build_payload_shape():
    payload = rr.build_payload(SAMPLE, "nova", "20260615T160001Z")
    assert payload["service"] == "nova"
    assert payload["timestamp"] == "20260615T160001Z"
    assert len(payload["scenarios"]) == 2

    flavors, servers = payload["scenarios"]
    assert flavors["cls"] == "NovaFlavors" and flavors["method"] == "list_flavors"
    assert flavors["iterations"] == 2
    assert flavors["errors"] == 0
    assert flavors["sla_passed"] is True
    assert flavors["runner"] == "constant"

    assert servers["errors"] == 1
    assert servers["sla_passed"] is False
    assert servers["hooks"] == 1
    assert [a["name"] for a in servers["atomics"]] == ["nova.boot_server", "nova.delete_server"]
    assert servers["error_samples"][0]["type"] == "TimeoutException"


def test_build_payload_tolerates_garbage():
    # Non-list input, non-dict members, and missing keys must not raise.
    assert rr.build_payload("not a list", "x", "t")["scenarios"] == []
    assert rr.build_payload([None, 3, "x"], "x", "t")["scenarios"] == []
    one = rr.build_payload([{"key": {}}], "x", "t")["scenarios"]
    assert one[0]["name"] == "unknown"
    assert one[0]["sla_passed"] is True  # no SLA criteria -> vacuously passing


def test_build_payload_tolerates_truthy_non_dict_shapes():
    # A crashed/partial task can leave key/kw/runner as a non-dict TRUTHY value
    # (string/list). `value or {}` would let those through and then raise
    # AttributeError on the next .get -- _as_dict must coerce them to {}.
    hostile = [
        {"key": "not-a-dict", "result": "not-a-list", "sla": "nope", "hooks": "nope"},
        {"key": {"name": "X.y", "kw": "not-a-dict"}, "result": [{"duration": 1.0}]},
        {"key": {"name": "X.z", "kw": {"runner": ["not", "a", "dict"]}}, "result": []},
        {"key": {"name": "X.w"}, "sla": ["not-a-dict", {"success": True}]},
    ]
    scenarios = rr.build_payload(hostile, "x", "t")["scenarios"]
    assert len(scenarios) == 4
    assert scenarios[0]["name"] == "unknown" and scenarios[0]["runner"] == "unknown"
    assert scenarios[1]["runner"] == "unknown" and scenarios[1]["config"] == {}
    assert scenarios[2]["runner"] == "unknown"
    # Non-dict SLA entries are dropped; the lone dict criterion still counts.
    assert scenarios[3]["sla_passed"] is True and len(scenarios[3]["sla"]) == 1


def test_to_float_handles_non_numeric():
    assert rr._to_float(None) == 0.0
    assert rr._to_float("nan-ish") == 0.0
    assert rr._to_float([1, 2]) == 0.0
    assert rr._to_float("3.5") == 3.5


# --- HTML shell + escaping --------------------------------------------------

def test_render_html_uses_relative_same_origin_assets_only():
    html = rr.render_html(rr.build_payload(SAMPLE, "nova", "20260615T160001Z"))
    # Relative to dashboard root (works behind a sub-path proxy).
    assert '../../style.css' in html
    assert '../../report.js' in html
    assert '../../report.css' in html
    assert '../../vendor/chart.umd.min.js' in html
    # No CDN, no sandbox, no inline executable script -- strict-CSP friendly.
    assert "googleapis.com" not in html
    assert "cdnjs.cloudflare.com" not in html
    assert "sandbox" not in html
    # The only <script> tags are the JSON data block and external src= refs.
    assert 'type="application/json"' in html


def test_embed_json_round_trips_and_escapes_markup():
    payload = rr.build_payload(SAMPLE, "nova", "t")
    html = rr.render_html(payload)
    assert _data_block(html)["service"] == "nova"
    # The raw markup characters are escaped in the serialized block.
    assert "<" not in rr._embed_json({"k": "<b>"})
    assert ">" not in rr._embed_json({"k": "<b>"})
    assert "&" not in rr._embed_json({"k": "a&b"})


def test_hostile_result_cannot_break_out_of_data_block():
    hostile = [
        {
            "key": {"name": "Evil.x", "kw": {}},
            "result": [{"duration": 1.0, "error": ["E", "</script><script>alert(1)</script>", "tb"]}],
            "sla": [],
        }
    ]
    html = rr.render_html(rr.build_payload(hostile, "evil", "t"))
    # The injected closing tag must not appear literally; the data block's only
    # </script> is its own legitimate terminator.
    assert "<script>alert(1)" not in html
    assert html.count("</script>") == 3  # data block + 2 external script tags
    # ...and the value still round-trips for rendering.
    recovered = _data_block(html)["scenarios"][0]["error_samples"][0]["message"]
    assert recovered == "</script><script>alert(1)</script>"


# --- CLI main() -------------------------------------------------------------

def test_main_with_valid_results(tmp_path):
    src = tmp_path / "nova.json"
    src.write_text(json.dumps(SAMPLE))
    out = tmp_path / "nova.html"
    rc = rr.main([str(src), str(out), "--service", "nova", "--timestamp", "t"])
    assert rc == 0
    assert _data_block(out.read_text())["service"] == "nova"


def test_main_with_malformed_results_emits_empty_state(tmp_path):
    src = tmp_path / "nova.json"
    src.write_text("this is not json (crashed task)")
    out = tmp_path / "nova.html"
    rc = rr.main([str(src), str(out), "--service", "nova", "--timestamp", "t"])
    assert rc == 0  # best effort: report still renders so the link never 404s
    assert _data_block(out.read_text())["scenarios"] == []


def test_main_with_missing_file_emits_empty_state(tmp_path):
    out = tmp_path / "nova.html"
    rc = rr.main([str(tmp_path / "nope.json"), str(out), "--service", "nova", "--timestamp", "t"])
    assert rc == 0
    assert out.exists()
    assert _data_block(out.read_text())["scenarios"] == []
