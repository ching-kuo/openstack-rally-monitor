#!/usr/bin/env python3
"""
Minimal static file server with security headers.
Usage: python serve.py [port]
"""
import mimetypes
import os
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
    "vendor/chart.umd.min.js",
    "vendor/inter-variable.woff2",
}
ALLOWED_JSON_SYMLINKS = {
    "results.json",
    "history.json",
    "health.json",
    "health_history.json",
    "announcement-state.json",
}
THEME_PREFIX = "themes/"
ALLOWED_THEME_SUFFIXES = {".css", ".svg", ".png", ".ico"}
RESULTS_ROOT = Path(os.environ.get("RESULTS_DIR", "/results")).resolve()
BRANDING_ROOT = (RESULTS_ROOT / "branding").resolve()

SECURITY_HEADERS = [
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-XSS-Protection", "1; mode=block"),
    (
        # script-src has no 'unsafe-inline': index.html ships zero inline
        # <script> blocks and zero inline event-handler attributes (the former
        # themes/custom <link onerror=...> handlers were moved into app.js). JS
        # runs only from same-origin files (app.js, vendor/). style-src KEEPS
        # 'unsafe-inline' because app.js writes inline style="" attributes via
        # innerHTML; CSP has no per-attribute hashing for those.
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
