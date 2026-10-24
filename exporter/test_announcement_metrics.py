"""Tests for announcement_metrics.py (operator-announcement gauges)."""
import json
import time

import announcement_metrics as am
import rally_exporter as exporter
from exporter_testlib import iso_offset, make_announcement, metrics_output

# ---------------------------------------------------------------------------
# load_announcements
# ---------------------------------------------------------------------------

class TestLoadAnnouncements:
    def test_absent_file_returns_default_no_error(self, results_dir):
        """Absence is the normal pre-first-post state: no error counter bump."""
        before = exporter.rally_exporter_errors_total.labels(
            file="announcement-state.json"
        )._value.get()
        result = exporter.load_announcements()
        after = exporter.rally_exporter_errors_total.labels(
            file="announcement-state.json"
        )._value.get()
        assert result == {"announcements": []}
        assert after == before  # NOT incremented

    def test_corrupt_file_returns_default_and_increments_error(self, results_dir):
        (results_dir / "announcement-state.json").write_text("{bad json")
        before = exporter.rally_exporter_errors_total.labels(
            file="announcement-state.json"
        )._value.get()
        result = exporter.load_announcements()
        after = exporter.rally_exporter_errors_total.labels(
            file="announcement-state.json"
        )._value.get()
        assert result == {"announcements": []}
        assert after == before + 1

    def test_valid_file_returns_data(self, results_dir):
        state = {"announcements": [make_announcement()]}
        (results_dir / "announcement-state.json").write_text(json.dumps(state))
        result = exporter.load_announcements()
        assert len(result["announcements"]) == 1


# ---------------------------------------------------------------------------
# announcement_metrics.is_announcement_active
# ---------------------------------------------------------------------------

class TestIsAnnouncementActive:
    def test_incident_no_bounds_is_active(self):
        rec = make_announcement(rec_type="incident")
        assert am.is_announcement_active(rec, time.time() * 1000.0) is True

    def test_unknown_type_not_active(self):
        rec = make_announcement(rec_type="bogus")
        assert am.is_announcement_active(rec, time.time() * 1000.0) is False

    def test_future_effective_from_not_active(self):
        rec = make_announcement(rec_type="scheduled", effective_from=iso_offset(3600),
                                expires_at=iso_offset(7200))
        assert am.is_announcement_active(rec, time.time() * 1000.0) is False

    def test_past_expires_at_not_active(self):
        rec = make_announcement(rec_type="maintenance", expires_at=iso_offset(-60))
        assert am.is_announcement_active(rec, time.time() * 1000.0) is False

    def test_active_window_is_active(self):
        rec = make_announcement(rec_type="scheduled", effective_from=iso_offset(-60),
                                expires_at=iso_offset(3600))
        assert am.is_announcement_active(rec, time.time() * 1000.0) is True

    def test_unparseable_bounds_ignored(self):
        rec = make_announcement(rec_type="incident", effective_from="not-a-date",
                                expires_at="garbage")
        assert am.is_announcement_active(rec, time.time() * 1000.0) is True


# ---------------------------------------------------------------------------
# Announcement metrics — update_metrics / announcement_metrics.apply_announcement_metrics
# ---------------------------------------------------------------------------

class TestAnnouncementMetrics:
    def test_absent_file_emits_zeros_no_error(self, results_dir):
        before = exporter.rally_exporter_errors_total.labels(
            file="announcement-state.json"
        )._value.get()
        exporter.update_metrics()
        after = exporter.rally_exporter_errors_total.labels(
            file="announcement-state.json"
        )._value.get()
        output = metrics_output()
        assert 'rally_announcement_active{type="incident"} 0.0' in output
        assert 'rally_announcement_active{type="maintenance"} 0.0' in output
        assert 'rally_announcement_active{type="scheduled"} 0.0' in output
        assert "rally_maintenance_mode 0.0" in output
        assert after == before  # absence is not an error

    def test_active_incident_counted(self, results_dir):
        state = {"announcements": [make_announcement(rec_type="incident")]}
        (results_dir / "announcement-state.json").write_text(json.dumps(state))
        exporter.update_metrics()
        output = metrics_output()
        assert 'rally_announcement_active{type="incident"} 1.0' in output
        assert "rally_maintenance_mode 0.0" in output

    def test_future_effective_from_not_counted(self, results_dir):
        state = {"announcements": [make_announcement(
            rec_type="scheduled", effective_from=iso_offset(3600), expires_at=iso_offset(7200)
        )]}
        (results_dir / "announcement-state.json").write_text(json.dumps(state))
        exporter.update_metrics()
        assert 'rally_announcement_active{type="scheduled"} 0.0' in metrics_output()

    def test_past_expires_at_not_counted(self, results_dir):
        state = {"announcements": [make_announcement(
            rec_type="maintenance", expires_at=iso_offset(-60)
        )]}
        (results_dir / "announcement-state.json").write_text(json.dumps(state))
        exporter.update_metrics()
        output = metrics_output()
        assert 'rally_announcement_active{type="maintenance"} 0.0' in output
        assert "rally_maintenance_mode 0.0" in output

    def test_active_maintenance_sets_mode(self, results_dir):
        state = {"announcements": [make_announcement(
            rec_type="maintenance", expires_at=iso_offset(3600)
        )]}
        (results_dir / "announcement-state.json").write_text(json.dumps(state))
        exporter.update_metrics()
        output = metrics_output()
        assert 'rally_announcement_active{type="maintenance"} 1.0' in output
        assert "rally_maintenance_mode 1.0" in output

    def test_corrupt_file_emits_zeros(self, results_dir):
        (results_dir / "announcement-state.json").write_text("{corrupt")
        exporter.update_metrics()
        output = metrics_output()
        assert 'rally_announcement_active{type="incident"} 0.0' in output
        assert 'rally_announcement_active{type="maintenance"} 0.0' in output
        assert 'rally_announcement_active{type="scheduled"} 0.0' in output
        assert "rally_maintenance_mode 0.0" in output
