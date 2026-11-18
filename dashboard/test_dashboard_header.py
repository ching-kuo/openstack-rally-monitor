"""Node-driven tests for the dashboard header: data freshness and the overdue
rules, the connection chip, the first-load error, the badge naming failing
services, and timestamp/chart label formatting (see node_testlib.py)."""

from node_testlib import FAKE_DOM, UTC_EN, imports, requires_node, run_json, run_module

pytestmark = requires_node


def test_format_age_buckets():
    out = run_module(
        imports("format", "formatAge")
        + "const m = 60000, now = 1e12;"
        "process.stdout.write([0, 0.5*m, 12*m, 60*m, 185*m, 49*60*m, -5*m]"
        ".map((d) => formatAge(now - d, now)).join('|'));"
    )
    assert out == "just now|just now|12m ago|1h ago|3h 5m ago|2d 1h ago|just now"


def test_overdue_rules_match_the_exporter_and_alerts():
    """runOverdue mirrors RallyStaleResults (measured from the run's END, past
    2 x schedule + duration); healthStale mirrors the exporter's
    rally_api_stale_after_seconds. Unknown thresholds never read as overdue."""
    out = run_json(
        imports("status", "runOverdue", "healthStale")
        + "const now = 1e12, s = 1000, h = 3600;"
        "process.stdout.write(JSON.stringify(["
        # start 3h01m ago, 1h run, 1h schedule: 2h01m since end > 2h + 1h? no.
        " runOverdue(now - (3*h + 60)*s, h, now, h),"
        # start 4h01m ago, 1h run: 3h01m since end > 3h -> overdue.
        " runOverdue(now - (4*h + 60)*s, h, now, h),"
        # an aborted run without a duration ends at its start.
        " runOverdue(now - (2*h + 60)*s, undefined, now, h),"
        " runOverdue(now - 1e9, 0, now, undefined),"
        " healthStale(now - 2701*s, now, 2700), healthStale(now - 2699*s, now, 2700),"
        " healthStale(now - 1e9, now, undefined), healthStale(NaN, now, 2700)]));"
    )
    assert out == [False, True, True, False, True, False, False, False]


FRESHNESS = (
    FAKE_DOM
    + imports("header", "renderFreshness")
    + "const now = Date.parse('2026-10-26T12:00:00Z');"
    "const results = {summary: {timestamp: '20261026T080000Z', run_duration_seconds: 60}};"
    "const health = {timestamp: '2026-10-26T11:10:00Z'};"
)


def test_render_freshness_uses_the_boot_config_thresholds():
    out = run_json(
        FRESHNESS
        + "renderFreshness(results, health, {schedule_interval_seconds: 3600,"
        " health_stale_after_seconds: 2700}, now);"
        "const r = els.lastRun, h = els.healthAge;"
        "process.stdout.write(JSON.stringify([r.textContent, r.className, h.textContent,"
        " h.className, h.hidden]));"
    )
    assert out == [
        "Last run 4h ago (overdue)", "stale", "Health checked 50m ago (overdue)", "stale", False,
    ]


def test_render_freshness_without_config_shows_no_overdue_hint():
    out = run_json(
        FRESHNESS
        + "renderFreshness(results, health, null, now);"
        "process.stdout.write(JSON.stringify([els.lastRun.textContent, els.lastRun.className,"
        " els.healthAge.textContent, els.healthAge.className]));"
    )
    assert out == ["Last run 4h ago", "", "Health checked 50m ago", ""]


def test_render_connection_names_the_age_of_the_cached_data():
    out = run_json(
        FAKE_DOM
        + imports("header", "renderConnection")
        + "const c = document.getElementById('connectionChip'), seen = [];"
        "renderConnection(false, false, null); seen.push(c.textContent, c.hidden);"
        "renderConnection(true, false, Date.parse('2026-10-26T09:05:00Z'));"
        " seen.push(c.textContent);"
        "renderConnection(true, true, null); seen.push(c.hidden);"
        "process.stdout.write(JSON.stringify(seen));",
        UTC_EN,
    )
    assert out == [
        "Can't reach server", False,
        "Can't load results.json, showing data from 09:05 AM UTC", True,
    ]


def test_first_load_failure_shows_an_error_not_loading():
    out = run_json(
        FAKE_DOM
        + imports("header", "showLoadError")
        + "showLoadError();"
        "process.stdout.write(JSON.stringify([els.healthBadge.className,"
        " els.healthBadge.querySelector('.health-text').textContent]));"
    )
    assert out == ["health-badge unhealthy", "Data unavailable"]


def test_header_badge_names_failing_services_with_overflow():
    out = run_json(
        imports("header", "headerBadge")
        + "const f = {status: 'failed'}, p = {status: 'passed'}, s = {status: 'skipped'};"
        "process.stdout.write(JSON.stringify(["
        " headerBadge({services: {nova: f, neutron: f, glance: f, cinder: f, swift: p}}, null),"
        " headerBadge({services: {nova: p}}, {overall: 'down',"
        "   services: {keystone: {status: 'down'}, nova: {status: 'up'}}}),"
        " headerBadge({services: {}, error: 'deployment_setup_failed'}, null),"
        " headerBadge({services: {keystone: p, octavia: s}}, null),"
        " headerBadge({services: {octavia: s, heat: s}}, null)]));"
    )
    assert out == [
        {"cls": "unhealthy", "label": "Issues Detected: nova, neutron, glance +1"},
        {"cls": "unhealthy", "label": "API Issues Detected: keystone"},
        {"cls": "unhealthy", "label": "Issues Detected"},
        {"cls": "healthy", "label": "All Healthy"},
        {"cls": "unhealthy", "label": "Issues Detected"},
    ]


def test_chart_label_keeps_am_pm_and_timestamps_name_the_zone():
    out = run_json(
        imports("format", "formatChartLabel", "formatTimestamp")
        + "process.stdout.write(JSON.stringify([formatChartLabel('20261026T141500Z'),"
        " formatTimestamp('20261026T141500Z')]));",
        UTC_EN,
    )
    assert out[0] == "Oct 26, 02:15 PM"
    assert out[1] == "Oct 26, 2026, 02:15 PM UTC"


def test_first_boot_health_placeholder_is_not_shown_as_a_check():
    """init_state.sh seeds health.json with timestamp "waiting"; that is not a
    check time, so no "Health checked" line appears until a real check."""
    out = run_json(
        FRESHNESS
        + "renderFreshness(results, {timestamp: 'waiting', overall: 'unknown'}, null, now);"
        "process.stdout.write(JSON.stringify([els.healthAge.hidden, els.healthAge.textContent]));"
    )
    assert out == [True, ""]
