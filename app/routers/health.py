"""
routers/health.py — GET /health
"""

import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db, check_db_health
from app.db.orm import EventORM
from app.models import HealthResponse, StoreHealthStatus

logger = logging.getLogger("router.health")
router = APIRouter()

STALE_FEED_THRESHOLD_MINUTES = 10


@router.get("/health", response_model=HealthResponse)
async def health(db: AsyncSession = Depends(get_db)):
    """
    Returns service health, DB status, and per-store feed freshness.
    STALE_FEED is raised if a store's most recent event is >10 min old.
    """
    now = datetime.now(timezone.utc)
    db_ok = await check_db_health()
    db_status = "healthy" if db_ok else "unhealthy"

    store_statuses: list[StoreHealthStatus] = []

    if db_ok:
        # Get last event timestamp per store
        rows = await db.execute(
            select(
                EventORM.store_id,
                func.max(EventORM.timestamp).label("last_event"),
            ).group_by(EventORM.store_id)
        )
        for row in rows.fetchall():
            store_id = row.store_id
            last_ts: datetime = row.last_event
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)

            lag_seconds = (now - last_ts).total_seconds()
            is_stale = lag_seconds > STALE_FEED_THRESHOLD_MINUTES * 60

            store_statuses.append(StoreHealthStatus(
                store_id=store_id,
                status="STALE_FEED" if is_stale else "OK",
                last_event_at=last_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                lag_seconds=round(lag_seconds, 1),
                warning="Feed lag exceeds 10 minutes — check pipeline" if is_stale else None,
            ))

    overall = "healthy" if (db_ok and all(s.status == "OK" for s in store_statuses)) else "degraded"
    if not db_ok:
        overall = "unhealthy"

    return HealthResponse(
        service="apex-retail-intelligence",
        status=overall,
        database=db_status,
        stores=store_statuses,
        as_of=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
