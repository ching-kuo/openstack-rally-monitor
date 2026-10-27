"""Contract tests for run_tests.sh Rally task argument generation. That main
uses write_task_args (and aborts when it fails) is tested in test_run_main.py."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from shell_testlib import BASH, write_shim

SCRIPT = Path(__file__).resolve().parent / "run_tests.sh"


def write_task_args(
    target: Path,
    *,
    flavor: str | None = None,
    image: str | None = None,
    auth_url: str | None = None,
    path: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["RESULTS_DIR"] = str(target.parent)
    if flavor is None:
        env.pop("RALLY_NOVA_FLAVOR", None)
    else:
        env["RALLY_NOVA_FLAVOR"] = flavor
    if image is None:
        env.pop("RALLY_NOVA_IMAGE", None)
    else:
        env["RALLY_NOVA_IMAGE"] = image
    if auth_url is None:
        env.pop("OS_AUTH_URL", None)
    else:
        env["OS_AUTH_URL"] = auth_url
    if path is not None:
        env["PATH"] = path

    return subprocess.run(
        [
            BASH,
            "-c",
            'source "$1"; write_task_args "$2"',
            "task-args-test",
            str(SCRIPT),
            str(target),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_defaults_match_existing_task_argument_contract(tmp_path: Path) -> None:
    target = tmp_path / "task_args.json"

    result = write_task_args(target)

    assert result.returncode == 0, result.stderr
    assert json.loads(target.read_text()) == {
        "env": {
            "RALLY_NOVA_FLAVOR": "m1.tiny",
            "RALLY_NOVA_IMAGE": "cirros-0.6.2-x86_64-disk",
            "OS_AUTH_URL": "",
        }
    }


def test_special_text_round_trips_as_json_data(tmp_path: Path) -> None:
    target = tmp_path / "task_args.json"
    flavor = 'tiny "quoted" \\ flavor\nsecond line'
    image = "cirros-雪-\timage"
    auth_url = 'https://identity.example/v3/path?note="quoted"&next=\\value'

    result = write_task_args(target, flavor=flavor, image=image, auth_url=auth_url)

    assert result.returncode == 0, result.stderr
    assert json.loads(target.read_text()) == {
        "env": {
            "RALLY_NOVA_FLAVOR": flavor,
            "RALLY_NOVA_IMAGE": image,
            "OS_AUTH_URL": auth_url,
        }
    }


def test_generation_failure_preserves_existing_target(tmp_path: Path) -> None:
    target = tmp_path / "task_args.json"
    target.write_text('{"existing": true}\n')
    failing_bin = tmp_path / "failing-bin"
    write_shim(failing_bin, "jq", "exit 42\n")

    result = write_task_args(
        target,
        path=f"{failing_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
    )

    assert result.returncode != 0
    assert target.read_text() == '{"existing": true}\n'
    assert not target.with_suffix(".json.tmp").exists()
