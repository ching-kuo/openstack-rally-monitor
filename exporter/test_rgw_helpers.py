"""Shell-level tests for scripts/rgw_helpers.sh and auto_purge_rgw in run_tests.sh."""

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "scripts" / "rgw_helpers.sh"
RUN_TESTS = ROOT / "scripts" / "run_tests.sh"


def run_bash(script: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(ROOT),
        env=merged_env,
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

        rgw_check_keystone_project() {{
            case "$1" in
                live-project) return 0 ;;
                orphan-project) return 1 ;;
                broken-project) return 2 ;;
            esac
            return 2
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
# auto_purge_rgw tests (function lives in run_tests.sh, sources rgw_helpers.sh)
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


def test_auto_purge_rgw_deletes_rally_owned_orphans(tmp_path):
    env = _auto_purge_env(tmp_path)
    env.update({
        "RGW_ADMIN_URL": "https://rgw.example.com/admin",
        "RGW_ACCESS_KEY": "ak",
        "RGW_SECRET_KEY": "sk",
    })
    ledger = tmp_path / "results" / "rally_project_ids.log"
    ledger.write_text(
        "2026-04-05T00:00:00Z proj-aaa swift\n",
        encoding="utf-8",
    )
    env["RGW_LEDGER_FILE"] = str(ledger)

    proc = run_bash(
        f"""
        set -euo pipefail
        source "{RUN_TESTS}"

        # Mock: one rally-owned orphan with one bucket
        rgw_find_orphaned_users() {{
            printf '%s\\n' 'proj-aaa$proj-aaa'
        }}
        rgw_list_user_buckets() {{
            printf '[{{"name":"test-bucket","num_objects":5}}]\\n'
        }}
        deleted_buckets=""
        deleted_users=""
        rgw_delete_bucket() {{ deleted_buckets="${{deleted_buckets}}$1,"; return 0; }}
        rgw_delete_user() {{ deleted_users="${{deleted_users}}$1,"; return 0; }}

        auto_purge_rgw

        echo "BUCKETS=${{deleted_buckets}}"
        echo "USERS=${{deleted_users}}"
        """,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "BUCKETS=test-bucket," in proc.stdout
    assert "USERS=proj-aaa$proj-aaa," in proc.stdout
    assert "purged=1" in proc.stdout


def test_auto_purge_rgw_skips_unknown_owner(tmp_path):
    env = _auto_purge_env(tmp_path)
    env.update({
        "RGW_ADMIN_URL": "https://rgw.example.com/admin",
        "RGW_ACCESS_KEY": "ak",
        "RGW_SECRET_KEY": "sk",
    })
    # Empty ledger -- no project is rally-owned
    ledger = tmp_path / "results" / "rally_project_ids.log"
    ledger.write_text("", encoding="utf-8")
    env["RGW_LEDGER_FILE"] = str(ledger)

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


def test_auto_purge_rgw_skips_user_on_bucket_failure(tmp_path):
    env = _auto_purge_env(tmp_path)
    env.update({
        "RGW_ADMIN_URL": "https://rgw.example.com/admin",
        "RGW_ACCESS_KEY": "ak",
        "RGW_SECRET_KEY": "sk",
    })
    ledger = tmp_path / "results" / "rally_project_ids.log"
    ledger.write_text(
        "2026-04-05T00:00:00Z proj-bbb swift\n",
        encoding="utf-8",
    )
    env["RGW_LEDGER_FILE"] = str(ledger)

    proc = run_bash(
        f"""
        set -euo pipefail
        source "{RUN_TESTS}"

        rgw_find_orphaned_users() {{
            printf '%s\\n' 'proj-bbb$proj-bbb'
        }}
        rgw_list_user_buckets() {{
            printf '[{{"name":"stuck-bucket","num_objects":10}}]\\n'
        }}
        rgw_delete_bucket() {{ return 1; }}
        deleted_users=""
        rgw_delete_user() {{ deleted_users="${{deleted_users}}$1,"; return 0; }}

        auto_purge_rgw

        echo "DELETED_USERS=${{deleted_users}}"
        """,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "\nDELETED_USERS=\n" in proc.stdout or proc.stdout.strip().endswith("DELETED_USERS=")
    assert "bucket deletion incomplete" in proc.stdout
    assert "failed=" in proc.stdout


def test_run_order_refreshes_cleanup_metrics_after_rgw_auto_purge():
    script = RUN_TESTS.read_text(encoding="utf-8")
    main_body = script.split("# Main", 1)[1]

    assert main_body.index("auto_purge_rgw") < main_body.index("check_cleanup")
    assert main_body.index("check_cleanup") < main_body.index("publish_dashboard_files")
