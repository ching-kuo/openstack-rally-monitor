"""HTTP-semantics tests for dashboard/serve.py: conditional requests (ETag /
Last-Modified -> 304), gzip, HEAD, the uniform 404, header hardening, and the
per-connection socket timeout. The allowlist itself is covered in test_serve.py.
"""
import gzip
import http.client
import socket
import time
from urllib.parse import urlparse

import pytest

import serve


def _host_port(base):
    host, port = urlparse(base).netloc.split(":")
    return host, int(port)


def request(base, path, method="GET", headers=None):
    """Return (status, headers_dict, body_bytes) for one request, raw bytes
    (no transparent decompression)."""
    conn = http.client.HTTPConnection(*_host_port(base), timeout=10)
    try:
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body
    finally:
        conn.close()


@pytest.fixture
def history(server):
    """A served JSON symlink (health_history.json) with a compressible body."""
    base, dashboard_dir, results_dir = server
    target = results_dir / "health_history.json"
    target.write_text('{"checks": [' + ",".join(['{"overall":"up"}'] * 200) + "]}")
    (dashboard_dir / "health_history.json").symlink_to(target)
    return base, target


class TestConditionalRequests:
    def test_json_is_revalidated_not_stored(self, history):
        base, _ = history
        code, headers, _ = request(base, "/health_history.json")
        assert code == 200
        assert headers["cache-control"] == "no-cache"
        assert headers["etag"]
        assert headers["last-modified"]

    def test_matching_etag_returns_304_with_security_headers(self, history):
        base, _ = history
        _, first, _ = request(base, "/health_history.json")
        code, headers, body = request(
            base, "/health_history.json", headers={"If-None-Match": first["etag"]}
        )
        assert code == 304
        assert body == b""
        assert headers["etag"] == first["etag"]
        assert headers["x-content-type-options"] == "nosniff"
        assert "content-security-policy" in headers

    def test_changed_file_gets_a_new_etag(self, history):
        base, target = history
        _, first, _ = request(base, "/health_history.json")
        target.write_text('{"checks": []}')
        code, headers, _ = request(
            base, "/health_history.json", headers={"If-None-Match": first["etag"]}
        )
        assert code == 200
        assert headers["etag"] != first["etag"]

    def test_if_modified_since_returns_304(self, history):
        base, _ = history
        _, first, _ = request(base, "/health_history.json")
        code, _, _ = request(
            base, "/health_history.json",
            headers={"If-Modified-Since": first["last-modified"]},
        )
        assert code == 304

    def test_if_none_match_takes_precedence_over_if_modified_since(self, history):
        base, _ = history
        _, first, _ = request(base, "/health_history.json")
        code, _, _ = request(
            base, "/health_history.json",
            headers={"If-None-Match": '"stale"', "If-Modified-Since": first["last-modified"]},
        )
        assert code == 200


class TestGzip:
    def test_gzip_when_accepted(self, history):
        base, target = history
        code, headers, body = request(
            base, "/health_history.json", headers={"Accept-Encoding": "gzip, deflate"}
        )
        assert code == 200
        assert headers["content-encoding"] == "gzip"
        assert headers["vary"] == "Accept-Encoding"
        assert int(headers["content-length"]) == len(body)
        assert gzip.decompress(body) == target.read_bytes()

    def test_identity_without_accept_encoding(self, history):
        base, target = history
        _, headers, body = request(base, "/health_history.json")
        assert "content-encoding" not in headers
        assert headers["vary"] == "Accept-Encoding"
        assert body == target.read_bytes()

    def test_gzip_and_identity_etags_differ(self, history):
        base, _ = history
        _, plain, _ = request(base, "/health_history.json")
        _, gz, _ = request(base, "/health_history.json", headers={"Accept-Encoding": "gzip"})
        assert plain["etag"] != gz["etag"]
        # The 304 path honors the per-encoding tag.
        code, _, _ = request(
            base, "/health_history.json",
            headers={"Accept-Encoding": "gzip", "If-None-Match": gz["etag"]},
        )
        assert code == 304

    def test_gzip_q0_is_refused(self, history):
        base, _ = history
        _, headers, _ = request(
            base, "/health_history.json", headers={"Accept-Encoding": "gzip;q=0"}
        )
        assert "content-encoding" not in headers


class TestHead:
    def test_head_matches_get_headers_without_body(self, history):
        base, _ = history
        _, get_headers, get_body = request(base, "/health_history.json")
        code, headers, body = request(base, "/health_history.json", method="HEAD")
        assert code == 200
        assert body == b""
        assert headers["content-length"] == str(len(get_body))
        assert headers["etag"] == get_headers["etag"]

    def test_head_on_disallowed_path_is_404(self, server):
        base, dashboard_dir, _ = server
        (dashboard_dir / "serve.py").write_text("# source")
        code, _, body = request(base, "/serve.py", method="HEAD")
        assert code == 404
        assert body == b""


class TestUniform404:
    def test_existing_disallowed_and_missing_paths_are_indistinguishable(self, server):
        base, dashboard_dir, _ = server
        (dashboard_dir / "secrets.txt").write_text("password123")
        existing = request(base, "/secrets.txt")
        missing = request(base, "/nothing-here.txt")
        assert existing[0] == missing[0] == 404
        assert existing[2] == missing[2]


class TestHardenedHeaders:
    def _headers(self, server):
        base, dashboard_dir, _ = server
        (dashboard_dir / "index.html").write_text("<html></html>")
        return request(base, "/")[1]

    def test_csp_locks_base_form_and_framing(self, server):
        csp = self._headers(server)["content-security-policy"]
        directives = {p.split()[0]: p.split()[1:] for p in csp.split(";") if p.strip()}
        assert directives["base-uri"] == ["'none'"]
        assert directives["form-action"] == ["'none'"]
        assert directives["frame-ancestors"] == ["'none'"]

    def test_legacy_xss_auditor_header_is_not_enabled(self, server):
        # "1; mode=block" can itself be abused; modern guidance is 0 or absent.
        assert self._headers(server).get("x-xss-protection", "0") == "0"


def test_idle_connection_is_closed_by_socket_timeout(server, monkeypatch):
    """A client that connects and never sends a request must not pin a server
    thread forever: the handler's socket timeout closes it."""
    monkeypatch.setattr(serve.SecureStaticHandler, "timeout", 0.5)
    base, _, _ = server
    with socket.create_connection(_host_port(base), timeout=10) as sock:
        started = time.monotonic()
        assert sock.recv(1) == b""  # server closed the idle connection
        assert time.monotonic() - started < 5


def test_handler_has_a_production_socket_timeout():
    assert 0 < serve.SecureStaticHandler.timeout <= 60


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("gzip", True),
        ("gzip;q=0", False),
        ("*;q=0, gzip", True),        # explicit gzip beats *
        ("gzip;q=0, *", False),       # ...in either order
        ("*", True),
        ("deflate, br", False),
        ("gzip;foo=bar;q=0.5", True),  # q among several params
        ("gzip;level=1;q=0", False),
        ("gzip;q=abc", True),          # unparseable q is ignored (q=1)
        ("", False),
    ],
)
def test_accepts_gzip(header, expected):
    assert serve._accepts_gzip(header) is expected


class TestGzipCache:
    @pytest.fixture
    def compress_calls(self, monkeypatch):
        calls = []
        real = serve.gzip.compress

        def counting(data, *args, **kwargs):
            calls.append(kwargs)
            return real(data, *args, **kwargs)

        monkeypatch.setattr(serve.gzip, "compress", counting)
        serve._GZIP_CACHE.clear()
        return calls

    def test_unchanged_file_is_compressed_once(self, history, compress_calls):
        base, target = history
        gz = {"Accept-Encoding": "gzip"}
        _, _, first = request(base, "/health_history.json", headers=gz)
        _, _, second = request(base, "/health_history.json", headers=gz)
        assert first == second
        assert len(compress_calls) == 1
        target.write_text('{"checks": [1]}')
        _, _, third = request(base, "/health_history.json", headers=gz)
        assert gzip.decompress(third) == b'{"checks": [1]}'
        assert len(compress_calls) == 2

    def test_gzip_body_is_stable_across_cache_eviction(self, history, compress_calls):
        """The -gz ETag is strong, so the same tag must always mean the same
        bytes; gzip's header timestamp would differ after an eviction."""
        base, _ = history
        gz = {"Accept-Encoding": "gzip"}
        _, first_headers, first = request(base, "/health_history.json", headers=gz)
        serve._GZIP_CACHE.clear()
        time.sleep(1.1)  # the gzip header timestamp has one-second resolution
        _, second_headers, second = request(base, "/health_history.json", headers=gz)
        assert len(compress_calls) == 2
        assert first_headers["etag"] == second_headers["etag"]
        assert first == second
        # Python < 3.14 defaults gzip.compress to the current time, so the
        # timestamp must be pinned explicitly (CI and the image run 3.13).
        assert all(call.get("mtime") == 0 for call in compress_calls)

    def test_reports_are_compressed_but_not_cached(self, server, compress_calls):
        """Run reports churn with every run; caching them would evict the hot
        dashboard entries, so only static assets and JSON are cached."""
        base, dashboard_dir, results_dir = server
        (results_dir / "20260612T120000Z").mkdir()
        (results_dir / "20260612T120000Z" / "nova.html").write_text("<html>" + "r" * 500)
        (dashboard_dir / "runs").symlink_to(results_dir)
        _, headers, body = request(
            base, "/runs/20260612T120000Z/nova.html", headers={"Accept-Encoding": "gzip"}
        )
        assert headers["content-encoding"] == "gzip"
        assert gzip.decompress(body).startswith(b"<html>")
        assert serve._GZIP_CACHE == {}

    def test_head_never_compresses(self, history, compress_calls):
        base, _ = history
        gz = {"Accept-Encoding": "gzip"}
        code, headers, body = request(base, "/health_history.json", method="HEAD", headers=gz)
        assert code == 200 and body == b""
        assert headers["content-encoding"] == "gzip"
        assert "content-length" not in headers  # unknown without compressing
        assert compress_calls == []
        _, _, gz_body = request(base, "/health_history.json", headers=gz)
        _, headers, _ = request(base, "/health_history.json", method="HEAD", headers=gz)
        assert headers["content-length"] == str(len(gz_body))  # from the cache
        assert len(compress_calls) == 1


def test_slow_reader_still_gets_the_whole_body(server, monkeypatch):
    """The short socket timeout guards only the request read. Python's sendall
    timeout caps the TOTAL send, so keeping it during the body write truncated
    large responses to slow clients."""
    monkeypatch.setattr(serve.SecureStaticHandler, "timeout", 0.3)
    base, dashboard_dir, results_dir = server
    payload = b'{"x": "' + b"a" * (32 * 1024 * 1024) + b'"}'
    (results_dir / "history.json").write_bytes(payload)
    (dashboard_dir / "history.json").symlink_to(results_dir / "history.json")
    with socket.create_connection(_host_port(base), timeout=30) as sock:
        sock.sendall(b"GET /history.json HTTP/1.0\r\n\r\n")
        time.sleep(1.5)  # let the server's send buffer fill and stall
        received = bytearray()
        while chunk := sock.recv(1 << 20):
            received.extend(chunk)
    assert received.endswith(payload)
