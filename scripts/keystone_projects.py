#!/usr/bin/env python3
"""Keystone project inventory over ONE openstacksdk session, failing closed.

Keystone v3 has no marker pagination for ``GET /v3/projects``: when the
deployment sets ``[DEFAULT] list_limit`` (or ``[resource] list_limit``) the
response silently stops at that many entries and carries ``"truncated": true``.
``openstack project list`` does not surface that flag, so a truncated list
made live projects look deleted -- and rgw_find_orphaned_users then reported
their RGW users as orphans (the manual purge reaps empty ones). This module
reads the raw response and raises :class:`TruncatedListing` instead.

Two consumers:
  * rgw_helpers.sh -- the bulk list for the orphan anti-join (CLI, no args),
    and, when that list is truncated or fails, per-project existence checks
    (``--check``: IDs on stdin, one ``<id> exists|gone|error`` line each, all
    over the same session instead of one ``openstack project show`` process
    per RGW user). Before reading stdin, ``--check`` first verifies that the
    token's own ``current_project_id`` exists so an auth or Identity-endpoint
    404 (or an unscoped/domain-scoped token) fails closed with exit 1 and
    prints nothing to stdout.
  * rally_project_tracker.py -- lists only ``c_rally_*`` projects with the
    server-side inexact filter ``name__startswith`` (keystone/api/projects.py
    passes ``name`` to build_driver_hints, which accepts ``<attr>__startswith``),
    so a cloud whose total project count exceeds list_limit does not make every
    tracker snapshot fail. A Keystone that ignores the filter just returns more
    projects; callers still match the prefix client-side.

ID charset: listed IDs are printed only when they match ``^[A-Za-z0-9_-]+$``.
That must stay a SUPERSET of the implicit-tenant uid charset in
rgw_helpers.sh (rgw_implicit_project_id / rgw_list_implicit_users:
``[A-Za-z0-9-]``) -- KEEP IN SYNC -- so an ID omitted here can never match an
RGW user (omitting it can only hide existence, never invent an orphan, and an
ID with whitespace could otherwise break line-based consumers).

CLI exit codes: 0 ok, 3 truncated listing, 1 any other failure.
"""
import argparse
import re
import sys

# One openstacksdk connection recipe (OS_* env, per-request timeout).
from api_health_check import build_connection

# Always fullmatch(): with re.match, `$` also matches before a trailing
# newline, so "victim\n" would pass and print as two lines.
PRINTABLE_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


class TruncatedListing(RuntimeError):
    """Keystone returned a partial project list (list_limit reached)."""


def list_projects(conn, name_prefix: str = "") -> list:
    """``[(id, name), ...]`` for the projects visible to ``conn``.

    ``name_prefix`` adds the server-side ``name__startswith`` filter (results
    are NOT re-filtered here). ``conn`` is an openstacksdk Connection (or a
    fake exposing ``identity.get(path)`` returning a response with
    ``status_code`` and ``json()``). Raises on HTTP errors, a malformed body,
    a structurally malformed record (non-object, missing/non-string/empty id,
    non-string name) -- one such record fails the whole listing, since
    dropping it would make a live project look deleted -- and truncation.
    """
    path = f"/projects?name__startswith={name_prefix}" if name_prefix else "/projects"
    response = conn.identity.get(path)
    status = getattr(response, "status_code", 200)
    if status >= 400:
        raise RuntimeError(f"GET /v3{path} returned HTTP {status}")
    body = response.json()
    if not isinstance(body, dict) or not isinstance(body.get("projects"), list):
        raise RuntimeError(f"GET /v3{path} returned an unexpected body")
    if body.get("truncated") or (body.get("links") or {}).get("next"):
        raise TruncatedListing(
            "Keystone truncated the project list (list_limit); refusing a partial view"
        )
    projects = []
    for record in body["projects"]:
        project_id = record.get("id") if isinstance(record, dict) else None
        name = record.get("name") if isinstance(record, dict) else None
        if not isinstance(project_id, str) or not project_id \
                or not isinstance(name, (str, type(None))):
            raise RuntimeError(f"GET /v3{path} returned a malformed record: {record!r:.120}")
        projects.append((project_id, name or ""))
    return projects


def project_status(conn, project_id: str) -> str:
    """``exists`` / ``gone`` (HTTP 404) / ``error`` for one project ID."""
    if not PRINTABLE_ID_RE.fullmatch(project_id):
        return "error"
    try:
        response = conn.identity.get(f"/projects/{project_id}")
    except Exception as exc:  # noqa: BLE001 - raise_exc sessions raise on 404
        status = getattr(exc, "http_status", None) or getattr(exc, "status_code", None)
        return "gone" if status == 404 else "error"
    status = getattr(response, "status_code", 0)
    if status == 200:
        return "exists"
    return "gone" if status == 404 else "error"


def main(argv=None, stdin=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="read project IDs on stdin; print '<id> exists|gone|error'")
    args = parser.parse_args(argv)
    stdin = stdin if stdin is not None else sys.stdin
    try:
        conn = build_connection()
        if args.check:
            own_project_id = conn.current_project_id
            if not isinstance(own_project_id, str):
                print(
                    "keystone_projects: token has no project scope "
                    f"(current_project_id={own_project_id!r}); refusing --check",
                    file=sys.stderr,
                )
                return 1
            own_status = project_status(conn, own_project_id)
            if own_status != "exists":
                print(
                    f"keystone_projects: token project {own_project_id!r} check "
                    f"returned {own_status!r}; refusing --check",
                    file=sys.stderr,
                )
                return 1
            for line in stdin:
                project_id = line.strip()
                if project_id:
                    print(f"{project_id} {project_status(conn, project_id)}")
            return 0
        projects = list_projects(conn)
    except TruncatedListing as exc:
        print(f"keystone_projects: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 - SDK missing, auth, HTTP, body
        print(f"keystone_projects: failed: {exc}", file=sys.stderr)
        return 1
    for project_id, _name in projects:
        if PRINTABLE_ID_RE.fullmatch(project_id):
            print(project_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
