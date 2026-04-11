"""Unit tests for dashboard/serve.py."""
import sys
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

import pytest

# Add dashboard directory to sys.path for import
sys.path.insert(0, str(Path(__file__).parent))
import serve


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def dirs(tmp_path):
    """Set up temporary dashboard and results directories."""
    dashboard_dir = tmp_path / "dashboard"
    results_dir = tmp_path / "results"
    dashboard_dir.mkdir()
    results_dir.mkdir()
    return dashboard_dir, results_dir


@pytest.fixture
def server(dirs, monkeypatch):
    """Start a test HTTP server on an ephemeral port with patched roots."""
    dashboard_dir, results_dir = dirs
    monkeypatch.setattr(serve, "SERVE_ROOT", dashboard_dir)
    monkeypatch.setattr(serve, "RESULTS_ROOT", results_dir)

    srv = HTTPServer(("127.0.0.1", 0), serve.SecureStaticHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever)
    t.daemon = True
    t.start()

    base = f"http://127.0.0.1:{port}"
    yield base, dashboard_dir, results_dir

    srv.shutdown()


def get(base, path):
    """Make a GET request, return (status, headers_dict, body_bytes)."""
    try:
        with urllib.request.urlopen(f"{base}{path}") as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


# ---------------------------------------------------------------------------
# Path traversal
# ---------------------------------------------------------------------------

class TestPathTraversal:
    @pytest.mark.parametrize("path", ["/../secret.txt", "/../../etc/passwd", "/%2e%2e/etc/passwd"])
    def test_dotdot_paths_are_blocked(self, server, path):
        base, dashboard_dir, _ = server
        secret = dashboard_dir.parent / "secret.txt"
        secret.write_text("TOP SECRET")
        code, _, _ = get(base, path)
        assert code == 403


# ---------------------------------------------------------------------------
# Allowlist enforcement
# ---------------------------------------------------------------------------

class TestAllowlist:
    @pytest.mark.parametrize(
        ("request_path", "filename", "content", "expected_fragment"),
        [
            ("/index.html", "index.html", "<html><body>ok</body></html>", b"ok"),
            ("/", "index.html", "<html></html>", None),
            ("/app.js", "app.js", "// ok", None),
            ("/style.css", "style.css", "body {}", None),
        ],
    )
    def test_allowed_static_files_are_served(
        self,
        server,
        request_path,
        filename,
        content,
        expected_fragment,
    ):
        base, dashboard_dir, _ = server
        (dashboard_dir / filename).write_text(content)
        code, _, body = get(base, request_path)
        assert code == 200
        if expected_fragment is not None:
            assert expected_fragment in body

    def test_unknown_file_blocked(self, server):
        base, dashboard_dir, _ = server
        (dashboard_dir / "secrets.txt").write_text("password123")
        code, _, _ = get(base, "/secrets.txt")
        assert code == 403

    def test_serve_py_itself_blocked(self, server):
        """serve.py must not be served even when it exists in SERVE_ROOT."""
        base, dashboard_dir, _ = server
        (dashboard_dir / "serve.py").write_text("# server source")
        code, _, _ = get(base, "/serve.py")
        assert code == 403

    def test_nonexistent_file_returns_404(self, server):
        base, _, _ = server
        code, _, _ = get(base, "/nonexistent.html")
        assert code == 404

    def test_all_json_symlinks_served(self, server):
        base, dashboard_dir, results_dir = server
        payloads = {
            "results.json": '{"ok": true}',
            "history.json": "{}",
            "health.json": "{}",
            "health_history.json": "{}",
        }
        for name, payload in payloads.items():
            (results_dir / name).write_text(payload)
            (dashboard_dir / name).symlink_to(results_dir / name)
            code, _, body = get(base, f"/{name}")
            assert code == 200, f"{name} should be served"
            if name == "results.json":
                assert b"ok" in body

    def test_symlink_pointing_outside_results_is_blocked(self, server):
        """Symlink to a file outside both SERVE_ROOT and RESULTS_ROOT is denied."""
        base, dashboard_dir, results_dir = server
        outside = dashboard_dir.parent / "outside.json"
        outside.write_text('{"secret": true}')
        (dashboard_dir / "results.json").symlink_to(outside)
        code, _, _ = get(base, "/results.json")
        assert code == 403


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

class TestSecurityHeaders:
    @pytest.mark.parametrize(
        ("header", "expected_value"),
        [
            ("x-frame-options", "DENY"),
            ("x-content-type-options", "nosniff"),
            ("content-security-policy", None),
        ],
    )
    def test_security_headers_present(self, server, header, expected_value):
        base, dashboard_dir, _ = server
        (dashboard_dir / "index.html").write_text("<html></html>")
        _, headers, _ = get(base, "/")
        assert header in headers
        if expected_value is not None:
            assert headers[header] == expected_value


# ---------------------------------------------------------------------------
# Cache-Control headers
# ---------------------------------------------------------------------------

class TestCacheHeaders:
    def test_json_symlink_has_no_store(self, server):
        base, dashboard_dir, results_dir = server
        (results_dir / "results.json").write_text("{}")
        (dashboard_dir / "results.json").symlink_to(results_dir / "results.json")
        _, headers, _ = get(base, "/results.json")
        assert "no-store" in headers.get("cache-control", "")

    @pytest.mark.parametrize(
        ("request_path", "filename", "content"),
        [
            ("/", "index.html", "<html></html>"),
            ("/app.js", "app.js", "// ok"),
        ],
    )
    def test_static_assets_have_max_age(self, server, request_path, filename, content):
        base, dashboard_dir, _ = server
        (dashboard_dir / filename).write_text(content)
        _, headers, _ = get(base, request_path)
        assert "max-age=300" in headers.get("cache-control", "")
