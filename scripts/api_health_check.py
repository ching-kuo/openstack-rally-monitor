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
# read-only GETs that reuse the cached token.
DEPENDENT_SERVICES = ("nova", "neutron", "glance", "cinder", "swift")

DEFAULT_WARN_MS = 5000


def _utc_now_iso() -> str:
    """Current UTC time as an ISO 8601 ``Z``-suffixed string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def run_checks(conn, now_fn=None, warn_ms: int = DEFAULT_WARN_MS) -> dict:
    """Run all health checks against ``conn`` and return the health document.

    ``conn`` may be a real openstacksdk Connection or any object exposing the
    same ``authorize()`` method and ``compute``/``network``/``image``/
    ``block_storage``/``object_store`` proxies (tests inject a fake).

    ``now_fn`` (defaults to ``_utc_now_iso``) stamps every result's
    ``checked_at`` and the document ``timestamp`` -- injected so tests get
    deterministic timestamps. ``warn_ms`` is the degraded threshold.

    Keystone is checked by timing ``conn.authorize()`` (the initial token
    issuance). If that fails, no token exists, so the five dependent services
    cannot be reached: keystone is reported "down" and each dependent service
    is reported "down" with latency 0 (they were never attempted), making the
    overall status "down".
    """
    now_fn = now_fn or _utc_now_iso
    services: dict = {}

    # --- keystone: time the token issuance ---------------------------------
    checked_at = now_fn()
    t0 = time.monotonic()
    auth_ok = True
    try:
        conn.authorize()
    except Exception:  # noqa: BLE001 - any SDK/auth error means keystone is down
        auth_ok = False
    keystone_ms = int(round((time.monotonic() - t0) * 1000))

    if not auth_ok:
        # Without a token the dependent services are unreachable. Report them
        # all down with latency 0 (never attempted) so overall is "down".
        services["keystone"] = _result("down", keystone_ms, checked_at)
        for name in DEPENDENT_SERVICES:
            services[name] = _result("down", 0, now_fn())
        return {
            "timestamp": now_fn(),
            "overall": _overall(services),
            "services": services,
        }

    services["keystone"] = _result(
        _classify(keystone_ms, warn_ms), keystone_ms, checked_at
    )

    # --- dependent services: one timed read-only GET each ------------------
    probes = _service_probes(conn)
    for name in DEPENDENT_SERVICES:
        checked_at = now_fn()
        t0 = time.monotonic()
        try:
            probes[name]()
            ms = int(round((time.monotonic() - t0) * 1000))
            status = _classify(ms, warn_ms)
        except Exception:  # noqa: BLE001 - any error means the service is down
            ms = int(round((time.monotonic() - t0) * 1000))
            status = "down"
        services[name] = _result(status, ms, checked_at)

    return {
        "timestamp": now_fn(),
        "overall": _overall(services),
        "services": services,
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
    try:
        conn = build_connection()
    except Exception as exc:  # noqa: BLE001 - SDK missing / unbuildable connection
        print(f"api_health_check: failed to build connection: {exc}", file=sys.stderr)
        return 1

    document = run_checks(conn, warn_ms=warn_ms)
    json.dump(document, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
