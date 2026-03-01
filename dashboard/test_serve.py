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
    def test_dotdot_in_path_is_blocked(self, server):
        base, dashboard_dir, _ = server
        secret = dashboard_dir.parent / "secret.txt"
        secret.write_text("TOP SECRET")
        code, _, _ = get(base, "/../secret.txt")
        assert code == 403

    def test_multiple_dotdot_segments_blocked(self, server):
        base, _, _ = server
        code, _, _ = get(base, "/../../etc/passwd")
        assert code == 403

    def test_url_encoded_dotdot_blocked(self, server):
        base, _, _ = server
        code, _, _ = get(base, "/%2e%2e/etc/passwd")
        assert code == 403


# ---------------------------------------------------------------------------
# Allowlist enforcement
# ---------------------------------------------------------------------------

class TestAllowlist:
    def test_index_html_served(self, server):
        base, dashboard_dir, _ = server
        (dashboard_dir / "index.html").write_text("<html><body>ok</body></html>")
        code, _, body = get(base, "/index.html")
        assert code == 200
        assert b"ok" in body

    def test_root_serves_index(self, server):
        base, dashboard_dir, _ = server
        (dashboard_dir / "index.html").write_text("<html></html>")
        code, _, _ = get(base, "/")
        assert code == 200

    def test_app_js_served(self, server):
        base, dashboard_dir, _ = server
        (dashboard_dir / "app.js").write_text("// ok")
        code, _, _ = get(base, "/app.js")
        assert code == 200

    def test_style_css_served(self, server):
        base, dashboard_dir, _ = server
        (dashboard_dir / "style.css").write_text("body {}")
        code, _, _ = get(base, "/style.css")
        assert code == 200

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

    def test_json_symlink_served(self, server):
        base, dashboard_dir, results_dir = server
        (results_dir / "results.json").write_text('{"ok": true}')
        (dashboard_dir / "results.json").symlink_to(results_dir / "results.json")
        code, _, body = get(base, "/results.json")
        assert code == 200
        assert b"ok" in body

    def test_all_json_symlinks_served(self, server):
        base, dashboard_dir, results_dir = server
        for name in ("results.json", "history.json", "health.json", "health_history.json"):
            (results_dir / name).write_text("{}")
            (dashboard_dir / name).symlink_to(results_dir / name)
            code, _, _ = get(base, f"/{name}")
            assert code == 200, f"{name} should be served"

    def test_symlink_pointing_outside_results_is_blocked(self, server):
        """Symlink to a file outside both SERVE_ROOT and RESULTS_ROOT is denied."""
        base, dashboard_dir, results_dir = server
        outside = dashboard_dir.parent / "outside.json"
        outside.write_text('{"secret": true}')
        (dashboard_dir / "results.json").symlink_to(outside)
        code, _, _ = get(base, "/results.json")
        assert code == 403

    def test_symlink_to_sensitive_file_blocked(self, server):
        """Symlink to /etc/passwd (or equivalent) must be blocked."""
        base, dashboard_dir, _ = server
        (dashboard_dir / "results.json").symlink_to("/etc/hostname")
        code, _, _ = get(base, "/results.json")
        assert code == 403


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

class TestSecurityHeaders:
    def test_x_frame_options_present(self, server):
        base, dashboard_dir, _ = server
        (dashboard_dir / "index.html").write_text("<html></html>")
        _, headers, _ = get(base, "/")
        assert "x-frame-options" in headers
        assert headers["x-frame-options"] == "DENY"

    def test_x_content_type_options_present(self, server):
        base, dashboard_dir, _ = server
        (dashboard_dir / "index.html").write_text("<html></html>")
        _, headers, _ = get(base, "/")
        assert "x-content-type-options" in headers
        assert headers["x-content-type-options"] == "nosniff"

    def test_content_security_policy_present(self, server):
        base, dashboard_dir, _ = server
        (dashboard_dir / "index.html").write_text("<html></html>")
        _, headers, _ = get(base, "/")
        assert "content-security-policy" in headers


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

    def test_html_has_max_age(self, server):
        base, dashboard_dir, _ = server
        (dashboard_dir / "index.html").write_text("<html></html>")
        _, headers, _ = get(base, "/")
        assert "max-age=300" in headers.get("cache-control", "")

    def test_js_has_max_age(self, server):
        base, dashboard_dir, _ = server
        (dashboard_dir / "app.js").write_text("// ok")
        _, headers, _ = get(base, "/app.js")
        assert "max-age=300" in headers.get("cache-control", "")
