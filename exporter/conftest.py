"""Fixtures shared by the exporter test modules."""
import pytest

import announcement_metrics as am
import health_metrics as hm
import rally_exporter as exporter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset module-level cache and metric state between tests."""
    exporter._summary_mtime = -1.0
    exporter._summary_data = {}
    exporter._summary_cache_time = 0.0
    exporter._cleanup_mtime = -1.0
    exporter._cleanup_data = {}
    exporter._cleanup_cache_time = 0.0
    exporter._health_mtime = -1.0
    exporter._health_data = {}
    exporter._health_cache_time = 0.0
    exporter._announce_mtime = -1.0
    exporter._announce_data = {}
    exporter._announce_cache_time = 0.0
    exporter._last_processed_ts = ""
    exporter._last_applied_cleanup = {}
    hm._last_applied_health = {}
    hm._last_health_stale = False
    hm._last_good_check_epoch = 0.0
    # Reset scalar metrics
    exporter.rally_data_valid.set(0)
    exporter.rally_overall_success.set(0)
    exporter.rally_last_run_timestamp.set(0)
    exporter.rally_last_run_end_timestamp.set(0)
    exporter.rally_run_duration_seconds.set(0)
    am.rally_maintenance_mode.set(0)
    exporter.rally_run_setup_ok.set(0)
    hm.rally_api_last_check_timestamp.set(0)
    # Clear labeled metrics
    for m in [
        exporter.rally_task_success,
        exporter.rally_task_duration_seconds,
        exporter.rally_task_iterations_total,
        exporter.rally_task_failures_total,
        exporter.rally_task_sla_passed,
        exporter.rally_service_status,
        exporter.rally_cleanup_failure,
        exporter.rally_orphaned_resources,
        exporter.rally_context_cleanup_warning,
        exporter.rally_context_orphaned_resources,
        hm.rally_api_up,
        hm.rally_api_latency_milliseconds,
        am.rally_announcement_active,
    ]:
        m.clear()
    exporter.rally_rgw_orphaned_users.set(0)
    exporter.rally_rgw_orphaned_buckets.set(0)
    exporter.rally_rgw_unknown_owner_orphans.set(0)
    exporter.rally_rgw_scan_ok.set(1)
    # rally_api_overall_up is unlabeled: .clear() is unsupported, so reset to a
    # known baseline between tests. The gauge now FAILS CLOSED -- unknown/missing
    # overall sets 0 -- and several tests pre-set it to 1 to prove the flip.
    hm.rally_api_overall_up.set(0)
    yield


@pytest.fixture
def results_dir(tmp_path, monkeypatch):
    """Temporary results directory with RESULTS_DIR patched."""
    monkeypatch.setattr(exporter, "RESULTS_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def client():
    """Flask test client."""
    exporter.app.config["TESTING"] = True
    with exporter.app.test_client() as c:
        yield c
