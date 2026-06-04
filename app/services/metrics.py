"""
services/metrics.py — Real-time store metric computation (FIXED).

Fixes:
- Prevents inflated conversion rates
- Uses proper DISTINCT visitor logic
- Correct funnel-safe computation
- Removes negative/invalid rates
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List

from sqlalchemy import func, select, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.orm import EventORM
from app.models import StoreMetrics, ZoneDwellStat

logger = logging.getLogger("service.metrics")


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def safe_div(n: float, d: float) -> float:
    return round(n / d, 4) if d else 0.0


# ─────────────────────────────────────────────
# Main Metrics
# ─────────────────────────────────────────────

async def get_store_metrics(store_id: str, db: AsyncSession) -> StoreMetrics:
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # ── 1. Unique visitors (ALL customer touchpoints) ────────────────
    q = await db.execute(
        select(func.count(distinct(EventORM.visitor_id))).where(
            EventORM.store_id == store_id,
            EventORM.is_staff == False,
            EventORM.timestamp >= day_start,
            EventORM.event_type == "ENTRY",
        )
    )
    unique_visitors = q.scalar() or 0

    # ── 2. Billing visitors (who entered billing queue) ─────────────
    billing_q = await db.execute(
        select(func.count(distinct(EventORM.visitor_id))).where(
            EventORM.store_id == store_id,
            EventORM.is_staff == False,
            EventORM.event_type == "BILLING_QUEUE_JOIN",
            EventORM.timestamp >= day_start,
        )
    )
    billing_visitors = billing_q.scalar() or 0

    # ── 3. Abandonment visitors ──────────────────────────────────────
    abandon_q = await db.execute(
        select(func.count(distinct(EventORM.visitor_id))).where(
            EventORM.store_id == store_id,
            EventORM.is_staff == False,
            EventORM.event_type == "BILLING_QUEUE_ABANDON",
            EventORM.timestamp >= day_start,
        )
    )
    abandon_visitors = abandon_q.scalar() or 0

    # ── 4. Purchased visitors (SAFE LOGIC) ──────────────────────────
    # IMPORTANT: cannot exceed billing_visitors
    purchased_visitors = max(
        0,
        billing_visitors - abandon_visitors
    )

    # clamp to avoid fake inflation
    purchased_visitors = min(purchased_visitors, billing_visitors)

    conversion_rate = safe_div(purchased_visitors, unique_visitors)

    # ── 5. Zone dwell stats ─────────────────────────────────────────
    dwell_q = await db.execute(
        select(
            EventORM.zone_id,
            func.avg(EventORM.dwell_ms),
            func.count(EventORM.id),
        ).where(
            EventORM.store_id == store_id,
            EventORM.is_staff == False,
            EventORM.event_type == "ZONE_DWELL",
            EventORM.zone_id.is_not(None),
            EventORM.timestamp >= day_start,
        ).group_by(EventORM.zone_id)
    )

    zone_stats: List[ZoneDwellStat] = [
        ZoneDwellStat(
            zone_id=row[0],
            avg_dwell_ms=round(row[1] or 0, 0),
            visit_count=row[2],
        )
        for row in dwell_q.fetchall()
    ]

    # ── 6. Queue depth (latest snapshot) ────────────────────────────
    qd_q = await db.execute(
        select(EventORM.queue_depth).where(
            EventORM.store_id == store_id,
            EventORM.event_type == "BILLING_QUEUE_JOIN",
            EventORM.queue_depth.is_not(None),
        ).order_by(EventORM.timestamp.desc()).limit(1)
    )
    row = qd_q.fetchone()
    queue_depth_current = row[0] if row else 0

    # ── 7. Abandonment rate (SAFE) ──────────────────────────────────
    abandonment_rate = safe_div(abandon_visitors, billing_visitors)

    # ── 8. Transactions ─────────────────────────────────────────────
    total_transactions = purchased_visitors

    return StoreMetrics(
        store_id=store_id,
        unique_visitors=unique_visitors,
        conversion_rate=conversion_rate,
        avg_dwell_ms_per_zone=zone_stats,
        queue_depth_current=queue_depth_current,
        abandonment_rate=abandonment_rate,
        total_transactions=total_transactions,
        as_of=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )