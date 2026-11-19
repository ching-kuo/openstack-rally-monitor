"""Tests for scripts/patch_rally.py: a patch that applies nothing must fail the
image build, never ship an unpatched Rally silently."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patch_rally as pr  # noqa: E402


def test_patch_file_rewrites_a_match(tmp_path):
    target = tmp_path / "m.py"
    target.write_text("password = old()\n")
    pr.patch_file(str(target), r"old\(\)", "new()")
    assert target.read_text() == "password = new()\n"


@pytest.mark.parametrize("content,min_count", [
    (None, 1),                         # target file gone
    ("x = something_else()\n", 1),     # pattern matches nothing
    ("x = old()\ny = keep()\n", 2),    # some call sites moved upstream
])
def test_patch_file_fails_when_too_little_applies(tmp_path, content, min_count):
    target = tmp_path / "m.py"
    if content is not None:
        target.write_text(content)
    with pytest.raises(pr.PatchError):
        pr.patch_file(str(target), r"old\(\)", "new()", min_count=min_count)


def test_patch_file_refuses_a_result_that_does_not_compile(tmp_path):
    target = tmp_path / "m.py"
    target.write_text("x = old()\n")
    with pytest.raises(pr.PatchError):
        pr.patch_file(str(target), r"old\(\)", "new(")
    assert target.read_text() == "x = old()\n"


def test_inject_goes_after_a_multiline_import(tmp_path):
    target = tmp_path / "runner.py"
    target.write_text('"""doc\nfrom here import nothing\n"""\nimport os\n'
                      "from a import (\n    b,\n    c,\n)\n\nX = 1\n")
    pr.inject_after_imports(str(target), "INJECTED = True", requires="os")
    text = target.read_text()
    compile(text, str(target), "exec")
    assert text.index("INJECTED = True") > text.index("    c,\n)")
    assert text.index("INJECTED = True") < text.index("X = 1")


def test_inject_accepts_a_submodule_import(tmp_path):
    target = tmp_path / "runner.py"
    target.write_text("import multiprocessing.queues\n")
    pr.inject_after_imports(str(target), "multiprocessing.x()", requires="multiprocessing")
    assert target.read_text().count("multiprocessing.x()") == 1


def test_existing_injection_in_a_file_that_does_not_compile_fails(tmp_path):
    target = tmp_path / "runner.py"
    target.write_text("import multiprocessing\nmultiprocessing.x()\nreturn\n")
    with pytest.raises(pr.PatchError):
        pr.inject_after_imports(str(target), "multiprocessing.x()", requires="multiprocessing")


def test_inject_is_idempotent(tmp_path):
    target = tmp_path / "runner.py"
    target.write_text("import os\nINJECTED = True\n")
    pr.inject_after_imports(str(target), "INJECTED = True", requires="os")
    assert target.read_text() == "import os\nINJECTED = True\n"


@pytest.mark.parametrize("content", [
    None,                                          # target file gone
    "X = 1\n",                                     # no imports at all
    "import os\nimport multiprocessing as mp\n",   # required name not bound
    "import os as multiprocessing\n",              # name bound to another module
    "import multiprocessing.queues as multiprocessing\n",  # bound to a submodule
    "import multiprocessing\nimport os as multiprocessing\n",  # rebound later
    "multiprocessing.x()\n",                       # already injected, never imported
    "try:\n    import multiprocessing\nexcept ImportError:\n    pass\n",
    "def broken(:\n",                              # target does not parse
])
def test_inject_fails_unless_the_required_module_is_imported(tmp_path, content):
    target = tmp_path / "runner.py"
    if content is not None:
        target.write_text(content)
    with pytest.raises(pr.PatchError):
        pr.inject_after_imports(str(target), "multiprocessing.x()", requires="multiprocessing")


def test_injection_mentioned_only_in_a_comment_is_still_applied(tmp_path):
    target = tmp_path / "runner.py"
    target.write_text("import multiprocessing\n# multiprocessing.x()\n")
    pr.inject_after_imports(str(target), "multiprocessing.x()", requires="multiprocessing")
    assert "\nmultiprocessing.x()\n" in target.read_text()


# The matched lines of each target in the pinned rally / rally-openstack
# release, at their pinned match counts.
FIXTURES = {
    "rally/task/runner.py": "import multiprocessing\nimport time\n",
    "rally_openstack/common/services/identity/keystone_v3.py":
        "f(name=username, password=password,\n  domain=domain_id, enabled=enabled)\n"
        "g(name=username, password=password, x=1)\n",
    "rally_openstack/common/services/identity/keystone_v2.py":
        "password = password or str(uuid.uuid4())\n",
    "rally_openstack/task/contexts/keystone/users.py":
        "password = (str(uuid.uuid4())\n            + 'x')\n",
    "rally_openstack/task/scenarios/keystone/basic.py":
        "password = self.generate_random_name()\n"
        + "self.admin_keystone.create_user(name, **kwargs)\n" * 4,
}


def _site_packages(tmp_path, monkeypatch, fixtures):
    for rel, text in fixtures.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    monkeypatch.setattr(pr, "SITE_PACKAGES", str(tmp_path))


def test_main_applies_every_patch(tmp_path, monkeypatch):
    _site_packages(tmp_path, monkeypatch, FIXTURES)
    pr.main()
    read = lambda rel: (tmp_path / rel).read_text()  # noqa: E731
    assert "set_start_method('fork', force=True)" in read("rally/task/runner.py")
    v3 = read("rally_openstack/common/services/identity/keystone_v3.py")
    assert v3.count("password=(password or") == 2
    assert '"ignore_change_password_upon_first_use": True, "ignore_password_expiry": True' in v3
    assert "secrets" in read("rally_openstack/common/services/identity/keystone_v2.py")
    assert "secrets" in read("rally_openstack/task/contexts/keystone/users.py")
    basic = read("rally_openstack/task/scenarios/keystone/basic.py")
    assert basic.count('password=kwargs.pop("password"') == 4
    assert 'password = "".join(' in basic


def test_main_fails_when_a_call_site_moved(tmp_path, monkeypatch):
    fixtures = dict(FIXTURES)
    rel = "rally_openstack/task/scenarios/keystone/basic.py"
    fixtures[rel] = fixtures[rel].replace("create_user(name, **kwargs)", "create(name)", 1)
    _site_packages(tmp_path, monkeypatch, fixtures)
    with pytest.raises(pr.PatchError):
        pr.main()


def test_only_main_runs_patches():
    """No module-level statement calls anything outside the __main__ guard, so
    importing the module (as these tests do) never touches site-packages."""
    tree = ast.parse(Path(pr.__file__).read_text())
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'":
            continue
        called = {c.func.id for c in ast.walk(node)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        assert not called & {"main", "patch_file", "inject_after_imports"}, ast.unparse(node)
