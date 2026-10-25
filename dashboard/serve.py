#!/usr/bin/env python3
"""
Minimal static file server with security headers.
Usage: python serve.py [port]
"""
import gzip
import mimetypes
import os
import re
import sys
from email.utils import formatdate, parsedate_to_datetime
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
    # the first run, so target.is_file() 404s it like announcement-state.json.
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
# in RESULTS_ROOT is re-checked on the resolved target below. KEEP IN SYNC with
# backfill_reports.sh TS_RE/SVC_RE, render_report.py _RUN_TS_RE and app.js
# RUN_TIMESTAMP_RE.
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
    # The legacy XSS auditor is disabled explicitly: "1; mode=block" could be
    # abused to suppress legitimate scripts, and the CSP below supersedes it.
    ("X-XSS-Protection", "0"),
    (
        # script-src has no 'unsafe-inline': index.html and the reports ship zero
        # inline EXECUTABLE <script> blocks and zero inline event-handler
        # attributes. The reports' embedded data lives in a
        # <script type="application/json"> block, which browsers never execute
        # and CSP script-src does not govern. JS runs only from same-origin files
        # (app.js, report.js, vendor/). style-src KEEPS 'unsafe-inline' because
        # app.js writes inline style="" attributes via innerHTML; CSP has no
        # per-attribute hashing for those. base-uri/form-action/frame-ancestors
        # do not fall back to default-src, so they are locked separately.
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    ),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
]

# gzip only pays off for text; images/fonts are already compressed.
COMPRESSIBLE_SUFFIXES = {".html", ".js", ".css", ".json", ".svg"}
# Per-recv timeout while reading the request line and headers: a client that
# goes silent (or stalls) is dropped instead of pinning a ThreadingHTTPServer
# thread. It is NOT a whole-request deadline, so a client that drips a byte
# every few seconds is not bounded -- a known limit; put a reverse proxy in
# front where that matters.
SOCKET_TIMEOUT_SECONDS = 30
# Seconds for writing the response. Python's sendall timeout caps the TOTAL
# send, so the short request timeout would truncate large bodies on slow links.
RESPONSE_TIMEOUT_SECONDS = 600

# Last gzip body per file, keyed by its ETag: an unchanged file is not
# recompressed on every refresh. Only the fixed ALLOWED_STATIC and
# ALLOWED_JSON_SYMLINKS names are cached, which bounds the dict by those sets;
# run reports (an ever-growing set, each fetched rarely) and theme assets are
# compressed on the fly.
_GZIP_CACHE: dict[str, tuple[str, bytes]] = {}


def _accepts_gzip(header: str) -> bool:
    """True when Accept-Encoding gives gzip a nonzero q-value. An explicit gzip
    entry wins over *; a q that does not parse is ignored (q=1)."""
    qvalues: dict[str, float] = {}
    for item in header.split(","):
        coding, *params = (part.strip() for part in item.split(";"))
        q = 1.0
        for param in params:
            name, _, value = param.partition("=")
            if name.strip().lower() == "q":
                try:
                    q = float(value)
                except ValueError:
                    pass
        qvalues.setdefault(coding.lower(), q)
    return qvalues.get("gzip", qvalues.get("*", 0.0)) > 0


def _etag_matches(header: str, etag: str) -> bool:
    """If-None-Match uses the weak comparison: W/ prefixes are ignored (our own
    tags are always strong, so only the client's need stripping)."""
    if header.strip() == "*":
        return True
    return any(t.strip().removeprefix("W/") == etag for t in header.split(","))


def _not_modified_since(header: str, mtime: float) -> bool:
    try:
        since = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return False
    if since.tzinfo is None:
        return False
    # Last-Modified has one-second resolution, so compare whole seconds.
    return int(mtime) <= since.timestamp()


class SecureStaticHandler(BaseHTTPRequestHandler):
    timeout = SOCKET_TIMEOUT_SECONDS

    def log_message(self, fmt, *args):
        pass  # suppress default access log

    def end_headers(self):
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        super().end_headers()

    def do_GET(self):
        self._serve(send_body=True)

    def do_HEAD(self):
        self._serve(send_body=False)

    def _resolve_allowed(self):
        """Map the request path to an allowlisted file, or return an HTTP
        error code. Every allowlist denial and every missing file is the same
        404, checked in that order, so the status never reveals whether a
        disallowed path exists."""
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        path = unquote(path)
        if path == "/":
            path = "/index.html"

        rel_path = path.lstrip("/")
        parts = PurePosixPath(rel_path).parts
        if any(part == ".." for part in parts):
            return 403  # rejected on the path alone, no filesystem lookup

        # The unresolved path is always within SERVE_ROOT (.. already blocked above).
        # resolve() follows the intentional /dashboard/*.json → /results/*.json symlinks.
        unresolved = SERVE_ROOT.joinpath(*parts)
        try:
            target = unresolved.resolve()
        except (OSError, ValueError):
            return 400

        if not self._is_allowed(unresolved, target) or not target.is_file():
            return 404
        return unresolved, target

    @staticmethod
    def _is_allowed(unresolved, target):
        # Enforce path containment: deny everything not in the explicit allowlists.
        # This prevents serving arbitrary files via symlink or future directory additions.
        rel_str = str(unresolved.relative_to(SERVE_ROOT))
        if rel_str in ALLOWED_STATIC and target.is_relative_to(SERVE_ROOT):
            return True  # known static asset within dashboard dir
        if rel_str in ALLOWED_JSON_SYMLINKS and target.is_relative_to(RESULTS_ROOT):
            return True  # known JSON symlink resolving into /results/
        if (
            rel_str.startswith(THEME_PREFIX)
            and PurePosixPath(rel_str).suffix in ALLOWED_THEME_SUFFIXES
            and (
                target.is_relative_to((SERVE_ROOT / "themes").resolve())
                or target.is_relative_to(BRANDING_ROOT)
            )
        ):
            return True  # theme assets from the shipped theme or operator branding dir
        # Self-contained themed Rally report under
        # /results/<TIMESTAMP>/<service>.html (via the /dashboard/runs ->
        # /results symlink), produced by scripts/render_report.py. Served
        # under the strict SECURITY_HEADERS like every other asset; the regex
        # is anchored so the timestamp and service segments cannot contain
        # separators or "..".
        return bool(
            RUN_REPORT_RE.fullmatch(rel_str)
            and unresolved.suffix.lower() == ".html"
            and target.is_relative_to(RESULTS_ROOT)
        )

    def _serve(self, send_body):
        # The request has been read; from here on the socket only writes.
        self.connection.settimeout(RESPONSE_TIMEOUT_SECONDS)
        resolved = self._resolve_allowed()
        if isinstance(resolved, int):
            self._send_error(resolved, send_body)
            return
        unresolved, target = resolved

        # Determine MIME from the requested path's suffix (already allowlisted),
        # not the symlink target's — operator symlinks inside branding/ may
        # point to a file with a different extension.
        suffix = unresolved.suffix.lower()
        content_type = mimetypes.guess_type(str(unresolved))[0] or "application/octet-stream"
        use_gzip = suffix in COMPRESSIBLE_SUFFIXES and _accepts_gzip(
            self.headers.get("Accept-Encoding", "")
        )
        rel_str = str(unresolved.relative_to(SERVE_ROOT))
        cache_key = (
            rel_str
            if use_gzip and (rel_str in ALLOWED_STATIC or rel_str in ALLOWED_JSON_SYMLINKS)
            else None
        )

        try:
            with target.open("rb") as fh:
                st = os.fstat(fh.fileno())
                # Validators from the same open file the body is read from. The
                # tag differs per encoding: gzip and identity bodies differ, so
                # one strong tag must not name both.
                etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}{"-gz" if use_gzip else ""}"'
                not_modified = self._not_modified(etag, st.st_mtime)
                cached = _GZIP_CACHE.get(cache_key) if cache_key else None
                if cached is not None and cached[0] != etag:
                    cached = None
                # HEAD never reads (or compresses) the body.
                skip_read = not_modified or not send_body or cached is not None
                data = b"" if skip_read else fh.read()
        except OSError:
            self._send_error(500, send_body)
            return

        if not_modified:
            self.send_response(304)
            self._send_cache_headers(suffix, etag, st.st_mtime)
            self.end_headers()
            return

        if cached is not None:
            data = cached[1]
        elif use_gzip and send_body:
            # mtime=0: the -gz ETag is strong, so recompressing (after an
            # eviction) must give identical bytes, not a new header timestamp.
            data = gzip.compress(data, compresslevel=6, mtime=0)
            if cache_key:
                _GZIP_CACHE[cache_key] = (etag, data)
        if send_body or cached is not None:
            length = len(data)
        else:
            # HEAD: identity size from stat; gzip size unknown before any GET.
            length = None if use_gzip else st.st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if length is not None:
            self.send_header("Content-Length", str(length))
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        self._send_cache_headers(suffix, etag, st.st_mtime)
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def _not_modified(self, etag, mtime):
        # If-None-Match wins over If-Modified-Since when both are sent (RFC 9110).
        if_none_match = self.headers.get("If-None-Match")
        if if_none_match is not None:
            return _etag_matches(if_none_match, etag)
        if_modified_since = self.headers.get("If-Modified-Since")
        return if_modified_since is not None and _not_modified_since(if_modified_since, mtime)

    def _send_cache_headers(self, suffix, etag, mtime):
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", formatdate(mtime, usegmt=True))
        if suffix in COMPRESSIBLE_SUFFIXES:
            self.send_header("Vary", "Accept-Encoding")
        if suffix == ".json":
            # Always revalidate (the data changes every run/check), but let the
            # browser reuse its copy on a 304 instead of re-downloading.
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "public, max-age=300")

    def _send_error(self, code, send_body):
        # All responses (including errors) carry the single strict header set.
        reason = self.responses.get(code, ("Error",))[0]
        body = f"{code} {reason}\n".encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
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
