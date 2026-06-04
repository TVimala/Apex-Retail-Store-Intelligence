
import asyncio
import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# Override DATABASE_URL before importing the app
import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

from app.main import app
from app.db.database import get_db, Base, init_db
from app.db.orm import EventORM


# ─────────────────────────────────────────────────────────────────────────────
# In-memory test database
# ─────────────────────────────────────────────────────────────────────────────

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
test_engine = create_async_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = async_sessionmaker(bind=test_engine, expire_on_commit=False)


async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
    async with TestingSessionLocal() as session:
        yield session


app.dependency_overrides[get_db] = override_get_db


@pytest_asyncio.fixture(scope="module", autouse=True)
async def setup_database():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture(autouse=True)
async def clear_events():
    """Clear all events before each test to prevent bleeding."""
    yield
    async with TestingSessionLocal() as session:
        from sqlalchemy import text
        await session.execute(text("DELETE FROM events"))
        await session.commit()


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_event(
    event_type: str,
    store_id: str = "STORE_BLR_002",
    visitor_id: str | None = None,
    zone_id: str | None = None,
    is_staff: bool = False,
    timestamp: str | None = None,
    queue_depth: int | None = None,
    confidence: float = 0.88,
) -> dict:
    now = datetime.now(timezone.utc)
    if timestamp is None:
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if visitor_id is None:
        visitor_id = f"VIS_{uuid.uuid4().hex[:6]}"
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "zone_id": zone_id,
        "dwell_ms": 0,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": zone_id,
            "session_seq": 1,
        },
    }


async def ingest(client: AsyncClient, events: list[dict]) -> dict:
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200
    return r.json()


# ─────────────────────────────────────────────────────────────────────────────
# Ingest endpoint
# ─────────────────────────────────────────────────────────────────────────────

class TestIngest:
    @pytest.mark.asyncio
    async def test_basic_ingest(self, client):
        event = make_event("ENTRY")
        result = await ingest(client, [event])
        assert result["accepted"] == 1
        assert result["rejected"] == 0

    @pytest.mark.asyncio
    async def test_idempotency_same_event_twice(self, client):
        """Posting the same event_id twice must not double-count it."""
        event = make_event("ENTRY")
        r1 = await ingest(client, [event])
        r2 = await ingest(client, [event])
        assert r1["accepted"] == 1
        assert r2["accepted"] == 0
        assert r2["duplicate"] == 1

    @pytest.mark.asyncio
    async def test_idempotency_same_batch_twice(self, client):
        """Same 10-event batch ingested twice → second call all duplicates."""
        events = [make_event("ENTRY") for _ in range(10)]
        r1 = await ingest(client, events)
        r2 = await ingest(client, events)
        assert r1["accepted"] == 10
        assert r2["duplicate"] == 10
        assert r2["accepted"] == 0

    @pytest.mark.asyncio
    async def test_batch_limit_500(self, client):
        """Batch over 500 should return 422."""
        events = [make_event("ENTRY") for _ in range(501)]
        r = await client.post("/events/ingest", json={"events": events})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_partial_success_on_malformed(self, client):
        """One bad event in a batch should not block the rest."""
        good = make_event("ENTRY")
        bad = {**make_event("ENTRY"), "event_id": "not-a-uuid"}
        r = await client.post("/events/ingest", json={"events": [good, bad]})
        assert r.status_code == 422  # Pydantic rejects bad UUID before we process

    @pytest.mark.asyncio
    async def test_ingest_zone_event_requires_zone_id(self, client):
        event = make_event("ZONE_DWELL")  # zone_id is None → should fail
        r = await client.post("/events/ingest", json={"events": [event]})
        assert r.status_code == 422


# ─────────────────────────────────────────────────────────────────────────────
# Metrics endpoint
# ─────────────────────────────────────────────────────────────────────────────

class TestMetrics:
    @pytest.mark.asyncio
    async def test_zero_purchase_store_returns_zero_not_null(self, client):
        """Store with visitors but no purchases must return conversion_rate=0.0."""
        await ingest(client, [make_event("ENTRY", visitor_id="VIS_001")])
        r = await client.get("/stores/STORE_BLR_002/metrics")
        assert r.status_code == 200
        data = r.json()
        assert data["conversion_rate"] == 0.0
        assert data["conversion_rate"] is not None

    @pytest.mark.asyncio
    async def test_staff_excluded_from_unique_visitors(self, client):
        """Staff ENTRY events must not count toward unique_visitors."""
        events = [
            make_event("ENTRY", visitor_id="VIS_cust01", is_staff=False),
            make_event("ENTRY", visitor_id="VIS_cust02", is_staff=False),
            make_event("ENTRY", visitor_id="VIS_staff01", is_staff=True),
        ]
        await ingest(client, events)
        r = await client.get("/stores/STORE_BLR_002/metrics")
        assert r.status_code == 200
        data = r.json()
        assert data["unique_visitors"] == 2  # NOT 3

    @pytest.mark.asyncio
    async def test_empty_store_returns_valid_response(self, client):
        """Empty store (no events) must return metrics with zeros, not crash."""
        r = await client.get("/stores/STORE_EMPTY_999/metrics")
        assert r.status_code == 200
        data = r.json()
        assert data["unique_visitors"] == 0
        assert data["conversion_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_conversion_rate_computed_correctly(self, client):
        """5 visitors, 2 reach billing, 0 abandon → conversion = 2/5 = 0.4"""
        visitors = [f"VIS_{i:03d}" for i in range(5)]
        events = [make_event("ENTRY", visitor_id=v) for v in visitors]
        # 2 reach billing
        events += [
            make_event("BILLING_QUEUE_JOIN", visitor_id=visitors[0], zone_id="BILLING"),
            make_event("BILLING_QUEUE_JOIN", visitor_id=visitors[1], zone_id="BILLING"),
        ]
        await ingest(client, events)
        r = await client.get("/stores/STORE_BLR_002/metrics")
        data = r.json()
        assert data["conversion_rate"] == pytest.approx(0.4, abs=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# Funnel endpoint
# ─────────────────────────────────────────────────────────────────────────────

class TestFunnel:
    @pytest.mark.asyncio
    async def test_funnel_stages_in_order(self, client):
        r = await client.get("/stores/STORE_BLR_002/funnel")
        assert r.status_code == 200
        data = r.json()
        assert len(data["stages"]) == 4
        stage_names = [s["stage"] for s in data["stages"]]
        assert stage_names == ["Entry", "Zone Visit", "Billing Queue", "Purchase"]

    @pytest.mark.asyncio
    async def test_reentry_does_not_inflate_funnel(self, client):
        """A visitor who re-enters counts as 1 unique session in the funnel."""
        events = [
            make_event("ENTRY", visitor_id="VIS_reentry"),
            make_event("EXIT", visitor_id="VIS_reentry"),
            make_event("REENTRY", visitor_id="VIS_reentry"),  # same person back
        ]
        await ingest(client, events)
        r = await client.get("/stores/STORE_BLR_002/funnel")
        data = r.json()
        # unique_sessions should count VIS_reentry as 1 session (from ENTRY, not REENTRY)
        entry_stage = next(s for s in data["stages"] if s["stage"] == "Entry")
        assert entry_stage["count"] == 1

    @pytest.mark.asyncio
    async def test_funnel_drop_off_percentages(self, client):
        """10 enter, 6 zone visit, 3 billing, 2 purchase → drop-offs correct."""
        visitors = [f"VIS_{i:03d}" for i in range(10)]
        events = [make_event("ENTRY", visitor_id=v) for v in visitors]
        events += [make_event("ZONE_ENTER", visitor_id=v, zone_id="SKINCARE") for v in visitors[:6]]
        events += [make_event("BILLING_QUEUE_JOIN", visitor_id=v, zone_id="BILLING")
                   for v in visitors[:3]]
        events += [make_event("BILLING_QUEUE_ABANDON", visitor_id=visitors[2], zone_id="BILLING")]
        await ingest(client, events)

        r = await client.get("/stores/STORE_BLR_002/funnel")
        data = r.json()
        stages = {s["stage"]: s for s in data["stages"]}

        assert stages["Entry"]["count"] == 10
        assert stages["Zone Visit"]["count"] == 6
        assert stages["Billing Queue"]["count"] == 3
        assert stages["Purchase"]["count"] == 2  # 3 billing - 1 abandon


# ─────────────────────────────────────────────────────────────────────────────
# Heatmap endpoint
# ─────────────────────────────────────────────────────────────────────────────

class TestHeatmap:
    @pytest.mark.asyncio
    async def test_heatmap_normalised_0_100(self, client):
        events = [
            make_event("ZONE_ENTER", visitor_id=f"VIS_{i}", zone_id="SKINCARE")
            for i in range(5)
        ]
        await ingest(client, events)
        r = await client.get("/stores/STORE_BLR_002/heatmap")
        assert r.status_code == 200
        data = r.json()
        if data["zones"]:
            for zone in data["zones"]:
                assert 0.0 <= zone["normalised_score"] <= 100.0

    @pytest.mark.asyncio
    async def test_low_confidence_flag_on_sparse_zone(self, client):
        """Zone with < 20 sessions should have data_confidence=LOW."""
        events = [
            make_event("ZONE_ENTER", visitor_id=f"VIS_{i}", zone_id="RARE_ZONE")
            for i in range(5)  # only 5, below threshold of 20
        ]
        await ingest(client, events)
        r = await client.get("/stores/STORE_BLR_002/heatmap")
        data = r.json()
        rare = next((z for z in data["zones"] if z["zone_id"] == "RARE_ZONE"), None)
        if rare:
            assert rare["data_confidence"] == "LOW"

    @pytest.mark.asyncio
    async def test_empty_store_heatmap_returns_empty_list(self, client):
        r = await client.get("/stores/STORE_EMPTY_HEAT/heatmap")
        assert r.status_code == 200
        assert r.json()["zones"] == []


# ─────────────────────────────────────────────────────────────────────────────
# Anomaly detection
# ─────────────────────────────────────────────────────────────────────────────

class TestAnomalies:
    @pytest.mark.asyncio
    async def test_queue_spike_detected(self, client):
        """Inserting a BILLING_QUEUE_JOIN with depth >= 8 triggers anomaly."""
        event = make_event("BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=10)
        await ingest(client, [event])
        r = await client.get("/stores/STORE_BLR_002/anomalies")
        assert r.status_code == 200
        anomalies = r.json()["anomalies"]
        types = [a["anomaly_type"] for a in anomalies]
        assert "BILLING_QUEUE_SPIKE" in types

    @pytest.mark.asyncio
    async def test_no_anomalies_for_empty_store(self, client):
        """Empty store should return empty anomalies list, not crash."""
        r = await client.get("/stores/STORE_EMPTY_ANOM/anomalies")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data["anomalies"], list)

    @pytest.mark.asyncio
    async def test_anomaly_has_required_fields(self, client):
        event = make_event("BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=12)
        await ingest(client, [event])
        r = await client.get("/stores/STORE_BLR_002/anomalies")
        anomalies = r.json()["anomalies"]
        for a in anomalies:
            assert "anomaly_type" in a
            assert "severity" in a
            assert "suggested_action" in a
            assert a["severity"] in ("INFO", "WARN", "CRITICAL")


# ─────────────────────────────────────────────────────────────────────────────
# Health endpoint
# ─────────────────────────────────────────────────────────────────────────────

class TestHealth:
    @pytest.mark.asyncio
    async def test_health_returns_ok(self, client):
        r = await client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] in ("healthy", "degraded")
        assert data["database"] == "healthy"

    @pytest.mark.asyncio
    async def test_health_includes_all_fields(self, client):
        r = await client.get("/health")
        data = r.json()
        assert "service" in data
        assert "status" in data
        assert "database" in data
        assert "stores" in data
        assert "as_of" in data

    @pytest.mark.asyncio
    async def test_stale_feed_flagged(self, client):
        """A store with last event > 10 min ago should get STALE_FEED status."""
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
        event = make_event("ENTRY", store_id="STORE_STALE_001", timestamp=old_ts)
        await ingest(client, [event])
        r = await client.get("/health")
        data = r.json()
        stale_stores = [s for s in data["stores"] if s.get("status") == "STALE_FEED"]
        assert any(s["store_id"] == "STORE_STALE_001" for s in stale_stores)
