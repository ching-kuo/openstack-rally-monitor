"""Tests for announcement_metrics.py (operator-announcement gauges)."""
import json
from pathlib import Path

import pytest

import announcement_metrics as am
import rally_exporter as exporter
from exporter_testlib import error_count, iso_offset, make_announcement, metrics_output

# Shared with the dashboard's JS test (KEEP IN SYNC).
ANNOUNCEMENT_CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "testdata" / "announcement_active_cases.json").read_text()
)


def announce_errors() -> float:
    return error_count("announcement-state.json")

# ---------------------------------------------------------------------------
# load_announcements
# ---------------------------------------------------------------------------

class TestLoadAnnouncements:
    # A valid file is covered through update_metrics in TestAnnouncementMetrics.
    def test_absent_file_returns_default_no_error(self, results_dir):
        """Absence is the normal pre-first-post state: no error counter bump."""
        before = announce_errors()
        assert exporter.load_announcements() == {"announcements": []}
        assert announce_errors() == before  # NOT incremented

    def test_corrupt_file_returns_default_and_increments_error(self, results_dir):
        (results_dir / "announcement-state.json").write_text("{bad json")
        before = announce_errors()
        assert exporter.load_announcements() == {"announcements": []}
        assert announce_errors() == before + 1


# ---------------------------------------------------------------------------
# announcement_metrics.is_announcement_active (cases shared with the JS test)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("rec", "expected"),
    [pytest.param(case["record"], case["active"], id=case["id"])
     for case in ANNOUNCEMENT_CASES["cases"]],
)
def test_is_announcement_active(rec, expected):
    assert am.is_announcement_active(rec, ANNOUNCEMENT_CASES["now_ms"]) is expected


# ---------------------------------------------------------------------------
# Announcement metrics — update_metrics / announcement_metrics.apply_announcement_metrics
# ---------------------------------------------------------------------------

ZEROS = (
    'rally_announcement_active{type="incident"} 0.0',
    'rally_announcement_active{type="maintenance"} 0.0',
    'rally_announcement_active{type="scheduled"} 0.0',
    "rally_maintenance_mode 0.0",
)


class TestAnnouncementMetrics:
    @pytest.mark.parametrize("contents", [None, "{corrupt"], ids=["absent", "corrupt"])
    def test_unusable_file_emits_zeros(self, results_dir, contents):
        if contents is not None:
            (results_dir / "announcement-state.json").write_text(contents)
        before = announce_errors()
        exporter.update_metrics()
        output = metrics_output()
        for line in ZEROS:
            assert line in output
        if contents is None:
            assert announce_errors() == before  # absence is not an error

    # (type, effective_from offset, expires_at offset) in seconds from now;
    # the record is built in the test body so the times are relative to when
    # the test runs, not to collection time.
    @pytest.mark.parametrize(
        ("rec_type", "starts_in", "expires_in", "line", "mode"),
        [
            pytest.param("incident", None, None,
                         'rally_announcement_active{type="incident"} 1.0', 0,
                         id="active-incident-counted"),
            pytest.param("scheduled", 3600, 7200,
                         'rally_announcement_active{type="scheduled"} 0.0', 0,
                         id="future-effective-from-not-counted"),
            pytest.param("maintenance", None, -60,
                         'rally_announcement_active{type="maintenance"} 0.0', 0,
                         id="past-expires-at-not-counted"),
            pytest.param("maintenance", None, 3600,
                         'rally_announcement_active{type="maintenance"} 1.0', 1,
                         id="active-maintenance-sets-mode"),
        ],
    )
    def test_record_drives_gauges(self, results_dir, rec_type, starts_in, expires_in, line, mode):
        rec = make_announcement(
            rec_type=rec_type,
            effective_from=None if starts_in is None else iso_offset(starts_in),
            expires_at=None if expires_in is None else iso_offset(expires_in),
        )
        state = {"announcements": [rec]}
        (results_dir / "announcement-state.json").write_text(json.dumps(state))
        exporter.update_metrics()
        output = metrics_output()
        assert line in output
        assert f"rally_maintenance_mode {float(mode)}" in output
