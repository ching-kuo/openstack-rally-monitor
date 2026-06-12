#!/usr/bin/env python3
"""Single-session OpenStack API health checker.

Replaces the six per-service ``openstack <cmd>`` CLI invocations that
``health_check.sh`` used to spawn. Each CLI call started a fresh Python
interpreter and performed a full keystoneauth token issuance, so the recorded
``latency_ms`` measured ~1-3 s of client startup + auth rather than API
latency, and every 15-minute cycle issued ~6 Keystone tokens.

This module authenticates ONCE (one openstacksdk Connection, one token), times
that initial token issuance as the keystone check, then issues one cheap
read-only GET per dependent service reusing the cached token. The result is the
same ``health.json`` document shape, emitted on stdout; ``health_check.sh``
owns writing the files and history.

A third status -- ``degraded`` -- is reported when a call succeeds but takes
longer than ``HEALTH_LATENCY_WARN_MS`` (default 5000). ``degraded`` counts as
UP for availability/uptime purposes (the service is reachable, just slow); the
distinction surfaces as an amber cell on the dashboard and in the latency gauge.

Testability: :func:`run_checks` takes a connection-like object and injectable
``now_fn``/``warn_ms``, so tests inject fakes (objects that succeed fast,
succeed slowly, or raise) without needing openstacksdk installed. The real
Connection is built lazily in :func:`build_connection`, imported inside the
function so this module stays importable in environments without the SDK.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

# Service check order. keystone is first and special (its "check" is the token
# issuance done while opening the connection); the remaining five are timed
# read-only GETs that reuse the cached token. This tuple is the set of services
# for which we have a *registered checker*; RALLY_SERVICES selects a subset of
# {keystone} | DEPENDENT_SERVICES (configured names without a checker are
# skipped -- see run_checks).
DEPENDENT_SERVICES = ("nova", "neutron", "glance", "cinder", "swift")

DEFAULT_WARN_MS = 5000

# Default monitored service set; mirrors run_tests.sh DEFAULT_RALLY_SERVICES.
DEFAULT_RALLY_SERVICES = ("keystone", "nova", "neutron", "glance", "cinder", "swift")


def _utc_now_iso() -> str:
    """Current UTC time as an ISO 8601 ``Z``-suffixed string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_rally_services(raw, default=DEFAULT_RALLY_SERVICES) -> tuple:
    """Parse RALLY_SERVICES into a normalized, ordered tuple of service names.

    Normalization mirrors run_tests.sh's parse_rally_services (keep the two in
    sync): split on commas, lowercase, strip ALL whitespace, drop empty
    segments, dedupe preserving first-seen order. An unset/empty value (or one
    that normalizes to nothing) falls back to ``default``.

    keystone is ALWAYS included in the effective set and placed first: the
    health check must authenticate regardless (the openstacksdk session), and
    that token issuance *is* a keystone availability check. Listing it
    explicitly anywhere is harmless (deduped). This keeps health.json honest --
    auth liveness is always reported -- and means "keystone" never silently
    disappears from the dashboard/exporter even if an operator trims it out.
    """
    if raw is None:
        names = list(default)
    else:
        names = []
        seen = set()
        for token in str(raw).split(","):
            name = "".join(token.split()).lower()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        if not names:
            names = list(default)

    # Always include keystone, first, deduped.
    effective = ["keystone"]
    for name in names:
        if name != "keystone":
            effective.append(name)
    return tuple(effective)


def parse_warn_ms(raw, default: int = DEFAULT_WARN_MS) -> int:
    """Parse HEALTH_LATENCY_WARN_MS defensively.

    Garbage, empty, negative, or non-integer values fall back to ``default``
    so a typo in the environment can never silently disable the degraded
    threshold or make every check degraded.
    """
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return value


def _consume_one(generator) -> None:
    """Consume at most one item from a generator/iterable.

    The SDK list calls (``servers``/``networks``/``images``/``volumes``) return
    lazy generators; merely calling them does not hit the API. ``next()`` forces
    exactly one page/item to be fetched -- the cheapest way to prove the service
    answered -- and a ``StopIteration`` (empty result) is success, not failure.
    """
    next(iter(generator), None)


def _service_probes(conn):
    """Map each dependent service to a zero-arg callable that performs its GET.

    Bound here (not at import) so the connection-like object can be a test fake.
    Calls are the cheapest read-only operations the SDK offers:
      - nova:    conn.compute.servers(limit=1)        -> list servers
      - neutron: conn.network.networks(limit=1)       -> list networks
      - glance:  conn.image.images(limit=1)           -> list images
      - cinder:  conn.block_storage.volumes(limit=1)  -> list volumes
      - swift:   conn.object_store.get_account_metadata() -> HEAD account
    """
    return {
        "nova": lambda: _consume_one(conn.compute.servers(limit=1)),
        "neutron": lambda: _consume_one(conn.network.networks(limit=1)),
        "glance": lambda: _consume_one(conn.image.images(limit=1)),
        "cinder": lambda: _consume_one(conn.block_storage.volumes(limit=1)),
        # get_account_metadata is a single HEAD on the account, cheaper than
        # listing containers and sufficient to prove Swift answered.
        "swift": lambda: conn.object_store.get_account_metadata(),
    }


def _classify(latency_ms: int, warn_ms: int) -> str:
    """Map a successful call's latency to 'up' or 'degraded'."""
    return "degraded" if latency_ms > warn_ms else "up"


def _result(status: str, latency_ms: int, checked_at: str) -> dict:
    """Build a single per-service result object."""
    return {"status": status, "latency_ms": int(latency_ms), "checked_at": checked_at}


def _overall(services: dict) -> str:
    """Aggregate per-service statuses with precedence down > degraded > up."""
    statuses = [svc.get("status") for svc in services.values()]
    if any(s == "down" for s in statuses):
        return "down"
    if any(s == "degraded" for s in statuses):
        return "degraded"
    return "up"


def run_checks(conn, now_fn=None, warn_ms: int = DEFAULT_WARN_MS, services=None) -> dict:
    """Run health checks against ``conn`` and return the health document.

    ``conn`` may be a real openstacksdk Connection or any object exposing the
    same ``authorize()`` method and ``compute``/``network``/``image``/
    ``block_storage``/``object_store`` proxies (tests inject a fake).

    ``now_fn`` (defaults to ``_utc_now_iso``) stamps every result's
    ``checked_at`` and the document ``timestamp`` -- injected so tests get
    deterministic timestamps. ``warn_ms`` is the degraded threshold.

    ``services`` is the effective monitored set (normalized, keystone first);
    defaults to the full known set so existing callers/tests are unchanged.
    Only the configured dependent services are checked. A configured service
    with NO registered checker (e.g. "octavia") is SKIPPED with a stderr
    warning rather than emitted as a synthetic status. Rationale: a placeholder
    status would propagate into overall/uptime/exporter semantics (an "unknown"
    would either pollute the down>degraded>up precedence or silently count as
    up), whereas skipping it is something every consumer already tolerates --
    the dashboard derives its service list from health.json keys, and the
    exporter clears-and-sets gauges per scrape. The omission is the honest
    signal: we have no probe for it. (The smoke test in run_tests.sh is the
    place a brand-new service gets a SKIP card via a missing scenario file.)

    Keystone is checked by timing ``conn.authorize()`` (the initial token
    issuance). If that fails, no token exists, so the dependent services cannot
    be reached: keystone is reported "down" and each checked dependent service
    is reported "down" with latency 0 (they were never attempted), making the
    overall status "down".
    """
    now_fn = now_fn or _utc_now_iso
    if services is None:
        services = ("keystone",) + DEPENDENT_SERVICES

    # The dependent services we will actually probe: configured order, keystone
    # excluded (it is the auth check), unregistered names dropped with a warning.
    probes = _service_probes(conn)
    dependents = []
    for name in services:
        if name == "keystone":
            continue
        if name in probes:
            dependents.append(name)
        else:
            print(
                f"api_health_check: no registered checker for '{name}'; "
                f"skipping (not included in health.json)",
                file=sys.stderr,
            )

    results: dict = {}

    # --- keystone: time the token issuance ---------------------------------
    checked_at = now_fn()
    t0 = time.monotonic()
    auth_ok = True
    try:
        conn.authorize()
    except Exception:  # noqa: BLE001 - any SDK/auth error means keystone is down
        auth_ok = False
    keystone_ms = int(round((time.monotonic() - t0) * 1000))

    if auth_ok:
        results["keystone"] = _result(
            _classify(keystone_ms, warn_ms), keystone_ms, checked_at
        )
        # --- dependent services: one timed read-only GET each --------------
        for name in dependents:
            checked_at = now_fn()
            t0 = time.monotonic()
            try:
                probes[name]()
                reachable = True
            except Exception:  # noqa: BLE001 - any error means the service is down
                reachable = False
            ms = int(round((time.monotonic() - t0) * 1000))
            status = _classify(ms, warn_ms) if reachable else "down"
            results[name] = _result(status, ms, checked_at)
    else:
        # Without a token the dependent services are unreachable. Report them
        # all down with latency 0 (never attempted) so overall is "down".
        results["keystone"] = _result("down", keystone_ms, checked_at)
        for name in dependents:
            results[name] = _result("down", 0, now_fn())

    return {
        "timestamp": now_fn(),
        "overall": _overall(results),
        "services": results,
    }


def build_connection():
    """Build a real openstacksdk Connection from OS_* environment variables.

    Imported here (not at module top level) so the module remains importable in
    test/dev environments without openstacksdk. ``openstack.connect()`` reads
    the standard ``OS_*`` environment variables (``cloud='envvars'`` default).
    openstacksdk ships in the image as a python-openstackclient dependency, so
    it is intentionally NOT added to any requirements file.
    """
    import openstack  # noqa: PLC0415 - lazy import; see docstring

    return openstack.connect()


def main(argv=None) -> int:
    """Build the real connection, run checks, print the document on stdout.

    Exit 0 even when services are down -- producing the report IS the success.
    Exit nonzero only on an internal error (e.g. openstacksdk missing or the
    connection factory raising before any check), so the wrapper can fall back
    to a synthetic all-down document.
    """
    warn_ms = parse_warn_ms(os.environ.get("HEALTH_LATENCY_WARN_MS"))
    services = parse_rally_services(os.environ.get("RALLY_SERVICES"))
    try:
        conn = build_connection()
    except Exception as exc:  # noqa: BLE001 - SDK missing / unbuildable connection
        print(f"api_health_check: failed to build connection: {exc}", file=sys.stderr)
        return 1

    document = run_checks(conn, warn_ms=warn_ms, services=services)
    json.dump(document, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
