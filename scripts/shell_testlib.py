"""Shared helpers for the shell-script test suites (scripts/ and exporter/).

Test-only (excluded from the image by .dockerignore). exporter tests import it
after putting scripts/ on sys.path."""
from __future__ import annotations

from pathlib import Path


def write_shim(bin_dir: Path, name: str, body: str) -> Path:
    """Write an executable bash shim bin_dir/name running body."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)
    return path


def write_curl_config_shim(bin_dir: Path, argv_file: Path, config_file: Path,
                           tail: str) -> Path:
    """Write a fake curl that records its argv to argv_file and copies the
    `-K <file>` config (how the scripts pass secrets, never via argv) to
    config_file, then runs the shell snippet tail (e.g. "exit 0\\n")."""
    return write_shim(
        bin_dir, "curl",
        f'printf "%s\\n" "$*" > "{argv_file}"\n'
        'prev=""\n'
        'for a in "$@"; do\n'
        f'  if [ "$prev" = "-K" ]; then cat "$a" > "{config_file}"; fi\n'
        '  prev="$a"\n'
        "done\n" + tail,
    )
