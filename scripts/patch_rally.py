import ast
import os
import re
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


class PatchError(RuntimeError):
    """A patch applied nothing or broke its target; fails the image build."""


def _check_compiles(path: str, content: str) -> None:
    try:
        compile(content, path, "exec")
    except SyntaxError as exc:
        raise PatchError(f"{path}: patched source does not compile: {exc}") from exc


def _write_checked(path: str, content: str) -> None:
    _check_compiles(path, content)
    with open(path, "w") as f:
        f.write(content)


def _binds_module(imports: list, name: str) -> bool:
    """True when the last top-level import binding ``name`` binds the module
    itself: `import m` and `import m.sub` do; `import os as m`,
    `import m.sub as m` and `from x import m` do not."""
    bound = False
    for node in imports:
        for alias in node.names:
            if isinstance(node, ast.Import) and alias.asname is None:
                if alias.name.split(".")[0] == name:
                    bound = True
            elif (alias.asname or alias.name) == name:
                bound = isinstance(node, ast.Import) and alias.name == name
    return bound


def _read(path: str) -> str:
    # A missing target means upstream moved the code: never skip silently.
    if not os.path.exists(path):
        raise PatchError(f"{path}: patch target not found")
    with open(path, "r") as f:
        return f.read()


def patch_file(path: str, pattern: str, replacement: str, min_count: int = 1) -> None:
    """Replace every match; fewer than ``min_count`` matches (the count in the
    pinned release) means upstream moved some call sites. Runs once on a fresh
    install: a second run finds no unpatched match and raises.
    shortcut: the count is textual, so a match inside a comment or string also
    counts; match on tokenize output if upstream ever leaves such copies."""
    new_content, count = re.subn(pattern, replacement, _read(path))
    if count < min_count:
        raise PatchError(f"{path}: pattern {pattern!r} matched {count}, expected >= {min_count}")
    _write_checked(path, new_content)


def inject_after_imports(path: str, injection: str, requires: str) -> None:
    """Inject code after the last top-level import of a Python file.

    ``requires`` is the module name the injection uses; it must be bound by a
    top-level ``import`` (compile() cannot catch a NameError at import time).
    """
    content = _read(path)
    try:
        tree = ast.parse(content)
    except SyntaxError as exc:
        raise PatchError(f"{path}: target does not parse: {exc}") from exc
    # ast gives the true end of a multi-line `from x import (...)` and ignores
    # import-like lines inside docstrings.
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    if not _binds_module(imports, requires):
        raise PatchError(f"{path}: no top-level `import {requires}` for the injection to use")
    if any(ast.unparse(n) == ast.unparse(ast.parse(injection)) for n in tree.body):
        _check_compiles(path, content)
        return  # Already patched (a real top-level statement, not a comment)
    lines = content.split("\n")
    lines.insert(imports[-1].end_lineno, "\n" + injection + "\n")
    _write_checked(path, "\n".join(lines))


def main() -> None:
    # ==============================================================================
    # Fix multiprocessing start method compatibility
    # Python 3.14+ changed the default start method from 'fork' to 'forkserver',
    # which breaks Rally's worker process pickling. Force 'fork' method.
    # On Python 3.13 and earlier this is a no-op (fork is already the default).
    # ==============================================================================
    inject_after_imports(
        _path("rally/task/runner.py"),
        "multiprocessing.set_start_method('fork', force=True)",
        requires="multiprocessing",
    )

    # ==============================================================================
    # Keystone password policy patches
    # ==============================================================================

    # Patch keystone_v3.py create_user to ensure generated random passwords have required complexity
    patch_file(
        _path("rally_openstack/common/services/identity/keystone_v3.py"),
        r'name=username, password=password,',
        f'name=username, password=(password or {_STRONG_PWD_EXPR}),',
        min_count=2,  # the users.create call and the create_user wrapper's username= call
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
        min_count=4,
    )


if __name__ == "__main__":
    main()
