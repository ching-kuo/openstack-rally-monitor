"""Unit tests for scripts/api_health_check.py — the single-session OpenStack
API health checker.

These tests never touch openstacksdk. api_health_check.run_checks() takes a
connection-like object, so we inject fakes whose per-service calls succeed
fast, succeed slowly (degraded), or raise (down). The module imports
openstacksdk lazily inside build_connection(), so importing it here is safe in
an environment without the SDK.
"""
import json
import re

import api_health_check as hc

DEPENDENT = ("nova", "neutron", "glance", "cinder", "swift")
ALL_SERVICES = ("keystone",) + DEPENDENT

# ISO 8601 with a trailing Z, e.g. 2026-06-12T01:02:03Z
ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _Proxy:
    """A namespace exposing one method that runs the configured behaviour.

    `method_name` is the SDK proxy method (e.g. "servers", "networks",
    "get_account_metadata"); `behaviour` is a zero-arg callable invoked when
    that method is called. List-style methods (servers/networks/images/volumes)
    return an iterable so run_checks's next(iter(...)) consumption works; the
    behaviour runs first so it can raise or sleep.
    """

    def __init__(self, method_name, behaviour, returns_iterable):
        self._method_name = method_name
        self._behaviour = behaviour
        self._returns_iterable = returns_iterable

    def __getattr__(self, name):
        if name != self._method_name:
            raise AttributeError(name)

        def _call(*args, **kwargs):
            self._behaviour()
            # List proxies return a generator; HEAD-style returns an object.
            return iter([object()]) if self._returns_iterable else object()

        return _call


# Which proxy attribute + method each dependent service uses, and whether the
# call returns an iterable (list) vs a single object (HEAD).
_PROXY_SPEC = {
    "nova": ("compute", "servers", True),
    "neutron": ("network", "networks", True),
    "glance": ("image", "images", True),
    "cinder": ("block_storage", "volumes", True),
    "swift": ("object_store", "get_account_metadata", False),
}


class FakeConnection:
    """A connection-like object whose authorize() and each service proxy run an
    injected behaviour.

    `behaviours` maps a service name -> zero-arg callable (default: a no-op
    "fast success"). The "keystone" behaviour runs inside authorize().
    """

    def __init__(self, behaviours=None):
        self._behaviours = dict(behaviours or {})
        for svc, (attr, method, returns_iter) in _PROXY_SPEC.items():
            behaviour = self._behaviours.get(svc, lambda: None)
            setattr(self, attr, _Proxy(method, behaviour, returns_iter))

    def authorize(self):
        self._behaviours.get("keystone", lambda: None)()
        return "fake-token"


def ok():
    """A fast, successful call (no measurable latency)."""
    return None


def boom():
    """A failing call -> the service is down."""
    raise RuntimeError("simulated failure")


class FakeClock:
    """Deterministic monotonic() that advances by a fixed step on each call.

    run_checks() reads the clock twice per service (before/after). With
    `step_ms` ms per advance, each service's measured latency is exactly
    `step_ms`. now_fn is supplied separately for stable ISO timestamps.
    """

    def __init__(self, step_ms):
        self._step = step_ms / 1000.0
        self._t = 0.0

    def __call__(self):
        cur = self._t
        self._t += self._step
        return cur


FIXED_NOW = "2026-06-12T00:00:00Z"


def run(conn, *, step_ms=0, warn_ms=hc.DEFAULT_WARN_MS, now=FIXED_NOW, services=None):
    """Drive run_checks with a deterministic clock and timestamp.

    ``services`` (the effective monitored set) defaults to None so run_checks
    uses its own full-known-set default; pass a tuple to exercise a configured
    subset.
    """
    clock = FakeClock(step_ms)
    # Monkeypatch time.monotonic for the duration of the call.
    orig = hc.time.monotonic
    hc.time.monotonic = clock
    try:
        return hc.run_checks(conn, now_fn=lambda: now, warn_ms=warn_ms, services=services)
    finally:
        hc.time.monotonic = orig


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_all_up_when_every_call_succeeds_fast():
    doc = run(FakeConnection(), step_ms=10)
    assert doc["overall"] == "up"
    assert set(doc["services"]) == set(ALL_SERVICES)
    for svc in ALL_SERVICES:
        assert doc["services"][svc]["status"] == "up"
        assert doc["services"][svc]["latency_ms"] == 10


def test_document_shape_keys_types_and_iso_timestamps():
    doc = run(FakeConnection(), step_ms=42)

    assert set(doc) == {"timestamp", "overall", "services"}
    assert ISO_Z_RE.match(doc["timestamp"])
    for svc, result in doc["services"].items():
        assert set(result) == {"status", "latency_ms", "checked_at"}
        assert isinstance(result["latency_ms"], int)  # not float
        assert ISO_Z_RE.match(result["checked_at"])
    # The whole document must be JSON-serialisable (it is dumped to stdout).
    json.loads(json.dumps(doc))


# ---------------------------------------------------------------------------
# Degraded
# ---------------------------------------------------------------------------
def test_slow_service_is_degraded_not_down():
    # step_ms 6000 > default warn 5000 -> every successful call is degraded.
    doc = run(FakeConnection(), step_ms=6000)
    assert doc["overall"] == "degraded"
    for svc in ALL_SERVICES:
        assert doc["services"][svc]["status"] == "degraded"
        assert doc["services"][svc]["latency_ms"] == 6000


def test_latency_exactly_at_threshold_is_up():
    # Boundary: latency_ms == warn_ms is up (degraded only when strictly over).
    doc = run(FakeConnection(), step_ms=5000, warn_ms=5000)
    assert doc["overall"] == "up"
    assert all(s["status"] == "up" for s in doc["services"].values())


def test_single_slow_service_makes_overall_degraded():
    # Only neutron is slow; the rest are instant. With step_ms=0 the fast
    # services measure 0ms; we make neutron raise-via-sleep is overkill, so we
    # instead give neutron its own slow behaviour using a per-call clock bump.
    # Simpler: use a connection where neutron's behaviour advances the clock.
    clock = FakeClock(0)

    def slow_neutron():
        clock._t += 6.0  # add 6000ms inside the timed window

    conn = FakeConnection({"neutron": slow_neutron})
    orig = hc.time.monotonic
    hc.time.monotonic = clock
    try:
        doc = hc.run_checks(conn, now_fn=lambda: FIXED_NOW, warn_ms=5000)
    finally:
        hc.time.monotonic = orig

    assert doc["services"]["neutron"]["status"] == "degraded"
    assert doc["services"]["nova"]["status"] == "up"
    assert doc["overall"] == "degraded"


# ---------------------------------------------------------------------------
# Down (per-service failure)
# ---------------------------------------------------------------------------
def test_failed_service_is_down_and_others_unaffected():
    doc = run(FakeConnection({"cinder": boom}), step_ms=10)
    assert doc["services"]["cinder"]["status"] == "down"
    # The failed call still records a measured latency (>= 0).
    assert doc["services"]["cinder"]["latency_ms"] >= 0
    assert doc["services"]["nova"]["status"] == "up"
    assert doc["overall"] == "down"


def test_down_takes_precedence_over_degraded():
    # nova slow (degraded), glance down -> overall down (down > degraded).
    clock = FakeClock(0)

    def slow_nova():
        clock._t += 6.0

    conn = FakeConnection({"nova": slow_nova, "glance": boom})
    orig = hc.time.monotonic
    hc.time.monotonic = clock
    try:
        doc = hc.run_checks(conn, now_fn=lambda: FIXED_NOW, warn_ms=5000)
    finally:
        hc.time.monotonic = orig

    assert doc["services"]["nova"]["status"] == "degraded"
    assert doc["services"]["glance"]["status"] == "down"
    assert doc["overall"] == "down"


# ---------------------------------------------------------------------------
# Auth failure
# ---------------------------------------------------------------------------
def test_auth_failure_marks_keystone_and_all_dependents_down():
    doc = run(FakeConnection({"keystone": boom}), step_ms=10)

    assert doc["overall"] == "down"
    assert doc["services"]["keystone"]["status"] == "down"
    # keystone records the time spent failing to authorize (>= 0).
    assert doc["services"]["keystone"]["latency_ms"] >= 0
    # Dependents are never attempted -> down with latency 0.
    for svc in DEPENDENT:
        assert doc["services"][svc]["status"] == "down"
        assert doc["services"][svc]["latency_ms"] == 0


def test_auth_failure_does_not_call_service_proxies():
    called = {"nova": False}

    def fail_auth():
        raise RuntimeError("no token")

    def mark_nova():
        called["nova"] = True

    doc = run(FakeConnection({"keystone": fail_auth, "nova": mark_nova}))
    assert called["nova"] is False  # proxy never invoked without a token
    assert doc["services"]["nova"]["status"] == "down"


# ---------------------------------------------------------------------------
# Overall aggregation precedence (unit test of _overall directly)
# ---------------------------------------------------------------------------
def test_overall_precedence_down_beats_degraded_beats_up():
    def svc(status):
        return {"status": status, "latency_ms": 0, "checked_at": FIXED_NOW}

    assert hc._overall({"a": svc("up"), "b": svc("up")}) == "up"
    assert hc._overall({"a": svc("up"), "b": svc("degraded")}) == "degraded"
    assert hc._overall({"a": svc("degraded"), "b": svc("down")}) == "down"
    assert hc._overall({"a": svc("up"), "b": svc("down")}) == "down"


# ---------------------------------------------------------------------------
# warn_ms parsing
# ---------------------------------------------------------------------------
def test_parse_warn_ms_valid():
    assert hc.parse_warn_ms("2500") == 2500
    assert hc.parse_warn_ms("  750 ") == 750
    assert hc.parse_warn_ms(3000) == 3000


def test_parse_warn_ms_garbage_falls_back_to_default():
    for bad in (None, "", "abc", "5s", "1.5", "0", "-100", "  "):
        assert hc.parse_warn_ms(bad) == hc.DEFAULT_WARN_MS


def test_parse_warn_ms_custom_default():
    assert hc.parse_warn_ms("nope", default=1234) == 1234


# ---------------------------------------------------------------------------
# main() / build_connection error handling
# ---------------------------------------------------------------------------
def test_main_returns_nonzero_when_connection_unbuildable(monkeypatch, capsys):
    def explode():
        raise RuntimeError("openstacksdk missing")

    monkeypatch.setattr(hc, "build_connection", explode)
    rc = hc.main([])
    assert rc != 0
    # Error goes to stderr; stdout must stay empty so the wrapper does not mv a
    # truncated/garbage health.json into place.
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "failed to build connection" in captured.err


def test_main_prints_document_and_returns_zero_even_when_down(monkeypatch, capsys):
    monkeypatch.setattr(hc, "build_connection", lambda: FakeConnection({"swift": boom}))
    rc = hc.main([])
    assert rc == 0  # producing the report IS success, even with a down service
    doc = json.loads(capsys.readouterr().out)
    assert doc["services"]["swift"]["status"] == "down"
    assert doc["overall"] == "down"


def test_main_honours_health_latency_warn_ms_env(monkeypatch, capsys):
    # A very low threshold via the env makes any nonzero latency degraded.
    monkeypatch.setenv("HEALTH_LATENCY_WARN_MS", "1")

    clock = FakeClock(50)  # 50ms per service
    monkeypatch.setattr(hc, "build_connection", lambda: FakeConnection())
    monkeypatch.setattr(hc.time, "monotonic", clock)

    rc = hc.main([])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["overall"] == "degraded"
    assert all(s["status"] == "degraded" for s in doc["services"].values())


def test_main_honours_rally_services_env(monkeypatch, capsys):
    # RALLY_SERVICES trims the monitored set; main() must parse it and only
    # check the configured (registered) services, always including keystone.
    monkeypatch.setenv("RALLY_SERVICES", "nova,cinder")
    monkeypatch.setattr(hc, "build_connection", lambda: FakeConnection())

    rc = hc.main([])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert set(doc["services"]) == {"keystone", "nova", "cinder"}


# ---------------------------------------------------------------------------
# parse_rally_services
# ---------------------------------------------------------------------------
def test_parse_rally_services_default_when_unset():
    # An unset value (None) falls back to the documented six-service default,
    # keystone first.
    assert hc.parse_rally_services(None) == hc.DEFAULT_RALLY_SERVICES
    assert hc.parse_rally_services(None)[0] == "keystone"


def test_parse_rally_services_empty_and_blank_fall_back_to_default():
    # Empty string, whitespace-only, and comma/whitespace-only all normalize to
    # nothing -> default. keystone is then still present (always-included).
    for raw in ("", "   ", ",", " , , ", ",,,"):
        result = hc.parse_rally_services(raw)
        # keystone-always means at minimum the keystone-prepended default set.
        assert result == hc.DEFAULT_RALLY_SERVICES, raw


def test_parse_rally_services_trims_lowercases_and_dedupes_preserving_order():
    # Mixed case, surrounding/interior whitespace, empty segments, and a
    # duplicate. Order is first-seen; keystone is forced first.
    raw = "  Nova , KEYSTONE , nova ,, Cinder , GLANCE "
    result = hc.parse_rally_services(raw)
    # keystone hoisted to front; remaining preserve first-seen order, deduped.
    assert result == ("keystone", "nova", "cinder", "glance")


def test_parse_rally_services_strips_internal_whitespace():
    # Whitespace INSIDE a token (not just around it) is stripped, matching the
    # bash awk gsub(/[[:space:]]/, "") behaviour.
    assert hc.parse_rally_services("no va, cind er") == ("keystone", "nova", "cinder")


def test_parse_rally_services_always_includes_keystone_first():
    # keystone is included and placed first even when absent from the input...
    result = hc.parse_rally_services("nova,neutron")
    assert result[0] == "keystone"
    assert result == ("keystone", "nova", "neutron")
    # ...and when listed (anywhere) it is deduped, not duplicated.
    result = hc.parse_rally_services("nova,keystone,neutron")
    assert result == ("keystone", "nova", "neutron")
    assert result.count("keystone") == 1
    # ...and when listed but not first, it still ends up first.
    result = hc.parse_rally_services("nova,neutron,keystone")
    assert result[0] == "keystone"


def test_parse_rally_services_keeps_unregistered_names():
    # Parsing does not know about the checker registry -- it keeps unknown names
    # (e.g. octavia); run_checks is where unregistered names are dropped. The
    # operator order is preserved (keystone still hoisted).
    assert hc.parse_rally_services("octavia,nova") == ("keystone", "octavia", "nova")


def test_parse_rally_services_custom_default():
    assert hc.parse_rally_services(None, default=("keystone", "nova")) == ("keystone", "nova")


# ---------------------------------------------------------------------------
# run_checks honours the configured service subset
# ---------------------------------------------------------------------------
def test_run_checks_only_checks_configured_services():
    # A trimmed set: only keystone + nova are checked; the other dependents are
    # absent from the output entirely (not reported as some placeholder status).
    doc = run(FakeConnection(), step_ms=10, services=("keystone", "nova"))
    assert set(doc["services"]) == {"keystone", "nova"}
    assert doc["services"]["keystone"]["status"] == "up"
    assert doc["services"]["nova"]["status"] == "up"


def test_run_checks_preserves_configured_order_keystone_first():
    # Output key order follows the configured (effective) order: keystone is
    # always emitted first, then the configured dependents in order.
    doc = run(FakeConnection(), step_ms=10, services=("keystone", "cinder", "nova"))
    assert list(doc["services"].keys()) == ["keystone", "cinder", "nova"]


def test_run_checks_skips_unregistered_service_with_stderr_warning(capsys):
    # A configured service with NO registered checker (octavia) is skipped and
    # warned about on stderr -- it must NOT appear in the output document, and
    # must NOT affect overall (which stays up with everything else fast).
    doc = run(
        FakeConnection(),
        step_ms=10,
        services=("keystone", "nova", "octavia"),
    )
    assert "octavia" not in doc["services"]
    assert set(doc["services"]) == {"keystone", "nova"}
    assert doc["overall"] == "up"
    err = capsys.readouterr().err
    assert "octavia" in err
    assert "no registered checker" in err


def test_run_checks_keystone_always_present_even_if_not_in_dependents():
    # Even a degenerate effective set that omits keystone (defensive: callers
    # should never do this since parse_rally_services hoists it) still checks
    # auth -- run_checks always times authorize() and emits keystone.
    doc = run(FakeConnection(), step_ms=10, services=("nova",))
    assert "keystone" in doc["services"]
    assert doc["services"]["keystone"]["status"] == "up"


def test_run_checks_unregistered_only_still_reports_keystone(capsys):
    # If every configured dependent is unregistered, the document still carries
    # keystone (the auth check) and nothing else; the unknown name is warned.
    doc = run(FakeConnection(), step_ms=10, services=("keystone", "heat"))
    assert set(doc["services"]) == {"keystone"}
    assert doc["overall"] == "up"
    assert "heat" in capsys.readouterr().err


def test_run_checks_subset_down_when_configured_service_fails():
    # Down propagation still works within a trimmed set.
    doc = run(
        FakeConnection({"cinder": boom}),
        step_ms=10,
        services=("keystone", "cinder"),
    )
    assert doc["services"]["cinder"]["status"] == "down"
    assert doc["overall"] == "down"
    assert "nova" not in doc["services"]  # not configured
