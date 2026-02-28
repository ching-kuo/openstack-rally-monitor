#!/usr/bin/env python3
"""
Minimal static file server with security headers.
Usage: python serve.py [port]
"""
import mimetypes
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

SERVE_ROOT = Path(os.getcwd()).resolve()

SECURITY_HEADERS = [
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-XSS-Protection", "1; mode=block"),
    (
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
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

        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        is_json = target.suffix.lower() == ".json"

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

    server = HTTPServer(("0.0.0.0", port), SecureStaticHandler)
    print(f"Serving {SERVE_ROOT} on port {port}", flush=True)
    server.serve_forever()
