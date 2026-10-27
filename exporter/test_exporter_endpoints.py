"""The exporter's Flask endpoints: /health, /ready and /metrics."""
import json
import time

import pytest

from exporter_testlib import make_summary


# ---------------------------------------------------------------------------
# Flask endpoints
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    def test_returns_200_with_ok(self, client, results_dir):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert "timestamp" in data


class TestReadyEndpoint:
    def test_not_ready_when_no_results(self, client, results_dir):
        resp = client.get("/ready")
        assert resp.status_code == 503
        data = resp.get_json()
        assert data["ready"] is False

    def test_not_ready_when_all_pending(self, client, results_dir):
        summary = make_summary(services={
            "nova": {"status": "pending", "duration": 0, "total_iterations": 0,
                     "failed_iterations": 0, "sla_passed": True, "scenarios": []}
        })
        (results_dir / "latest_summary.json").write_text(json.dumps(summary))
        resp = client.get("/ready")
        assert resp.status_code == 503

    @pytest.mark.parametrize(
        ("timestamp", "reason"),
        [
            pytest.param("20200101T000000Z", "timestamp_too_old", id="too-old"),
            pytest.param("garbage", "timestamp_invalid", id="garbage"),
            pytest.param("2024-01-01T00:00:00Z", "timestamp_invalid", id="iso-not-compact"),
        ],
    )
    def test_not_ready_when_timestamp_unusable(self, client, results_dir, timestamp, reason):
        (results_dir / "latest_summary.json").write_text(json.dumps(make_summary(timestamp=timestamp)))
        resp = client.get("/ready")
        assert resp.status_code == 503
        assert resp.get_json() == {"ready": False, "reasons": [reason]}

    def test_ready_with_recent_valid_results(self, client, results_dir):
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        summary = make_summary(timestamp=ts)
        (results_dir / "latest_summary.json").write_text(json.dumps(summary))
        resp = client.get("/ready")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ready"] is True


class TestMetricsEndpoint:
    def test_serves_prometheus_text_with_expected_metrics(self, client, results_dir):
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.content_type
        body = resp.data
        assert b"rally_data_valid" in body
        assert b"rally_overall_success" in body
        assert b"rally_last_run_timestamp" in body
        assert b"rally_exporter_errors_total" in body
