import hashlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger("tracker")

# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TrackState:
    track_id: int
    visitor_id: str
    last_foot: tuple[float, float]
    last_seen: float          # wall clock seconds
    is_active: bool = True
    colour_hist: Optional[np.ndarray] = None
    traj: deque = field(default_factory=lambda: deque(maxlen=30))


@dataclass
class ExitRecord:
    visitor_id: str
    foot: tuple[float, float]
    exit_ts: datetime
    colour_hist: Optional[np.ndarray] = None


# ─────────────────────────────────────────────────────────────────────────────
# ByteTrack wrapper
# ─────────────────────────────────────────────────────────────────────────────

class ByteTrackWrapper:
    def __init__(self, fps: float = 15.0):
        self.fps = fps
        self._tracker = None
        self._iou_tracker = None
        self._init_tracker()

    def _init_tracker(self):
        try:
            import supervision as sv
            self._tracker = sv.ByteTrack(
                track_activation_threshold=0.25,
                lost_track_buffer=int(self.fps * 2),   # 2s buffer
                minimum_matching_threshold=0.8,
                frame_rate=int(self.fps),
            )
            logger.info("ByteTrack (supervision) initialised")
        except ImportError:
            logger.warning("supervision not available — using fallback IoU tracker")
            self._iou_tracker = _IoUTracker()

    def update(self, detections: list[dict], frame: np.ndarray) -> list[dict]:
        """
        Args:
            detections: [{"box": (x1,y1,x2,y2), "conf": float}, ...]
            frame: BGR numpy array

        Returns:
            [{"track_id": int, "box": (x1,y1,x2,y2), "conf": float}, ...]
        """
        if not detections:
            return []

        if self._tracker is not None:
            return self._sv_update(detections, frame)
        return self._iou_tracker.update(detections)

    def _sv_update(self, detections: list[dict], frame: np.ndarray) -> list[dict]:
        import supervision as sv
        boxes = np.array([d["box"] for d in detections], dtype=np.float32)
        confs = np.array([d["conf"] for d in detections], dtype=np.float32)
        class_ids = np.zeros(len(detections), dtype=int)

        sv_dets = sv.Detections(
            xyxy=boxes,
            confidence=confs,
            class_id=class_ids,
        )
        tracked = self._tracker.update_with_detections(sv_dets)
        results = []
        for i in range(len(tracked)):
            results.append({
                "track_id": int(tracked.tracker_id[i]),
                "box": tuple(map(int, tracked.xyxy[i])),
                "conf": float(tracked.confidence[i]),
            })
        return results


class _IoUTracker:
    def __init__(self, iou_threshold: float = 0.3, max_age: int = 30):
        self._next_id = 1
        self._tracks: dict[int, dict] = {}  # id → {box, age}
        self._iou_threshold = iou_threshold
        self._max_age = max_age

    def update(self, detections: list[dict]) -> list[dict]:
        if not detections:
            # Age out tracks
            dead = [tid for tid, t in self._tracks.items() if t["age"] > self._max_age]
            for tid in dead:
                del self._tracks[tid]
            return []

        det_boxes = [d["box"] for d in detections]
        matched_track_ids = {}

        # Match detections to existing tracks
        for tid, track in self._tracks.items():
            best_iou = 0
            best_di = -1
            for di, dbox in enumerate(det_boxes):
                if di in matched_track_ids.values():
                    continue
                iou = _iou(track["box"], dbox)
                if iou > best_iou:
                    best_iou = iou
                    best_di = di
            if best_iou >= self._iou_threshold and best_di >= 0:
                matched_track_ids[tid] = best_di
                track["box"] = det_boxes[best_di]
                track["age"] = 0

        # Age unmatched tracks
        unmatched_tids = [tid for tid in self._tracks if tid not in matched_track_ids]
        for tid in unmatched_tids:
            self._tracks[tid]["age"] += 1

        # Remove dead tracks
        dead = [tid for tid, t in self._tracks.items() if t["age"] > self._max_age]
        for tid in dead:
            del self._tracks[tid]

        # Create new tracks for unmatched detections
        matched_det_indices = set(matched_track_ids.values())
        for di, dbox in enumerate(det_boxes):
            if di not in matched_det_indices:
                self._tracks[self._next_id] = {"box": dbox, "age": 0}
                matched_track_ids[self._next_id] = di
                self._next_id += 1

        results = []
        for tid, di in matched_track_ids.items():
            results.append({
                "track_id": tid,
                "box": det_boxes[di],
                "conf": detections[di]["conf"],
            })
        return results


def _iou(a: tuple, b: tuple) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter == 0:
        return 0.0
    union = (ax2-ax1)*(ay2-ay1) + (bx2-bx1)*(by2-by1) - inter
    return inter / union


# ─────────────────────────────────────────────────────────────────────────────
# Re-ID Registry
# ─────────────────────────────────────────────────────────────────────────────

class ReIDRegistry:
    SPATIAL_REENTRY_RADIUS_PX = 120  # foot must be within this radius
    COLOUR_SIMILARITY_THRESHOLD = 0.65

    def __init__(self, cooldown_seconds: float = 120.0):
        self._cooldown = cooldown_seconds
        self._active: dict[int, TrackState] = {}       # track_id → state
        self._exited: list[ExitRecord] = []            # recently exited visitors
        self._visitor_to_track: dict[str, int] = {}

    def get_or_create(
        self,
        track_id: int,
        foot: tuple[float, float],
        ts: datetime,
        colour_hist: Optional[np.ndarray] = None,
    ) -> tuple[str, bool]:
        """
        Returns (visitor_id, is_reentry).
        """
        now = ts.timestamp()

        if track_id in self._active:
            state = self._active[track_id]
            state.last_foot = foot
            state.last_seen = now
            state.traj.append(foot)
            return state.visitor_id, False

        # New track — check if it matches an exited visitor
        visitor_id, is_reentry = self._match_exited(foot, now, colour_hist)

        if visitor_id is None:
            visitor_id = self._new_visitor_id(foot, now)
            is_reentry = False

        state = TrackState(
            track_id=track_id,
            visitor_id=visitor_id,
            last_foot=foot,
            last_seen=now,
            colour_hist=colour_hist,
        )
        state.traj.append(foot)
        self._active[track_id] = state
        self._visitor_to_track[visitor_id] = track_id
        return visitor_id, is_reentry

    def mark_exited(self, visitor_id: str, ts: datetime):
        """Call when an EXIT event is emitted for a visitor."""
        track_id = self._visitor_to_track.get(visitor_id)
        if track_id and track_id in self._active:
            state = self._active.pop(track_id)
            self._exited.append(ExitRecord(
                visitor_id=visitor_id,
                foot=state.last_foot,
                exit_ts=ts,
                colour_hist=state.colour_hist,
            ))
        # Prune old exit records
        cutoff = ts.timestamp() - self._cooldown
        self._exited = [e for e in self._exited if e.exit_ts.timestamp() > cutoff]

    def _match_exited(
        self,
        foot: tuple[float, float],
        now: float,
        colour_hist: Optional[np.ndarray],
    ) -> tuple[Optional[str], bool]:
        for record in self._exited:
            age = now - record.exit_ts.timestamp()
            if age > self._cooldown:
                continue
            dist = _euclidean(foot, record.foot)
            if dist > self.SPATIAL_REENTRY_RADIUS_PX:
                continue
            # Optional colour similarity check
            if colour_hist is not None and record.colour_hist is not None:
                sim = _hist_similarity(colour_hist, record.colour_hist)
                if sim < self.COLOUR_SIMILARITY_THRESHOLD:
                    continue
            return record.visitor_id, True
        return None, False

    @staticmethod
    def _new_visitor_id(foot: tuple[float, float], ts: float) -> str:
        raw = f"{foot[0]:.1f}:{foot[1]:.1f}:{ts:.3f}"
        h = hashlib.sha1(raw.encode()).hexdigest()[:6]
        return f"VIS_{h}"


def _euclidean(a: tuple[float, float], b: tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _hist_similarity(h1: np.ndarray, h2: np.ndarray) -> float:
    """Bhattacharyya-based similarity in [0, 1]."""
    try:
        return float(cv2.compareHist(h1.astype(np.float32), h2.astype(np.float32),
                                     cv2.HISTCMP_BHATTACHARYYA))
    except Exception:
        return 0.0
