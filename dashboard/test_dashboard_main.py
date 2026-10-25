"""Node-driven tests for the entry point (main.js) and data fetching, run
against stubbed fetches (see node_testlib.py)."""
import json

from node_testlib import FAKE_DOM, JS_DIR, NEVER, imports, requires_node, run_json

pytestmark = requires_node


def test_main_renders_from_stubbed_data_and_applies_the_permalink():
    results = {
        "summary": {"timestamp": "20261026T080000Z", "services": {"nova": {"status": "passed"}}},
        "cleanup": {"details": {}},
    }
    history = {"runs": [
        {"timestamp": "20261026T040000Z", "services": {"nova": {"status": "failed"}}},
        results["summary"],
    ]}
    files = {"results.json": results, "history.json": history}
    out = run_json(
        FAKE_DOM
        + "globalThis.setInterval = () => 0;"
        "window.location.hash = '#run=20261026T040000Z';"
        f"const files = {json.dumps(files)};"
        "globalThis.fetch = async (url) => files[url]"
        " ? {ok: true, status: 200, json: async () => files[url]}"
        " : {ok: false, status: 404};"
        f"await import({(JS_DIR / 'main.js').as_uri()!r});"
        "await new Promise((r) => setTimeout(r, 50));"
        "process.stdout.write(JSON.stringify([els.healthBadge.className,"
        " els.servicesSectionTitle.textContent, els.historicalBanner.hidden,"
        " els.servicesGrid.children[0].className, els.connectionChip.hidden,"
        " els.lastUpdated.textContent.startsWith('Updated')]));"
    )
    assert out == [
        "health-badge healthy", "Core Services (Historical)", False,
        "service-card status-failed", True, True,
    ]


def test_fetch_all_times_out_a_stalled_request_or_body():
    """A fetch (or body read) that never settles must end as unreachable after
    the deadline instead of holding the refresh forever."""
    out = run_json(
        imports("data", "fetchAll")
        + f"globalThis.fetch = async () => {NEVER};"
        "const a = await fetchAll(30);"
        f"globalThis.fetch = async () => ({{ok: true, status: 200, json: () => {NEVER}}});"
        "const b = await fetchAll(30);"
        "process.stdout.write(JSON.stringify([a.results, a.reachable, b.results, b.reachable]));"
    )
    assert out == [None, False, None, False]


def test_a_stalled_refresh_does_not_block_the_next_one():
    results = {"summary": {"timestamp": "20261026T080000Z", "services": {}}, "cleanup": {}}
    out = run_json(
        FAKE_DOM
        + "globalThis.setInterval = () => 0;"
        # Every deadline fires after at most 20 ms (the real one is 30 s).
        "const realSetTimeout = setTimeout;"
        "globalThis.setTimeout = (fn, ms) => realSetTimeout(fn, Math.min(ms, 20));"
        "let stall = true;"
        f"const results = {json.dumps(results)};"
        f"globalThis.fetch = async (url) => stall ? {NEVER}"
        " : (url === 'results.json' ? {ok: true, status: 200, json: async () => results}"
        " : {ok: false, status: 404});"
        f"await import({(JS_DIR / 'main.js').as_uri()!r});"
        "await new Promise((r) => realSetTimeout(r, 100));"
        "const first = [els.connectionChip.hidden, els.connectionChip.textContent,"
        " els.refreshBtn.attrs['aria-disabled']];"
        "stall = false; els.refreshBtn.dispatch('click');"
        "await new Promise((r) => realSetTimeout(r, 100));"
        "process.stdout.write(JSON.stringify([...first, els.connectionChip.hidden]));"
    )
    assert out == [False, "Can't reach server", "false", True]


# Boot main.js against `serve` (url -> () => response); missing urls are 404.
MAIN_BOOT = (
    FAKE_DOM
    + "globalThis.setInterval = () => 0;"
    # Fetch deadlines fire after at most 20 ms (the real one is 30 s), so a
    # stalled stub does not keep node alive.
    "const realSetTimeout = setTimeout;"
    "globalThis.setTimeout = (fn, ms) => realSetTimeout(fn, Math.min(ms, 20));"
    "const tick = () => new Promise((r) => realSetTimeout(r, 30));"
    "const ok = (data) => () => ({ok: true, status: 200, json: async () => data});"
    "const status = (code) => () => ({ok: false, status: code});"
    "const offline = () => { throw new TypeError('network down'); };"
    "let serve = {}; let fetches = 0;"
    "globalThis.fetch = async (url) => { fetches += 1;"
    " return url in serve ? serve[url]() : status(404)(); };"
    "const bootMain = async () => {"
    f" await import({(JS_DIR / 'main.js').as_uri()!r}); await tick(); }};"
    "const refreshNow = async () => { els.refreshBtn.dispatch('click'); await tick(); };"
)


def test_banner_and_run_chip_keep_their_last_good_copy_on_a_failed_fetch():
    """One failed fetch must not hide an active incident banner or the
    run-in-progress chip; only a real 404 (or valid data) clears them."""
    out = run_json(
        MAIN_BOOT
        + "serve['results.json'] = ok({summary: {timestamp: 'x', services: {}}, cleanup: {}});"
        "serve['announcement-state.json'] = ok({announcements: [{id: 'a1', type: 'incident',"
        " body: 'Keystone down', created_at: new Date().toISOString()}]});"
        "serve['run_state.json'] = ok({state: 'running', started_at: new Date().toISOString()});"
        "await bootMain();"
        "const seen = () => [!els.announcementBanner.hidden, !els.runStateChip.hidden];"
        "const first = seen();"
        "serve['announcement-state.json'] = offline; serve['run_state.json'] = status(500);"
        "await refreshNow(); const second = seen();"
        "delete serve['announcement-state.json']; delete serve['run_state.json'];"
        "await refreshNow();"
        "process.stdout.write(JSON.stringify([first, second, seen()]));"
    )
    assert out == [[True, True], [True, True], [False, False]]


def test_refresh_button_keeps_focus_while_busy_and_ignores_repeat_clicks():
    out = run_json(
        MAIN_BOOT
        + f"await bootMain(); globalThis.fetch = async () => {{ fetches += 1; return {NEVER}; }};"
        "els.refreshBtn.focus(); const before = fetches;"
        "els.refreshBtn.dispatch('click'); els.refreshBtn.dispatch('click');"
        "process.stdout.write(JSON.stringify([els.refreshBtn.attrs['aria-disabled'],"
        " Boolean(els.refreshBtn.disabled), document.activeElement === els.refreshBtn,"
        " fetches - before]));"
    )
    # One refresh's worth of fetches (7 files): the second click was ignored.
    assert out == ["true", False, True, 7]


def test_footer_update_time_only_follows_a_refresh_that_loaded_data():
    out = run_json(
        MAIN_BOOT
        + "serve = {'results.json': offline, 'history.json': offline};"
        "await bootMain(); const footer = document.getElementById('lastUpdated');"
        "const failed = footer.textContent;"
        "serve['results.json'] = ok({summary: {timestamp: 'x', services: {}}, cleanup: {}});"
        "await refreshNow();"
        "process.stdout.write(JSON.stringify([failed, footer.textContent.startsWith('Updated'),"
        " els.refreshBtn.attrs['aria-disabled']]));"
    )
    assert out == ["", True, "false"]


def test_custom_theme_links_are_pruned_by_their_sheet_without_refetching():
    out = run_json(
        FAKE_DOM
        + imports("theme", "pruneFailedThemeLinks")
        + "globalThis.fetch = () => { throw new Error('must not refetch'); };"
        "const link = (sheet) => ({sheet, removed: false, remove() { this.removed = true; }});"
        "const links = [link(null), link({})];"
        "document.querySelectorAll = () => links;"
        "let onLoad = null; window.addEventListener = (type, fn) => { if (type === 'load') onLoad = fn; };"
        "document.readyState = 'interactive'; pruneFailedThemeLinks();"
        "const beforeLoad = links.map((l) => l.removed);"
        "onLoad();"
        "process.stdout.write(JSON.stringify([beforeLoad, links.map((l) => l.removed)]));"
    )
    # A link is judged only once the page (and every stylesheet) has loaded.
    assert out == [[False, False], [True, False]]
