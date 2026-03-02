import os
import re
import secrets
import sysconfig

# Locate the site-packages directory dynamically so the patch survives
# Python minor-version bumps without manual path updates.
SITE_PACKAGES = sysconfig.get_paths()["purelib"]

# Inline Python expression embedded into patched library files at build time.
# Generates a 20-char password containing at least one character from each
# required class (upper, lower, digit, special).  Uses only stdlib so it works
# in any Rally library context without additional imports.
_STRONG_PWD_EXPR = (
    '"".join(['
    '__import__("secrets").choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ"), '
    '__import__("secrets").choice("abcdefghijklmnopqrstuvwxyz"), '
    '__import__("secrets").choice("0123456789"), '
    '__import__("secrets").choice("!@#$%^&*"), '
    '__import__("secrets").token_hex(8)'
    '])'
)


def _path(rel):
    return os.path.join(SITE_PACKAGES, rel)


def patch_file(path, pattern, replacement):
    if not os.path.exists(path):
        return
    with open(path, "r") as f:
        content = f.read()
    new_content = re.sub(pattern, replacement, content)
    with open(path, "w") as f:
        f.write(new_content)


def inject_after_imports(path, injection):
    """Inject code after the import block at the top of a Python file."""
    if not os.path.exists(path):
        return
    with open(path, "r") as f:
        content = f.read()
    if injection in content:
        return  # Already patched
    # Find the last top-level (non-indented) import line and inject after it
    lines = content.split("\n")
    last_import_idx = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Only match non-indented imports (top-level)
        if (stripped.startswith("import ") or stripped.startswith("from ")) and not line[0].isspace():
            last_import_idx = i
    lines.insert(last_import_idx + 1, "\n" + injection + "\n")
    with open(path, "w") as f:
        f.write("\n".join(lines))


# ==============================================================================
# Fix multiprocessing start method compatibility
# Python 3.14+ changed the default start method from 'fork' to 'forkserver',
# which breaks Rally's worker process pickling. Force 'fork' method.
# On Python 3.13 and earlier this is a no-op (fork is already the default).
# ==============================================================================
inject_after_imports(
    _path("rally/task/runner.py"),
    "multiprocessing.set_start_method('fork', force=True)"
)

# ==============================================================================
# Keystone password policy patches
# ==============================================================================

# Patch keystone_v3.py create_user to ensure generated random passwords have required complexity
patch_file(
    _path("rally_openstack/common/services/identity/keystone_v3.py"),
    r'name=username, password=password,',
    f'name=username, password=(password or {_STRONG_PWD_EXPR}),',
)

# Patch keystone_v3.py to disable password expiry and forced password change
patch_file(
    _path("rally_openstack/common/services/identity/keystone_v3.py"),
    r'domain=domain_id, enabled=enabled\)',
    r'domain=domain_id, enabled=enabled, options={"ignore_change_password_upon_first_use": True, "ignore_password_expiry": True})'
)

# Patch keystone_v2.py
patch_file(
    _path("rally_openstack/common/services/identity/keystone_v2.py"),
    r'password = password or str\(uuid\.uuid4\(\)\)',
    f'password = password or {_STRONG_PWD_EXPR}',
)

# Patch contexts/keystone/users.py
patch_file(
    _path("rally_openstack/task/contexts/keystone/users.py"),
    r'password = \(str\(uuid\.uuid4\(\)\)',
    f'password = ({_STRONG_PWD_EXPR}',
)

# Patch scenarios/keystone/basic.py
patch_file(
    _path("rally_openstack/task/scenarios/keystone/basic.py"),
    r'password = self\.generate_random_name\(\)',
    f'password = {_STRONG_PWD_EXPR}',
)

# Also patch basic.py create_user to pass a default strong password if kwargs has no password
patch_file(
    _path("rally_openstack/task/scenarios/keystone/basic.py"),
    r'self\.admin_keystone\.create_user\((.*?)\*\*kwargs\)',
    rf'self.admin_keystone.create_user(\1password=kwargs.pop("password", {_STRONG_PWD_EXPR}), **kwargs)',
)
