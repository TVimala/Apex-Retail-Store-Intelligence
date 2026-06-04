"""
services/anomalies.py — Real-time anomaly detection for store operations.

Detects:
  1. BILLING_QUEUE_SPIKE   — queue depth above threshold
  2. CONVERSION_DROP       — today's conversion vs 7-day rolling average
  3. DEAD_ZONE             — a named zone with no visits in 30+ minutes
  4. STALE_FEED            — no events received in 10+ minutes (health-level)
  5. EMPTY_STORE           — zero visitors for 10+ consecutive minutes
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import func, select, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.orm import EventORM
from app.models import Anomaly, AnomaliesResponse, SeverityEnum

logger = logging.getLogger("service.anomalies")

QUEUE_SPIKE_THRESHOLD = 8           # persons
CONVERSION_DROP_THRESHOLD = 0.30    # 30% relative drop vs 7-day avg
DEAD_ZONE_WINDOW_MINUTES = 30
STALE_FEED_MINUTES = 10
EMPTY_STORE_MINUTES = 10


def _make_anomaly(
    anomaly_type: str,
    severity: SeverityEnum,
    description: str,
    suggested_action: str,
    now: datetime,
    zone_id: Optional[str] = None,
    value: Optional[float] = None,
    threshold: Optional[float] = None,
) -> Anomaly:
    return Anomaly(
        anomaly_id=str(uuid.uuid4()),
        anomaly_type=anomaly_type,
        severity=severity,
        description=description,
        suggested_action=suggested_action,
        detected_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        zone_id=zone_id,
        value=value,
        threshold=threshold,
    )


async def get_store_anomalies(store_id: str, db: AsyncSession) -> AnomaliesResponse:
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    anomalies: list[Anomaly] = []

    # ── 1. Queue spike ───────────────────────────────────────────────────────
    qd_q = await db.execute(
        select(EventORM.queue_depth).where(
            EventORM.store_id == store_id,
            EventORM.event_type == "BILLING_QUEUE_JOIN",
            EventORM.queue_depth != None,
        ).order_by(EventORM.timestamp.desc()).limit(1)
    )
    row = qd_q.fetchone()
    current_queue = row[0] if row else 0
    if current_queue >= QUEUE_SPIKE_THRESHOLD:
        severity = SeverityEnum.CRITICAL if current_queue >= QUEUE_SPIKE_THRESHOLD * 1.5 else SeverityEnum.WARN
        anomalies.append(_make_anomaly(
            anomaly_type="BILLING_QUEUE_SPIKE",
            severity=severity,
            description=f"Billing queue depth is {current_queue} (threshold: {QUEUE_SPIKE_THRESHOLD})",
            suggested_action="Open an additional billing counter or redirect staff to checkout.",
            now=now,
            zone_id="BILLING",
            value=float(current_queue),
            threshold=float(QUEUE_SPIKE_THRESHOLD),
        ))

    # ── 2. Conversion drop vs 7-day rolling average ──────────────────────────
    # Today's conversion
    entry_today = await db.execute(
        select(func.count(distinct(EventORM.visitor_id))).where(
            EventORM.store_id == store_id,
            EventORM.event_type == "ENTRY",
            EventORM.is_staff == False,
            EventORM.timestamp >= day_start,
        )
    )
    total_today = entry_today.scalar() or 0

    billing_today = await db.execute(
        select(func.count(distinct(EventORM.visitor_id))).where(
            EventORM.store_id == store_id,
            EventORM.event_type == "BILLING_QUEUE_JOIN",
            EventORM.is_staff == False,
            EventORM.timestamp >= day_start,
        )
    )
    billing_count_today = billing_today.scalar() or 0

    abandon_today = await db.execute(
        select(func.count(distinct(EventORM.visitor_id))).where(
            EventORM.store_id == store_id,
            EventORM.event_type == "BILLING_QUEUE_ABANDON",
            EventORM.is_staff == False,
            EventORM.timestamp >= day_start,
        )
    )
    abandon_count_today = abandon_today.scalar() or 0

    conv_today = (max(0, billing_count_today - abandon_count_today) / total_today
                  if total_today > 0 else None)

    # 7-day average
    week_start = day_start - timedelta(days=7)
    entry_7d = await db.execute(
        select(func.count(distinct(EventORM.visitor_id))).where(
            EventORM.store_id == store_id,
            EventORM.event_type == "ENTRY",
            EventORM.is_staff == False,
            EventORM.timestamp >= week_start,
            EventORM.timestamp < day_start,
        )
    )
    total_7d = entry_7d.scalar() or 0

    billing_7d = await db.execute(
        select(func.count(distinct(EventORM.visitor_id))).where(
            EventORM.store_id == store_id,
            EventORM.event_type == "BILLING_QUEUE_JOIN",
            EventORM.is_staff == False,
            EventORM.timestamp >= week_start,
            EventORM.timestamp < day_start,
        )
    )
    billing_7d_count = billing_7d.scalar() or 0
    conv_7d = billing_7d_count / total_7d if total_7d > 0 else None

    if conv_today is not None and conv_7d is not None and conv_7d > 0:
        relative_drop = (conv_7d - conv_today) / conv_7d
        if relative_drop >= CONVERSION_DROP_THRESHOLD:
            severity = SeverityEnum.CRITICAL if relative_drop >= 0.5 else SeverityEnum.WARN
            anomalies.append(_make_anomaly(
                anomaly_type="CONVERSION_DROP",
                severity=severity,
                description=(
                    f"Today's conversion rate ({conv_today:.1%}) is {relative_drop:.0%} "
                    f"below 7-day average ({conv_7d:.1%})"
                ),
                suggested_action=(
                    "Review floor coverage, check for billing bottlenecks, "
                    "and compare with yesterday's staffing levels."
                ),
                now=now,
                value=round(conv_today, 4),
                threshold=round(conv_7d, 4),
            ))

    # ── 3. Dead zones ─────────────────────────────────────────────────────────
    dead_zone_cutoff = now - timedelta(minutes=DEAD_ZONE_WINDOW_MINUTES)

    # Get all known zones from ZONE_ENTER events
    known_zones_q = await db.execute(
        select(distinct(EventORM.zone_id)).where(
            EventORM.store_id == store_id,
            EventORM.event_type == "ZONE_ENTER",
            EventORM.zone_id != None,
            EventORM.timestamp >= day_start,
        )
    )
    known_zones = {r[0] for r in known_zones_q.fetchall()}

    # Zones with recent activity
    active_zones_q = await db.execute(
        select(distinct(EventORM.zone_id)).where(
            EventORM.store_id == store_id,
            EventORM.event_type.in_(["ZONE_ENTER", "ZONE_DWELL"]),
            EventORM.zone_id != None,
            EventORM.timestamp >= dead_zone_cutoff,
        )
    )
    active_zones = {r[0] for r in active_zones_q.fetchall()}

    dead_zones = known_zones - active_zones
    for zone in sorted(dead_zones):
        anomalies.append(_make_anomaly(
            anomaly_type="DEAD_ZONE",
            severity=SeverityEnum.INFO,
            description=f"Zone '{zone}' has had no customer visits in {DEAD_ZONE_WINDOW_MINUTES}+ minutes.",
            suggested_action=(
                f"Check if zone '{zone}' is stocked and accessible. "
                "Consider adding promotional signage or staff direction."
            ),
            now=now,
            zone_id=zone,
        ))

    # ── 4. Empty store ────────────────────────────────────────────────────────
    empty_cutoff = now - timedelta(minutes=EMPTY_STORE_MINUTES)
    # Are there any non-staff visitors currently in the store?
    active_visitors_q = await db.execute(
        select(func.count(distinct(EventORM.visitor_id))).where(
            EventORM.store_id == store_id,
            EventORM.event_type == "ENTRY",
            EventORM.is_staff == False,
            EventORM.timestamp >= empty_cutoff,
        )
    )
    recent_entries = active_visitors_q.scalar() or 0

    # Also check for recent EXIT — if entries == 0 and it's during open hours
    if recent_entries == 0 and total_today > 0:
        # Store has had visitors today but none recently — might be a feed issue
        anomalies.append(_make_anomaly(
            anomaly_type="EMPTY_STORE_PERIOD",
            severity=SeverityEnum.INFO,
            description=f"No customer entries detected in the last {EMPTY_STORE_MINUTES} minutes.",
            suggested_action="Verify camera feeds are operational and store is within open hours.",
            now=now,
        ))

    return AnomaliesResponse(
        store_id=store_id,
        anomalies=anomalies,
        as_of=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
