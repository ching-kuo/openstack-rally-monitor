#!/usr/bin/env python3
"""
Minimal static file server with security headers.
Usage: python serve.py [port]
"""
import mimetypes
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

SERVE_ROOT = Path(os.getcwd()).resolve()

# Deny-by-default allowlists. Update ALLOWED_STATIC for new dashboard assets;
# theme assets are gated by THEME_PREFIX + ALLOWED_THEME_SUFFIXES + BRANDING_ROOT.
ALLOWED_STATIC = {
    "index.html",
    "app.js",
    "style.css",
    # Self-contained report renderer assets (loaded by runs/<ts>/<service>.html
    # via relative ../../ paths). Same-origin, so reports serve under the strict
    # CSP like every other asset.
    "report.css",
    "report.js",
    "vendor/chart.umd.min.js",
    "vendor/inter-variable.woff2",
}
ALLOWED_JSON_SYMLINKS = {
    "results.json",
    "history.json",
    "health.json",
    "health_history.json",
    "announcement-state.json",
    # Liveness of the current Rally run (state: running|idle). Written atomically
    # by run_tests.sh, symlinked into /dashboard by entrypoint.sh; dangling until
    # the first run, so target.exists() 404s it like announcement-state.json.
    "run_state.json",
}
THEME_PREFIX = "themes/"
ALLOWED_THEME_SUFFIXES = {".css", ".svg", ".png", ".ico"}
# Rally HTML reports: run_tests.sh writes ${RUN_DIR}/<service>.html (one per
# service) into each run directory under /results/<TIMESTAMP>/, and entrypoint.sh
# symlinks /dashboard/runs -> /results. This pattern admits ONLY that exact
# shape: runs/<rally timestamp>/<service>.html. The timestamp segment is the
# compact UTC rally form (YYYYMMDDTHHMMSSZ); the name is a lowercase service
# token. Anchored ^...$, no path separators inside the captures, so it cannot be
# coaxed into traversal or into matching anything but a report file. Containment
# in RESULTS_ROOT is re-checked on the resolved target below.
RUN_REPORT_RE = re.compile(r"^runs/[0-9]{8}T[0-9]{6}Z/[a-z0-9_-]+\.html$")
RESULTS_ROOT = Path(os.environ.get("RESULTS_DIR", "/results")).resolve()
BRANDING_ROOT = (RESULTS_ROOT / "branding").resolve()

# Strict, locked-down headers for EVERY response: dashboard assets, JSON, and
# the self-contained Rally reports under runs/. The reports are now rendered by
# scripts/render_report.py from same-origin assets (report.js, report.css,
# vendored Chart.js) with their data embedded in a non-executable
# <script type="application/json"> block, so they need no CDN origins, no
# inline executable script, and no sandbox carve-out -- the previous
# REPORT_SECURITY_HEADERS / relaxed-CSP branch was removed with the AngularJS
# report it existed to serve.
SECURITY_HEADERS = [
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-XSS-Protection", "1; mode=block"),
    (
        # script-src has no 'unsafe-inline': index.html and the reports ship zero
        # inline EXECUTABLE <script> blocks and zero inline event-handler
        # attributes. The reports' embedded data lives in a
        # <script type="application/json"> block, which browsers never execute
        # and CSP script-src does not govern. JS runs only from same-origin files
        # (app.js, report.js, vendor/). style-src KEEPS 'unsafe-inline' because
        # app.js writes inline style="" attributes via innerHTML; CSP has no
        # per-attribute hashing for those.
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'",
    ),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
]


class SecureStaticHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress default access log

    def end_headers(self):
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        super().end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        path = unquote(path)
        if path == "/":
            path = "/index.html"

        rel_path = path.lstrip("/")
        parts = PurePosixPath(rel_path).parts
        if any(part == ".." for part in parts):
            self._send_error(403)
            return

        # The unresolved path is always within SERVE_ROOT (.. already blocked above).
        # resolve() follows the intentional /dashboard/*.json → /results/*.json symlinks.
        unresolved = SERVE_ROOT.joinpath(*parts)
        try:
            target = unresolved.resolve()
        except (OSError, ValueError):
            self._send_error(400)
            return

        if not target.exists() or not target.is_file():
            self._send_error(404)
            return

        # Enforce path containment: deny everything not in the explicit allowlists.
        # This prevents serving arbitrary files via symlink or future directory additions.
        rel_str = str(unresolved.relative_to(SERVE_ROOT))
        if rel_str in ALLOWED_STATIC and target.is_relative_to(SERVE_ROOT):
            pass  # known static asset within dashboard dir
        elif rel_str in ALLOWED_JSON_SYMLINKS and target.is_relative_to(RESULTS_ROOT):
            pass  # known JSON symlink resolving into /results/
        elif (
            rel_str.startswith(THEME_PREFIX)
            and PurePosixPath(rel_str).suffix in ALLOWED_THEME_SUFFIXES
            and (
                target.is_relative_to((SERVE_ROOT / "themes").resolve())
                or target.is_relative_to(BRANDING_ROOT)
            )
        ):
            pass  # theme assets from the shipped theme or operator branding dir
        elif (
            RUN_REPORT_RE.fullmatch(rel_str)
            and unresolved.suffix.lower() == ".html"
            and target.is_relative_to(RESULTS_ROOT)
        ):
            # Self-contained themed Rally report under
            # /results/<TIMESTAMP>/<service>.html (via the /dashboard/runs ->
            # /results symlink), produced by scripts/render_report.py. Served
            # under the strict SECURITY_HEADERS like every other asset; the regex
            # is anchored so the timestamp and service segments cannot contain
            # separators or "..".
            pass
        else:
            self._send_error(403)
            return

        # Determine MIME from the requested path's suffix (already allowlisted),
        # not the symlink target's — operator symlinks inside branding/ may
        # point to a file with a different extension.
        content_type = mimetypes.guess_type(str(unresolved))[0] or "application/octet-stream"
        is_json = unresolved.suffix.lower() == ".json"

        try:
            data = target.read_bytes()
        except OSError:
            self._send_error(500)
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if is_json:
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, code):
        # All responses (including errors) carry the single strict header set.
        reason = self.responses.get(code, ("Error",))[0]
        body = f"{code} {reason}\n".encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    try:
        port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    except ValueError:
        print("Invalid port", file=sys.stderr)
        sys.exit(1)

    server = ThreadingHTTPServer(("0.0.0.0", port), SecureStaticHandler)
    print(f"Serving {SERVE_ROOT} on port {port}", flush=True)
    server.serve_forever()
