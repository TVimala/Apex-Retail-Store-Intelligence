"""
models.py — Pydantic schemas for the Store Intelligence API.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────

class EventTypeEnum(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


class SeverityEnum(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


# ─────────────────────────────────────────────────────────────────────────────
# Inbound event schema
# ─────────────────────────────────────────────────────────────────────────────

class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 0


class RetailEventIn(BaseModel):
    event_id: str = Field(..., description="UUID-v4 string")
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: EventTypeEnum
    timestamp: str  # ISO-8601 UTC
    zone_id: Optional[str] = None
    dwell_ms: int = Field(default=0, ge=0)
    is_staff: bool = False
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, v: str) -> str:
        # Accept with or without Z suffix
        try:
            v_clean = v.replace("Z", "+00:00")
            datetime.fromisoformat(v_clean)
        except ValueError:
            raise ValueError(f"Invalid ISO-8601 timestamp: {v}")
        return v

    @field_validator("event_id")
    @classmethod
    def validate_uuid(cls, v: str) -> str:
        try:
            UUID(v)
        except ValueError:
            raise ValueError(f"event_id must be a valid UUID: {v}")
        return v

    @model_validator(mode="after")
    def zone_required_for_zone_events(self) -> RetailEventIn:
        zone_events = {
            EventTypeEnum.ZONE_ENTER,
            EventTypeEnum.ZONE_EXIT,
            EventTypeEnum.ZONE_DWELL,
            EventTypeEnum.BILLING_QUEUE_JOIN,
            EventTypeEnum.BILLING_QUEUE_ABANDON,
        }
        if self.event_type in zone_events and not self.zone_id:
            raise ValueError(f"zone_id is required for event_type={self.event_type}")
        return self


class IngestRequest(BaseModel):
    events: list[RetailEventIn] = Field(..., max_length=500)


# ─────────────────────────────────────────────────────────────────────────────
# API response schemas
# ─────────────────────────────────────────────────────────────────────────────

class IngestResponse(BaseModel):
    accepted: int
    rejected: int
    duplicate: int
    errors: list[dict] = []


class ZoneDwellStat(BaseModel):
    zone_id: str
    avg_dwell_ms: float
    visit_count: int


class StoreMetrics(BaseModel):
    store_id: str
    unique_visitors: int
    conversion_rate: float          # 0.0–1.0
    avg_dwell_ms_per_zone: list[ZoneDwellStat]
    queue_depth_current: int
    abandonment_rate: float         # 0.0–1.0
    total_transactions: int
    as_of: str                      # ISO-8601


class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float


class FunnelResponse(BaseModel):
    store_id: str
    stages: list[FunnelStage]
    unique_sessions: int
    as_of: str


class HeatmapZone(BaseModel):
    zone_id: str
    visit_frequency: int
    avg_dwell_ms: float
    normalised_score: float   # 0–100
    data_confidence: str      # "HIGH" | "LOW"


class HeatmapResponse(BaseModel):
    store_id: str
    zones: list[HeatmapZone]
    as_of: str


class Anomaly(BaseModel):
    anomaly_id: str
    anomaly_type: str
    severity: SeverityEnum
    description: str
    suggested_action: str
    detected_at: str
    zone_id: Optional[str] = None
    value: Optional[float] = None
    threshold: Optional[float] = None


class AnomaliesResponse(BaseModel):
    store_id: str
    anomalies: list[Anomaly]
    as_of: str


class StoreHealthStatus(BaseModel):
    store_id: str
    status: str   # "OK" | "STALE_FEED" | "NO_DATA"
    last_event_at: Optional[str]
    lag_seconds: Optional[float]
    warning: Optional[str] = None


class HealthResponse(BaseModel):
    service: str
    status: str   # "healthy" | "degraded" | "unhealthy"
    database: str
    stores: list[StoreHealthStatus]
    as_of: str
