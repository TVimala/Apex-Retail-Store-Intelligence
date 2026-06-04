"""
routers/stores.py — Store analytics endpoints.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models import (
    StoreMetrics, FunnelResponse, HeatmapResponse, AnomaliesResponse
)
from app.services.metrics import get_store_metrics
from app.services.funnel import get_store_funnel
from app.services.heatmap import get_store_heatmap
from app.services.anomalies import get_store_anomalies

logger = logging.getLogger("router.stores")
router = APIRouter()


@router.get("/{store_id}/metrics", response_model=StoreMetrics)
async def store_metrics(store_id: str, db: AsyncSession = Depends(get_db)):
    """
    Returns real-time metrics for today's session window.
    Excludes staff events. Handles zero-purchase stores gracefully.
    """
    try:
        return await get_store_metrics(store_id, db)
    except Exception as e:
        logger.exception("metrics error store=%s", store_id)
        raise HTTPException(status_code=500, detail={"error": "metrics_error", "store_id": store_id})


@router.get("/{store_id}/funnel", response_model=FunnelResponse)
async def store_funnel(store_id: str, db: AsyncSession = Depends(get_db)):
    """
    Conversion funnel: Entry → Zone Visit → Billing Queue → Purchase.
    Session is the unit; re-entries do not inflate visitor counts.
    """
    try:
        return await get_store_funnel(store_id, db)
    except Exception as e:
        logger.exception("funnel error store=%s", store_id)
        raise HTTPException(status_code=500, detail={"error": "funnel_error", "store_id": store_id})


@router.get("/{store_id}/heatmap", response_model=HeatmapResponse)
async def store_heatmap(store_id: str, db: AsyncSession = Depends(get_db)):
    """
    Zone visit frequency + avg dwell, normalised 0–100.
    Includes data_confidence flag for low-traffic zones.
    """
    try:
        return await get_store_heatmap(store_id, db)
    except Exception as e:
        logger.exception("heatmap error store=%s", store_id)
        raise HTTPException(status_code=500, detail={"error": "heatmap_error", "store_id": store_id})


@router.get("/{store_id}/anomalies", response_model=AnomaliesResponse)
async def store_anomalies(store_id: str, db: AsyncSession = Depends(get_db)):
    """
    Active operational anomalies with severity and suggested actions.
    """
    try:
        return await get_store_anomalies(store_id, db)
    except Exception as e:
        logger.exception("anomalies error store=%s", store_id)
        raise HTTPException(status_code=500, detail={"error": "anomalies_error", "store_id": store_id})
