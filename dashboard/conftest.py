"""Shared fixtures for the dashboard/serve.py test suites."""
import sys
import threading
from http.server import HTTPServer
from pathlib import Path

import pytest

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
    # A short poll interval: shutdown() waits up to one interval (default 0.5s).
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01})
    t.daemon = True
    t.start()

    base = f"http://127.0.0.1:{port}"
    yield base, dashboard_dir, results_dir

    srv.shutdown()
    srv.server_close()
