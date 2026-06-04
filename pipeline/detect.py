import argparse
import json
import logging
import sys
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

import cv2
import numpy as np

from tracker import ByteTrackWrapper, ReIDRegistry
from emit import EventEmitter, EventType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("detect")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
YOLO_CONFIDENCE_THRESHOLD = 0.35   # emit low-conf events but flag them
STAFF_COLOUR_THRESHOLD = 0.72      # colour-histogram similarity score
DWELL_INTERVAL_SECONDS = 30        # emit ZONE_DWELL every N seconds
REENTRY_COOLDOWN_SECONDS = 120     # grace window after EXIT before REENTRY
BILLING_QUEUE_MIN_DEPTH = 1        # minimum queue depth to emit QUEUE_JOIN
ENTRY_LINE_MARGIN_PX = 40          # pixels either side of the entry line


def load_layout(layout_path: str) -> dict:
    with open(layout_path) as f:
        return json.load(f)


def get_store_config(layout: dict, store_id: str) -> dict:
    stores = layout if isinstance(layout, list) else layout.get("stores", [layout])
    for s in stores:
        if s.get("store_id") == store_id:
            return s
    # Fallback: return the first store
    logger.warning("store_id %s not found in layout — using first entry", store_id)
    return stores[0] if stores else {}


def polygon_contains(polygon: list[list[float]], cx: float, cy: float) -> bool:
    """Ray-casting point-in-polygon test."""
    n = len(polygon)
    inside = False
    px, py = cx, cy
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def foot_point(box: tuple[int, int, int, int]) -> tuple[float, float]:
    """Return bottom-centre of bounding box as (cx, cy)."""
    x1, y1, x2, y2 = box
    return (x1 + x2) / 2.0, float(y2)


def classify_zone(foot: tuple[float, float], zones: list[dict]) -> str | None:
    for zone in zones:
        poly = zone.get("polygon") or zone.get("boundary")
        if poly and polygon_contains(poly, foot[0], foot[1]):
            return zone["zone_id"]
    return None


def is_staff_by_colour(frame: np.ndarray, box: tuple) -> tuple[bool, float]:
    x1, y1, x2, y2 = box
    h = y2 - y1
    # Crop torso (middle third vertically)
    torso_y1 = y1 + h // 3
    torso_y2 = y1 + 2 * h // 3
    torso = frame[torso_y1:torso_y2, x1:x2]
    if torso.size == 0:
        return False, 0.0

    hsv = cv2.cvtColor(torso, cv2.COLOR_BGR2HSV)
    # Dark colours: low Value channel
    v_channel = hsv[:, :, 2]
    dark_ratio = float(np.sum(v_channel < 80) / max(v_channel.size, 1))

    # Saturation: uniforms are usually low-saturation (grey/navy/black)
    s_channel = hsv[:, :, 1]
    low_sat_ratio = float(np.sum(s_channel < 60) / max(s_channel.size, 1))

    score = (dark_ratio * 0.6 + low_sat_ratio * 0.4)
    return score >= STAFF_COLOUR_THRESHOLD, round(score, 3)


class EntryExitClassifier:
    """
    Determines ENTRY vs EXIT by tracking vertical crossing of an entry line.
    The entry line is defined in store_layout.json per camera.
    Falls back to top-25%-of-frame heuristic for cameras without an explicit line.
    """

    def __init__(self, entry_line_y: float | None, frame_height: int):
        self.line_y = entry_line_y if entry_line_y is not None else frame_height * 0.25
        self._prev_y: dict[int, float] = {}  # track_id → prev foot_y

    def classify(self, track_id: int, foot_y: float) -> str | None:
        """Return 'ENTRY', 'EXIT', or None."""
        prev = self._prev_y.get(track_id)
        self._prev_y[track_id] = foot_y
        if prev is None:
            return None
        crossed = (prev < self.line_y <= foot_y) or (prev > self.line_y >= foot_y)
        if not crossed:
            return None
        return "ENTRY" if foot_y > prev else "EXIT"


class ZoneDwellTracker:
    """Tracks continuous zone dwell per visitor, emitting ZONE_DWELL every 30 s."""

    def __init__(self):
        # visitor_id → {zone_id: (enter_ts, last_dwell_emit_ts)}
        self._state: dict[str, dict[str, tuple[float, float]]] = {}

    def update(self, visitor_id: str, zone_id: str | None, now_ts: float):
        vstate = self._state.setdefault(visitor_id, {})

        # Exit all zones the visitor is no longer in
        for z in list(vstate.keys()):
            if z != zone_id:
                del vstate[z]

        if zone_id is None:
            return []

        events = []
        if zone_id not in vstate:
            vstate[zone_id] = (now_ts, now_ts)
        else:
            enter_ts, last_emit_ts = vstate[zone_id]
            elapsed_since_emit = now_ts - last_emit_ts
            if elapsed_since_emit >= DWELL_INTERVAL_SECONDS:
                total_dwell_ms = int((now_ts - enter_ts) * 1000)
                events.append((zone_id, total_dwell_ms))
                vstate[zone_id] = (enter_ts, now_ts)
        return events


class BillingQueueMonitor:
    """Estimates queue depth from the count of people in the billing zone."""

    def __init__(self):
        self._in_billing: dict[str, float] = {}  # visitor_id → enter_ts
        self._depth = 0

    @property
    def depth(self) -> int:
        return len(self._in_billing)

    def enter(self, visitor_id: str, ts: float):
        self._in_billing[visitor_id] = ts

    def leave(self, visitor_id: str):
        self._in_billing.pop(visitor_id, None)

    def snapshot(self) -> dict[str, float]:
        return dict(self._in_billing)


# ─────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────────────────────

def process_clip(
    clip_path: str,
    store_id: str,
    camera_id: str,
    layout: dict,
    emitter: EventEmitter,
    replay_speed: float = 0.0,  # 0 = as-fast-as-possible
    clip_start_utc: datetime | None = None,
):
    """
    Core processing loop.

    Args:
        clip_path: Path to the MP4/AVI clip.
        store_id: e.g. 'STORE_BLR_002'
        camera_id: e.g. 'CAM_ENTRY_01'
        layout: Parsed store_layout.json
        emitter: EventEmitter instance
        replay_speed: Real-time multiplier (1.0 = real time, 0 = max speed)
        clip_start_utc: UTC datetime for frame 0; defaults to now.
    """
    store_cfg = get_store_config(layout, store_id)
    zones = store_cfg.get("zones", [])
    cameras = store_cfg.get("cameras", [])

    # Find camera-specific entry line
    cam_cfg = next((c for c in cameras if c.get("camera_id") == camera_id), {})
    entry_line_y = cam_cfg.get("entry_line_y")
    is_entry_camera = cam_cfg.get("type", "").upper() in ("ENTRY", "ENTRY_EXIT")

    cap = cv2.VideoCapture(clip_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open clip: {clip_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    logger.info(
        "Opened %s | %dx%d @%.1f fps | %d frames (~%.1f min)",
        clip_path, frame_width, frame_height, fps, total_frames, total_frames / fps / 60,
    )

    # Lazy import to avoid hard dependency crash if ultralytics not installed
    try:
        from ultralytics import YOLO
        model = YOLO("yolov8n.pt")
        logger.info("YOLOv8n loaded")
    except ImportError:
        logger.error("ultralytics not installed — install with: pip install ultralytics")
        raise

    tracker = ByteTrackWrapper(fps=fps)
    reid = ReIDRegistry(cooldown_seconds=REENTRY_COOLDOWN_SECONDS)
    entry_classifier = EntryExitClassifier(entry_line_y, frame_height)
    dwell_tracker = ZoneDwellTracker()
    billing_monitor = BillingQueueMonitor()

    clip_start = clip_start_utc or datetime.now(timezone.utc)
    frame_idx = 0
    visitor_zone_prev: dict[str, str | None] = {}

    logger.info("Processing %s …", camera_id)
    t_wall_start = time.perf_counter()

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_ts: float = frame_idx / fps
        utc_ts = clip_start + timedelta(seconds=frame_ts)
        frame_idx += 1

        # ── Detection ────────────────────────────────────────────────────────
        results = model(frame, classes=[0], verbose=False)  # class 0 = person
        detections = []
        for r in results:
            for box in r.boxes:
                conf = float(box.conf[0])
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                detections.append({"box": (x1, y1, x2, y2), "conf": conf})

        # ── Tracking ─────────────────────────────────────────────────────────
        tracked = tracker.update(detections, frame)  # list of {track_id, box, conf}

        for t in tracked:
            track_id = t["track_id"]
            box = t["box"]
            conf = t["conf"]
            foot = foot_point(box)

            # ── Staff classification ──────────────────────────────────────
            staff_flag, staff_score = is_staff_by_colour(frame, box)

            # ── Re-ID ─────────────────────────────────────────────────────
            visitor_id, is_reentry = reid.get_or_create(track_id, foot, utc_ts)

            # ── Zone classification ───────────────────────────────────────
            current_zone = classify_zone(foot, zones)
            prev_zone = visitor_zone_prev.get(visitor_id)

            # ── Entry / Exit ──────────────────────────────────────────────
            if is_entry_camera:
                crossing = entry_classifier.classify(track_id, foot[1])
                if crossing == "ENTRY":
                    if is_reentry:
                        emitter.emit(
                            EventType.REENTRY, store_id, camera_id, visitor_id,
                            utc_ts, zone_id=None, dwell_ms=0,
                            is_staff=staff_flag, confidence=conf,
                        )
                    else:
                        emitter.emit(
                            EventType.ENTRY, store_id, camera_id, visitor_id,
                            utc_ts, zone_id=None, dwell_ms=0,
                            is_staff=staff_flag, confidence=conf,
                        )
                elif crossing == "EXIT":
                    emitter.emit(
                        EventType.EXIT, store_id, camera_id, visitor_id,
                        utc_ts, zone_id=None, dwell_ms=0,
                        is_staff=staff_flag, confidence=conf,
                    )
                    reid.mark_exited(visitor_id, utc_ts)

            # ── Zone transitions ──────────────────────────────────────────
            if current_zone != prev_zone:
                if prev_zone is not None:
                    emitter.emit(
                        EventType.ZONE_EXIT, store_id, camera_id, visitor_id,
                        utc_ts, zone_id=prev_zone, dwell_ms=0,
                        is_staff=staff_flag, confidence=conf,
                    )
                    if prev_zone.upper() in ("BILLING", "BILLING_COUNTER", "CHECKOUT"):
                        billing_monitor.leave(visitor_id)

                if current_zone is not None:
                    emitter.emit(
                        EventType.ZONE_ENTER, store_id, camera_id, visitor_id,
                        utc_ts, zone_id=current_zone, dwell_ms=0,
                        is_staff=staff_flag, confidence=conf,
                    )
                    if current_zone.upper() in ("BILLING", "BILLING_COUNTER", "CHECKOUT"):
                        billing_monitor.enter(visitor_id, frame_ts)
                        if billing_monitor.depth > BILLING_QUEUE_MIN_DEPTH:
                            emitter.emit(
                                EventType.BILLING_QUEUE_JOIN, store_id, camera_id,
                                visitor_id, utc_ts, zone_id=current_zone, dwell_ms=0,
                                is_staff=staff_flag, confidence=conf,
                                metadata={"queue_depth": billing_monitor.depth},
                            )

                visitor_zone_prev[visitor_id] = current_zone

            # ── Dwell events ──────────────────────────────────────────────
            if current_zone:
                dwell_events = dwell_tracker.update(visitor_id, current_zone, frame_ts)
                for dzone, dwell_ms in dwell_events:
                    emitter.emit(
                        EventType.ZONE_DWELL, store_id, camera_id, visitor_id,
                        utc_ts, zone_id=dzone, dwell_ms=dwell_ms,
                        is_staff=staff_flag, confidence=conf,
                    )

        # ── Replay speed throttle ─────────────────────────────────────────
        if replay_speed > 0:
            elapsed = time.perf_counter() - t_wall_start
            expected = frame_ts / replay_speed
            sleep_for = expected - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

        if frame_idx % 450 == 0:  # log every 30s at 15fps
            logger.info("  Frame %d / %d (%.1f%%)", frame_idx, total_frames,
                        100 * frame_idx / max(total_frames, 1))

    cap.release()
    elapsed_total = time.perf_counter() - t_wall_start
    logger.info(
        "Done — %d frames in %.1fs (%.1f fps throughput) | %d events emitted",
        frame_idx, elapsed_total, frame_idx / elapsed_total, emitter.event_count,
    )


def main():
    parser = argparse.ArgumentParser(description="Apex Retail CCTV Detection Pipeline")
    parser.add_argument("--clip", required=True, help="Path to CCTV clip")
    parser.add_argument("--store", required=True, help="Store ID e.g. STORE_BLR_002")
    parser.add_argument("--camera", required=True, help="Camera ID e.g. CAM_ENTRY_01")
    parser.add_argument("--layout", required=True, help="Path to store_layout.json")
    parser.add_argument("--output", required=True, help="Output JSONL file for events")
    parser.add_argument("--api-url", default=None, help="If set, POST events to this URL too")
    parser.add_argument("--replay-speed", type=float, default=0.0,
                        help="Real-time multiplier (1.0=real time, 0=max speed)")
    parser.add_argument("--clip-start-utc", default=None,
                        help="ISO-8601 UTC timestamp for frame 0 (default: now)")
    args = parser.parse_args()

    layout = load_layout(args.layout)
    clip_start = (
        datetime.fromisoformat(args.clip_start_utc.replace("Z", "+00:00"))
        if args.clip_start_utc else None
    )

    emitter = EventEmitter(output_path=args.output, api_url=args.api_url)

    try:
        process_clip(
            clip_path=args.clip,
            store_id=args.store,
            camera_id=args.camera,
            layout=layout,
            emitter=emitter,
            replay_speed=args.replay_speed,
            clip_start_utc=clip_start,
        )
    finally:
        emitter.flush()
        logger.info("Events written to %s", args.output)


if __name__ == "__main__":
    main()
