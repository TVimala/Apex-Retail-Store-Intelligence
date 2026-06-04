"""
emit.py — Event schema definition and emitter for the Apex Retail pipeline.

Handles:
  - Event construction with UUID generation
  - ISO-8601 UTC timestamp formatting
  - JSONL file output (buffered, flushed on close)
  - Optional live HTTP POST to the Intelligence API
  - Session sequence tracking per visitor
"""

import json
import logging
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional
import threading

import requests

logger = logging.getLogger("emit")


class EventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


@dataclass
class EventMetadata:
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 0


@dataclass
class RetailEvent:
    event_id: str
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: str
    timestamp: str        # ISO-8601 UTC
    zone_id: Optional[str]
    dwell_ms: int
    is_staff: bool
    confidence: float
    metadata: dict

    def to_dict(self) -> dict:
        d = asdict(self)
        # Ensure confidence is rounded to avoid float noise
        d["confidence"] = round(d["confidence"], 4)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))


class EventEmitter:
    """
    Thread-safe event emitter.

    - Writes JSONL to output_path (one event per line).
    - Optionally POSTs batches to the Intelligence API.
    - Tracks per-visitor session sequence numbers.
    """

    BATCH_SIZE = 50   # POST to API in batches of this size
    FLUSH_INTERVAL_S = 5.0

    def __init__(self, output_path: str, api_url: Optional[str] = None):
        self._output_path = Path(output_path)
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._output_path, "a", buffering=1)  # line-buffered
        self._api_url = api_url
        self._batch: list[dict] = []
        self._lock = threading.Lock()
        self._session_seq: dict[str, int] = {}
        self._event_count = 0

        if api_url:
            logger.info("Live API ingest enabled → %s", api_url)

    @property
    def event_count(self) -> int:
        return self._event_count

    def emit(
        self,
        event_type: EventType,
        store_id: str,
        camera_id: str,
        visitor_id: str,
        timestamp: datetime,
        zone_id: Optional[str],
        dwell_ms: int,
        is_staff: bool,
        confidence: float,
        metadata: Optional[dict] = None,
    ) -> RetailEvent:
        with self._lock:
            seq = self._session_seq.get(visitor_id, 0) + 1
            self._session_seq[visitor_id] = seq

            extra_meta = metadata or {}
            extra_meta["session_seq"] = seq
            if zone_id and "sku_zone" not in extra_meta:
                extra_meta["sku_zone"] = zone_id
            if "queue_depth" not in extra_meta:
                extra_meta["queue_depth"] = None

            event = RetailEvent(
                event_id=str(uuid.uuid4()),
                store_id=store_id,
                camera_id=camera_id,
                visitor_id=visitor_id,
                event_type=event_type.value,
                timestamp=timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                zone_id=zone_id,
                dwell_ms=dwell_ms,
                is_staff=is_staff,
                confidence=round(confidence, 4),
                metadata=extra_meta,
            )

            self._file.write(event.to_json() + "\n")
            self._event_count += 1

            if self._api_url:
                self._batch.append(event.to_dict())
                if len(self._batch) >= self.BATCH_SIZE:
                    self._post_batch()

            return event

    def _post_batch(self):
        """POST current batch to the API. Called with lock held."""
        if not self._batch:
            return
        payload = {"events": self._batch}
        self._batch = []
        # Post in a background thread to not block detection
        thread = threading.Thread(
            target=self._do_post, args=(payload,), daemon=True
        )
        thread.start()

    def _do_post(self, payload: dict):
        try:
            r = requests.post(
                f"{self._api_url}/events/ingest",
                json=payload,
                timeout=10,
            )
            if r.status_code not in (200, 207):
                logger.warning("API ingest returned %d: %s", r.status_code, r.text[:200])
        except requests.RequestException as e:
            logger.error("API ingest failed: %s", e)

    def flush(self):
        with self._lock:
            if self._api_url and self._batch:
                self._post_batch()
            self._file.flush()

    def close(self):
        self.flush()
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
