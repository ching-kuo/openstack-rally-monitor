"""Tests for scripts/keystone_projects.py: a truncated Keystone project list
must raise, never pass as a complete view (live projects would look gone)."""
from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import keystone_projects as kp  # noqa: E402


def conn_returning(body, status=200):
    resp = SimpleNamespace(status_code=status, json=lambda: body)
    return SimpleNamespace(identity=SimpleNamespace(get=lambda path: resp))


def test_lists_ids_and_names():
    body = {"projects": [{"id": "a", "name": "admin"}, {"id": "b", "name": None}],
            "links": {"next": None}}
    assert kp.list_projects(conn_returning(body)) == [("a", "admin"), ("b", "")]


@pytest.mark.parametrize("body", [
    {"projects": [{"id": "a", "name": "x"}], "truncated": True},
    {"projects": [{"id": "a", "name": "x"}], "links": {"next": "https://ks/v3/projects?marker=a"}},
])
def test_truncated_listing_raises(body):
    with pytest.raises(kp.TruncatedListing):
        kp.list_projects(conn_returning(body))


@pytest.mark.parametrize("body,status", [({"error": {}}, 403), ({"nope": 1}, 200), ([], 200)])
def test_bad_responses_raise(body, status):
    with pytest.raises(RuntimeError):
        kp.list_projects(conn_returning(body, status))


def test_main_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(kp, "build_connection",
                        lambda: conn_returning({"projects": [{"id": "a"}], "truncated": True}))
    assert kp.main([]) == 3
    monkeypatch.setattr(kp, "build_connection",
                        lambda: conn_returning({"projects": [{"id": "a"}, {"id": "b"}]}))
    assert kp.main([]) == 0
    assert capsys.readouterr().out.split() == ["a", "b"]

    def boom():
        raise ImportError("no openstacksdk")
    monkeypatch.setattr(kp, "build_connection", boom)
    assert kp.main([]) == 1


@pytest.mark.parametrize("record", [
    "not-a-dict",
    {"name": "no-id"},
    {"id": None, "name": "x"},
    {"id": 42, "name": "x"},
    {"id": "", "name": "x"},
    {"id": "ok", "name": 7},
])
def test_any_malformed_record_rejects_the_whole_listing(record):
    """Dropping a bad record would make a live project look deleted (its RGW
    user then reads as orphaned): the whole listing must fail instead."""
    body = {"projects": [{"id": "good", "name": "admin"}, record]}
    with pytest.raises(RuntimeError, match="malformed"):
        kp.list_projects(conn_returning(body))


def test_unusual_but_valid_ids_do_not_fail_the_listing(monkeypatch, capsys):
    """Only structural problems fail the listing; an ID outside the printable
    charset (a superset of the RGW implicit-uid charset) is listed but not
    printed -- it can never match an RGW user."""
    body = {"projects": [{"id": "a_b", "name": "x"}, {"id": "has space", "name": "y"},
                         {"id": "abc-123", "name": "z"}]}
    assert len(kp.list_projects(conn_returning(body))) == 3
    monkeypatch.setattr(kp, "build_connection", lambda: conn_returning(body))
    assert kp.main([]) == 0
    assert capsys.readouterr().out.split() == ["a_b", "abc-123"]


def test_name_prefix_uses_the_server_side_filter():
    seen = []
    resp = SimpleNamespace(status_code=200, json=lambda: {"projects": []})
    conn = SimpleNamespace(identity=SimpleNamespace(get=lambda p: seen.append(p) or resp))
    kp.list_projects(conn, "c_rally_")
    assert seen == ["/projects?name__startswith=c_rally_"]


class FakeCheckConn:
    def __init__(self, statuses, current_project_id="monitor-proj"):
        self.statuses = statuses
        self._current_project_id = current_project_id
        self.checked = []
        self.identity = SimpleNamespace(get=self._get)

    @property
    def current_project_id(self):
        if isinstance(self._current_project_id, Exception):
            raise self._current_project_id
        return self._current_project_id

    def _get(self, path):
        project_id = path.rsplit("/", 1)[1]
        self.checked.append(project_id)
        status = self.statuses[project_id]
        if isinstance(status, Exception):
            raise status
        return SimpleNamespace(status_code=status, json=lambda: {})


def test_check_mode_reports_each_id_over_one_session(monkeypatch, capsys):
    not_found = RuntimeError("404")
    not_found.http_status = 404
    conn = FakeCheckConn({
        "monitor-proj": 200, "live": 200, "dead": 404, "boom": 500,
        "raised": not_found, "down": RuntimeError("timeout"),
    })
    built = []
    monkeypatch.setattr(kp, "build_connection", lambda: built.append(1) or conn)
    stdin = io.StringIO("live\ndead\nboom\nraised\ndown\nbad/id\n")
    assert kp.main(["--check"], stdin=stdin) == 0
    assert capsys.readouterr().out.splitlines() == [
        # A raised 404 is not the project GET (the proxy returns that as a
        # response); it is e.g. a token re-auth POST 404, so never "gone".
        "live exists", "dead gone", "boom error", "raised error", "down error", "bad/id error",
    ]
    assert built == [1]


def test_lookup_exception_text_is_never_logged(monkeypatch, capsys):
    # keystoneauth copies the server's error message into the exception, and a
    # gateway may echo a credential there; log class and status only.
    leaky = RuntimeError("echoed password=hunter2")
    leaky.http_status = 401
    conn = FakeCheckConn({"monitor-proj": 200, "x": leaky})
    monkeypatch.setattr(kp, "build_connection", lambda: conn)
    assert kp.main(["--check"], stdin=io.StringIO("x\n")) == 0
    captured = capsys.readouterr()
    assert captured.out == "x error\n"
    assert "hunter2" not in captured.err
    assert "RuntimeError" in captured.err and "401" in captured.err


def test_check_mode_aborts_when_every_get_returns_404(monkeypatch, capsys):
    conn = FakeCheckConn({"monitor-proj": 404, "a": 404, "b": 404})
    monkeypatch.setattr(kp, "build_connection", lambda: conn)
    assert kp.main(["--check"], stdin=io.StringIO("a\nb\n")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip()
    assert conn.checked == ["monitor-proj"]


def test_check_mode_reports_gone_when_own_project_exists(monkeypatch, capsys):
    conn = FakeCheckConn({"monitor-proj": 200, "a": 404, "b": 404})
    monkeypatch.setattr(kp, "build_connection", lambda: conn)
    assert kp.main(["--check"], stdin=io.StringIO("a\nb\n")) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["a gone", "b gone"]
    assert captured.err == ""
    assert conn.checked == ["monitor-proj", "a", "b"]


def test_check_mode_aborts_when_current_project_id_raises(monkeypatch, capsys):
    auth_404 = RuntimeError("NotFound")
    auth_404.http_status = 404
    conn = FakeCheckConn({"a": 404}, current_project_id=auth_404)
    monkeypatch.setattr(kp, "build_connection", lambda: conn)
    assert kp.main(["--check"], stdin=io.StringIO("a\n")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "NotFound" in captured.err
    assert conn.checked == []


def test_check_mode_aborts_when_current_project_id_is_none(monkeypatch, capsys):
    conn = FakeCheckConn({"a": 404}, current_project_id=None)
    monkeypatch.setattr(kp, "build_connection", lambda: conn)
    assert kp.main(["--check"], stdin=io.StringIO("a\n")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip()
    assert conn.checked == []


@pytest.mark.parametrize("own_status", [500, RuntimeError("timeout")])
def test_check_mode_aborts_when_own_project_errors(monkeypatch, capsys, own_status):
    conn = FakeCheckConn({"monitor-proj": own_status, "a": 404})
    monkeypatch.setattr(kp, "build_connection", lambda: conn)
    assert kp.main(["--check"], stdin=io.StringIO("a\n")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip()
    assert conn.checked == ["monitor-proj"]


def test_newline_suffixed_id_is_never_printed_or_checked(monkeypatch, capsys):
    """re.match with `$` accepts "victim\\n"; printed, it becomes two lines
    and can vouch for a different project. fullmatch only."""
    body = {"projects": [{"id": "victim\nother", "name": "x"}, {"id": "ok\n", "name": "y"},
                         {"id": "fine", "name": "z"}]}
    monkeypatch.setattr(kp, "build_connection", lambda: conn_returning(body))
    assert kp.main([]) == 0
    assert capsys.readouterr().out == "fine\n"
    assert kp.project_status(conn_returning({}), "abc\n") == "error"
