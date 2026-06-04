"""
services/heatmap.py — Zone visit frequency and dwell heatmap.

Normalises visit frequency + avg dwell to 0–100 scale.
Flags low-confidence zones (fewer than 20 sessions).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.orm import EventORM
from app.models import HeatmapResponse, HeatmapZone

logger = logging.getLogger("service.heatmap")

LOW_CONFIDENCE_THRESHOLD = 20


async def get_store_heatmap(store_id: str, db: AsyncSession) -> HeatmapResponse:
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    rows = await db.execute(
        select(
            EventORM.zone_id,
            func.count(EventORM.id).label("visit_count"),
            func.avg(EventORM.dwell_ms).label("avg_dwell"),
        ).where(
            EventORM.store_id == store_id,
            EventORM.event_type.in_(["ZONE_ENTER", "ZONE_DWELL"]),
            EventORM.is_staff == False,
            EventORM.zone_id != None,
            EventORM.timestamp >= day_start,
        ).group_by(EventORM.zone_id)
    )
    data = rows.fetchall()

    if not data:
        return HeatmapResponse(store_id=store_id, zones=[], as_of=now.strftime("%Y-%m-%dT%H:%M:%SZ"))

    # Normalise
    max_visits = max(r.visit_count for r in data)
    max_dwell = max(r.avg_dwell or 0 for r in data)

    zones = []
    for r in data:
        freq_norm = (r.visit_count / max_visits * 100) if max_visits else 0
        dwell_norm = ((r.avg_dwell or 0) / max_dwell * 100) if max_dwell else 0
        score = round(freq_norm * 0.6 + dwell_norm * 0.4, 1)

        zones.append(HeatmapZone(
            zone_id=r.zone_id,
            visit_frequency=r.visit_count,
            avg_dwell_ms=round(r.avg_dwell or 0, 0),
            normalised_score=score,
            data_confidence="LOW" if r.visit_count < LOW_CONFIDENCE_THRESHOLD else "HIGH",
        ))

    zones.sort(key=lambda z: z.normalised_score, reverse=True)
    return HeatmapResponse(
        store_id=store_id,
        zones=zones,
        as_of=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
