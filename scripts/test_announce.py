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

from shell_testlib import BASE_PATH, BASH, write_shim

SCRIPT = Path(__file__).resolve().parent / "announce.sh"
RUN_STATUS_CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "testdata" / "run_status_cases.json").read_text()
)["cases"]


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
        [BASH, str(SCRIPT), *argv],
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
        assert rec["id"] == result.stdout.strip()
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
    @pytest.mark.parametrize("argv", [
        pytest.param(("--type", "maintenance", "--body", "x"),
                     id="maintenance-without-expires-at"),
        pytest.param(("--type", "scheduled", "--body", "x",
                      "--expires-at", "2026-05-17T12:00:00Z"),
                     id="scheduled-without-effective-from"),
        pytest.param(("--type", "scheduled", "--body", "x",
                      "--effective-from", "2026-05-17T10:00:00Z"),
                     id="scheduled-without-expires-at"),
        pytest.param(("--type", "bogus", "--body", "x"), id="unknown-type"),
        pytest.param(("--type", "incident", "--body", "x" * 501), id="overlong-body"),
        pytest.param(("--type", "maintenance", "--body", "x",
                      "--expires-at", "2026-13-45T99:99:99Z"),
                     id="bogus-iso-timestamp"),
        pytest.param(("--type", "maintenance", "--body", "x",
                      "--expires-at", "2026-05-16T18:00:00"),
                     id="iso-timestamp-missing-trailing-z"),
        pytest.param(("--type", "incident", "--body", "   "), id="empty-body"),
    ])
    def test_invalid_post_rejected_without_writing_state(
        self, tmp_path: Path, argv: tuple[str, ...],
    ) -> None:
        result = run_cli(tmp_path, "post", *argv)
        assert result.returncode != 0
        assert not (tmp_path / "announcement-state.json").exists()


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
        # Idempotent, but a typo'd id must not look like a successful clear.
        assert "no announcement with id no-such-id" in result.stderr

    def test_clear_reports_what_it_removed(self, tmp_path: Path) -> None:
        record_id = run_cli(
            tmp_path, "post", "--type", "incident", "--body", "x", expect_success=True,
        ).stdout.strip()
        result = run_cli(tmp_path, "clear", record_id, expect_success=True)
        assert f"cleared {record_id}" in result.stderr

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


INCIDENT = {"id": "incident-1", "type": "incident", "body": "x",
            "created_at": "2026-05-16T14:00:00Z", "expires_at": None,
            "effective_from": None, "updates": []}


def _one_failed() -> dict:
    services = make_services_dict("passed")
    services["nova"]["status"] = "failed"
    return {"timestamp": "20260516T140000Z", "services": services}


@pytest.fixture
def incident_state(tmp_path: Path) -> Path:
    """A state file holding one active incident."""
    write_state(tmp_path, {"announcements": [INCIDENT]})
    return tmp_path


class TestAutoClear:
    def test_all_green_clears_incidents_only(self, tmp_path: Path) -> None:
        write_state(tmp_path, {
            "announcements": [
                INCIDENT,
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

    # The empty-services case is the exact failure mode the incident banner
    # exists to communicate: deployment setup failed and .services is {}. A
    # naive `all(...)` over an empty iteration returns true; the guard must
    # reject it. A missing or malformed summary file is a no-op.
    @pytest.mark.parametrize("summary", [
        pytest.param(json.dumps(_one_failed()), id="one-failed"),
        pytest.param(json.dumps({"timestamp": "20260516T140000Z", "services": {}}),
                     id="empty-services-guard"),
        pytest.param(json.dumps({"timestamp": "20260516T140000Z",
                                 "error": "deployment_setup_failed",
                                 "services": make_services_dict("passed")}),
                     id="error-field-guard"),
        pytest.param(None, id="missing-summary-file"),
        pytest.param("{this is not json", id="malformed-summary-file"),
    ])
    def test_non_green_summary_leaves_incidents(
        self, incident_state: Path, summary: str | None,
    ) -> None:
        summary_path = incident_state / "summary.json"
        if summary is not None:
            summary_path.write_text(summary)
        result = run_cli(
            incident_state, "auto-clear-if-all-green",
            "--summary-file", str(summary_path),
        )
        assert result.returncode == 0
        assert read_state(incident_state)["announcements"] == [INCIDENT]

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

    @pytest.mark.parametrize(
        "case",
        RUN_STATUS_CASES,
        ids=[c["id"] for c in RUN_STATUS_CASES],
    )
    def test_run_status_cases(self, incident_state: Path, case: dict) -> None:
        summary_path = write_summary(incident_state, case["summary"], name="summary.json")
        result = run_cli(
            incident_state, "auto-clear-if-all-green",
            "--summary-file", str(summary_path),
        )
        assert result.returncode == 0
        expected = [] if case["passed"] else [INCIDENT]
        assert read_state(incident_state)["announcements"] == expected


# ---------------------------------------------------------------------------
# Atomicity + ID entropy
# ---------------------------------------------------------------------------

class TestRobustness:
    def test_burst_posts_get_distinct_ids(self, tmp_path: Path) -> None:
        """Posts within the same second still get distinct IDs and records
        (end to end; the generator itself is stressed below)."""
        ids = [
            run_cli(tmp_path, "post", "--type", "incident", "--body", "x",
                    expect_success=True).stdout.strip()
            for _ in range(5)
        ]
        assert len(set(ids)) == 5
        assert [rec["id"] for rec in read_state(tmp_path)["announcements"]] == ids

    # announce.sh runs main when sourced, so generate_id is extracted from it.
    # `date` is stubbed to one second, so only the random suffix differs; a
    # `command` override that fails selects the /dev/urandom fallback.
    GENERATE_ID = (
        f'eval "$(sed -n "/^generate_id() {{/,/^}}/p" "{SCRIPT}")"\n'
        "date() { echo 20260612T120000Z; }\n"
    )
    FALLBACK = "command() { return 1; }\n"

    @pytest.mark.parametrize(("prelude", "shims", "logged", "suffix"), [
        pytest.param("", {"openssl": "printf deadbeef\n"}, "openssl rand -hex 4",
                     "deadbeef", id="openssl"),
        # Bytes 00 0a ff 01: leading zeros and a newline byte must survive
        # the od|tr formatting as hex digits.
        pytest.param(FALLBACK, {"head": r"printf '\x00\x0a\xff\x01'"}, "head -c 4 /dev/urandom",
                     "000aff01", id="urandom-fallback"),
    ])
    def test_generate_id_asks_for_32_bits_as_8_hex(
        self, tmp_path: Path, prelude: str, shims: dict[str, str], logged: str, suffix: str,
    ) -> None:
        """Deterministic: the generator requests exactly 4 random bytes and
        formats them as exactly 8 lowercase hex characters."""
        calls = tmp_path / "calls.log"
        for name, body in shims.items():
            write_shim(tmp_path / "bin", name, f'echo "{name} $*" >> "{calls}"\n{body}\n')
        result = subprocess.run(
            [BASH, "-c", self.GENERATE_ID + prelude + "generate_id incident"],
            env={"PATH": f"{tmp_path / 'bin'}:{BASE_PATH}"},
            capture_output=True, text=True, check=True,
        )
        assert result.stdout == f"incident-20260612T120000Z-{suffix}"
        assert calls.read_text().splitlines() == [logged]

    @pytest.mark.parametrize("prelude", ["", FALLBACK], ids=["openssl", "urandom-fallback"])
    def test_generate_id_smoke_no_same_second_collisions(self, prelude: str) -> None:
        """200 real IDs in one stubbed second are distinct and well formed
        (birthday odds of a chance failure ~5e-6 per case). Background jobs
        in batches of 100, because each call forks openssl or head|od|tr."""
        snippet = self.GENERATE_ID + prelude + (
            "for _ in 1 2; do\n"
            "  for _ in $(seq 100); do { id=$(generate_id incident); echo \"$id\"; } & done\n"
            "  wait\n"
            "done\n"
        )
        result = subprocess.run([BASH, "-c", snippet], capture_output=True,
                                text=True, check=True)
        ids = result.stdout.split()
        assert len(ids) == 200
        assert all(re.fullmatch(r"incident-20260612T120000Z-[0-9a-f]{8}", i) for i in ids)
        assert len(set(ids)) == 200

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
        write_shim(bin_dir, "jq", "exit 1\n")

        env = os.environ.copy()
        env["RESULTS_DIR"] = str(tmp_path)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        result = subprocess.run(
            [BASH, str(SCRIPT), "post", "--type", "incident", "--body", "second"],
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

    @pytest.mark.parametrize(
        "argv",
        [
            ("post", "--type"),
            ("post", "--type", "incident", "--body"),
            ("post", "--type", "maintenance", "--body", "x", "--expires-at"),
            ("update", "some-id", "--body"),
            ("auto-clear-if-all-green", "--summary-file"),
        ],
    )
    def test_flag_without_value_is_a_clear_error(self, tmp_path: Path, argv: tuple) -> None:
        result = run_cli(tmp_path, *argv)
        assert result.returncode != 0
        assert f"{argv[-1]} requires a value" in result.stderr
        assert "unbound variable" not in result.stderr

    @pytest.mark.parametrize("sub", ["post", "update", "clear", "auto-clear-if-all-green"])
    def test_subcommand_help_prints_usage(self, tmp_path: Path, sub: str) -> None:
        result = run_cli(tmp_path, sub, "--help")
        assert result.returncode == 0
        assert "Usage" in result.stderr
        assert not (tmp_path / "announcement-state.json").exists()

    def test_unknown_subcommand_with_help_is_still_an_error(self, tmp_path: Path) -> None:
        result = run_cli(tmp_path, "frobnicate", "--help")
        assert result.returncode == 64
        assert "unknown subcommand" in result.stderr

    def test_usage_documents_type_rules_and_auto_clear(self, tmp_path: Path) -> None:
        result = run_cli(tmp_path, "--help")
        assert "maintenance requires --expires-at" in result.stderr
        assert "auto-cleared" in result.stderr
        assert "UTC" in result.stderr
