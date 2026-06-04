"""
translate_events.py — Translates real Purplle-format JSONL events into the
Apex Retail Intelligence API event schema.

The raw sample_events.jsonl uses field names from Purplle's actual system,
which differ from the challenge schema.  This script normalises them.

Raw field mapping:
  entry/exit events:
    id_token       → visitor_id   (prefixed VIS_)
    store_code     → store_id     (mapped: store_1076 → STORE_BLR_002 etc.)
    camera_id      → camera_id   (normalised: cam1 → CAM_ENTRY_01)
    event_timestamp→ timestamp
    event_type     → event_type   (entry→ENTRY, exit→EXIT)
    is_staff       → is_staff

  zone events:
    track_id       → visitor_id   (prefixed VIS_T)
    store_id       → store_id     (mapped: ST1076 → STORE_BLR_002)
    camera_id      → camera_id
    zone_id        → zone_id
    event_time     → timestamp
    event_type     → event_type   (zone_entered→ZONE_ENTER, zone_exited→ZONE_EXIT)

  queue events:
    track_id       → visitor_id
    store_id       → store_id
    camera_id      → camera_id
    zone_id        → zone_id
    queue_join_ts  → timestamp
    queue_position_at_join → metadata.queue_depth
    abandoned      → determines BILLING_QUEUE_JOIN vs BILLING_QUEUE_ABANDON

Usage:
    python pipeline/translate_events.py \\
        --input  data/raw_sample_events.jsonl \\
        --output data/sample_events.jsonl \\
        --store  STORE_BLR_002

    # Or ingest directly into the API:
    python pipeline/translate_events.py \\
        --input  data/raw_sample_events.jsonl \\
        --output data/sample_events.jsonl \\
        --store  STORE_BLR_002 \\
        --api    http://localhost:8000
"""

import argparse
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("translate_events")

# ─────────────────────────────────────────────────────────────────────────────
# Store code normalisation map
# (Purplle internal codes → challenge store IDs)
# ─────────────────────────────────────────────────────────────────────────────
STORE_CODE_MAP: dict[str, str] = {
    "store_1076": "STORE_BLR_002",
    "ST1076":     "STORE_BLR_002",
    "store_1008": "STORE_MUM_001",
    "ST1008":     "STORE_MUM_001",
    "store_1012": "STORE_DEL_001",
    "ST1012":     "STORE_DEL_001",
    "store_1021": "STORE_HYD_001",
    "ST1021":     "STORE_HYD_001",
    "store_1033": "STORE_CHE_001",
    "ST1033":     "STORE_CHE_001",
}


def normalise_store(raw: str, default: str) -> str:
    return STORE_CODE_MAP.get(str(raw), default)


def normalise_camera(camera_id: str, event_type: str) -> str:
    """Map raw camera IDs to the challenge camera ID format."""
    raw = str(camera_id).lower()
    if "entry" in event_type.lower() or raw in ("cam1", "cam_entry"):
        return "CAM_ENTRY_01"
    if "billing" in raw or "cam6" in raw or "cam5" in raw:
        return "CAM_BILLING_01"
    return f"CAM_FLOOR_{raw.replace('cam', '').zfill(2)}" if raw.startswith("cam") else camera_id.upper()


def normalise_timestamp(ts_str: str) -> str:
    """Ensure ISO-8601 UTC with Z suffix."""
    if not ts_str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ts_str = str(ts_str).replace(" ", "T")
    if ts_str.endswith("Z"):
        return ts_str[:19] + "Z"
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_event(
    event_type: str,
    store_id: str,
    camera_id: str,
    visitor_id: str,
    timestamp: str,
    zone_id: Optional[str] = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 0.88,
    queue_depth: Optional[int] = None,
    session_seq: int = 1,
) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": bool(is_staff),
        "confidence": round(confidence, 4),
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": zone_id,
            "session_seq": session_seq,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Per-event-type translators
# ─────────────────────────────────────────────────────────────────────────────

def translate_entry_exit(raw: dict, default_store: str) -> Optional[dict]:
    et = raw.get("event_type", "").lower()
    if et not in ("entry", "exit"):
        return None

    visitor_id = f"VIS_{raw.get('id_token', uuid.uuid4().hex[:6])}"
    store_id = normalise_store(raw.get("store_code", ""), default_store)
    camera_id = normalise_camera(raw.get("camera_id", "cam1"), et)
    timestamp = normalise_timestamp(raw.get("event_timestamp", ""))
    is_staff = bool(raw.get("is_staff", False))

    return make_event(
        event_type=et.upper(),
        store_id=store_id,
        camera_id=camera_id,
        visitor_id=visitor_id,
        timestamp=timestamp,
        zone_id=None,
        dwell_ms=0,
        is_staff=is_staff,
        confidence=0.88 if not raw.get("is_face_hidden") else 0.62,
    )


def translate_zone_event(raw: dict, default_store: str) -> Optional[dict]:
    et = raw.get("event_type", "").lower()
    if et not in ("zone_entered", "zone_exited"):
        return None

    track_id = raw.get("track_id", uuid.uuid4().hex[:6])
    visitor_id = f"VIS_T{track_id}"
    store_id = normalise_store(raw.get("store_id", ""), default_store)
    camera_id = normalise_camera(raw.get("camera_id", "CAM2"), et)
    timestamp = normalise_timestamp(raw.get("event_time", ""))
    zone_id = raw.get("zone_id") or raw.get("zone_name", "UNKNOWN_ZONE")

    api_event_type = "ZONE_ENTER" if et == "zone_entered" else "ZONE_EXIT"

    return make_event(
        event_type=api_event_type,
        store_id=store_id,
        camera_id=camera_id,
        visitor_id=visitor_id,
        timestamp=timestamp,
        zone_id=zone_id,
        dwell_ms=0,
        is_staff=False,
        confidence=0.85,
    )


def translate_queue_event(raw: dict, default_store: str) -> Optional[list[dict]]:
    et = raw.get("event_type", "").lower()
    if et not in ("queue_completed", "queue_abandoned"):
        return None

    track_id = raw.get("track_id", uuid.uuid4().hex[:6])
    visitor_id = f"VIS_T{track_id}"
    store_id = normalise_store(raw.get("store_id", ""), default_store)
    camera_id = normalise_camera(raw.get("camera_id", "CAM6"), et)
    zone_id = raw.get("zone_id") or "BILLING"
    queue_depth = raw.get("queue_position_at_join")
    join_ts = normalise_timestamp(raw.get("queue_join_ts", ""))
    exit_ts = normalise_timestamp(raw.get("queue_exit_ts", ""))
    abandoned = bool(raw.get("abandoned", False))

    wait_s = raw.get("wait_seconds", 0) or 0
    dwell_ms = int(wait_s * 1000)

    events = []

    # Always emit BILLING_QUEUE_JOIN
    events.append(make_event(
        event_type="BILLING_QUEUE_JOIN",
        store_id=store_id,
        camera_id=camera_id,
        visitor_id=visitor_id,
        timestamp=join_ts,
        zone_id=zone_id,
        dwell_ms=0,
        is_staff=False,
        confidence=0.91,
        queue_depth=queue_depth,
    ))

    if abandoned:
        # Emit BILLING_QUEUE_ABANDON at exit time
        events.append(make_event(
            event_type="BILLING_QUEUE_ABANDON",
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=visitor_id,
            timestamp=exit_ts,
            zone_id=zone_id,
            dwell_ms=dwell_ms,
            is_staff=False,
            confidence=0.91,
            queue_depth=queue_depth,
        ))

    return events


# ─────────────────────────────────────────────────────────────────────────────
# Main translator
# ─────────────────────────────────────────────────────────────────────────────

def translate_file(input_path: Path, output_path: Path, default_store: str) -> list[dict]:
    translated: list[dict] = []
    skipped = 0

    with open(input_path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("Line %d: JSON parse error: %s", i, e)
                skipped += 1
                continue

            et = raw.get("event_type", "").lower()

            if et in ("entry", "exit"):
                ev = translate_entry_exit(raw, default_store)
                if ev:
                    translated.append(ev)
            elif et in ("zone_entered", "zone_exited"):
                ev = translate_zone_event(raw, default_store)
                if ev:
                    translated.append(ev)
            elif et in ("queue_completed", "queue_abandoned"):
                evs = translate_queue_event(raw, default_store)
                if evs:
                    translated.extend(evs)
            else:
                logger.debug("Line %d: Unknown event_type=%r — skipping", i, et)
                skipped += 1

    # Write translated events
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        for ev in translated:
            f.write(json.dumps(ev, separators=(",", ":")) + "\n")

    logger.info(
        "Translated %d events → %s  (skipped: %d)",
        len(translated), output_path, skipped
    )
    return translated


def post_to_api(events: list[dict], api_url: str, batch_size: int = 500):
    total = len(events)
    accepted = 0
    for i in range(0, total, batch_size):
        batch = events[i:i + batch_size]
        try:
            r = requests.post(
                f"{api_url.rstrip('/')}/events/ingest",
                json={"events": batch},
                timeout=30,
            )
            r.raise_for_status()
            result = r.json()
            accepted += result.get("accepted", 0)
            logger.info("Batch %d: accepted=%d duplicate=%d rejected=%d",
                        i // batch_size + 1,
                        result.get("accepted", 0),
                        result.get("duplicate", 0),
                        result.get("rejected", 0))
        except requests.RequestException as e:
            logger.error("Batch %d failed: %s", i // batch_size + 1, e)
    logger.info("Done. %d / %d events accepted.", accepted, total)


def main():
    parser = argparse.ArgumentParser(
        description="Translate Purplle raw events → Apex Retail API schema"
    )
    parser.add_argument("--input",  required=True, help="Input raw JSONL file")
    parser.add_argument("--output", required=True, help="Output translated JSONL file")
    parser.add_argument("--store",  default="STORE_BLR_002",
                        help="Default store ID if not mappable from raw data")
    parser.add_argument("--api",    default=None,
                        help="If set, POST translated events to this API URL")
    args = parser.parse_args()

    events = translate_file(Path(args.input), Path(args.output), args.store)

    if args.api and events:
        logger.info("Posting %d events to %s …", len(events), args.api)
        post_to_api(events, args.api)


if __name__ == "__main__":
    main()
