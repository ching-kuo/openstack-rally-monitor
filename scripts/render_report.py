#!/usr/bin/env python3
"""Render a self-contained, dashboard-themed HTML report for one Rally service.

Replaces ``rally task report`` (which produces a light-theme AngularJS page that
pulls AngularJS/d3/nvd3 from public CDNs and breaks when those scripts cannot
load). This renderer instead:

  * reads the ``rally task results`` JSON we already persist as
    ``<RUN_DIR>/<service>.json`` (the "old-json-results" exporter shape: a list
    of workloads, each ``{key:{name,description,pos,kw}, result:[...], sla:[...],
    hooks:[...], load_duration, full_duration, created_at}``);
  * aggregates each workload into a small, render-ready payload; and
  * emits a tiny HTML shell that embeds that payload in a non-executable
    ``<script type="application/json">`` block and pulls the dashboard's own
    theme cascade plus ``report.css`` / ``report.js`` / vendored Chart.js by
    RELATIVE path (``../../`` -> dashboard root via the /dashboard/runs symlink).

Because every asset is same-origin and there is zero inline executable script,
the report is served under the dashboard's STRICT Content-Security-Policy -- no
``'unsafe-inline'`` script, no CDN origins, no ``sandbox`` carve-out. It also
renders with no network access at all (air-gapped friendly).

Usage:
    render_report.py <results.json> <output.html> --service <name> --timestamp <ts>

Exit status is always 0 on a best-effort basis: a malformed/empty results file
still yields a valid (empty-state) report so the dashboard link never 404s.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import sys
from pathlib import Path
from typing import Any


def _to_float(value: Any) -> float:
    """Coerce a Rally numeric field to float, defaulting to 0.0 defensively."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_dict(value: Any) -> dict[str, Any]:
    """Return value if it is a dict, else {}.

    Guards every nested ``.get`` against a truthy-but-non-dict shape (a crashed
    or partial task can leave ``key``/``kw``/``runner`` as a string or list). A
    bare ``value or {}`` is NOT enough: a non-empty string/list is truthy and
    would survive, then raise AttributeError on the next ``.get``.
    """
    return value if isinstance(value, dict) else {}


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile over an already-sorted list (empty -> 0.0)."""
    if not sorted_values:
        return 0.0
    rank = math.ceil(pct / 100.0 * len(sorted_values))
    idx = min(max(rank, 1), len(sorted_values)) - 1
    return sorted_values[idx]


def _duration_stats(durations: list[float]) -> dict[str, float]:
    """min/max/avg/median/p90/p95 over per-iteration total durations."""
    if not durations:
        return {"min": 0.0, "max": 0.0, "avg": 0.0, "median": 0.0, "p90": 0.0, "p95": 0.0}
    ordered = sorted(durations)
    n = len(ordered)
    return {
        "min": ordered[0],
        "max": ordered[-1],
        "avg": sum(ordered) / n,
        "median": _percentile(ordered, 50),
        "p90": _percentile(ordered, 90),
        "p95": _percentile(ordered, 95),
    }


def _atomic_breakdown(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate per-atomic-action durations across iterations.

    Rally's old-json-results atomic_actions is an ordered ``{name: duration}``
    map per iteration. We preserve first-seen order and report count/avg/min/max.
    """
    order: list[str] = []
    buckets: dict[str, list[float]] = {}
    for item in results:
        actions = item.get("atomic_actions") or {}
        if not isinstance(actions, dict):
            continue
        for name, dur in actions.items():
            if name not in buckets:
                buckets[name] = []
                order.append(name)
            buckets[name].append(_to_float(dur))
    breakdown = []
    for name in order:
        vals = buckets[name]
        breakdown.append(
            {
                "name": name,
                "count": len(vals),
                "avg": sum(vals) / len(vals) if vals else 0.0,
                "min": min(vals) if vals else 0.0,
                "max": max(vals) if vals else 0.0,
            }
        )
    return breakdown


def _error_samples(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect distinct iteration errors as {type, message, traceback, count}.

    A Rally iteration error is ``[type, message, traceback]``; older/edge shapes
    may carry fewer elements, so every index is read defensively. Distinct
    errors are keyed by (type, message) and counted.
    """
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for item in results:
        err = item.get("error") or []
        if not isinstance(err, list) or not err:
            continue
        # err is a non-empty list here; index 0 always exists. A Rally iteration
        # error is [type, message, traceback], but older/edge shapes carry fewer
        # elements, so message/traceback are read defensively with index 1/2.
        etype = str(err[0])
        message = str(err[1]) if len(err) > 1 else etype
        traceback = str(err[2]) if len(err) > 2 else ""
        key = (etype, message)
        if key not in seen:
            sample = {"type": etype, "message": message, "traceback": traceback, "count": 0}
            seen[key] = sample
            order.append(key)
        seen[key]["count"] += 1
    return [seen[k] for k in order]


def _scenario_payload(workload: dict[str, Any]) -> dict[str, Any]:
    """Normalize one Rally workload into the render-ready scenario shape."""
    key = _as_dict(workload.get("key"))
    name = str(key.get("name") or "unknown")
    cls, _, method = name.partition(".")
    results = workload.get("result")
    if not isinstance(results, list):
        results = []

    durations = [_to_float(r.get("duration")) for r in results if isinstance(r, dict)]
    error_count = sum(
        1
        for r in results
        if isinstance(r, dict) and isinstance(r.get("error"), list) and r.get("error")
    )

    sla = workload.get("sla")
    if not isinstance(sla, list):
        sla = []
    sla_dicts = [s for s in sla if isinstance(s, dict)]
    sla_passed = all(bool(s.get("success")) for s in sla_dicts) if sla_dicts else True

    kw = _as_dict(key.get("kw"))
    runner = _as_dict(kw.get("runner"))
    runner_type = str(runner.get("type") or "unknown")

    hooks = workload.get("hooks")
    hook_count = len(hooks) if isinstance(hooks, list) else 0

    return {
        "name": name,
        "cls": cls or name,
        "method": method or name,
        "description": str(key.get("description") or ""),
        "load_duration": _to_float(workload.get("load_duration")),
        "full_duration": _to_float(workload.get("full_duration")),
        "iterations": len(results),
        "runner": runner_type,
        "errors": error_count,
        "hooks": hook_count,
        "sla_passed": sla_passed,
        "sla": [
            {
                "criterion": str(s.get("criterion") or ""),
                "success": bool(s.get("success")),
                "detail": str(s.get("detail") or ""),
            }
            for s in sla_dicts
        ],
        "duration_stats": _duration_stats(durations),
        "durations": durations,
        "atomics": _atomic_breakdown([r for r in results if isinstance(r, dict)]),
        "error_samples": _error_samples([r for r in results if isinstance(r, dict)]),
        "config": kw,
    }


def build_payload(raw: Any, service: str, timestamp: str) -> dict[str, Any]:
    """Build the full report payload from parsed ``rally task results`` JSON."""
    workloads = raw if isinstance(raw, list) else []
    scenarios = [
        _scenario_payload(w) for w in workloads if isinstance(w, dict)
    ]
    return {"service": service, "timestamp": timestamp, "scenarios": scenarios}


def _embed_json(data: dict[str, Any]) -> str:
    """Serialize for a ``<script type="application/json">`` block.

    Escapes the characters that could close the script element or be reparsed as
    markup; JSON.parse decodes the \\uXXXX escapes back transparently.
    """
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def render_html(payload: dict[str, Any]) -> str:
    """Render the self-contained HTML shell around the embedded payload.

    Asset paths are RELATIVE (``../../``): a report lives at
    ``/runs/<ts>/<service>.html`` and the browser resolves ``../../style.css`` to
    the dashboard root, which keeps the page working behind a sub-path reverse
    proxy (no absolute ``/style.css`` assumption) and matches index.html's own
    relative-path convention.
    """
    service = html.escape(str(payload.get("service") or ""))
    timestamp = html.escape(str(payload.get("timestamp") or ""))
    data_block = _embed_json(payload)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Rally Report &middot; {service} &middot; {timestamp}</title>
<link rel="icon" type="image/svg+xml" href="../../themes/default/favicon.svg">
<link rel="stylesheet" href="../../themes/default/tokens.css">
<link rel="stylesheet" href="../../style.css">
<link rel="stylesheet" class="custom-theme-css" href="../../themes/custom/tokens.css">
<link rel="stylesheet" class="custom-theme-css" href="../../themes/custom/overrides.css">
<link rel="stylesheet" href="../../report.css">
<script type="application/json" id="rally-report-data">{data_block}</script>
<script src="../../vendor/chart.umd.min.js"></script>
<script src="../../report.js" defer></script>
</head>
<body class="report-body">
<header class="report-header">
<a class="report-back" href="../../index.html">&larr; Dashboard</a>
<div class="report-title">
<span class="report-service">{service}</span>
<span class="report-timestamp">{timestamp}</span>
</div>
</header>
<div class="report-layout">
<nav class="report-nav" id="reportNav" aria-label="Report navigation"></nav>
<main class="report-main" id="reportMain"></main>
</div>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a themed Rally HTML report.")
    parser.add_argument("results", help="Path to the rally task results JSON file")
    parser.add_argument("output", help="Path to write the HTML report to")
    parser.add_argument("--service", default="", help="Service name (e.g. nova)")
    parser.add_argument("--timestamp", default="", help="Run timestamp (UTC)")
    args = parser.parse_args(argv)

    raw: Any = []
    try:
        with open(args.results, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        # Best effort: a crashed task leaves non-JSON or no file. Emit an
        # empty-state report so the dashboard "Full Rally report" link still
        # resolves instead of 404ing.
        print(f"render_report: could not parse {args.results}: {exc}", file=sys.stderr)
        raw = []

    try:
        output = render_html(build_payload(raw, args.service, args.timestamp))
    except Exception as exc:  # noqa: BLE001 -- best effort; never 404 the link
        # build_payload/render_html are defensive, but an unforeseen result
        # shape must still not abort the run with no report. Fall back to an
        # empty-state report (which is built from a known-good empty list).
        print(f"render_report: render failed for {args.results}: {exc}", file=sys.stderr)
        output = render_html(build_payload([], args.service, args.timestamp))
    try:
        out_path = Path(args.output)
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp.write_text(output, encoding="utf-8")
        tmp.replace(out_path)
    except OSError as exc:
        print(f"render_report: could not write {args.output}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
