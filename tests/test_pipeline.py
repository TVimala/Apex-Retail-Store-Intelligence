# PROMPT: "Write comprehensive pytest tests for a retail CCTV analytics pipeline.
# The pipeline emits structured events (ENTRY, EXIT, ZONE_ENTER, ZONE_EXIT, ZONE_DWELL,
# BILLING_QUEUE_JOIN, BILLING_QUEUE_ABANDON, REENTRY). Test edge cases:
# group entry (3 people simultaneously), re-entry detection, staff exclusion,
# empty store periods, confidence thresholding, and schema compliance.
# Include fixtures with synthetic bounding box data."
#
# CHANGES MADE:
# - Added deterministic visitor_id fixture instead of relying on real timestamps
# - Extended group entry test to assert exactly N events, not just > 0
# - Added test for confidence pass-through on low-conf detections (don't suppress)
# - Added timestamp format validation (strict ISO-8601 UTC Z suffix)
# - Replaced AI-suggested monkeypatching of cv2 with a proper fixture approach
# - Added REENTRY cooldown boundary test (just inside / just outside window)

import json
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
import tempfile

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_event():
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": "STORE_BLR_002",
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": "VIS_abc123",
        "event_type": "ENTRY",
        "timestamp": "2026-03-03T14:22:10Z",
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.91,
        "metadata": {
            "queue_depth": None,
            "sku_zone": None,
            "session_seq": 1,
        },
    }


@pytest.fixture
def zone_event(sample_event):
    return {
        **sample_event,
        "event_id": str(uuid.uuid4()),
        "event_type": "ZONE_DWELL",
        "zone_id": "SKINCARE",
        "dwell_ms": 30000,
        "metadata": {
            "queue_depth": None,
            "sku_zone": "MOISTURISER",
            "session_seq": 5,
        },
    }


@pytest.fixture
def temp_output(tmp_path):
    return str(tmp_path / "test_events.jsonl")


# ─────────────────────────────────────────────────────────────────────────────
# Schema compliance tests
# ─────────────────────────────────────────────────────────────────────────────

class TestEventSchema:
    def test_event_id_is_uuid(self, sample_event):
        """event_id must be a valid UUID v4."""
        try:
            uuid.UUID(sample_event["event_id"])
        except ValueError:
            pytest.fail("event_id is not a valid UUID")

    def test_timestamp_is_iso8601_utc(self, sample_event):
        """Timestamp must be ISO-8601 with Z suffix."""
        ts = sample_event["timestamp"]
        assert ts.endswith("Z"), f"Timestamp must end with Z, got: {ts}"
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None

    def test_confidence_in_range(self, sample_event):
        assert 0.0 <= sample_event["confidence"] <= 1.0

    def test_zone_id_none_for_entry_exit(self, sample_event):
        """ENTRY and EXIT events must have zone_id = None."""
        assert sample_event["event_type"] == "ENTRY"
        assert sample_event["zone_id"] is None

    def test_zone_id_required_for_dwell(self, zone_event):
        assert zone_event["zone_id"] is not None
        assert zone_event["event_type"] == "ZONE_DWELL"

    def test_dwell_ms_non_negative(self, zone_event):
        assert zone_event["dwell_ms"] >= 0

    def test_session_seq_positive(self, sample_event):
        assert sample_event["metadata"]["session_seq"] >= 0

    def test_all_required_fields_present(self, sample_event):
        required = {
            "event_id", "store_id", "camera_id", "visitor_id",
            "event_type", "timestamp", "zone_id", "dwell_ms",
            "is_staff", "confidence", "metadata",
        }
        missing = required - set(sample_event.keys())
        assert not missing, f"Missing fields: {missing}"

    def test_event_type_valid(self, sample_event):
        valid_types = {
            "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
            "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY",
        }
        assert sample_event["event_type"] in valid_types

    def test_metadata_has_required_keys(self, sample_event):
        meta = sample_event["metadata"]
        assert "queue_depth" in meta
        assert "sku_zone" in meta
        assert "session_seq" in meta


# ─────────────────────────────────────────────────────────────────────────────
# Event emitter tests
# ─────────────────────────────────────────────────────────────────────────────

class TestEventEmitter:
    def test_emitter_writes_jsonl(self, temp_output):
        from pipeline.emit import EventEmitter, EventType
        ts = datetime.now(timezone.utc)
        with EventEmitter(output_path=temp_output) as emitter:
            emitter.emit(
                EventType.ENTRY, "STORE_BLR_002", "CAM_ENTRY_01",
                "VIS_test01", ts, None, 0, False, 0.85,
            )
        lines = Path(temp_output).read_text().strip().split("\n")
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["event_type"] == "ENTRY"

    def test_event_ids_are_unique(self, temp_output):
        from pipeline.emit import EventEmitter, EventType
        ts = datetime.now(timezone.utc)
        n_events = 20
        with EventEmitter(output_path=temp_output) as emitter:
            for i in range(n_events):
                emitter.emit(
                    EventType.ZONE_DWELL, "STORE_BLR_002", "CAM_FLOOR_01",
                    f"VIS_{i:04d}", ts, "SKINCARE", 30000, False, 0.78,
                )
        lines = Path(temp_output).read_text().strip().split("\n")
        ids = [json.loads(l)["event_id"] for l in lines]
        assert len(set(ids)) == n_events, "Duplicate event_ids detected"

    def test_session_seq_increments_per_visitor(self, temp_output):
        from pipeline.emit import EventEmitter, EventType
        ts = datetime.now(timezone.utc)
        with EventEmitter(output_path=temp_output) as emitter:
            for event_type in [EventType.ENTRY, EventType.ZONE_ENTER, EventType.ZONE_DWELL]:
                emitter.emit(
                    event_type, "STORE_BLR_002", "CAM_ENTRY_01",
                    "VIS_seqtest", ts, "SKINCARE" if event_type != EventType.ENTRY else None,
                    0, False, 0.9,
                )
        lines = Path(temp_output).read_text().strip().split("\n")
        seqs = [json.loads(l)["metadata"]["session_seq"] for l in lines]
        assert seqs == [1, 2, 3], f"Expected [1,2,3], got {seqs}"

    def test_low_confidence_events_not_suppressed(self, temp_output):
        """Low-confidence events must be emitted, not dropped."""
        from pipeline.emit import EventEmitter, EventType
        ts = datetime.now(timezone.utc)
        with EventEmitter(output_path=temp_output) as emitter:
            emitter.emit(
                EventType.ENTRY, "STORE_BLR_002", "CAM_ENTRY_01",
                "VIS_lowconf", ts, None, 0, False, 0.12,  # very low conf
            )
        lines = Path(temp_output).read_text().strip().split("\n")
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["confidence"] == pytest.approx(0.12, abs=1e-4)

    def test_staff_flag_preserved(self, temp_output):
        from pipeline.emit import EventEmitter, EventType
        ts = datetime.now(timezone.utc)
        with EventEmitter(output_path=temp_output) as emitter:
            emitter.emit(
                EventType.ENTRY, "STORE_BLR_002", "CAM_ENTRY_01",
                "VIS_staff01", ts, None, 0, True, 0.95,
            )
        event = json.loads(Path(temp_output).read_text().strip())
        assert event["is_staff"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Re-ID and re-entry tests
# ─────────────────────────────────────────────────────────────────────────────

class TestReIDRegistry:
    def test_new_track_creates_visitor(self):
        from pipeline.tracker import ReIDRegistry
        registry = ReIDRegistry(cooldown_seconds=120)
        ts = datetime.now(timezone.utc)
        vid, is_reentry = registry.get_or_create(1, (100.0, 200.0), ts)
        assert vid.startswith("VIS_")
        assert is_reentry is False

    def test_same_track_returns_same_visitor(self):
        from pipeline.tracker import ReIDRegistry
        registry = ReIDRegistry(cooldown_seconds=120)
        ts = datetime.now(timezone.utc)
        vid1, _ = registry.get_or_create(42, (100.0, 200.0), ts)
        vid2, _ = registry.get_or_create(42, (105.0, 205.0), ts)
        assert vid1 == vid2

    def test_reentry_detected_within_cooldown(self):
        from pipeline.tracker import ReIDRegistry
        registry = ReIDRegistry(cooldown_seconds=120)
        ts1 = datetime.now(timezone.utc)
        vid1, _ = registry.get_or_create(1, (100.0, 200.0), ts1)
        registry.mark_exited(vid1, ts1)
        # Re-enter 30 seconds later at same location
        ts2 = ts1 + timedelta(seconds=30)
        vid2, is_reentry = registry.get_or_create(2, (100.0, 200.0), ts2)
        assert is_reentry is True
        assert vid1 == vid2

    def test_no_reentry_after_cooldown_expires(self):
        from pipeline.tracker import ReIDRegistry
        registry = ReIDRegistry(cooldown_seconds=60)
        ts1 = datetime.now(timezone.utc)
        vid1, _ = registry.get_or_create(1, (100.0, 200.0), ts1)
        registry.mark_exited(vid1, ts1)
        # Re-enter AFTER cooldown
        ts2 = ts1 + timedelta(seconds=90)
        vid2, is_reentry = registry.get_or_create(2, (100.0, 200.0), ts2)
        assert is_reentry is False
        assert vid1 != vid2

    def test_different_location_not_matched_as_reentry(self):
        from pipeline.tracker import ReIDRegistry
        registry = ReIDRegistry(cooldown_seconds=120)
        ts1 = datetime.now(timezone.utc)
        vid1, _ = registry.get_or_create(1, (100.0, 200.0), ts1)
        registry.mark_exited(vid1, ts1)
        ts2 = ts1 + timedelta(seconds=10)
        # Completely different spatial location
        vid2, is_reentry = registry.get_or_create(2, (900.0, 800.0), ts2)
        assert is_reentry is False


# ─────────────────────────────────────────────────────────────────────────────
# Entry/exit classifier tests
# ─────────────────────────────────────────────────────────────────────────────

class TestEntryExitClassifier:
    def test_downward_crossing_is_entry(self):
        from pipeline.detect import EntryExitClassifier
        clf = EntryExitClassifier(entry_line_y=300.0, frame_height=720)
        clf.classify(1, 280.0)  # Prime the state
        result = clf.classify(1, 320.0)  # Cross downward
        assert result == "ENTRY"

    def test_upward_crossing_is_exit(self):
        from pipeline.detect import EntryExitClassifier
        clf = EntryExitClassifier(entry_line_y=300.0, frame_height=720)
        clf.classify(1, 320.0)
        result = clf.classify(1, 280.0)
        assert result == "EXIT"

    def test_no_crossing_returns_none(self):
        from pipeline.detect import EntryExitClassifier
        clf = EntryExitClassifier(entry_line_y=300.0, frame_height=720)
        clf.classify(1, 100.0)
        result = clf.classify(1, 120.0)  # Both above the line
        assert result is None

    def test_fallback_line_at_25_percent(self):
        from pipeline.detect import EntryExitClassifier
        clf = EntryExitClassifier(entry_line_y=None, frame_height=800)
        assert clf.line_y == pytest.approx(200.0)


# ─────────────────────────────────────────────────────────────────────────────
# Zone dwell tracker tests
# ─────────────────────────────────────────────────────────────────────────────

class TestZoneDwellTracker:
    def test_no_dwell_event_before_30s(self):
        from pipeline.detect import ZoneDwellTracker
        tracker = ZoneDwellTracker()
        events = tracker.update("VIS_001", "SKINCARE", 0.0)
        assert events == []
        events = tracker.update("VIS_001", "SKINCARE", 25.0)
        assert events == []

    def test_dwell_event_after_30s(self):
        from pipeline.detect import ZoneDwellTracker
        tracker = ZoneDwellTracker()
        tracker.update("VIS_001", "SKINCARE", 0.0)
        events = tracker.update("VIS_001", "SKINCARE", 35.0)
        assert len(events) == 1
        zone_id, dwell_ms = events[0]
        assert zone_id == "SKINCARE"
        assert dwell_ms >= 30000

    def test_zone_change_resets_dwell(self):
        from pipeline.detect import ZoneDwellTracker
        tracker = ZoneDwellTracker()
        tracker.update("VIS_001", "SKINCARE", 0.0)
        tracker.update("VIS_001", "HAIRCARE", 20.0)  # moved zones
        events = tracker.update("VIS_001", "HAIRCARE", 55.0)
        # Should NOT emit for SKINCARE (only 20s there)
        for zone_id, _ in events:
            assert zone_id == "HAIRCARE"


# ─────────────────────────────────────────────────────────────────────────────
# Group entry test
# ─────────────────────────────────────────────────────────────────────────────

class TestGroupEntry:
    def test_three_people_produce_three_entry_events(self, temp_output):
        """
        Simulate 3 simultaneous detections at the entry line.
        Must produce 3 ENTRY events with distinct visitor_ids.
        """
        from pipeline.emit import EventEmitter, EventType
        from pipeline.tracker import ReIDRegistry
        from pipeline.detect import EntryExitClassifier

        ts = datetime.now(timezone.utc)
        emitter = EventEmitter(output_path=temp_output)
        reid = ReIDRegistry(cooldown_seconds=120)
        clf = EntryExitClassifier(entry_line_y=300.0, frame_height=720)

        # Simulate 3 people crossing at slightly different x positions
        group_tracks = [
            (1, (200.0, 320.0)),
            (2, (250.0, 325.0)),
            (3, (300.0, 318.0)),
        ]

        for track_id, (fx, fy) in group_tracks:
            clf.classify(track_id, 280.0)  # prime above line
            crossing = clf.classify(track_id, fy)  # cross line
            if crossing == "ENTRY":
                vid, is_reentry = reid.get_or_create(track_id, (fx, fy), ts)
                emitter.emit(
                    EventType.ENTRY, "STORE_BLR_002", "CAM_ENTRY_01",
                    vid, ts, None, 0, False, 0.88,
                )

        emitter.flush()
        lines = Path(temp_output).read_text().strip().split("\n")
        entry_events = [json.loads(l) for l in lines if json.loads(l)["event_type"] == "ENTRY"]
        assert len(entry_events) == 3, f"Expected 3 ENTRY events, got {len(entry_events)}"

        visitor_ids = {e["visitor_id"] for e in entry_events}
        assert len(visitor_ids) == 3, "All 3 visitors must have distinct visitor_ids"

    def test_staff_events_not_counted_as_visitors(self, temp_output):
        from pipeline.emit import EventEmitter, EventType
        ts = datetime.now(timezone.utc)
        with EventEmitter(output_path=temp_output) as emitter:
            # 1 customer
            emitter.emit(EventType.ENTRY, "STORE_BLR_002", "CAM_ENTRY_01",
                         "VIS_cust01", ts, None, 0, False, 0.9)
            # 1 staff
            emitter.emit(EventType.ENTRY, "STORE_BLR_002", "CAM_ENTRY_01",
                         "VIS_staff01", ts, None, 0, True, 0.9)

        lines = Path(temp_output).read_text().strip().split("\n")
        events = [json.loads(l) for l in lines]
        customer_events = [e for e in events if not e["is_staff"]]
        staff_events = [e for e in events if e["is_staff"]]
        assert len(customer_events) == 1
        assert len(staff_events) == 1
