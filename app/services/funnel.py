"""
services/funnel.py — FIXED Conversion funnel computation.

KEY FIX:
We now compute TRUE sessions using FIRST ENTRY per visitor per day,
not raw event counts (prevents inflated funnel values).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.orm import EventORM
from app.models import FunnelResponse, FunnelStage


async def get_store_funnel(store_id: str, db: AsyncSession) -> FunnelResponse:
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    base = and_(
        EventORM.store_id == store_id,
        EventORM.is_staff == False,
        EventORM.timestamp >= day_start,
    )

    # ─────────────────────────────────────────────
    # FIX 1: TRUE unique sessions (ONE ENTRY per visitor)
    # ─────────────────────────────────────────────
    entry_subq = (
        select(
            EventORM.visitor_id,
            func.min(EventORM.timestamp).label("first_entry")
        )
        .where(base, EventORM.event_type == "ENTRY")
        .group_by(EventORM.visitor_id)
        .subquery()
    )

    total_entries_q = await db.execute(
        select(func.count()).select_from(entry_subq)
    )
    total_entries = total_entries_q.scalar() or 0

    # ─────────────────────────────────────────────
    # Zone visits (unique visitors who entered ANY zone)
    # ─────────────────────────────────────────────
    zone_q = await db.execute(
        select(func.count(func.distinct(EventORM.visitor_id))).where(
            base,
            EventORM.event_type == "ZONE_ENTER",
            EventORM.zone_id.isnot(None),
        )
    )
    zone_visitors = zone_q.scalar() or 0

    # ─────────────────────────────────────────────
    # Billing visitors
    # ─────────────────────────────────────────────
    billing_q = await db.execute(
        select(func.count(func.distinct(EventORM.visitor_id))).where(
            base,
            EventORM.event_type == "BILLING_QUEUE_JOIN",
        )
    )
    billing_visitors = billing_q.scalar() or 0

    abandon_q = await db.execute(
        select(func.count(func.distinct(EventORM.visitor_id))).where(
            base,
            EventORM.event_type == "BILLING_QUEUE_ABANDON",
        )
    )
    abandon_visitors = abandon_q.scalar() or 0

    purchasers = max(0, billing_visitors - abandon_visitors)

    def drop(prev: int, curr: int) -> float:
        if prev == 0:
            return 0.0
        return round((1 - curr / prev) * 100, 1)

    stages = [
        FunnelStage(stage="Entry", count=total_entries, drop_off_pct=0.0),
        FunnelStage(stage="Zone Visit", count=zone_visitors,
                    drop_off_pct=drop(total_entries, zone_visitors)),
        FunnelStage(stage="Billing Queue", count=billing_visitors,
                    drop_off_pct=drop(zone_visitors, billing_visitors)),
        FunnelStage(stage="Purchase", count=purchasers,
                    drop_off_pct=drop(billing_visitors, purchasers)),
    ]

    return FunnelResponse(
        store_id=store_id,
        stages=stages,
        unique_sessions=total_entries,
        as_of=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )