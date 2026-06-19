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

import shutil
import subprocess
from pathlib import Path

BASH = shutil.which("bash") or "/bin/bash"
SCRIPT = Path(__file__).resolve().parent / "purge_orphans.sh"


def run_purge_rgw(stubs: str) -> dict[str, str]:
    """Source purge_orphans.sh, apply stub overrides, run purge_rgw, and emit
    the snapshot/counters as KEY=VALUE lines parsed into a dict."""
    harness = f"""
        set -euo pipefail
        export PURGE_ORPHANS_NO_MAIN=1
        source "{SCRIPT}"
        # Stub overrides replace the curl/Keystone-backed helpers.
        {stubs}
        purge_rgw >/tmp/purge_rgw.log 2>&1 || true
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
"""


def test_empty_unknown_owner_is_eligible():
    r = run_purge_rgw(STUBS)
    assert r["EMPTY"] == "1"
    # rally_uid + empty_uid are both eligible users; data_uid is not.
    assert "empty_uid" in r["SNAP_USERS"]
    assert "rally_uid" in r["SNAP_USERS"]
    assert "data_uid" not in r["SNAP_USERS"]


def test_unknown_owner_with_data_is_skipped():
    r = run_purge_rgw(STUBS)
    assert r["SKIPPED"] == "1"
    assert "data_uid" not in r["SNAP_USERS"]
    # The empty orphan owns no buckets, so only the rally-owned bucket snapshots.
    assert "keepme" not in r["SNAP_BUCKETS"]
    assert "rally_uid|b1" in r["SNAP_BUCKETS"]


def test_found_count_includes_empty_orphan():
    r = run_purge_rgw(STUBS)
    # rally_uid (1 user + 1 bucket) + empty_uid (1 user) = 3; data_uid skipped.
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


def test_unknown_owner_with_empty_bucket_is_skipped():
    r = run_purge_rgw(EMPTY_BUCKET_STUBS)
    assert r["EMPTY"] == "0"
    assert r["SKIPPED"] == "1"
    assert "shellbucket_uid" not in r["SNAP_USERS"]
    assert r["FOUND"] == "0"
