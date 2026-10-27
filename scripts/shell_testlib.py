"""Shared helpers for the shell-script test suites in scripts/.

Test-only (excluded from the image by .dockerignore)."""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

# macOS ships bash 3.2 at /bin/bash; the scripts need bash >= 4 (mapfile), so
# use the first bash on PATH.
_REAL_BASH = shutil.which("bash") or "/bin/bash"

# A directory holding only `bash`, a wrapper around _REAL_BASH, so a nested
# `bash` or a `#!/usr/bin/env bash` shebang resolves to the same bash >= 4,
# never to /bin/bash 3.2, without putting anything else from its directory
# (e.g. all of Homebrew) ahead of the system directories. Removed at exit.
#
# The wrapper defaults LANG to C (an unset LANG already means the C locale, so
# behavior is unchanged). Homebrew's bash links gettext's libintl, whose
# setlocale() with no LANG/LC_* asks CoreFoundation for the preferred
# languages; in a forked, non-exec'd subshell that intermittently segfaults
# (exit 139). The trigger is a function run in a pipeline subshell that uses a
# temporary locale assignment, e.g. rgw_helpers.sh::_rgw_implicit_pairs'
# `LC_ALL=C awk` inside `$(_rgw_implicit_pairs <<< "$1" | cut -f2)`. Most
# tests pass a minimal env without LANG, so it is set here once for all.
_BASH_DIR = tempfile.mkdtemp(prefix="shell-testlib-bash-")
BASH = os.path.join(_BASH_DIR, "bash")
with open(BASH, "w") as wrapper:
    wrapper.write(f'#!/bin/sh\n: "${{LANG:=C}}"\nexport LANG\nexec "{_REAL_BASH}" "$@"\n')
os.chmod(BASH, 0o755)
atexit.register(shutil.rmtree, _BASH_DIR, ignore_errors=True)

# A minimal PATH for script runs that still finds jq/flock on Linux and macOS
# (Homebrew last). Tests prepend their shim directory to it.
BASE_PATH = ":".join([_BASH_DIR, "/usr/local/bin", "/usr/bin", "/bin", "/opt/homebrew/bin"])


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
