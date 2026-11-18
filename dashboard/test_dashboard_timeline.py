"""Node-driven tests for the run timeline: pinning by timestamp, the #run=
permalink, and keyboard focus across re-renders (see node_testlib.py)."""

from node_testlib import FAKE_DOM, imports, requires_node, run_json

pytestmark = requires_node


def test_parse_run_hash_accepts_only_run_timestamps():
    out = run_json(
        imports("status", "parseRunHash")
        + "process.stdout.write(JSON.stringify(['#run=20261026T120000Z', '#run=../x',"
        " '#run=20261026T120000Z<b>', '', '#overview'].map(parseRunHash)));"
    )
    assert out == ["20261026T120000Z", None, None, None, None]


SELECT_SETUP = (
    FAKE_DOM
    + imports("data", "state")
    + imports("timeline", "selectRun", "renderSelection", "backToLatest", "renderTimeline")
    + "const run = (ts) => ({timestamp: ts, services: {nova: {status: 'passed'}}});"
    "state.history = {runs: [run('20261026T000000Z'), run('20261026T040000Z'),"
    " run('20261026T080000Z')]};"
    "state.historyLoaded = true;"
    "state.results = {summary: run('20261026T080000Z')};"
    "const cellFor = (ts) => els.timeline.children.find((c) => c.dataset.key === ts);"
)


def test_select_run_pins_by_timestamp_and_writes_the_permalink():
    out = run_json(
        SELECT_SETUP
        + "selectRun('20261026T040000Z');"
        # Pruning the oldest run must not move the pin onto another run.
        "state.history.runs.shift(); renderSelection();"
        "const first = [state.pinnedRunTs, els.historicalBanner.hidden,"
        " els.servicesSectionTitle.textContent, replacedUrls.at(-1)];"
        "backToLatest();"
        "process.stdout.write(JSON.stringify([...first, state.pinnedRunTs, replacedUrls.at(-1)]));"
    )
    assert out == [
        "20261026T040000Z", False, "Core Services (Historical)", "/#run=20261026T040000Z",
        None, "/",
    ]


def test_unknown_pinned_run_falls_back_to_latest_with_a_notice():
    out = run_json(
        SELECT_SETUP
        + "state.pinnedRunTs = '20250101T000000Z'; renderSelection();"
        "process.stdout.write(JSON.stringify([state.pinnedRunTs, els.runNotice.hidden,"
        " els.runNotice.textContent.includes('not in the retained history'),"
        " els.historicalBanner.hidden]));"
    )
    assert out == [None, False, True, True]


def test_pin_and_hash_survive_until_history_has_loaded():
    """A #run= permalink must not be erased because history.json failed or has
    not loaded yet; it is resolved once history actually loads."""
    out = run_json(
        SELECT_SETUP
        + "state.historyLoaded = false; state.history = {runs: []};"
        "state.pinnedRunTs = '20261026T040000Z'; renderSelection();"
        "const before = [state.pinnedRunTs, replacedUrls.length, els.runNotice.hidden];"
        "state.history = {runs: [run('20261026T040000Z'), run('20261026T080000Z')]};"
        "state.historyLoaded = true; renderSelection();"
        "process.stdout.write(JSON.stringify([...before, state.pinnedRunTs,"
        " els.historicalBanner.hidden]));"
    )
    assert out == ["20261026T040000Z", 0, True, "20261026T040000Z", False]


def test_keyboard_focus_survives_selecting_a_run_and_refreshing():
    out = run_json(
        SELECT_SETUP
        + "renderTimeline(); cellFor('20261026T040000Z').focus();"
        "selectRun('20261026T040000Z');"
        "const afterSelect = document.activeElement;"
        "const selectInPage = els.timeline.children.includes(afterSelect);"
        "const marks = () => els.timeline.children.map((c) =>"
        " [c.classList.contains('selected'), c.attrs['aria-current'] ?? null, c.tabIndex]);"
        "const selectedMarks = marks();"
        "renderTimeline();"  # a refresh re-renders the cells again
        "const afterRefresh = document.activeElement;"
        "process.stdout.write(JSON.stringify([afterSelect.dataset.key, selectInPage,"
        " afterRefresh.dataset.key, els.timeline.children.includes(afterRefresh),"
        " selectedMarks, marks()]));"
    )
    # Selecting marks the cell in place exactly as a full re-render does.
    marks = [[False, None, -1], [True, "true", 0], [False, None, -1]]
    assert out == ["20261026T040000Z", True, "20261026T040000Z", True, marks, marks]


def test_roving_stop_follows_the_pinned_run_when_focus_is_elsewhere():
    out = run_json(
        SELECT_SETUP
        + "state.pinnedRunTs = '20261026T000000Z'; renderTimeline();"
        "process.stdout.write(JSON.stringify([els.timeline.children.map((c) => c.tabIndex),"
        " document.activeElement]));"
    )
    assert out == [[0, -1, -1], None]


def test_health_cell_focus_survives_a_refresh():
    out = run_json(
        FAKE_DOM
        + imports("health", "renderHealthTimeline")
        + "const check = (ts) => ({timestamp: ts, overall: 'up', services: {}});"
        "renderHealthTimeline({checks: [check('2026-10-26T10:00:00Z'), check('2026-10-26T10:15:00Z')]});"
        "els.healthTimeline.children[0].focus();"
        # A new check arrives; the focused check is now the second-oldest cell.
        "renderHealthTimeline({checks: [check('2026-10-26T09:45:00Z'),"
        " check('2026-10-26T10:00:00Z'), check('2026-10-26T10:15:00Z')]});"
        "const cells = els.healthTimeline.children;"
        "process.stdout.write(JSON.stringify([cells.indexOf(document.activeElement),"
        " cells.map((c) => c.tabIndex)]));"
    )
    assert out == [1, [-1, 0, -1]]


def test_roving_tab_stop_and_edge_tooltips():
    out = run_json(
        imports("dom", "setRovingTabStop", "tooltipEdgeClass")
        + "const cells = [{}, {}, {}];"
        "setRovingTabStop(cells, null); const a = cells.map((c) => c.tabIndex);"
        "setRovingTabStop(cells, cells[1]); const b = cells.map((c) => c.tabIndex);"
        "const edges = (count, idx) => idx.map((i) => tooltipEdgeClass(i, count));"
        "process.stdout.write(JSON.stringify([a, b, edges(20, [0, 9, 10, 19]),"
        " edges(9, [0, 3, 4, 5, 7, 8]), tooltipEdgeClass(0, 1)]));"
    )
    # Left half anchors left, right half right, the exact middle centres: with
    # tooltips capped at half the viewport (style.css) none can overflow. Run 8
    # of 9 used to centre (below the old 85% band) and clip on a 400px phone.
    assert out == [
        [-1, -1, 0],
        [-1, 0, -1],
        ["tip-left", "tip-left", "tip-right", "tip-right"],
        ["tip-left", "tip-left", "", "tip-right", "tip-right", "tip-right"],
        "",
    ]


def test_clearing_the_hash_returns_to_the_latest_run():
    out = run_json(
        SELECT_SETUP
        + imports("timeline", "applyLocationHash")
        + "const seen = [];"
        "for (const hash of ['#run=20261026T040000Z', '#overview', '', '#run=20261026T040000Z', '#']) {"
        "  window.location.hash = hash; applyLocationHash(); seen.push(state.pinnedRunTs);"
        "}"
        "process.stdout.write(JSON.stringify(seen));"
    )
    # An unrelated fragment keeps the pin; an empty hash or a bare "#" clears it.
    assert out == ["20261026T040000Z", "20261026T040000Z", None, "20261026T040000Z", None]


def test_permalink_to_a_newer_run_refreshes_history_before_giving_up():
    """A run that finished after the last refresh is not in the cached history
    yet: refresh once (keeping the pin and hash meanwhile) before declaring it
    unretained."""
    out = run_json(
        SELECT_SETUP
        + imports("timeline", "applyLocationHash", "setRefreshHook")
        + "let finish; let refreshes = 0;"
        "setRefreshHook(() => { refreshes += 1; return new Promise((r) => { finish = r; }); });"
        "window.location.hash = '#run=20261026T120000Z'; applyLocationHash();"
        "const during = [state.pinnedRunTs, replacedUrls.length, els.runNotice.hidden];"
        "renderSelection();"  # a render while the refresh runs keeps the pin too
        "const stillPinned = state.pinnedRunTs;"
        "state.history.runs.push(run('20261026T120000Z')); finish();"
        "await new Promise((r) => setTimeout(r, 10));"
        "process.stdout.write(JSON.stringify([during, stillPinned, refreshes, state.pinnedRunTs,"
        " els.runNotice.hidden]));"
    )
    assert out == [
        ["20261026T120000Z", 0, True], "20261026T120000Z", 1, "20261026T120000Z", True,
    ]


def test_permalink_to_an_unknown_run_falls_back_after_the_refresh():
    out = run_json(
        SELECT_SETUP
        + imports("timeline", "applyLocationHash", "setRefreshHook")
        + "setRefreshHook(async () => {});"
        "window.location.hash = '#run=20250101T000000Z'; applyLocationHash();"
        "await new Promise((r) => setTimeout(r, 10));"
        "process.stdout.write(JSON.stringify([state.pinnedRunTs, els.runNotice.hidden,"
        " replacedUrls.at(-1)]));"
    )
    assert out == [None, False, "/"]


def test_timeline_cell_details_for_skipped_pending_and_empty_runs():
    out = run_json(
        FAKE_DOM
        + imports("data", "state")
        + imports("timeline", "renderTimeline")
        + "state.history = {runs: ["
        "  {timestamp: '20261026T000000Z', services: {keystone: {status: 'passed'}, octavia: {status: 'skipped'}}},"
        "  {timestamp: '20261026T040000Z', services: {octavia: {status: 'skipped'}, heat: {status: 'skipped'}}},"
        "  {timestamp: '20261026T080000Z', services: {keystone: {status: 'passed'}, nova: {status: 'pending'}}},"
        "  {timestamp: '20261026T120000Z', services: {}}"
        "]};"
        "renderTimeline();"
        "process.stdout.write(JSON.stringify(els.timeline.children.map((c) =>"
        "  [c.className, c.attrs['aria-label']])));"
    )
    assert out[0][0].startswith("timeline-cell passed")
    assert "All services passed" in out[0][1]
    assert out[1][0].startswith("timeline-cell failed")
    assert "All services skipped" in out[1][1]
    assert out[2][0].startswith("timeline-cell pending")
    assert "Some services pending." in out[2][1]
    assert out[3][0].startswith("timeline-cell failed")
    assert "No service data" in out[3][1]
