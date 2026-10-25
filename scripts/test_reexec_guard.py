"""Every executable shell entry point must re-execute itself as the rally
user when started as root (os_helpers.sh::reexec_as_rally); a root run leaves
root-owned locks/state that later rally cron runs cannot write. Pure source
libraries and the root-by-design entrypoint are the only exceptions."""
from __future__ import annotations

from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent

# entrypoint.sh must run as root (it drops to rally itself); the rest are
# source-only libraries with no top-level side effects.
ALLOWLIST = {
    "entrypoint.sh",
    "entrypoint_config.sh",
    "os_helpers.sh",
    "rgw_helpers.sh",
    "run_lib.sh",
    "purge_rgw.sh",
}


@pytest.mark.parametrize(
    "script", sorted(p.name for p in SCRIPTS.glob("*.sh") if p.name not in ALLOWLIST)
)
def test_entry_script_reexecs_as_rally(script: str) -> None:
    source = (SCRIPTS / script).read_text()
    assert 'reexec_as_rally "$@"' in source, (
        f"{script} must call reexec_as_rally (or be added to ALLOWLIST if it "
        "is a pure source library)"
    )


def test_allowlist_has_no_stale_entries() -> None:
    for name in ALLOWLIST:
        assert (SCRIPTS / name).exists(), name
