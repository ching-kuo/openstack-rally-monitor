"""Shell-level tests for scripts/rgw_helpers.sh."""

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "scripts" / "rgw_helpers.sh"


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
