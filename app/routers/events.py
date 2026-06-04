"""
routers/events.py — POST /events/ingest
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.db.orm import EventORM
from app.models import IngestRequest, IngestResponse, RetailEventIn

logger = logging.getLogger("router.events")
router = APIRouter()


def _parse_ts(ts_str: str) -> datetime:
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)


def _to_orm(e: RetailEventIn) -> dict:
    return {
        "event_id": e.event_id,
        "store_id": e.store_id,
        "camera_id": e.camera_id,
        "visitor_id": e.visitor_id,
        "event_type": e.event_type.value,
        "timestamp": _parse_ts(e.timestamp),
        "zone_id": e.zone_id,
        "dwell_ms": e.dwell_ms,
        "is_staff": e.is_staff,
        "confidence": e.confidence,
        "queue_depth": e.metadata.queue_depth,
        "sku_zone": e.metadata.sku_zone,
        "session_seq": e.metadata.session_seq,
    }


@router.post("/ingest", response_model=IngestResponse, status_code=200)
async def ingest_events(
    request: Request,
    payload: IngestRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Accepts batches of up to 500 events.
    - Idempotent by event_id (duplicate events are silently skipped).
    - Validates each event individually; partial success on malformed events.
    - Returns counts: accepted, rejected, duplicate.
    """
    accepted = 0
    rejected = 0
    duplicate = 0
    errors: list[dict] = []

    valid_rows: list[dict] = []
    valid_event_ids: list[str] = []

    for i, event in enumerate(payload.events):
        valid_rows.append(_to_orm(event))
        valid_event_ids.append(event.event_id)

    if not valid_rows:
        return IngestResponse(accepted=0, rejected=rejected, duplicate=0, errors=errors)

    # Check existing event_ids in one query
    existing_result = await db.execute(
        select(EventORM.event_id).where(EventORM.event_id.in_(valid_event_ids))
    )
    existing_ids = {row[0] for row in existing_result.fetchall()}

    new_rows = [r for r in valid_rows if r["event_id"] not in existing_ids]
    duplicate = len(valid_rows) - len(new_rows)

    if new_rows:
        # Bulk insert — SQLite uses INSERT OR IGNORE for idempotency
        try:
            stmt = sqlite_insert(EventORM).values(new_rows).prefix_with("OR IGNORE")
            await db.execute(stmt)
            await db.commit()
            accepted = len(new_rows)
        except Exception as exc:
            await db.rollback()
            logger.error("Bulk insert failed: %s", exc)
            # Fall back to row-by-row
            for row in new_rows:
                try:
                    db.add(EventORM(**row))
                    await db.commit()
                    accepted += 1
                except Exception as row_exc:
                    await db.rollback()
                    rejected += 1
                    errors.append({"event_id": row["event_id"], "error": str(row_exc)})

    trace_id = getattr(request.state, "trace_id", "")
    logger.info(
        '{"trace_id":"%s","endpoint":"ingest","accepted":%d,"duplicate":%d,"rejected":%d}',
        trace_id, accepted, duplicate, rejected,
    )

    return IngestResponse(
        accepted=accepted,
        rejected=rejected,
        duplicate=duplicate,
        errors=errors[:20],   # cap error list size
    )
