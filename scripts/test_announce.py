"""Regression tests for scripts/announce.sh — the operator announcement CLI.

The CLI manages /results/announcement-state.json with five subcommands:
post | update | clear | list | auto-clear-if-all-green.

Tests drive the script via subprocess with RESULTS_DIR pointed at a tmp_path.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "announce.sh"


def _has_flock() -> bool:
    return subprocess.run(
        ["/bin/sh", "-c", "command -v flock"],
        capture_output=True, check=False,
    ).returncode == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_cli(
    tmp_path: Path,
    *argv: str,
    expect_success: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["RESULTS_DIR"] = str(tmp_path)
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT), *argv],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if expect_success and result.returncode != 0:
        raise AssertionError(
            f"CLI exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def read_state(tmp_path: Path) -> dict:
    path = tmp_path / "announcement-state.json"
    return json.loads(path.read_text())


def write_state(tmp_path: Path, state: dict) -> None:
    (tmp_path / "announcement-state.json").write_text(json.dumps(state))


def write_summary(tmp_path: Path, summary: dict, name: str = "latest_summary.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(summary))
    return path


# ---------------------------------------------------------------------------
# post: happy paths
# ---------------------------------------------------------------------------

class TestPostHappyPath:
    def test_post_incident_creates_well_formed_record(self, tmp_path: Path) -> None:
        result = run_cli(
            tmp_path, "post", "--type", "incident", "--body", "RGW degraded",
            expect_success=True,
        )
        record_id = result.stdout.strip()
        assert re.match(r"^incident-\d{8}T\d{6}Z-[0-9a-f]{8}$", record_id), record_id

        state = read_state(tmp_path)
        assert list(state.keys()) == ["announcements"]
        assert len(state["announcements"]) == 1
        rec = state["announcements"][0]
        assert rec["id"] == record_id
        assert rec["type"] == "incident"
        assert rec["body"] == "RGW degraded"
        assert rec["expires_at"] is None
        assert rec["effective_from"] is None
        assert rec["updates"] == []
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", rec["created_at"])

    def test_post_maintenance_requires_expires_at(self, tmp_path: Path) -> None:
        result = run_cli(
            tmp_path, "post", "--type", "maintenance", "--body", "Lab maintenance",
            "--expires-at", "2026-05-16T18:00:00Z",
            expect_success=True,
        )
        record_id = result.stdout.strip()
        rec = read_state(tmp_path)["announcements"][0]
        assert rec["id"] == record_id
        assert rec["type"] == "maintenance"
        assert rec["expires_at"] == "2026-05-16T18:00:00Z"
        assert rec["effective_from"] is None

    def test_post_scheduled_requires_both_timestamps(self, tmp_path: Path) -> None:
        result = run_cli(
            tmp_path, "post", "--type", "scheduled", "--body", "Upgrade",
            "--effective-from", "2026-05-17T10:00:00Z",
            "--expires-at", "2026-05-17T12:00:00Z",
            expect_success=True,
        )
        rec = read_state(tmp_path)["announcements"][0]
        assert rec["type"] == "scheduled"
        assert rec["effective_from"] == "2026-05-17T10:00:00Z"
        assert rec["expires_at"] == "2026-05-17T12:00:00Z"

    def test_post_trims_whitespace_from_body(self, tmp_path: Path) -> None:
        run_cli(
            tmp_path, "post", "--type", "incident", "--body", "   trimmed   ",
            expect_success=True,
        )
        assert read_state(tmp_path)["announcements"][0]["body"] == "trimmed"

    def test_post_500_char_body_accepted(self, tmp_path: Path) -> None:
        body = "x" * 500
        result = run_cli(
            tmp_path, "post", "--type", "incident", "--body", body,
            expect_success=True,
        )
        assert read_state(tmp_path)["announcements"][0]["body"] == body
        assert result.returncode == 0

    def test_post_body_with_quotes_and_backslashes(self, tmp_path: Path) -> None:
        body = 'has "quotes" and back\\slash'
        run_cli(tmp_path, "post", "--type", "incident", "--body", body, expect_success=True)
        assert read_state(tmp_path)["announcements"][0]["body"] == body


# ---------------------------------------------------------------------------
# post: validation
# ---------------------------------------------------------------------------

class TestPostValidation:
    def test_maintenance_without_expires_at_rejected(self, tmp_path: Path) -> None:
        result = run_cli(tmp_path, "post", "--type", "maintenance", "--body", "x")
        assert result.returncode != 0
        assert not (tmp_path / "announcement-state.json").exists()

    def test_scheduled_without_effective_from_rejected(self, tmp_path: Path) -> None:
        result = run_cli(
            tmp_path, "post", "--type", "scheduled", "--body", "x",
            "--expires-at", "2026-05-17T12:00:00Z",
        )
        assert result.returncode != 0

    def test_scheduled_without_expires_at_rejected(self, tmp_path: Path) -> None:
        result = run_cli(
            tmp_path, "post", "--type", "scheduled", "--body", "x",
            "--effective-from", "2026-05-17T10:00:00Z",
        )
        assert result.returncode != 0

    def test_unknown_type_rejected(self, tmp_path: Path) -> None:
        result = run_cli(tmp_path, "post", "--type", "bogus", "--body", "x")
        assert result.returncode != 0

    def test_overlong_body_rejected(self, tmp_path: Path) -> None:
        body = "x" * 501
        result = run_cli(tmp_path, "post", "--type", "incident", "--body", body)
        assert result.returncode != 0

    def test_bogus_iso_timestamp_rejected(self, tmp_path: Path) -> None:
        result = run_cli(
            tmp_path, "post", "--type", "maintenance", "--body", "x",
            "--expires-at", "2026-13-45T99:99:99Z",
        )
        assert result.returncode != 0

    def test_iso_timestamp_missing_trailing_z_rejected(self, tmp_path: Path) -> None:
        result = run_cli(
            tmp_path, "post", "--type", "maintenance", "--body", "x",
            "--expires-at", "2026-05-16T18:00:00",
        )
        assert result.returncode != 0

    def test_empty_body_rejected(self, tmp_path: Path) -> None:
        result = run_cli(tmp_path, "post", "--type", "incident", "--body", "   ")
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------

class TestUpdate:
    def test_update_appends_to_incident(self, tmp_path: Path) -> None:
        post_result = run_cli(
            tmp_path, "post", "--type", "incident", "--body", "initial",
            expect_success=True,
        )
        record_id = post_result.stdout.strip()
        run_cli(tmp_path, "update", record_id, "--body", "follow-up", expect_success=True)

        rec = read_state(tmp_path)["announcements"][0]
        assert len(rec["updates"]) == 1
        assert rec["updates"][0]["body"] == "follow-up"
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", rec["updates"][0]["ts"])

    def test_update_on_scheduled_rejected(self, tmp_path: Path) -> None:
        post_result = run_cli(
            tmp_path, "post", "--type", "scheduled", "--body", "x",
            "--effective-from", "2026-05-17T10:00:00Z",
            "--expires-at", "2026-05-17T12:00:00Z",
            expect_success=True,
        )
        record_id = post_result.stdout.strip()
        result = run_cli(tmp_path, "update", record_id, "--body", "nope")
        assert result.returncode != 0

    def test_update_missing_id_rejected(self, tmp_path: Path) -> None:
        write_state(tmp_path, {"announcements": []})
        result = run_cli(tmp_path, "update", "no-such-id", "--body", "x")
        assert result.returncode != 0

    def test_update_trims_whitespace(self, tmp_path: Path) -> None:
        post_result = run_cli(
            tmp_path, "post", "--type", "incident", "--body", "x", expect_success=True,
        )
        record_id = post_result.stdout.strip()
        run_cli(tmp_path, "update", record_id, "--body", "   spaced   ", expect_success=True)
        assert read_state(tmp_path)["announcements"][0]["updates"][0]["body"] == "spaced"

    def test_update_overlong_body_rejected(self, tmp_path: Path) -> None:
        post_result = run_cli(
            tmp_path, "post", "--type", "incident", "--body", "x", expect_success=True,
        )
        record_id = post_result.stdout.strip()
        result = run_cli(tmp_path, "update", record_id, "--body", "x" * 501)
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------

class TestClear:
    def test_clear_removes_record(self, tmp_path: Path) -> None:
        post_result = run_cli(
            tmp_path, "post", "--type", "incident", "--body", "x", expect_success=True,
        )
        record_id = post_result.stdout.strip()
        run_cli(tmp_path, "clear", record_id, expect_success=True)
        assert read_state(tmp_path)["announcements"] == []

    def test_clear_missing_id_is_idempotent(self, tmp_path: Path) -> None:
        write_state(tmp_path, {"announcements": []})
        result = run_cli(tmp_path, "clear", "no-such-id")
        assert result.returncode == 0

    def test_clear_missing_id_when_file_absent_is_idempotent(self, tmp_path: Path) -> None:
        result = run_cli(tmp_path, "clear", "no-such-id")
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

class TestList:
    def test_list_on_missing_file_returns_empty(self, tmp_path: Path) -> None:
        result = run_cli(tmp_path, "list", expect_success=True)
        assert json.loads(result.stdout) == {"announcements": []}

    def test_list_returns_all_records(self, tmp_path: Path) -> None:
        run_cli(tmp_path, "post", "--type", "incident", "--body", "one", expect_success=True)
        run_cli(tmp_path, "post", "--type", "incident", "--body", "two", expect_success=True)
        result = run_cli(tmp_path, "list", expect_success=True)
        payload = json.loads(result.stdout)
        assert len(payload["announcements"]) == 2
        bodies = {rec["body"] for rec in payload["announcements"]}
        assert bodies == {"one", "two"}


# ---------------------------------------------------------------------------
# auto-clear-if-all-green
# ---------------------------------------------------------------------------

SERVICES = ("keystone", "nova", "neutron", "glance", "cinder", "swift")


def make_services_dict(status: str = "passed") -> dict:
    """Keyed-object .services shape (matches build_summary in run_tests.sh)."""
    return {
        svc: {
            "status": status,
            "duration": 0,
            "total_iterations": 1,
            "failed_iterations": 0,
            "sla_passed": True,
            "scenarios": [],
        }
        for svc in SERVICES
    }


class TestAutoClear:
    def test_all_green_clears_incidents_only(self, tmp_path: Path) -> None:
        write_state(tmp_path, {
            "announcements": [
                {"id": "incident-1", "type": "incident", "body": "x",
                 "created_at": "2026-05-16T14:00:00Z", "expires_at": None,
                 "effective_from": None, "updates": []},
                {"id": "maint-1", "type": "maintenance", "body": "y",
                 "created_at": "2026-05-16T10:00:00Z",
                 "expires_at": "2026-05-16T18:00:00Z",
                 "effective_from": None, "updates": []},
                {"id": "sched-1", "type": "scheduled", "body": "z",
                 "created_at": "2026-05-16T09:00:00Z",
                 "expires_at": "2026-05-17T12:00:00Z",
                 "effective_from": "2026-05-17T10:00:00Z", "updates": []},
            ]
        })
        summary_path = write_summary(tmp_path, {
            "timestamp": "20260516T140000Z",
            "services": make_services_dict("passed"),
        })
        run_cli(
            tmp_path, "auto-clear-if-all-green",
            "--summary-file", str(summary_path),
            expect_success=True,
        )
        state = read_state(tmp_path)
        ids = {rec["id"] for rec in state["announcements"]}
        assert ids == {"maint-1", "sched-1"}

    def test_one_failed_leaves_incidents(self, tmp_path: Path) -> None:
        write_state(tmp_path, {
            "announcements": [
                {"id": "incident-1", "type": "incident", "body": "x",
                 "created_at": "2026-05-16T14:00:00Z", "expires_at": None,
                 "effective_from": None, "updates": []},
            ]
        })
        services = make_services_dict("passed")
        services["nova"]["status"] = "failed"
        summary_path = write_summary(tmp_path, {
            "timestamp": "20260516T140000Z",
            "services": services,
        })
        run_cli(
            tmp_path, "auto-clear-if-all-green",
            "--summary-file", str(summary_path),
            expect_success=True,
        )
        state = read_state(tmp_path)
        assert len(state["announcements"]) == 1
        assert state["announcements"][0]["id"] == "incident-1"

    def test_empty_services_guard_leaves_incidents(self, tmp_path: Path) -> None:
        """The exact failure mode the incident banner exists to communicate:
        deployment setup failed and .services is {}. A naive `all(...)` over an
        empty iteration returns true; the guard must reject this case."""
        write_state(tmp_path, {
            "announcements": [
                {"id": "incident-1", "type": "incident", "body": "x",
                 "created_at": "2026-05-16T14:00:00Z", "expires_at": None,
                 "effective_from": None, "updates": []},
            ]
        })
        summary_path = write_summary(tmp_path, {
            "timestamp": "20260516T140000Z",
            "services": {},
        })
        run_cli(
            tmp_path, "auto-clear-if-all-green",
            "--summary-file", str(summary_path),
            expect_success=True,
        )
        assert len(read_state(tmp_path)["announcements"]) == 1

    def test_error_field_guard_leaves_incidents(self, tmp_path: Path) -> None:
        write_state(tmp_path, {
            "announcements": [
                {"id": "incident-1", "type": "incident", "body": "x",
                 "created_at": "2026-05-16T14:00:00Z", "expires_at": None,
                 "effective_from": None, "updates": []},
            ]
        })
        services = make_services_dict("passed")
        summary_path = write_summary(tmp_path, {
            "timestamp": "20260516T140000Z",
            "error": "deployment_setup_failed",
            "services": services,
        })
        run_cli(
            tmp_path, "auto-clear-if-all-green",
            "--summary-file", str(summary_path),
            expect_success=True,
        )
        assert len(read_state(tmp_path)["announcements"]) == 1

    def test_missing_summary_file_is_no_op(self, tmp_path: Path) -> None:
        write_state(tmp_path, {
            "announcements": [
                {"id": "incident-1", "type": "incident", "body": "x",
                 "created_at": "2026-05-16T14:00:00Z", "expires_at": None,
                 "effective_from": None, "updates": []},
            ]
        })
        result = run_cli(
            tmp_path, "auto-clear-if-all-green",
            "--summary-file", str(tmp_path / "does-not-exist.json"),
        )
        assert result.returncode == 0
        assert len(read_state(tmp_path)["announcements"]) == 1

    def test_malformed_summary_file_is_no_op(self, tmp_path: Path) -> None:
        bad_summary = tmp_path / "bad.json"
        bad_summary.write_text("{this is not json")
        write_state(tmp_path, {
            "announcements": [
                {"id": "incident-1", "type": "incident", "body": "x",
                 "created_at": "2026-05-16T14:00:00Z", "expires_at": None,
                 "effective_from": None, "updates": []},
            ]
        })
        result = run_cli(
            tmp_path, "auto-clear-if-all-green",
            "--summary-file", str(bad_summary),
        )
        assert result.returncode == 0
        assert len(read_state(tmp_path)["announcements"]) == 1

    def test_runs_without_announcement_state_file(self, tmp_path: Path) -> None:
        """Auto-clear must not fail when there's no state file yet (idle dashboard)."""
        summary_path = write_summary(tmp_path, {
            "timestamp": "20260516T140000Z",
            "services": make_services_dict("passed"),
        })
        result = run_cli(
            tmp_path, "auto-clear-if-all-green",
            "--summary-file", str(summary_path),
        )
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Atomicity + ID entropy
# ---------------------------------------------------------------------------

class TestRobustness:
    def test_id_entropy_under_burst_posts(self, tmp_path: Path) -> None:
        """100 sequential posts produce 100 distinct IDs (the 32-bit random
        suffix is what prevents same-second collisions)."""
        seen: set[str] = set()
        for _ in range(100):
            result = run_cli(
                tmp_path, "post", "--type", "incident", "--body", "x",
                expect_success=True,
            )
            record_id = result.stdout.strip()
            assert record_id not in seen
            seen.add(record_id)
        assert len(seen) == 100
        assert len(read_state(tmp_path)["announcements"]) == 100

    def test_corrupt_state_file_does_not_clobber_on_post(self, tmp_path: Path) -> None:
        """If the state file is corrupt JSON, the CLI must not silently
        overwrite it with a fresh empty file — operator data could be lost."""
        (tmp_path / "announcement-state.json").write_text("{this is not json")
        result = run_cli(tmp_path, "post", "--type", "incident", "--body", "x")
        assert result.returncode != 0
        # File contents preserved verbatim
        assert (tmp_path / "announcement-state.json").read_text() == "{this is not json"

    def test_concurrent_posts_do_not_lose_records(self, tmp_path: Path) -> None:
        """flock must serialize concurrent mutators. Without locking, two
        simultaneous read-modify-write cycles can race and drop one record.
        With the lock, every post must show up in the final state."""
        import concurrent.futures

        if not _has_flock():
            pytest.skip("flock unavailable in this environment")

        # Seed an empty state so all 20 posts share the same starting point.
        write_state(tmp_path, {"announcements": []})

        def post_one(idx: int) -> str:
            result = run_cli(
                tmp_path, "post", "--type", "incident", "--body", f"msg-{idx}",
                expect_success=True,
            )
            return result.stdout.strip()

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(post_one, range(20)))

        assert len(set(ids)) == 20
        final = read_state(tmp_path)
        assert len(final["announcements"]) == 20
        stored_ids = {rec["id"] for rec in final["announcements"]}
        assert stored_ids == set(ids)

    def test_write_failure_leaves_existing_state_intact(self, tmp_path: Path) -> None:
        """If jq fails inside write_state_atomic, the previous state file must
        survive untouched — set -e exits before mv, and the trap removes the
        partial tmp file. Simulate by shadowing jq with a stub that exits 1
        only on the final '.' re-encode (the validate step still works because
        the predicate uses jq -e via a separate invocation chain — here we
        force every jq to fail and assert the prior file is preserved.)"""
        run_cli(tmp_path, "post", "--type", "incident", "--body", "first",
                expect_success=True)
        first_state = (tmp_path / "announcement-state.json").read_text()

        bin_dir = tmp_path / "fake-bin"
        bin_dir.mkdir()
        jq_stub = bin_dir / "jq"
        jq_stub.write_text("#!/bin/sh\nexit 1\n")
        jq_stub.chmod(0o755)

        env = os.environ.copy()
        env["RESULTS_DIR"] = str(tmp_path)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT), "post", "--type", "incident", "--body", "second"],
            capture_output=True, text=True, env=env, check=False,
        )
        assert result.returncode != 0
        # Original state file untouched — neither overwritten nor truncated.
        assert (tmp_path / "announcement-state.json").read_text() == first_state
        # No leftover tmp file (the cleanup trap fired).
        leftovers = list(tmp_path.glob("announcement-state.json.tmp*"))
        assert leftovers == [], f"trap should have removed tmpfiles, found: {leftovers}"


# ---------------------------------------------------------------------------
# Usage / help
# ---------------------------------------------------------------------------

class TestUsage:
    def test_no_args_prints_usage(self, tmp_path: Path) -> None:
        result = run_cli(tmp_path)
        assert result.returncode != 0
        assert "Usage" in result.stderr or "usage" in result.stderr

    def test_unknown_subcommand_rejected(self, tmp_path: Path) -> None:
        result = run_cli(tmp_path, "frobnicate")
        assert result.returncode != 0
