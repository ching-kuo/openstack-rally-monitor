"""Shell-level tests for scripts/rgw_helpers.sh, including auto_purge_rgw."""

import subprocess
from pathlib import Path

from shell_testlib import BASE_PATH, BASH, write_curl_config_shim

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
HELPERS = SCRIPTS / "rgw_helpers.sh"
RUN_TESTS = SCRIPTS / "run_tests.sh"


def run_bash(script: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    """Run `script` with a minimal environment: BASE_PATH plus only what the
    test sets. Never the developer's own, whose RGW_*/OS_* credentials could
    point the helpers at a live RGW or Keystone."""
    return subprocess.run(
        [BASH, "-c", script],
        cwd=str(ROOT),
        env={"PATH": BASE_PATH, **(env or {})},
        text=True,
        capture_output=True,
    )


def test_rgw_available_requires_all_credentials():
    proc = run_bash(
        f"""
        set -euo pipefail
        source "{HELPERS}"
        rgw_available && echo yes || echo no
        """,
        env={
            "RGW_ADMIN_URL": "https://rgw.example.com/admin",
            "RGW_ACCESS_KEY": "access",
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "no"


def test_rgw_implicit_project_id_extracts_matching_uid():
    proc = run_bash(
        f"""
        set -euo pipefail
        source "{HELPERS}"
        rgw_implicit_project_id 'project-123$project-123'
        """
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "project-123"


def test_rgw_is_rally_owned_reads_timestamped_ledger(tmp_path):
    ledger = tmp_path / "rally_project_ids.log"
    ledger.write_text(
        "2026-04-05T00:00:00Z project-a swift\n"
        "2026-04-05T00:01:00Z project-b nova\n",
        encoding="utf-8",
    )
    proc = run_bash(
        f"""
        set -euo pipefail
        source "{HELPERS}"
        RGW_LEDGER_FILE="{ledger}"
        rgw_is_rally_owned project-b && echo yes || echo no
        """,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "yes"


def test_rgw_find_orphaned_users_skips_inconclusive_keystone_results():
    proc = run_bash(
        f"""
        set -euo pipefail
        source "{HELPERS}"

        rgw_list_implicit_users() {{
            printf '%s\\n' \
                'live-project$live-project' \
                'orphan-project$orphan-project' \
                'broken-project$broken-project'
        }}

        rgw_prefetch_keystone_projects() {{ return 1; }}

        rgw_check_keystone_projects() {{
            local id
            while IFS= read -r id; do
                case "$id" in
                    live-project) echo "$id exists" ;;
                    orphan-project) echo "$id gone" ;;
                    broken-project) echo "$id error" ;;
                esac
            done
        }}

        output_file="$(mktemp)"
        rgw_find_orphaned_users > "${{output_file}}"
        status="$?"
        output="$(cat "${{output_file}}")"
        rm -f "${{output_file}}"
        printf '%s|%s|%s\\n' "${{status}}" "$RGW_LAST_FIND_ERRORS" "${{output}}"
        """,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "0|1|orphan-project$orphan-project"


def test_rgw_list_implicit_users_parses_paginated_keys_response():
    proc = run_bash(
        f"""
        set -euo pipefail
        source "{HELPERS}"

        calls=0
        rgw_curl() {{
            local body_var="$1" status_var="$2"
            calls=$((calls + 1))
            if [[ "${{calls}}" -eq 1 ]]; then
                printf -v "${{body_var}}" '%s' '{{"keys":["live$live","skip-me","orphan$orphan"],"truncated":true,"marker":"orphan$orphan"}}'
            else
                printf -v "${{body_var}}" '%s' '{{"keys":["next$next"],"truncated":false}}'
            fi
            printf -v "${{status_var}}" '%s' '200'
        }}

        rgw_list_implicit_users
        """,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines() == ["live$live", "orphan$orphan", "next$next"]


# ---------------------------------------------------------------------------
# auto_purge_rgw tests (function lives in rgw_helpers.sh, sourced by run_tests.sh)
# ---------------------------------------------------------------------------

def _auto_purge_env(tmp_path: Path) -> dict[str, str]:
    """Common env vars so sourcing run_tests.sh doesn't fail on missing dirs."""
    results = tmp_path / "results"
    results.mkdir()
    run_dir = results / "20260411T000000Z"
    run_dir.mkdir()
    (run_dir / "run.log").touch()
    return {
        "RESULTS_DIR": str(results),
        "RUN_DIR": str(run_dir),
        "RUN_LOG": str(run_dir / "run.log"),
    }


def test_auto_purge_rgw_skips_when_not_configured(tmp_path):
    env = _auto_purge_env(tmp_path)
    proc = run_bash(
        f"""
        set -euo pipefail
        source "{RUN_TESTS}"
        auto_purge_rgw
        echo "exit_code=$?"
        """,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    # Should not print any purge log line
    assert "Auto-purging" not in proc.stdout


def _rgw_env(tmp_path: Path, ledger_text: str) -> dict[str, str]:
    env = _auto_purge_env(tmp_path)
    env.update({
        "RGW_ADMIN_URL": "https://rgw.example.com/admin",
        "RGW_ACCESS_KEY": "ak",
        "RGW_SECRET_KEY": "sk",
    })
    ledger = tmp_path / "results" / "rally_project_ids.log"
    ledger.write_text(ledger_text, encoding="utf-8")
    env["RGW_LEDGER_FILE"] = str(ledger)
    return env


def test_auto_purge_rgw_deletes_rally_owned_orphans_with_purge_data(tmp_path):
    env = _rgw_env(tmp_path, "2026-04-05T00:00:00Z proj-aaa swift\n")
    proc = run_bash(
        f"""
        set -euo pipefail
        source "{RUN_TESTS}"

        rgw_find_orphaned_users() {{
            printf '%s\\n' 'proj-aaa$proj-aaa'
        }}
        deleted_users=""
        rgw_delete_user() {{ deleted_users="${{deleted_users}}$1:${{2:-false}},"; return 0; }}

        auto_purge_rgw

        echo "USERS=${{deleted_users}}"
        """,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    # Buckets go with the user (purge-data), never by bare bucket name.
    assert "USERS=proj-aaa$proj-aaa:true," in proc.stdout
    assert "purged=1" in proc.stdout


def test_auto_purge_rgw_skips_unknown_owner(tmp_path):
    env = _rgw_env(tmp_path, "")
    proc = run_bash(
        f"""
        set -euo pipefail
        source "{RUN_TESTS}"

        rgw_find_orphaned_users() {{
            printf '%s\\n' 'unknown-proj$unknown-proj'
        }}
        deleted_users=""
        rgw_delete_user() {{ deleted_users="${{deleted_users}}$1,"; return 0; }}

        auto_purge_rgw

        echo "DELETED=${{deleted_users}}"
        """,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "\nDELETED=\n" in proc.stdout or proc.stdout.strip().endswith("DELETED=")
    assert "skipped_unknown=1" in proc.stdout


def test_auto_purge_rgw_counts_failed_user_deletion(tmp_path):
    env = _rgw_env(tmp_path, "2026-04-05T00:00:00Z proj-bbb swift\n")
    proc = run_bash(
        f"""
        set -euo pipefail
        source "{RUN_TESTS}"

        rgw_find_orphaned_users() {{
            printf '%s\\n' 'proj-bbb$proj-bbb'
        }}
        rgw_delete_user() {{ return 1; }}

        auto_purge_rgw
        """,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "deletion FAILED" in proc.stdout
    assert "purged=0 failed=1" in proc.stdout


def test_auto_purge_rgw_fails_closed_on_inconclusive_scan(tmp_path):
    """Any inconclusive Keystone lookup skips the whole purge, even for an
    orphan the ledger vouches for."""
    env = _rgw_env(tmp_path, "2026-04-05T00:00:00Z proj-ccc swift\n")
    proc = run_bash(
        f"""
        set -euo pipefail
        source "{RUN_TESTS}"

        rgw_find_orphaned_users() {{
            RGW_LAST_FIND_ERRORS=2
            printf '%s\\n' 'proj-ccc$proj-ccc'
        }}
        deleted_users=""
        rgw_delete_user() {{ deleted_users="${{deleted_users}}$1,"; return 0; }}

        auto_purge_rgw

        echo "DELETED=${{deleted_users}}"
        """,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "skipping auto-purge (fail-closed)" in proc.stdout
    assert proc.stdout.strip().endswith("DELETED=")


def test_auto_purge_rgw_skips_when_scan_fails(tmp_path):
    env = _rgw_env(tmp_path, "2026-04-05T00:00:00Z proj-ddd swift\n")
    proc = run_bash(
        f"""
        set -euo pipefail
        source "{RUN_TESTS}"

        rgw_find_orphaned_users() {{ printf '%s\\n' 'proj-ddd$proj-ddd'; return 1; }}
        deleted_users=""
        rgw_delete_user() {{ deleted_users="${{deleted_users}}$1,"; return 0; }}

        auto_purge_rgw

        echo "DELETED=${{deleted_users}}"
        """,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "RGW orphan scan failed; skipping auto-purge" in proc.stdout
    assert proc.stdout.strip().endswith("DELETED=")


# ---------------------------------------------------------------------------
# rgw_delete_user request shape
# ---------------------------------------------------------------------------

def _delete_user(status: str, purge: str) -> subprocess.CompletedProcess[str]:
    return run_bash(
        f"""
        set -euo pipefail
        source "{HELPERS}"
        rgw_curl() {{
            local body_var="$1" status_var="$2"
            shift 2
            echo "CURL $*"
            echo "TIMEOUT=${{RGW_CURL_TIMEOUT}}"
            printf -v "${{body_var}}" '%s' ''
            printf -v "${{status_var}}" '%s' '{status}'
        }}
        rgw_delete_user 'p$p' {purge} && echo RC=0 || echo RC=$?
        """
    )


def test_rgw_delete_user_purge_data_flag():
    proc = _delete_user("200", "true")
    assert "CURL DELETE /user uid=p$p format=json purge-data=true" in proc.stdout
    assert "RC=0" in proc.stdout
    proc = _delete_user("200", "")
    assert "CURL DELETE /user uid=p$p format=json\n" in proc.stdout


def test_rgw_delete_user_404_is_distinct_and_logged():
    # 404 = nothing deleted: a distinct code so callers do not count it.
    proc = _delete_user("404", "true")
    assert "RC=2" in proc.stdout
    assert "already absent (HTTP 404)" in proc.stderr


def test_purge_data_delete_gets_the_long_timeout():
    proc = _delete_user("200", "true")
    assert "TIMEOUT=600" in proc.stdout
    proc = _delete_user("200", "")
    assert "TIMEOUT=30" in proc.stdout


def test_auto_purge_rgw_does_not_count_an_absent_user(tmp_path):
    env = _rgw_env(tmp_path, "2026-04-05T00:00:00Z proj-eee swift\n")
    proc = run_bash(
        f"""
        set -euo pipefail
        source "{RUN_TESTS}"
        rgw_find_orphaned_users() {{ printf '%s\\n' 'proj-eee$proj-eee'; }}
        rgw_delete_user() {{ return 2; }}
        auto_purge_rgw
        """,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "already absent" in proc.stdout
    assert "purged=0 failed=0" in proc.stdout


def test_rgw_delete_user_conflict_fails():
    # RGW refuses (409) to drop a user that still owns buckets without
    # purge-data -- the fail-closed path for empty unknown-owner orphans.
    proc = _delete_user("409", "")
    assert "RC=1" in proc.stdout


# ---------------------------------------------------------------------------
# rgw_find_orphaned_users: bulk anti-join vs per-project fallback parity
# ---------------------------------------------------------------------------

PARITY_USERS = [
    "live$live", "gone$gone", "Gone-2$Gone-2", "a$b", "dup$dup$dup", "$",
    "x$", "$x", "bad_char$bad_char", "nodollar", "", "gone$gone",
    "caf\u00e9$caf\u00e9", "live-2$live-2",
]
PARITY_PROJECTS = ["live", "live-2", "unrelated"]


def _find_orphans(tmp_path: Path, bulk: bool) -> subprocess.CompletedProcess[str]:
    users = tmp_path / "users.txt"
    users.write_text("\n".join(PARITY_USERS) + "\n", encoding="utf-8")
    projects = tmp_path / "projects.txt"
    projects.write_text("\n".join(PARITY_PROJECTS) + "\n", encoding="utf-8")
    prefetch = (
        f'_RGW_KEYSTONE_PROJECTS_FILE=$(mktemp); cp "{projects}" "$_RGW_KEYSTONE_PROJECTS_FILE"'
        if bulk else "return 1"
    )
    return run_bash(
        f"""
        set -euo pipefail
        source "{HELPERS}"
        rgw_list_implicit_users() {{ cat "{users}"; }}
        rgw_prefetch_keystone_projects() {{ {prefetch}; }}
        rgw_check_keystone_projects() {{
            local id
            while IFS= read -r id; do
                if grep -Fxq "$id" "{projects}"; then echo "$id exists"; else echo "$id gone"; fi
            done
        }}
        rgw_find_orphaned_users
        echo "ERRORS=$RGW_LAST_FIND_ERRORS"
        """,
        env={"LC_ALL": "en_US.UTF-8"},
    )


def test_bulk_anti_join_matches_per_project_fallback(tmp_path):
    bulk = _find_orphans(tmp_path, bulk=True)
    fallback = _find_orphans(tmp_path, bulk=False)
    assert bulk.returncode == 0, bulk.stderr
    assert fallback.returncode == 0, fallback.stderr
    assert bulk.stdout == fallback.stdout
    assert bulk.stdout.splitlines() == [
        "gone$gone", "Gone-2$Gone-2", "gone$gone", "ERRORS=0",
    ]


def test_truncated_bulk_listing_falls_back_to_per_project_lookups(tmp_path):
    """keystone_projects.py exits 3 on a truncated list; a partial bulk view
    must never feed the anti-join (live projects would look gone)."""
    proc = run_bash(
        f"""
        set -euo pipefail
        source "{HELPERS}"
        rgw_list_keystone_project_ids() {{ echo live; return 3; }}
        rgw_prefetch_keystone_projects && echo BULK || echo FALLBACK
        """
    )
    assert proc.stdout.strip() == "FALLBACK"


def test_empty_bulk_listing_falls_back_to_per_project_lookups(tmp_path):
    """A successful-but-empty project list must not mark every user orphaned."""
    proc = run_bash(
        f"""
        set -euo pipefail
        source "{HELPERS}"
        rgw_list_keystone_project_ids() {{ :; }}
        rgw_prefetch_keystone_projects && echo BULK || echo FALLBACK
        """
    )
    assert proc.stdout.strip() == "FALLBACK"


# ---------------------------------------------------------------------------
# rgw_list_user_bucket_names: the stats-free inventory
# ---------------------------------------------------------------------------

def _names(status: str, body: str) -> subprocess.CompletedProcess[str]:
    return run_bash(
        f"""
        set -euo pipefail
        source "{HELPERS}"
        rgw_curl() {{
            local body_var="$1" status_var="$2"
            shift 2
            echo "CURL $*" >&2
            printf -v "${{body_var}}" '%s' '{body}'
            printf -v "${{status_var}}" '%s' '{status}'
        }}
        rgw_list_user_bucket_names 'p$p' && echo RC=0 || echo RC=$?
        """
    )


def test_bucket_names_listing_is_stats_free():
    proc = _names("200", '["b1","b2"]')
    assert proc.stdout.split() == ["b1", "b2", "RC=0"]
    assert "stats" not in proc.stderr
    assert "CURL GET /bucket uid=p$p format=json" in proc.stderr


def test_bucket_names_listing_accepts_object_entries_and_404():
    assert _names("200", '[{"bucket":"b1"}]').stdout.split() == ["b1", "RC=0"]
    assert "RC=2" in _names("404", "").stdout


def test_bucket_names_listing_rejects_unexpected_bodies():
    assert "RC=1" in _names("200", '{"buckets":[]}').stdout
    assert "RC=1" in _names("200", '[{"other":1}]').stdout
    assert "RC=1" in _names("500", "[]").stdout


def test_fallback_checker_failure_is_inconclusive_not_orphaned():
    """A checker that dies (or skips IDs) leaves them unanswered: counted as
    errors, never emitted as orphans."""
    proc = run_bash(
        f"""
        set -euo pipefail
        source "{HELPERS}"
        rgw_list_implicit_users() {{ printf '%s\\n' 'a$a' 'b$b' 'c$c'; }}
        rgw_prefetch_keystone_projects() {{ return 1; }}
        rgw_check_keystone_projects() {{ cat >/dev/null; echo "a gone"; return 1; }}
        rgw_find_orphaned_users
        echo "ERRORS=$RGW_LAST_FIND_ERRORS"
        """
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["a$a", "ERRORS=2"]


def test_fallback_checks_each_project_once_over_one_checker_call():
    proc = run_bash(
        f"""
        set -euo pipefail
        source "{HELPERS}"
        rgw_list_implicit_users() {{ printf '%s\\n' 'a$a' 'b$b' 'a$a'; }}
        rgw_prefetch_keystone_projects() {{ return 1; }}
        calls=0
        rgw_check_keystone_projects() {{
            echo "CALL" >&2
            local id; while IFS= read -r id; do echo "IN $id" >&2; echo "$id gone"; done
        }}
        rgw_find_orphaned_users
        """
    )
    assert proc.stderr.count("CALL") == 1
    assert proc.stderr.count("IN a") == 1
    assert proc.stdout.split() == ["a$a", "b$b", "a$a"]


def test_implicit_user_listing_rejects_newline_suffixed_uid():
    """jq's `$` matches before a trailing newline; a uid "a$a\\n" printed raw
    would become the line "a$a" and target a different user."""
    proc = run_bash(
        f"""
        set -euo pipefail
        source "{HELPERS}"
        rgw_curl() {{
            local body_var="$1" status_var="$2"
            printf -v "${{body_var}}" '%s' '{{"keys":["a$a\\n","b$b"],"truncated":false}}'
            printf -v "${{status_var}}" '%s' '200'
        }}
        rgw_list_implicit_users
        """,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "b$b\n"


def test_rgw_curl_keeps_credentials_out_of_argv(tmp_path):
    """argv is world-readable via /proc/<pid>/cmdline, so the RGW key pair
    must reach curl only through its -K config, never `--user KEY:SECRET`."""
    bin_dir = tmp_path / "bin"
    argv_file = tmp_path / "argv.txt"
    config_file = tmp_path / "config.txt"
    write_curl_config_shim(bin_dir, argv_file, config_file, "printf 200\n")
    proc = run_bash(
        f"""
        set -euo pipefail
        source "{HELPERS}"
        rgw_curl body code GET "/user" "format=json"
        echo "code=$code"
        """,
        env={
            "PATH": f"{bin_dir}:{BASE_PATH}",
            "RGW_ADMIN_URL": "https://rgw.example.com/admin",
            "RGW_ACCESS_KEY": "AKEY",
            "RGW_SECRET_KEY": 'SECRET"KEY',
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert "code=200" in proc.stdout
    argv = argv_file.read_text()
    assert "SECRET" not in argv and "AKEY" not in argv
    assert "--aws-sigv4" in argv
    assert config_file.read_text() == 'user = "AKEY:SECRET\\"KEY"\n'
    assert "SECRET" not in proc.stderr
