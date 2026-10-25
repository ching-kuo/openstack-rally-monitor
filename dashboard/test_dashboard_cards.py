"""Node-driven tests for the service cards, the run error card, the detail
modal, the cleanup section and the charts (see node_testlib.py)."""
import json
import re
from pathlib import Path

from node_testlib import FAKE_DOM, imports, requires_node, run_json, run_module

pytestmark = requires_node


def test_run_error_card_shows_error_and_detail_as_text():
    detail = '<img src=x onerror=alert(1)>\nkeystone: 401 Unauthorized'
    out = run_json(
        FAKE_DOM
        + imports("services", "renderServiceCards")
        + "renderServiceCards({timestamp: '20261026T120000Z', services: {},"
        f" error: 'deployment_setup_failed', error_detail: {json.dumps(detail)}}},"
        " {runs: []}, null);"
        "const card = els.servicesGrid.children[0];"
        "process.stdout.write(JSON.stringify({cls: card.className, html: card.innerHTML,"
        " texts: card.children.map((c) => c.textContent)}));"
    )
    assert out["cls"] == "run-error-card"
    assert out["html"] == ""  # built from text nodes only, never innerHTML
    assert "Error: deployment_setup_failed" in out["texts"]
    assert detail in out["texts"]
    assert any("/results/20261026T120000Z/deployment.log" in t for t in out["texts"])


def test_run_log_hint_uses_run_log_for_other_aborts():
    out = run_module(
        imports("services", "runLogHint")
        + "process.stdout.write(runLogHint({timestamp: '20261026T120000Z',"
        " error: 'task_args_write_failed'}));"
    )
    assert "/results/20261026T120000Z/run.log" in out


def test_service_card_is_a_keyboard_button():
    out = run_json(
        FAKE_DOM
        + imports("services", "renderServiceCards")
        + "renderServiceCards({timestamp: 't', services: {nova: {status: 'failed'}}},"
        " {runs: []}, null);"
        "const card = els.servicesGrid.children[0];"
        "card.dispatch('keydown', {key: 'Enter'});"
        "const opened = els.modalOverlay.className;"
        "process.stdout.write(JSON.stringify([card.tabIndex, card.attrs.role,"
        " card.attrs['aria-label'], opened, document.activeElement === els.modalClose]));"
    )
    assert out == [0, "button", "nova: failed. Open scenario details", "active", True]


def test_cleanup_badge_covers_rgw_and_context_leftovers():
    out = run_json(
        imports("status", "cleanupBadge")
        + "process.stdout.write(JSON.stringify(["
        " {rgw_scan_status: 'error'}, {rgw_orphaned_users: 2},"
        " {context_details: {networks: 1}}, {context_cleanup_warning: 1},"
        " {rgw_scan_status: 'skipped', context_details: {networks: 0}}]"
        ".map((c) => cleanupBadge(c).text)));"
    )
    assert out == ["SCAN ERROR", "RGW ORPHANS", "CONTEXT LEFTOVERS", "CONTEXT LEFTOVERS", "OK"]


def test_render_cleanup_shows_context_rgw_and_scan_errors():
    out = run_json(
        FAKE_DOM
        + imports("cleanup", "renderCleanup")
        + "renderCleanup({cleanup_failed: 0, openstack_scan_status: 'error',"
        " openstack_scan_errors: 2, details: {servers: 1}, context_details: {networks: 3},"
        " rgw_scan_status: 'ok', rgw_orphaned_users: 1, rgw_orphaned_buckets: 4,"
        " rgw_orphaned_objects: '<b>', rgw_rally_owned_orphans: 1});"
        "process.stdout.write(JSON.stringify([els.cleanupBadge.textContent,"
        " els.cleanupNote.textContent, els.cleanupGrid.innerHTML]));"
    )
    badge, note, grid = out
    assert badge == "SCAN ERROR"
    assert "2 OpenStack list calls failed" in note
    assert "3 context resources" in note
    assert "+3 context (c_rally_*)" in grid
    assert "RGW Users" in grid and "4 buckets" in grid and "0 objects" in grid
    assert "<b>" not in grid


def test_render_cleanup_hides_rgw_tile_when_not_configured():
    grid = run_module(
        FAKE_DOM
        + imports("cleanup", "renderCleanup")
        + "renderCleanup({rgw_scan_status: 'skipped', details: {}});"
        "process.stdout.write(els.cleanupGrid.innerHTML);"
    )
    assert "RGW" not in grid


def test_chart_with_one_point_shows_not_enough_data():
    out = run_json(
        FAKE_DOM
        + imports("charts", "renderCharts")
        + "const run = {timestamp: '20261026T000000Z', services: {nova: {duration: 1}}};"
        "renderCharts({runs: [run]});"
        "const one = [els.durationChart.hidden,"
        " els.durationChart.querySelector('.chart-empty').hidden];"
        "renderCharts({runs: [run, {...run, timestamp: '20261026T040000Z'}]});"
        "process.stdout.write(JSON.stringify([...one, els.durationChart.hidden]));"
    )
    assert out == [True, False, False]


def test_closing_the_modal_after_a_refresh_focuses_the_new_card():
    out = run_json(
        FAKE_DOM
        + imports("services", "renderServiceCards")
        + imports("modal", "initModal")
        + "initModal();"
        "const summary = {timestamp: 't', services: {nova: {status: 'passed'},"
        " cinder: {status: 'failed'}}};"
        "renderServiceCards(summary, {runs: []}, null);"
        "const opener = els.servicesGrid.children[1];"
        "opener.focus(); opener.dispatch('keydown', {key: 'Enter'});"
        "renderServiceCards(summary, {runs: []}, null);"  # a refresh replaces the cards
        "const stale = opener.isConnected;"
        "els.modalClose.dispatch('click');"
        "const active = document.activeElement;"
        "process.stdout.write(JSON.stringify([stale, active.dataset.service,"
        " els.servicesGrid.children.includes(active)]));"
    )
    assert out == [False, "cinder", True]


def test_closing_the_modal_falls_back_to_the_services_heading():
    out = run_json(
        FAKE_DOM
        + imports("services", "renderServiceCards")
        + imports("modal", "initModal")
        + "initModal();"
        "renderServiceCards({timestamp: 't', services: {nova: {status: 'passed'}}}, {runs: []}, null);"
        "const opener = els.servicesGrid.children[0];"
        "opener.focus(); opener.dispatch('keydown', {key: 'Enter'});"
        "renderServiceCards({timestamp: 't', services: {}}, {runs: []}, null);"  # nova is gone
        "els.modalClose.dispatch('click');"
        "process.stdout.write(JSON.stringify([document.activeElement === els.servicesSectionTitle,"
        " els.servicesSectionTitle.tabIndex]));"
    )
    assert out == [True, -1]


def test_charts_do_not_animate_under_reduced_motion():
    out = run_json(
        FAKE_DOM
        + imports("charts", "renderCharts")
        + "const run = (ts) => ({timestamp: ts, services: {nova: {duration: 1}}});"
        "const history = {runs: [run('20261026T000000Z'), run('20261026T040000Z')]};"
        "globalThis.matchMedia = (q) => ({matches: q === '(prefers-reduced-motion: reduce)'});"
        "renderCharts(history);"
        "globalThis.matchMedia = () => ({matches: false});"
        "renderCharts(history);"
        "process.stdout.write(JSON.stringify(chartConfigs.map((c) => c.options.animation ?? null)));"
    )
    assert out == [False, None]


def test_card_focus_survives_a_refresh():
    out = run_json(
        FAKE_DOM
        + imports("services", "renderServiceCards")
        + "const card = (s) => ({status: s});"
        "const render = (services) => renderServiceCards({timestamp: 't', services}, {runs: []}, null);"
        "render({nova: card('passed'), cinder: card('failed')});"
        "els.servicesGrid.children[1].focus(); render({nova: card('passed'), cinder: card('failed')});"
        "const kept = [document.activeElement.dataset.service,"
        " els.servicesGrid.children.includes(document.activeElement)];"
        "render({nova: card('passed')});"  # cinder is gone: fall back to the heading
        "const fallback = document.activeElement === els.servicesSectionTitle;"
        "const other = document.getElementById('refreshBtn');"
        "other.focus(); render({nova: card('passed')});"  # focus elsewhere: untouched
        "process.stdout.write(JSON.stringify([...kept, fallback, document.activeElement === other]));"
    )
    assert out == ["cinder", True, True, True]


def test_announcement_types_ignore_prototype_keys():
    """KEEP IN SYNC with announcement_metrics.py: only the three enum types."""
    out = run_json(
        imports("announcements", "isAnnouncementActive")
        + "const now = Date.now();"
        "process.stdout.write(JSON.stringify(['toString', 'constructor', '__proto__', 'incident']"
        ".map((type) => isAnnouncementActive({type}, now))));"
    )
    assert out == [False, False, False, True]


def _css_block(path: Path, selector: str) -> str:
    match = re.search(rf"^{re.escape(selector)} \{{(.*?)^\}}", path.read_text(), re.M | re.S)
    assert match, selector
    return match.group(1)


def test_modal_becomes_visible_at_once_so_open_can_focus_it():
    """openModal focuses the close button in the same frame it adds .active.
    A visibility transition on open kept the dialog visibility:hidden for
    0.3s, so focus() was a no-op; only the close may delay it (to fade out)."""
    css = Path(__file__).resolve().parent / "overlays.css"
    opened = _css_block(css, ".modal-overlay.active")
    closed = _css_block(css, ".modal-overlay")
    assert re.search(r"transition:[^;]*visibility 0s(?! *[\d.]+m?s)[^;]*;", opened), opened
    assert re.search(r"transition:[^;]*visibility 0s [\w-]* *0\.3s", closed), closed
    # The close button inherits visibility; an `all` transition on it
    # (var(--transition)) would re-delay it and swallow focus() on first open.
    button = _css_block(css, ".modal-close")
    decl = re.search(r"^\s*transition:([^;]*);", button, re.M).group(1)
    assert "var(--transition)" not in decl and "all" not in decl, decl
