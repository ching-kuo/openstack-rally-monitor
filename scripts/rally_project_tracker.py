#!/usr/bin/env python3
"""Long-lived Rally context-project provenance tracker (one per service task).

``run_tests.sh::run_service_tests`` starts one of these in the background
around each ``rally task start``. It records every ``c_rally_*`` Keystone
project that appears while the task runs into the RGW provenance ledger
(``rally_project_ids.log``), which is the SOLE authorization basis for
``auto_purge_rgw`` (see rgw_helpers.sh) -- including purge-data deletes, so a
wrong ledger line can destroy data. Every doubt therefore resolves to "do not
record".

It replaces a bash loop that spawned ``openstack project list`` every ~5 s for
the whole task: each spawn started a fresh interpreter and issued a fresh
Keystone token before an admin all-project list. Here ONE openstacksdk
Connection (one token, refreshed by keystoneauth only when it expires) serves
the snapshot and every poll. The poll cadence is unchanged on purpose: Rally
contexts can create and delete a project within seconds, so a longer interval
would miss short-lived contexts.

Semantics:
  1. Snapshot the c_rally_* projects that exist BEFORE the task starts; they
     are "seen" and never attributed to this service. The snapshot is retried
     (SNAPSHOT_ATTEMPTS); if it still fails, the tracker records NOTHING for
     this task -- an empty seen-set would attribute pre-existing projects
     (other runs' leftovers) to this one and authorize purging them. Their
     RGW orphans then stay unknown-owner (never auto-purged).
  2. A line is written to the ready FIFO once the snapshot is done (or
     abandoned); the shell waits for it before starting the task, so the
     snapshot never races the task.
  3. Poll every ``interval`` seconds until SIGTERM (sent by the shell once the
     task exits) or until the parent shell dies. Each new project ID is
     appended as ``<UTC ts> <project_id> <service>`` and logged. A failed poll
     (including a truncated Keystone listing, see keystone_projects.py) logs
     one warning per tracker; later failures are silent.
  4. One final poll after the stop signal (its failure is ignored), so
     projects created right before the task exited are still recorded.

The SIGTERM handler only sets a flag: calling ``threading.Event.set()`` from
a handler can deadlock against the main thread waiting on the same Event's
lock, so the loop sleeps in short slices and checks the flag instead.

Testability: :class:`Tracker` and :func:`run` take a connection-like object
(see keystone_projects.list_projects) plus injectable clock/log/sleep
functions; the real Connection is built lazily so the module imports without
openstacksdk.
"""
import argparse
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone

from keystone_projects import PRINTABLE_ID_RE, build_connection, list_projects

CONTEXT_PREFIX = "c_rally_"
# The RALLY_SERVICES allowlist (os_helpers.sh::parse_rally_services).
SERVICE_RE = re.compile(r"[a-z0-9_-]+")
DEFAULT_INTERVAL_SECONDS = 5.0
SNAPSHOT_ATTEMPTS = 3
SNAPSHOT_RETRY_DELAY_SECONDS = 2.0
STOP_CHECK_SECONDS = 0.2


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_logger(run_log):
    """Return a log function matching run_tests.sh's log(): a ``[ts] msg``
    line on stdout plus a best-effort append to the run log."""

    def log(msg: str) -> None:
        line = f"[{_utc_now_iso()}] {msg}"
        print(line, flush=True)
        if run_log and os.path.isfile(run_log):
            try:
                with open(run_log, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass

    return log


def list_context_project_ids(conn) -> list:
    """IDs of all Keystone projects whose name starts with ``c_rally_``.

    Lists with the server-side ``name__startswith`` filter, so only Rally
    context projects count against Keystone's list_limit; the prefix is still
    matched here in case a Keystone ignores the filter. Raises on any listing
    failure, truncation included. An ID outside the printable charset is
    skipped: it can never be an implicit-tenant RGW uid, and it would break
    the space-separated ledger line.
    """
    return [
        pid for pid, name in list_projects(conn, CONTEXT_PREFIX)
        if name.startswith(CONTEXT_PREFIX) and PRINTABLE_ID_RE.fullmatch(pid)
    ]


class Tracker:
    """Seen-set plus ledger writer for one service's task."""

    def __init__(self, conn, service: str, ledger_path: str, log, now_fn=None):
        self.conn = conn
        self.service = service
        self.ledger_path = ledger_path
        self.log = log
        self.now_fn = now_fn or _utc_now_iso
        self.seen = set()

    def snapshot(self, attempts: int = SNAPSHOT_ATTEMPTS,
                 delay: float = SNAPSHOT_RETRY_DELAY_SECONDS, sleep_fn=None) -> bool:
        """Seed the seen-set; False when every attempt failed."""
        sleep_fn = sleep_fn or time.sleep
        for attempt in range(1, attempts + 1):
            try:
                self.seen.update(list_context_project_ids(self.conn))
                return True
            except Exception as exc:  # noqa: BLE001 - any SDK/auth/truncation error
                self.log(
                    f"  {self.service}: WARNING - pre-run Rally project snapshot "
                    f"failed (attempt {attempt}/{attempts}): {exc}"
                )
                if attempt < attempts:
                    sleep_fn(delay * attempt)
        return False

    def poll(self) -> bool:
        try:
            current = list_context_project_ids(self.conn)
        except Exception:  # noqa: BLE001 - any SDK/auth/truncation error
            return False
        for project_id in current:
            if project_id in self.seen:
                continue
            if not self.record(project_id):
                # Not marked seen, so the next poll retries the append.
                return False
            self.seen.add(project_id)
            self.log(f"  {self.service}: recorded Rally project {project_id}")
        return True

    def record(self, project_id: str) -> bool:
        """Append one ``<ts> <project_id> <service>`` ledger line.

        The ledger authorizes purge-data deletes and is parsed by fields, so
        an ID or service containing whitespace or a newline (which could
        forge a second line vouching for another project) is refused, not
        written. Both must fullmatch their charset.
        """
        if not PRINTABLE_ID_RE.fullmatch(project_id) or not SERVICE_RE.fullmatch(self.service):
            self.log(f"  WARNING - refusing to record malformed ledger entry {project_id!r} {self.service!r}")
            return False
        try:
            with open(self.ledger_path, "a", encoding="utf-8") as ledger:
                ledger.write(f"{self.now_fn()} {project_id} {self.service}\n")
        except OSError:
            return False
        return True


def run(tracker: Tracker, should_stop, interval: float, parent_alive,
        sleep_fn=time.sleep) -> None:
    """Poll until ``should_stop()`` or the parent dies, then poll once more."""
    warned = False
    while not should_stop() and parent_alive():
        if not tracker.poll() and not warned:
            tracker.log(
                f"  {tracker.service}: WARNING - failed to poll Rally context projects; "
                "RGW provenance may be incomplete for this task"
            )
            warned = True
        waited = 0.0
        while waited < interval and not should_stop():
            step = min(STOP_CHECK_SECONDS, interval - waited)
            sleep_fn(step)
            waited += step
    tracker.poll()


def _signal_ready(path: str) -> None:
    """Tell the shell the snapshot is done: write a line to its ready FIFO
    (which the shell holds open read-write, so this never blocks).

    Best effort: a failure only makes the shell wait out its bound; it must
    not kill the tracker after a good snapshot.
    """
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("ready\n")
    except OSError:
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--service", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--run-log", default="")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    args = parser.parse_args(argv)

    log = make_logger(args.run_log)
    stop = {"requested": False}
    signal.signal(signal.SIGTERM, lambda _signum, _frame: stop.update(requested=True))
    parent = os.getppid()

    # Readiness is signalled exactly once, on every path, so the shell never
    # waits out its 120s bound on a tracker that already gave up.
    tracker = None
    try:
        conn = build_connection()
    except Exception as exc:  # noqa: BLE001 - SDK missing / unbuildable connection
        log(
            f"  {args.service}: WARNING - provenance tracker could not connect ({exc}); "
            "RGW provenance is not recorded for this task"
        )
    else:
        tracker = Tracker(conn, args.service, args.ledger, log)
        if not tracker.snapshot():
            log(
                f"  {args.service}: WARNING - no pre-run snapshot; RGW provenance is NOT "
                "recorded for this task (its orphans stay unknown-owner, never auto-purged)"
            )
            tracker = None
    _signal_ready(args.ready_file)
    if tracker is None:
        return 1
    run(tracker, lambda: stop["requested"], args.interval, lambda: os.getppid() == parent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
