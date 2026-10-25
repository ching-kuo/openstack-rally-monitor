"""Keep each hashed pip-compile lockfile in step with the .in file it is built from.

Two pairs: docker/requirements.in -> docker/requirements.txt (the image,
installed with --require-hashes) and .github/ci/requirements-audit.in ->
requirements-audit.txt (the CI pip-audit tool). A pin bumped in the .in file
without a re-lock would be silently ignored by the hashed install, so every
direct pin (including any in files the .in pulls in with -r) must appear
verbatim in its lock. (The reverse, a package dropped from the .in but left in
the lock, cannot be checked here: a lock also lists every transitive
dependency.)
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LOCK_PAIRS = [
    (REPO / "docker" / "requirements.in", REPO / "docker" / "requirements.txt"),
    (REPO / ".github" / "ci" / "requirements-audit.in", REPO / ".github" / "ci" / "requirements-audit.txt"),
]
LOCK_IDS = ["image", "ci-audit"]
PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+?)(?:\s|\\|$)")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pins(path: Path) -> dict[str, str]:
    found = {}
    for line in path.read_text().splitlines():
        match = PIN_RE.match(line)
        if match:
            found[canonical(match.group(1))] = match.group(2)
    return found


def direct_pin_files(lock_in: Path) -> list[Path]:
    files = [lock_in]
    for line in lock_in.read_text().splitlines():
        match = re.match(r"^-r\s+(\S+)\s*$", line)
        if match:
            files.append((lock_in.parent / match.group(1)).resolve())
    return files


@pytest.mark.parametrize(("lock_in", "lock"), LOCK_PAIRS, ids=LOCK_IDS)
def test_every_direct_pin_is_locked_at_the_same_version(lock_in, lock):
    locked = pins(lock)
    direct = {}
    for path in direct_pin_files(lock_in):
        assert path.is_file(), path
        direct.update(pins(path))
    assert direct, f"no == pins parsed from {lock_in.relative_to(REPO)}"
    for name, version in direct.items():
        assert locked.get(name) == version, (
            f"{lock_in.relative_to(REPO)} pins {name}=={version} but "
            f"{lock.relative_to(REPO)} has {locked.get(name)}; regenerate it "
            "(see docker/requirements.in)"
        )


@pytest.mark.parametrize(("lock_in", "lock"), LOCK_PAIRS, ids=LOCK_IDS)
def test_every_locked_package_is_hashed(lock_in, lock):
    blocks = re.split(r"\n(?=[A-Za-z0-9])", lock.read_text())
    entries = [block for block in blocks if PIN_RE.match(block)]
    assert len(entries) >= len(pins(lock_in))
    for entry in entries:
        assert "--hash=sha256:" in entry, entry.splitlines()[0]


@pytest.mark.parametrize(("lock_in", "lock"), LOCK_PAIRS, ids=LOCK_IDS)
def test_lock_has_no_option_lines(lock_in, lock):
    # pip-compile can record pip options (e.g. a stray --no-index from a click
    # incompatibility) as top-level lines, which pip then applies to the
    # install. Only package entries and their indented --hash lines belong in
    # a lock.
    options = [line for line in lock.read_text().splitlines() if line.startswith("-")]
    assert options == []
