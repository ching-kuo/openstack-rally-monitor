"""Tests for scripts/cleanup_monitor.sh (orphan counts -> cleanup_metrics.json).

The script runs end to end with PATH shims: `openstack` answers each list
subcommand from a per-test table, `flock` always grants the lock (util-linux
flock is absent on macOS; the lock is not under test).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

BASH = shutil.which("bash") or "/bin/bash"
SCRIPT = Path(__file__).resolve().parent / "cleanup_monitor.sh"
BASE_PATH = "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"


def _shim_case(out: str) -> str:
    if out == "FAIL":
        return 'echo "Internal Server Error (HTTP 500)" >&2; exit 1'
    if out == "NOENDPOINT":
        return 'echo "public endpoint for volumev3 service in RegionOne region not found" >&2; exit 1'
    return "printf %s " + json.dumps(out)


def run_monitor(tmp_path: Path, responses: dict[str, str],
                services: str = "") -> tuple[subprocess.CompletedProcess, dict]:
    """responses maps "<resource> list" to the shim's stdout; "FAIL" makes
    that call exit 1, "NOENDPOINT" fails it like a service missing from the
    catalog. Unlisted subcommands return []."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cases = "\n".join(
        f'  "{key}") {_shim_case(out)} ;;'
        for key, out in responses.items()
    )
    (bin_dir / "openstack").write_text(
        "#!/usr/bin/env bash\n"
        'key="$1 $2"; [[ "$1" == security ]] && key="$1 $2 $3"\n'
        f'case "$key" in\n{cases}\n  *) echo "[]" ;;\nesac\n'
    )
    (bin_dir / "flock").write_text("#!/usr/bin/env bash\nexit 0\n")
    for shim in bin_dir.iterdir():
        shim.chmod(0o755)
    result = subprocess.run(
        [BASH, str(SCRIPT)],
        env={
            "PATH": f"{bin_dir}:{BASE_PATH}",
            "RESULTS_DIR": str(tmp_path),
            "CLEANUP_LOCK_FILE": str(tmp_path / "cleanup.lock"),
            "RALLY_SERVICES": services,
        },
        capture_output=True, text=True, check=False,
    )
    metrics_file = tmp_path / "cleanup_metrics.json"
    metrics = json.loads(metrics_file.read_text()) if metrics_file.exists() else {}
    return result, metrics


def test_counts_scenario_and_context_orphans(tmp_path: Path) -> None:
    result, m = run_monitor(tmp_path, {
        "server list": json.dumps([
            {"Name": "s_rally_a", "ID": "id1"}, {"Name": "c_rally_b", "ID": "id2"}, {"Name": "prod"},
        ]),
        "project list": json.dumps([{"Name": "c_rally_p", "ID": "id3"}, {"Name": "c_rally_q", "ID": "id4"}]),
        "security group list": json.dumps([{"Name": "s_rally_sg", "ID": "id5"}]),
    })
    assert result.returncode == 1, result.stdout  # s_rally_* orphans present
    assert m["details"]["servers"] == 1
    assert m["context_details"]["servers"] == 1
    assert m["context_details"]["projects"] == 2
    assert m["details"]["security_groups"] == 1
    assert m["orphaned_resources"]["neutron"] == 1
    assert m["openstack_scan_status"] == "ok"
    assert m["openstack_scan_errors"] == 0
    assert m["rgw_scan_status"] == "skipped"


def test_null_name_does_not_zero_the_type(tmp_path: Path) -> None:
    result, m = run_monitor(tmp_path, {
        "network list": json.dumps([
            {"Name": None}, {"ID": "no-name"}, {"Name": "s_rally_n", "ID": "id6"}, {"Name": "c_rally_m", "ID": "id7"},
        ]),
    })
    assert m["details"]["networks"] == 1
    assert m["context_details"]["networks"] == 1
    assert m["openstack_scan_status"] == "ok"


def test_failed_list_call_is_a_scan_error_not_zero_orphans(tmp_path: Path) -> None:
    result, m = run_monitor(tmp_path, {
        "server list": "FAIL",
        "volume list": "not json",
        "image list": json.dumps([{"Name": "c_rally_img", "ID": "id8"}]),
    })
    assert result.returncode == 1
    assert m["openstack_scan_status"] == "error"
    assert m["openstack_scan_errors"] == 2
    # The healthy listings still count.
    assert m["context_details"]["images"] == 1
    assert "list call failed" in result.stderr


def test_clean_cloud_exits_zero(tmp_path: Path) -> None:
    result, m = run_monitor(tmp_path, {})
    assert result.returncode == 0, result.stdout
    assert m["cleanup_failed"] == 0
    assert m["context_cleanup_warning"] == 0
    assert m["openstack_scan_status"] == "ok"


def test_every_type_is_scanned_even_for_unmonitored_services(tmp_path: Path) -> None:
    """Nova scenarios leave c_rally networks/routers behind: neutron listings
    must be scanned even when only nova is monitored."""
    result, m = run_monitor(tmp_path, {
        "network list": json.dumps([{"Name": "c_rally_net", "ID": "n1"}]),
        "volume list": "FAIL",
    }, services="nova")
    assert m["context_details"]["networks"] == 1
    # A real failure is an error whatever the service.
    assert m["openstack_scan_status"] == "error"
    assert m["openstack_scan_errors"] == 1


def test_missing_endpoint_of_unmonitored_service_is_not_applicable(tmp_path: Path) -> None:
    result, m = run_monitor(tmp_path, {"volume list": "NOENDPOINT"}, services="nova,neutron,glance")
    assert m["openstack_scan_status"] == "ok", result.stderr
    assert "no cinder endpoint" in result.stderr
    assert result.returncode == 0


def test_missing_endpoint_of_monitored_service_is_a_scan_error(tmp_path: Path) -> None:
    # Wrong OS_INTERFACE / OS_REGION_NAME must not hide a monitored service.
    result, m = run_monitor(tmp_path, {"volume list": "NOENDPOINT"})
    assert m["openstack_scan_status"] == "error"
    assert m["openstack_scan_errors"] == 1
    assert result.returncode == 1


def test_keystone_listings_are_always_scanned(tmp_path: Path) -> None:
    result, m = run_monitor(tmp_path, {"user list": "FAIL"}, services="nova")
    assert m["openstack_scan_status"] == "error"
    assert m["openstack_scan_errors"] == 1


def test_rally_entry_without_valid_id_is_a_scan_error(tmp_path: Path) -> None:
    # A partial view must never pass as complete (shared os_helpers.sh rule).
    result, m = run_monitor(tmp_path, {"server list": json.dumps([{"Name": "s_rally_a"}])})
    assert m["openstack_scan_status"] == "error"
    assert m["details"]["servers"] == 0
