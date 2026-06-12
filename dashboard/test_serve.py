"""Unit tests for dashboard/serve.py."""
import sys
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path, PurePosixPath

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
    monkeypatch.setattr(serve, "BRANDING_ROOT", (results_dir / "branding").resolve())

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
            "announcement-state.json": '{"announcements": []}',
            "run_state.json": '{"state": "idle"}',
        }
        for name, payload in payloads.items():
            (results_dir / name).write_text(payload)
            (dashboard_dir / name).symlink_to(results_dir / name)
            code, _, body = get(base, f"/{name}")
            assert code == 200, f"{name} should be served"
            if name == "results.json":
                assert b"ok" in body

    def test_run_state_dangling_symlink_returns_404(self, server):
        """Like announcement-state.json, the run_state.json symlink is created
        unconditionally at boot but its target is seeded by entrypoint.sh and
        rewritten by run_tests.sh; a dangling symlink must 404, not 5xx."""
        base, dashboard_dir, results_dir = server
        (dashboard_dir / "run_state.json").symlink_to(
            results_dir / "run_state.json"
        )
        code, _, _ = get(base, "/run_state.json")
        assert code == 404

    def test_run_state_has_no_store_cache_control(self, server):
        base, dashboard_dir, results_dir = server
        (results_dir / "run_state.json").write_text('{"state": "running"}')
        (dashboard_dir / "run_state.json").symlink_to(
            results_dir / "run_state.json"
        )
        _, headers, _ = get(base, "/run_state.json")
        assert "no-store" in headers.get("cache-control", "")
        assert headers["content-type"].startswith("application/json")

    def test_announcement_state_dangling_symlink_returns_404(self, server):
        """The symlink is created unconditionally at container start; the
        target file appears only after the first post. A dangling symlink
        must 404, not 5xx."""
        base, dashboard_dir, results_dir = server
        (dashboard_dir / "announcement-state.json").symlink_to(
            results_dir / "announcement-state.json"
        )
        code, _, _ = get(base, "/announcement-state.json")
        assert code == 404

    def test_announcement_state_has_no_store_cache_control(self, server):
        base, dashboard_dir, results_dir = server
        (results_dir / "announcement-state.json").write_text('{"announcements": []}')
        (dashboard_dir / "announcement-state.json").symlink_to(
            results_dir / "announcement-state.json"
        )
        _, headers, _ = get(base, "/announcement-state.json")
        assert "no-store" in headers.get("cache-control", "")
        assert headers["content-type"].startswith("application/json")

    def test_arbitrary_results_json_not_widened_by_allowlist(self, server):
        """Adding announcement-state.json must not widen the allowlist to
        every *.json in /results/."""
        base, dashboard_dir, results_dir = server
        (results_dir / "secrets.json").write_text('{"token": "leak"}')
        (dashboard_dir / "secrets.json").symlink_to(results_dir / "secrets.json")
        code, _, _ = get(base, "/secrets.json")
        assert code == 403

    def test_symlink_pointing_outside_results_is_blocked(self, server):
        """Symlink to a file outside both SERVE_ROOT and RESULTS_ROOT is denied."""
        base, dashboard_dir, results_dir = server
        outside = dashboard_dir.parent / "outside.json"
        outside.write_text('{"secret": true}')
        (dashboard_dir / "results.json").symlink_to(outside)
        code, _, _ = get(base, "/results.json")
        assert code == 403


# ---------------------------------------------------------------------------
# Theme asset allowlist
# ---------------------------------------------------------------------------

class TestThemeAllowlist:
    def test_default_tokens_served_with_css_mime_type(self, server):
        base, dashboard_dir, _ = server
        theme_dir = dashboard_dir / "themes" / "default"
        theme_dir.mkdir(parents=True)
        (theme_dir / "tokens.css").write_text(":root { --color-brand-primary: #6366f1; }")

        code, headers, body = get(base, "/themes/default/tokens.css")

        assert code == 200
        assert headers["content-type"].startswith("text/css")
        assert b"--color-brand-primary" in body

    def test_default_logo_served_with_svg_mime_type(self, server):
        base, dashboard_dir, _ = server
        theme_dir = dashboard_dir / "themes" / "default"
        theme_dir.mkdir(parents=True)
        (theme_dir / "logo.svg").write_text("<svg></svg>")

        code, headers, body = get(base, "/themes/default/logo.svg")

        assert code == 200
        assert headers["content-type"].startswith("image/svg+xml")
        assert b"<svg" in body

    def test_custom_tokens_served_from_branding_symlink(self, server):
        base, dashboard_dir, results_dir = server
        branding_dir = results_dir / "branding"
        branding_dir.mkdir()
        (branding_dir / "tokens.css").write_text(":root { --chart-series-1: #ff00ff; }")
        themes_dir = dashboard_dir / "themes"
        themes_dir.mkdir()
        (themes_dir / "custom").symlink_to(branding_dir)

        code, _, body = get(base, "/themes/custom/tokens.css")

        assert code == 200
        assert b"--chart-series-1" in body

    def test_missing_custom_theme_file_returns_404(self, server):
        base, dashboard_dir, results_dir = server
        branding_dir = results_dir / "branding"
        branding_dir.mkdir()
        themes_dir = dashboard_dir / "themes"
        themes_dir.mkdir()
        (themes_dir / "custom").symlink_to(branding_dir)

        code, _, _ = get(base, "/themes/custom/tokens.css")

        assert code == 404

    @pytest.mark.parametrize(
        "path",
        [
            "/themes/default/secret.txt",
            "/themes/default/manifest.webmanifest",
        ],
    )
    def test_disallowed_theme_extensions_are_blocked(self, server, path):
        base, dashboard_dir, _ = server
        target = dashboard_dir.joinpath(*PurePosixPath(path.lstrip("/")).parts)
        target.parent.mkdir(parents=True)
        target.write_text("secret")

        code, _, _ = get(base, path)

        assert code == 403

    def test_theme_traversal_is_blocked(self, server):
        base, _, _ = server
        code, _, _ = get(base, "/themes/../etc/passwd")
        assert code == 403

    def test_custom_theme_symlink_outside_branding_is_blocked(self, server):
        base, dashboard_dir, results_dir = server
        (results_dir / "tokens.css").write_text(":root {}")
        themes_dir = dashboard_dir / "themes" / "custom"
        themes_dir.mkdir(parents=True)
        (themes_dir / "tokens.css").symlink_to(results_dir / "tokens.css")

        code, _, _ = get(base, "/themes/custom/tokens.css")

        assert code == 403

    def test_custom_theme_symlink_outside_results_is_blocked(self, server):
        base, dashboard_dir, _ = server
        outside = dashboard_dir.parent / "outside.css"
        outside.write_text(":root {}")
        themes_dir = dashboard_dir / "themes" / "custom"
        themes_dir.mkdir(parents=True)
        (themes_dir / "tokens.css").symlink_to(outside)

        code, _, _ = get(base, "/themes/custom/tokens.css")

        assert code == 403

    def test_custom_theme_symlink_inside_branding_uses_requested_suffix(self, server):
        base, dashboard_dir, results_dir = server
        branding_dir = results_dir / "branding"
        branding_dir.mkdir()
        (branding_dir / "actual.txt").write_text(":root {}")
        themes_dir = dashboard_dir / "themes"
        themes_dir.mkdir()
        (themes_dir / "custom").symlink_to(branding_dir)
        (branding_dir / "foo.css").symlink_to(branding_dir / "actual.txt")

        code, headers, body = get(base, "/themes/custom/foo.css")

        assert code == 200
        # MIME type must follow the requested suffix, not the symlink target
        # (otherwise the browser, given X-Content-Type-Options: nosniff, drops
        # the stylesheet because it arrives as text/plain).
        assert headers["content-type"].startswith("text/css")
        assert b":root" in body


# ---------------------------------------------------------------------------
# Served Rally HTML reports (runs/<timestamp>/<service>.html)
# ---------------------------------------------------------------------------

class TestRunReports:
    """The fourth allowlist branch serves generated Rally reports under
    /results/<TIMESTAMP>/<service>.html via the /dashboard/runs -> /results
    symlink, gated by RUN_REPORT_RE + RESULTS_ROOT containment, with a relaxed
    per-path CSP. These tests mirror the rigor of the theme-allowlist suite."""

    VALID_TS = "20260612T120000Z"

    def _make_report(self, results_dir, dashboard_dir, ts, name, content="<html>report</html>"):
        """Create /results/<ts>/<name> and the runs -> results symlink."""
        run_dir = results_dir / ts
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / name).write_text(content)
        runs_link = dashboard_dir / "runs"
        if not runs_link.exists():
            runs_link.symlink_to(results_dir)

    def test_valid_report_is_served(self, server):
        base, dashboard_dir, results_dir = server
        self._make_report(results_dir, dashboard_dir, self.VALID_TS, "nova.html")
        code, headers, body = get(base, f"/runs/{self.VALID_TS}/nova.html")
        assert code == 200
        assert b"report" in body
        assert headers["content-type"].startswith("text/html")

    def test_report_uses_relaxed_per_path_csp(self, server):
        """Report responses get a dedicated CSP that permits the inline scripts
        and the two CDN origins the default Rally template needs."""
        base, dashboard_dir, results_dir = server
        self._make_report(results_dir, dashboard_dir, self.VALID_TS, "nova.html")
        _, headers, _ = get(base, f"/runs/{self.VALID_TS}/nova.html")
        csp = _parse_csp(headers["content-security-policy"])
        # Relaxed exactly where the generated report needs it...
        assert "'unsafe-inline'" in csp.get("script-src", [])
        assert "https://ajax.googleapis.com" in csp.get("script-src", [])
        assert "https://cdnjs.cloudflare.com" in csp.get("script-src", [])
        assert "https://cdnjs.cloudflare.com" in csp.get("style-src", [])
        # ...and sandboxed as defense-in-depth, while framing stays denied.
        assert "sandbox" in csp
        assert headers["x-frame-options"] == "DENY"

    def test_strict_csp_still_applies_to_dashboard_assets(self, server):
        """The relaxed CSP must be scoped to the runs/ branch only: a normal
        asset on the same server keeps script-src 'self' with no inline/CDN."""
        base, dashboard_dir, results_dir = server
        (dashboard_dir / "index.html").write_text("<html></html>")
        self._make_report(results_dir, dashboard_dir, self.VALID_TS, "nova.html")

        _, report_headers, _ = get(base, f"/runs/{self.VALID_TS}/nova.html")
        _, index_headers, _ = get(base, "/")

        report_csp = _parse_csp(report_headers["content-security-policy"])
        index_csp = _parse_csp(index_headers["content-security-policy"])

        assert report_csp != index_csp
        assert index_csp.get("script-src") == ["'self'"]
        assert "'unsafe-inline'" not in index_csp.get("script-src", [])
        assert "https://ajax.googleapis.com" not in index_csp.get("script-src", [])
        assert "sandbox" not in index_csp

    def test_report_csp_does_not_leak_to_next_request(self, server):
        """A report response (relaxed CSP) followed by a dashboard-asset request
        on the same client connection must NOT carry the relaxed CSP. serve.py
        runs HTTP/1.0 (connection closes per request, fresh handler each time)
        AND do_GET resets _security_headers to strict at entry, so the relaxed
        set can never bleed into a subsequent response. Reuse one HTTPConnection
        to exercise the sequence regardless of how connections are pooled."""
        import http.client
        from urllib.parse import urlparse

        base, dashboard_dir, results_dir = server
        (dashboard_dir / "index.html").write_text("<html></html>")
        self._make_report(results_dir, dashboard_dir, self.VALID_TS, "nova.html")

        netloc = urlparse(base).netloc
        host, port = netloc.split(":")
        conn = http.client.HTTPConnection(host, int(port))
        try:
            conn.request("GET", f"/runs/{self.VALID_TS}/nova.html")
            r1 = conn.getresponse()
            r1.read()
            assert "'unsafe-inline'" in r1.getheader("content-security-policy")

            # Second request after the report must be strict again.
            conn.request("GET", "/index.html")
            r2 = conn.getresponse()
            r2.read()
            csp2 = _parse_csp(r2.getheader("content-security-policy"))
            assert csp2.get("script-src") == ["'self'"]
            assert "'unsafe-inline'" not in csp2.get("script-src", [])
        finally:
            conn.close()

    @pytest.mark.parametrize(
        "path",
        [
            "/runs/../secret.html",                 # traversal out of runs/
            "/runs/evil/nova.html",                 # timestamp segment not a rally ts
            "/runs/20260612T120000Z/nova.json",     # wrong suffix
            "/runs/20260612T120000Z/no..dots.html", # name chars outside [a-z0-9_-]
            "/runs/20260612T120000Z/Nova.html",     # uppercase not in name class
            "/runs/20260612T1200Z/nova.html",       # malformed timestamp (too short)
            "/runs/20260612T120000Z/sub/nova.html", # extra path segment
            "/runs/nova.html",                      # missing timestamp segment
        ],
    )
    def test_disallowed_report_paths_are_blocked(self, server, path):
        base, dashboard_dir, results_dir = server
        # Materialize a real file at the traversal target / each bad path so the
        # 403 is the allowlist's doing, not a 404. Build it under whichever root
        # the (pre-normalization) path would resolve to.
        runs_link = dashboard_dir / "runs"
        if not runs_link.exists():
            runs_link.symlink_to(results_dir)
        # The out-of-runs traversal target:
        (dashboard_dir / "secret.html").write_text("<html>secret</html>")
        # A real file at the literal good location (so the suffix/name cases are
        # not just 404s on a missing file):
        good_dir = results_dir / "20260612T120000Z"
        good_dir.mkdir(parents=True, exist_ok=True)
        (good_dir / "nova.json").write_text("{}")
        (good_dir / "nova.html").write_text("<html>ok</html>")
        (good_dir / "no..dots.html").write_text("<html>ok</html>")
        (good_dir / "Nova.html").write_text("<html>ok</html>")
        (good_dir / "sub").mkdir(exist_ok=True)
        (good_dir / "sub" / "nova.html").write_text("<html>ok</html>")
        # A real file under a NON-timestamp dir, so the bad-timestamp case is a
        # hard allowlist 403 rather than a 404 on a missing file.
        evil_dir = results_dir / "evil"
        evil_dir.mkdir(parents=True, exist_ok=True)
        (evil_dir / "nova.html").write_text("<html>evil</html>")

        code, _, _ = get(base, path)
        # Either is acceptable (both deny the file); but the cases where a real
        # file exists at the requested location must be a hard allowlist 403,
        # proving the regex/containment — not an incidental 404.
        assert code in (403, 404), f"{path} must not be served (got {code})"
        hard_deny = {
            "/runs/../secret.html",                 # blocked pre-resolve (.. guard)
            "/runs/evil/nova.html",                 # real file, bad timestamp dir
            "/runs/20260612T120000Z/nova.json",     # real file, wrong suffix
            "/runs/20260612T120000Z/no..dots.html", # real file, bad name chars
            "/runs/20260612T120000Z/Nova.html",     # real file, uppercase name
            "/runs/20260612T120000Z/sub/nova.html", # real file, extra segment
        }
        if path in hard_deny:
            assert code == 403, f"{path} should be a hard 403 (got {code})"

    def test_report_symlink_escaping_results_root_is_blocked(self, server):
        """A run directory replaced by a symlink that escapes RESULTS_ROOT must
        be denied even though the path matches RUN_REPORT_RE, because the
        resolved target fails the is_relative_to(RESULTS_ROOT) containment
        check (mirrors test_symlink_pointing_outside_results_is_blocked)."""
        base, dashboard_dir, results_dir = server
        outside = dashboard_dir.parent / "outside_report.html"
        outside.write_text("<html>escaped</html>")
        runs_link = dashboard_dir / "runs"
        runs_link.symlink_to(results_dir)
        # results/<ts> is a directory containing a symlink whose target is
        # outside RESULTS_ROOT.
        run_dir = results_dir / self.VALID_TS
        run_dir.mkdir(parents=True)
        (run_dir / "nova.html").symlink_to(outside)

        code, _, _ = get(base, f"/runs/{self.VALID_TS}/nova.html")
        assert code == 403


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

def _parse_csp(value):
    """Parse a Content-Security-Policy header into {directive: [tokens]}."""
    directives = {}
    for part in value.split(";"):
        tokens = part.split()
        if tokens:
            directives[tokens[0]] = tokens[1:]
    return directives


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

    def test_csp_script_src_has_no_unsafe_inline(self, server):
        """Regression: script-src must not permit inline scripts. index.html
        ships zero inline <script> blocks and zero inline event handlers, so
        'unsafe-inline' was dropped from script-src. style-src must KEEP it
        because app.js writes inline style="" attributes via innerHTML."""
        base, dashboard_dir, _ = server
        (dashboard_dir / "index.html").write_text("<html></html>")
        _, headers, _ = get(base, "/")
        csp = _parse_csp(headers["content-security-policy"])

        assert csp.get("script-src") == ["'self'"]
        assert "'unsafe-inline'" not in csp.get("script-src", [])
        # style-src intentionally retains 'unsafe-inline'.
        assert "'unsafe-inline'" in csp.get("style-src", [])
        assert "'self'" in csp.get("style-src", [])
        # The fallback directive also stays locked to same-origin.
        assert csp.get("default-src") == ["'self'"]


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
