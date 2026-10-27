"""Tests for scripts/purge_orphans.sh RGW orphan classification.

purge_rgw() decides which RadosGW orphans (users whose Keystone project is
confirmed gone) are eligible for deletion. The matrix:

  - rally-owned (project ID in the provenance ledger)  -> eligible (+ buckets)
  - unknown owner but EMPTY (0 buckets/objects)         -> eligible (NEW)
  - unknown owner WITH data                             -> skipped

These tests source purge_orphans.sh with PURGE_ORPHANS_NO_MAIN=1 (so main does
not run), stub the rgw_* helpers it calls, then invoke purge_rgw and assert on
the resulting snapshot arrays / counters. The real rgw_count_buckets /
rgw_count_objects (pure jq) are kept so the empty-vs-data distinction is
exercised end to end.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from shell_testlib import BASE_PATH, BASH

SCRIPT = Path(__file__).resolve().parent / "purge_orphans.sh"


def run_purge_rgw(stubs: str, tmp_path: Path) -> dict[str, str]:
    """Source purge_orphans.sh, apply stub overrides, run purge_rgw, and emit
    the snapshot/counters as KEY=VALUE lines parsed into a dict."""
    harness = f"""
        set -euo pipefail
        export PURGE_ORPHANS_NO_MAIN=1
        source "{SCRIPT}"
        # Stub overrides replace the curl/Keystone-backed helpers.
        {stubs}
        purge_rgw >"{tmp_path / 'purge_rgw.log'}" 2>&1 || true
        echo "EMPTY=${{_RGW_EMPTY_ORPHANS}}"
        echo "SKIPPED=${{_RGW_SKIPPED_UNKNOWN}}"
        echo "SNAP_USERS=${{_SNAP_RGW_USERS[*]+"${{_SNAP_RGW_USERS[*]}}"}}"
        echo "SNAP_BUCKETS=${{_SNAP_RGW_BUCKETS[*]+"${{_SNAP_RGW_BUCKETS[*]}}"}}"
        echo "FOUND=${{TOTAL_FOUND}}"
    """
    out = subprocess.run(
        [BASH, "-c", harness], capture_output=True, text=True, check=True
    ).stdout
    result = {}
    for line in out.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            result[k] = v.strip()
    return result


# Three orphans (project already confirmed gone): rally-owned with a bucket,
# unknown-owner-empty, unknown-owner-with-data.
STUBS = """
rgw_available() { return 0; }
rgw_find_orphaned_users() {
    RGW_LAST_FIND_ERRORS=0
    printf '%s\\n' rally_uid empty_uid data_uid
}
rgw_classify_owner() {
    case "$1" in
        rally_uid) echo rally_owned ;;
        *)         echo unknown_owner ;;
    esac
}
rgw_list_user_buckets() {
    case "$1" in
        rally_uid) echo '[{"name":"b1","num_objects":3}]' ;;
        empty_uid) echo '[]' ;;
        data_uid)  echo '[{"name":"keepme","num_objects":42}]' ;;
    esac
}
# Stats-free inventory: by default agrees with the stats listing above (and
# with any per-test override of it).
rgw_list_user_bucket_names() { rgw_list_user_buckets "$1" | jq -r '.[].name'; }
rgw_user_exists() { return 0; }
"""


def test_orphan_classification(tmp_path: Path):
    r = run_purge_rgw(STUBS, tmp_path)
    # rally_uid and the empty unknown-owner empty_uid are eligible; data_uid
    # (unknown owner WITH data) is skipped.
    assert r["EMPTY"] == "1"
    assert r["SKIPPED"] == "1"
    assert "empty_uid" in r["SNAP_USERS"]
    assert "rally_uid" in r["SNAP_USERS"]
    assert "data_uid" not in r["SNAP_USERS"]
    # The empty orphan owns no buckets, so only the rally-owned bucket snapshots.
    assert "keepme" not in r["SNAP_BUCKETS"]
    assert "rally_uid|b1" in r["SNAP_BUCKETS"]
    # rally_uid (1 user + 1 bucket) + empty_uid (1 user) = 3.
    assert r["FOUND"] == "3"


# A bucket that currently holds zero objects is still a non-empty user — the
# bucket namespace exists. The bucket_count guard (not object_count alone) must
# keep this user OUT of the eligible set, or we would delete a user that owns a
# named bucket. This locks in that the bucket_count check is load-bearing.
EMPTY_BUCKET_STUBS = """
rgw_available() { return 0; }
rgw_find_orphaned_users() { RGW_LAST_FIND_ERRORS=0; printf '%s\\n' shellbucket_uid; }
rgw_classify_owner() { echo unknown_owner; }
rgw_list_user_buckets() { echo '[{"name":"shell","num_objects":0}]'; }
"""


def test_unknown_owner_with_empty_bucket_is_skipped(tmp_path: Path):
    r = run_purge_rgw(EMPTY_BUCKET_STUBS, tmp_path)
    assert r["EMPTY"] == "0"
    assert r["SKIPPED"] == "1"
    assert "shellbucket_uid" not in r["SNAP_USERS"]
    assert r["FOUND"] == "0"


# ---------------------------------------------------------------------------
# Phase 2 (RGW): buckets go with the user via purge-data, never by bare name
# ---------------------------------------------------------------------------

def run_harness(body: str, tmp_path: Path, env: dict | None = None,
                args: str = "") -> subprocess.CompletedProcess:
    harness = f"""
        set -euo pipefail
        export PURGE_ORPHANS_NO_MAIN=1
        source "{SCRIPT}" {args}
        {body}
    """
    full_env = {
        "PATH": f"{tmp_path / 'bin'}:{BASE_PATH}",
        "RESULTS_DIR": str(tmp_path),
        "RALLY_RUN_LOCK_FILE": str(tmp_path / "rally-run.lock"),
        **(env or {}),
    }
    (tmp_path / "bin").mkdir(exist_ok=True)
    return subprocess.run([BASH, "-c", harness], capture_output=True, text=True,
                          env=full_env, check=False)


def test_phase2_purges_rally_owned_with_data_and_empty_orphan_without(tmp_path: Path):
    body = STUBS + """
        rgw_delete_user() { echo "DELETE_USER $1 ${2:-false}"; }
        purge_rgw >/dev/null
        purge_rgw_delete
        echo "DELETED=${TOTAL_DELETED} FAILED=${TOTAL_FAILED}"
    """
    r = run_harness(body, tmp_path)
    assert r.returncode == 0, r.stderr
    # rally-owned: purge-data removes its buckets in the same request.
    assert "DELETE_USER rally_uid true" in r.stdout
    # Empty unknown-owner: no purge-data, so RGW refuses (fail-closed) if a
    # bucket appeared after the Phase 1 snapshot.
    assert "DELETE_USER empty_uid false" in r.stdout
    assert "DELETE_USER data_uid" not in r.stdout
    # rally_uid + its 1 bucket + empty_uid
    assert "DELETED=3 FAILED=0" in r.stdout


def test_phase2_failed_user_deletion_counts_its_buckets(tmp_path: Path):
    body = STUBS + """
        rgw_delete_user() { [[ "$1" != rally_uid ]]; }
        purge_rgw >/dev/null
        purge_rgw_delete
        echo "DELETED=${TOTAL_DELETED} FAILED=${TOTAL_FAILED}"
    """
    r = run_harness(body, tmp_path)
    assert "DELETED=1 FAILED=2" in r.stdout


# ---------------------------------------------------------------------------
# OpenStack listing: null names and unparseable output
# ---------------------------------------------------------------------------

def test_null_name_does_not_zero_the_count(tmp_path: Path):
    body = """
        openstack() { echo '[{"Name":null,"ID":"n"},{"Name":"s_rally_a","ID":"a"},{"ID":"x"},{"Name":"c_rally_b","ID":"b"}]'; }
        purge_servers
        echo "FOUND=${TOTAL_FOUND} SNAP=${_SNAP_SERVERS[*]} ERR=${_LISTING_ERRORS}"
    """
    r = run_harness(body, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "FOUND=2 SNAP=a b ERR=0" in r.stdout


def test_unparseable_listing_is_a_listing_error(tmp_path: Path):
    body = """
        openstack() { echo 'not json'; }
        purge_servers
        echo "FOUND=${TOTAL_FOUND} ERR=${_LISTING_ERRORS}"
    """
    r = run_harness(body, tmp_path)
    assert "FOUND=0 ERR=1" in r.stdout


# ---------------------------------------------------------------------------
# main: dry-run listing errors, run-lock interlock
# ---------------------------------------------------------------------------

def write_flock_shim(tmp_path: Path, rc: int) -> None:
    """flock stand-in (util-linux flock is absent on macOS) plus an existing
    run lock file, as run_tests.sh leaves behind after its first run."""
    (tmp_path / "rally-run.lock").touch()
    (tmp_path / "bin").mkdir(exist_ok=True)
    shim = tmp_path / "bin" / "flock"
    shim.write_text(f"#!/usr/bin/env bash\necho \"flock $*\" >> \"{tmp_path / 'flock.log'}\"\nexit {rc}\n")
    shim.chmod(0o755)


def test_dry_run_with_failed_list_calls_exits_nonzero(tmp_path: Path):
    write_flock_shim(tmp_path, 0)
    r = run_harness('openstack() { return 1; }; main', tmp_path)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "WARNING" in r.stdout and "incomplete" in r.stdout


def test_confirm_refuses_while_test_run_holds_lock(tmp_path: Path):
    write_flock_shim(tmp_path, 1)
    body = """
        openstack() { echo "OPENSTACK $*" >&2; echo '[{"Name":"s_rally_a","ID":"a"}]'; }
        CONFIRM=true
        main
    """
    r = run_harness(body, tmp_path)
    assert r.returncode == 4, r.stdout + r.stderr
    assert "test run is in progress" in r.stdout
    # Refused before Phase 1: nothing listed, nothing deleted.
    assert "OPENSTACK" not in r.stderr


def test_confirm_takes_the_run_lock_non_blocking(tmp_path: Path):
    write_flock_shim(tmp_path, 0)
    body = """
        openstack() { echo '[]'; }
        CONFIRM=true
        main
    """
    r = run_harness(body, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "flock -n 200" in (tmp_path / "flock.log").read_text()


def test_dry_run_warns_from_run_state_and_never_touches_the_lock(tmp_path: Path):
    write_flock_shim(tmp_path, 1)
    (tmp_path / "run_state.json").write_text('{"state": "running"}')
    r = run_harness("openstack() { echo '[]'; }; main", tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARNING" in r.stdout and "test run is in progress" in r.stdout
    # Even a brief exclusive probe could make a starting cron run skip.
    assert not (tmp_path / "flock.log").exists()


def test_dry_run_quiet_when_idle(tmp_path: Path):
    (tmp_path / "run_state.json").write_text('{"state": "idle"}')
    r = run_harness("openstack() { echo '[]'; }; main", tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "test run is in progress" not in r.stdout


def test_confirm_aborts_before_phase2_when_a_list_call_fails(tmp_path: Path):
    """The _LISTING_ERRORS increment used to happen inside $(...) and was
    lost, so --confirm never took this abort."""
    write_flock_shim(tmp_path, 0)
    body = """
        openstack() {
            case "$1 $2" in
                "server list") return 1 ;;
                *" delete") echo "DELETED $*" >&2 ;;
                *) echo '[{"Name":"s_rally_a","ID":"a"}]' ;;
            esac
        }
        CONFIRM=true
        main
    """
    r = run_harness(body, tmp_path)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "ABORTED" in r.stdout
    assert "DELETED" not in r.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_confirm_refuses_when_lock_file_cannot_be_opened(tmp_path: Path):
    # It exists but we cannot open it: cannot prove no run -> fail closed.
    write_flock_shim(tmp_path, 0)
    lock = tmp_path / "rally-run.lock"
    lock.chmod(0)
    try:
        r = run_harness("openstack() { echo '[]'; }; CONFIRM=true; main", tmp_path)
    finally:
        lock.chmod(0o644)
    assert r.returncode == 4, r.stdout + r.stderr
    assert "cannot open run lock" in r.stdout


def test_confirm_opens_the_lock_read_only_and_never_creates_it(tmp_path: Path):
    """Opening it for writing as root created a root-owned lock file the
    rally user's cron run_tests.sh could no longer open."""
    write_flock_shim(tmp_path, 0)
    lock = tmp_path / "rally-run.lock"
    lock.chmod(0o444)
    r = run_harness("openstack() { echo '[]'; }; CONFIRM=true; main", tmp_path)
    lock.chmod(0o644)
    assert r.returncode == 0, r.stdout + r.stderr


def test_confirm_refuses_when_the_lock_file_is_absent(tmp_path: Path):
    """Without the file we cannot hold the lock, and a cron run could create
    and take it mid-purge. Creating it here (often as root) would lock the
    rally user's runs out, so refuse; the entrypoint pre-creates it at boot."""
    write_flock_shim(tmp_path, 0)
    lock = tmp_path / "rally-run.lock"
    lock.unlink()
    body = """
        openstack() { echo "OPENSTACK $*" >&2; echo '[]'; }
        CONFIRM=true
        main
    """
    r = run_harness(body, tmp_path)
    assert r.returncode == 4, r.stdout + r.stderr
    assert "run lock" in r.stdout and "missing" in r.stdout
    assert not lock.exists()
    assert "OPENSTACK" not in r.stderr


def test_run_tests_and_purge_share_one_lock_setting(tmp_path: Path):
    """One RALLY_RUN_LOCK_FILE, defined in os_helpers.sh, used by both."""
    custom = str(tmp_path / "custom.lock")
    probe = f"""
        source "{SCRIPT.parent / 'run_tests.sh'}"
        echo "RUN=$RALLY_RUN_LOCK_FILE"
    """
    r = subprocess.run([BASH, "-c", probe], capture_output=True, text=True,
                       env={"PATH": BASE_PATH,
                            "RALLY_RUN_LOCK_FILE": custom, "RESULTS_DIR": str(tmp_path)})
    assert f"RUN={custom}" in r.stdout, r.stderr
    run_tests = (SCRIPT.parent / "run_tests.sh").read_text()
    assert "/rally/logs/rally-run.lock" not in run_tests
    assert "/rally/logs/rally-run.lock" not in SCRIPT.read_text()


def test_leading_tab_or_bad_id_rejects_the_listing(tmp_path: Path):
    """An entry whose ID is empty/non-string/odd must not shift its name into
    the ID column (bash `read` collapses a leading tab): the whole listing is
    a listing error and nothing is snapshotted."""
    body = """
        openstack() { echo '[{"Name":"s_rally_victim","ID":""},{"Name":"s_rally_a","ID":"a"}]'; }
        purge_servers
        echo "FOUND=${TOTAL_FOUND} SNAP=${_SNAP_SERVERS[*]+${_SNAP_SERVERS[*]}} ERR=${_LISTING_ERRORS}"
        openstack() { echo '[{"Name":"s_rally_x","ID":"a b"}]'; }
        purge_servers
        echo "ERR=${_LISTING_ERRORS}"
    """
    r = run_harness(body, tmp_path)
    assert "FOUND=0 SNAP= ERR=1" in r.stdout, r.stdout + r.stderr
    assert "ERR=2" in r.stdout


def test_tab_in_name_stays_in_the_name_column(tmp_path: Path):
    body = """
        openstack() { printf '%s' '[{"Name":"s_rally_a\\tb","ID":"id-1"}]'; }
        purge_servers
        echo "SNAP=${_SNAP_SERVERS[*]}"
    """
    r = run_harness(body, tmp_path)
    assert "SNAP=id-1" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------------------
# Phase 2 accounting: re-list before delete, 404 deletes nothing
# ---------------------------------------------------------------------------

PHASE2 = STUBS + """
    purge_rgw >/dev/null
    TOTAL_DELETED=0; TOTAL_FAILED=0
"""


def test_phase2_404_counts_no_deletions(tmp_path: Path):
    body = PHASE2 + """
        rgw_delete_user() { return 2; }
        purge_rgw_delete
        echo "DELETED=${TOTAL_DELETED} FAILED=${TOTAL_FAILED}"
    """
    r = run_harness(body, tmp_path)
    assert "DELETED=0 FAILED=0" in r.stdout, r.stdout + r.stderr
    assert "already absent" in r.stdout


def test_phase2_counts_buckets_from_the_relist(tmp_path: Path):
    # rally_uid gained a bucket after Phase 1: purge-data removes both, so
    # both are counted and recorded.
    body = PHASE2 + """
        rgw_list_user_buckets() {
            case "$1" in
                rally_uid) echo '[{"name":"b1"},{"name":"b2"}]' ;;
                *) echo '[]' ;;
            esac
        }
        rgw_delete_user() { :; }
        purge_rgw_delete
        echo "DELETED=${TOTAL_DELETED} PURGED=${_RGW_PURGED_BUCKETS[*]}"
    """
    r = run_harness(body, tmp_path)
    assert "DELETED=4 PURGED=rally_uid|b1 rally_uid|b2" in r.stdout, r.stdout + r.stderr


def test_phase2_new_buckets_count_against_the_threshold(tmp_path: Path):
    body = PHASE2 + """
        MAX_DELETE_THRESHOLD=3
        rgw_list_user_buckets() {
            case "$1" in
                rally_uid) echo '[{"name":"b1"},{"name":"b2"},{"name":"b3"}]' ;;
                *) echo '[]' ;;
            esac
        }
        rgw_delete_user() { echo "DELETE_USER $1"; }
        purge_rgw_delete
    """
    r = run_harness(body, tmp_path)
    assert "DELETE_USER rally_uid" not in r.stdout, r.stdout
    assert "exceed MAX_DELETE_THRESHOLD" in r.stdout
    assert "DELETE_USER empty_uid" in r.stdout


def test_phase2_skips_empty_orphan_that_gained_a_bucket(tmp_path: Path):
    body = PHASE2 + """
        rgw_list_user_buckets() {
            case "$1" in
                rally_uid) echo '[{"name":"b1"}]' ;;
                empty_uid) echo '[{"name":"surprise"}]' ;;
                *) echo '[]' ;;
            esac
        }
        rgw_delete_user() { echo "DELETE_USER $1"; }
        purge_rgw_delete
    """
    r = run_harness(body, tmp_path)
    assert "DELETE_USER empty_uid" not in r.stdout, r.stdout
    assert "unknown owner now owns 1 bucket(s)" in r.stdout


def test_phase2_relist_failure_skips_the_user(tmp_path: Path):
    body = PHASE2 + """
        rgw_list_user_buckets() { return 1; }
        rgw_delete_user() { echo "DELETE_USER $1"; }
        purge_rgw_delete
        echo "FAILED=${TOTAL_FAILED}"
    """
    r = run_harness(body, tmp_path)
    assert "DELETE_USER" not in r.stdout
    assert "FAILED=2" in r.stdout


def test_phase2_skips_user_when_inventories_disagree(tmp_path: Path):
    """The stats listing omitted a bucket (Reef/Quincy skip buckets whose
    stats failed): purge-data would delete more than was counted -> skip."""
    body = PHASE2 + """
        rgw_list_user_bucket_names() {
            case "$1" in rally_uid) printf '%s\\n' b1 hidden ;; esac
        }
        rgw_delete_user() { echo "DELETE_USER $1"; }
        purge_rgw_delete
    """
    r = run_harness(body, tmp_path)
    assert "DELETE_USER rally_uid" not in r.stdout, r.stdout + r.stderr
    assert "inventories disagree" in r.stdout


def test_failed_purge_records_partial_outcome(tmp_path: Path):
    body = PHASE2 + """
        rgw_list_user_buckets() {
            case "$1" in rally_uid) echo '[{"name":"b1"},{"name":"b2"}]' ;; *) echo '[]' ;; esac
        }
        calls=0
        rgw_list_user_bucket_names() {
            case "$1" in
                rally_uid)
                    calls=$((calls + 1))
                    if [[ -f "$RESULTS_DIR/deleted" ]]; then echo b2; else printf '%s\\n' b1 b2; fi ;;
            esac
        }
        rgw_delete_user() { [[ "$1" == rally_uid ]] && { touch "$RESULTS_DIR/deleted"; return 1; }; return 0; }
        purge_rgw_delete
        echo "DELETED=${TOTAL_DELETED} FAILED=${TOTAL_FAILED} PURGED=${_RGW_PURGED_BUCKETS[*]}"
    """
    r = run_harness(body, tmp_path)
    assert "1 bucket(s) removed before the failure, 1 remain" in r.stdout, r.stdout + r.stderr
    # b1 purged; user + b2 failed; empty_uid deleted (1).
    assert "DELETED=2 FAILED=2 PURGED=rally_uid|b1" in r.stdout


def test_failed_purge_where_user_is_gone_counts_as_deleted(tmp_path: Path):
    body = PHASE2 + """
        rgw_delete_user() { [[ "$1" == rally_uid ]] && return 1; return 0; }
        rgw_user_exists() { [[ "$1" == rally_uid ]] && return 1; return 0; }
        purge_rgw_delete
        echo "DELETED=${TOTAL_DELETED} FAILED=${TOTAL_FAILED}"
    """
    r = run_harness(body, tmp_path)
    assert "the user is gone; counted as deleted" in r.stdout, r.stdout + r.stderr
    assert "DELETED=3 FAILED=0" in r.stdout


def test_failed_purge_with_failed_recheck_is_indeterminate(tmp_path: Path):
    body = PHASE2 + """
        rgw_delete_user() { [[ "$1" == rally_uid ]] && return 1; return 0; }
        rgw_user_exists() { return 2; }
        purge_rgw_delete
        echo "DELETED=${TOTAL_DELETED} FAILED=${TOTAL_FAILED} IND=${_RGW_INDETERMINATE[*]} N=${TOTAL_INDETERMINATE}"
    """
    r = run_harness(body, tmp_path)
    assert "INDETERMINATE" in r.stdout, r.stdout + r.stderr
    # Indeterminate is neither deleted nor failed.
    assert "DELETED=1 FAILED=0 IND=rally_uid" in r.stdout


def test_missing_endpoint_counts_as_listing_error_only_for_monitored_service(tmp_path: Path):
    body = """
        openstack() { echo "public endpoint for volumev3 service in RegionOne region not found" >&2; return 1; }
        purge_volumes
        echo "ERR=${_LISTING_ERRORS}"
    """
    monitored = run_harness(body, tmp_path, env={"RALLY_SERVICES": "nova,cinder"})
    assert "ERR=1" in monitored.stdout, monitored.stdout + monitored.stderr
    unmonitored = run_harness(body, tmp_path, env={"RALLY_SERVICES": "nova"})
    assert "ERR=0" in unmonitored.stdout
    assert "cinder not in RALLY_SERVICES" in unmonitored.stdout


TIMEOUT_PHASE2 = PHASE2 + """
    RGW_SETTLE_DELAYS="0 0 0"
    rgw_list_user_buckets() {
        case "$1" in rally_uid) echo '[{"name":"b1"},{"name":"b2"},{"name":"b3"}]' ;; *) echo '[]' ;; esac
    }
    # purge-data times out client-side (curl 28) while RGW keeps deleting.
    rgw_delete_user() { [[ "$1" == rally_uid ]] && return 3; return 0; }
"""


def test_timeout_with_stable_buckets_is_still_indeterminate(tmp_path: Path):
    """A bucket list that stops changing proves nothing after a timeout (the
    objects inside may still be deleting): only the user disappearing is
    definitive, so every look in the budget is used and the outcome is
    INDETERMINATE -- not partial purged/failed counts."""
    body = TIMEOUT_PHASE2 + """
        rgw_list_user_bucket_names() {
            case "$1" in rally_uid) echo b3 ;; esac
        }
        rgw_list_user_buckets() {
            case "$1" in rally_uid) echo '[{"name":"b3"}]' ;; *) echo '[]' ;; esac
        }
        rgw_user_exists() { echo "LOOK $1" >> "$RESULTS_DIR/looks"; return 0; }
        purge_rgw_delete
        echo "DELETED=${TOTAL_DELETED} FAILED=${TOTAL_FAILED} N=${TOTAL_INDETERMINATE} PURGED=${_RGW_PURGED_BUCKETS[*]+${_RGW_PURGED_BUCKETS[*]}}"
    """
    r = run_harness(body, tmp_path)
    assert "user still present after the timeout re-check budget" in r.stdout, r.stdout + r.stderr
    # Only empty_uid was deleted; rally_uid is neither failed nor purged.
    assert "DELETED=1 FAILED=0 N=1 PURGED=" in r.stdout
    # The full budget: one immediate look + one per delay ("0 0 0").
    looks = (tmp_path / "looks").read_text().splitlines()
    assert looks.count("LOOK rally_uid") == 4


def test_non_timeout_failure_keeps_partial_accounting(tmp_path: Path):
    body = PHASE2 + """
        rgw_list_user_buckets() {
            case "$1" in rally_uid) echo '[{"name":"b1"},{"name":"b2"}]' ;; *) echo '[]' ;; esac
        }
        rgw_list_user_bucket_names() {
            case "$1" in
                rally_uid)
                    if [[ -f "$RESULTS_DIR/failed" ]]; then echo b2; else printf '%s\\n' b1 b2; fi ;;
            esac
        }
        rgw_delete_user() { [[ "$1" == rally_uid ]] && { touch "$RESULTS_DIR/failed"; return 1; }; return 0; }
        purge_rgw_delete
        echo "DELETED=${TOTAL_DELETED} FAILED=${TOTAL_FAILED} N=${TOTAL_INDETERMINATE}"
    """
    r = run_harness(body, tmp_path)
    assert "1 bucket(s) removed before the failure, 1 remain" in r.stdout, r.stdout + r.stderr
    assert "DELETED=2 FAILED=2 N=0" in r.stdout


def test_timeout_still_shrinking_after_budget_is_indeterminate(tmp_path: Path):
    body = TIMEOUT_PHASE2 + """
        # Called in $(...) subshells, so count calls in a file.
        rgw_list_user_bucket_names() {
            case "$1" in
                rally_uid)
                    echo x >> "$RESULTS_DIR/calls"; n=$(( $(wc -l < "$RESULTS_DIR/calls") ))
                    case "$n" in
                        1|2) printf '%s\\n' b1 b2 b3 x1 x2 ;;
                        3) printf '%s\\n' b1 b2 b3 x1 ;;
                        4) printf '%s\\n' b1 b2 b3 ;;
                        *) printf '%s\\n' b1 b2 ;;
                    esac ;;
            esac
        }
        rgw_list_user_buckets() {
            case "$1" in rally_uid) echo '[{"name":"b1"},{"name":"b2"},{"name":"b3"},{"name":"x1"},{"name":"x2"}]' ;; *) echo '[]' ;; esac
        }
        MAX_DELETE_THRESHOLD=100
        purge_rgw_delete
        echo "DELETED=${TOTAL_DELETED} FAILED=${TOTAL_FAILED} N=${TOTAL_INDETERMINATE}"
    """
    r = run_harness(body, tmp_path)
    assert "user still present after the timeout re-check budget" in r.stdout, r.stdout + r.stderr
    assert "DELETED=1 FAILED=0 N=1" in r.stdout


def test_timeout_then_user_gone_counts_everything_deleted(tmp_path: Path):
    body = TIMEOUT_PHASE2 + """
        checks=0
        rgw_user_exists() {
            [[ "$1" == rally_uid ]] || return 0
            checks=$((checks + 1)); (( checks >= 2 )) && return 1; return 0
        }
        purge_rgw_delete
        echo "DELETED=${TOTAL_DELETED} FAILED=${TOTAL_FAILED} N=${TOTAL_INDETERMINATE}"
    """
    r = run_harness(body, tmp_path)
    assert "the user is gone; counted as deleted" in r.stdout, r.stdout + r.stderr
    assert "DELETED=5 FAILED=0 N=0" in r.stdout


def test_newline_suffixed_openstack_id_rejects_the_listing(tmp_path: Path):
    body = """
        openstack() { printf '%s' '[{"Name":"s_rally_a","ID":"abc\\n"}]'; }
        purge_servers
        echo "SNAP=${_SNAP_SERVERS[*]+${_SNAP_SERVERS[*]}} ERR=${_LISTING_ERRORS}"
    """
    r = run_harness(body, tmp_path)
    assert "SNAP= ERR=1" in r.stdout, r.stdout + r.stderr
