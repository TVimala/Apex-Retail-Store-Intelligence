
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

from app.main import app
from app.db.database import get_db, Base
from app.db.orm import EventORM

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
test_engine2 = create_async_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSession2 = async_sessionmaker(bind=test_engine2, expire_on_commit=False)


async def override_get_db2():
    async with TestingSession2() as session:
        yield session


app.dependency_overrides[get_db] = override_get_db2


@pytest_asyncio.fixture(scope="module", autouse=True)
async def setup_db_anomaly():
    async with test_engine2.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine2.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture(autouse=True)
async def clear_db():
    yield
    async with TestingSession2() as session:
        from sqlalchemy import text
        await session.execute(text("DELETE FROM events"))
        await session.commit()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


def make_evt(event_type, store_id="STORE_TEST", visitor_id=None, zone_id=None,
             is_staff=False, timestamp=None, queue_depth=None, confidence=0.88):
    now = datetime.now(timezone.utc)
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_TEST",
        "visitor_id": visitor_id or f"VIS_{uuid.uuid4().hex[:6]}",
        "event_type": event_type,
        "timestamp": timestamp or now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zone_id": zone_id,
        "dwell_ms": 0,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {"queue_depth": queue_depth, "sku_zone": zone_id, "session_seq": 1},
    }


async def post_events(client, events):
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200
    return r.json()


class TestQueueSpikeAnomaly:
    @pytest.mark.asyncio
    async def test_no_anomaly_below_threshold(self, client):
        e = make_evt("BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=3)
        await post_events(client, [e])
        r = await client.get("/stores/STORE_TEST/anomalies")
        spikes = [a for a in r.json()["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        assert len(spikes) == 0

    @pytest.mark.asyncio
    async def test_warn_at_threshold(self, client):
        e = make_evt("BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=8)
        await post_events(client, [e])
        r = await client.get("/stores/STORE_TEST/anomalies")
        spikes = [a for a in r.json()["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        assert len(spikes) == 1
        assert spikes[0]["severity"] in ("WARN", "CRITICAL")

    @pytest.mark.asyncio
    async def test_critical_above_1_5x_threshold(self, client):
        e = make_evt("BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=13)  # 8 * 1.5 = 12
        await post_events(client, [e])
        r = await client.get("/stores/STORE_TEST/anomalies")
        spikes = [a for a in r.json()["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        assert len(spikes) == 1
        assert spikes[0]["severity"] == "CRITICAL"

    @pytest.mark.asyncio
    async def test_spike_anomaly_has_suggested_action(self, client):
        e = make_evt("BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=10)
        await post_events(client, [e])
        r = await client.get("/stores/STORE_TEST/anomalies")
        spikes = [a for a in r.json()["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        assert len(spikes) == 1
        action = spikes[0]["suggested_action"]
        assert isinstance(action, str) and len(action) > 10


class TestDeadZoneAnomaly:
    @pytest.mark.asyncio
    async def test_dead_zone_detected(self, client):
        """A zone with activity only 35 min ago should be flagged as dead."""
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=35)).strftime("%Y-%m-%dT%H:%M:%SZ")
        events = [make_evt("ZONE_ENTER", zone_id="PERFUME_ZONE", timestamp=old_ts)
                  for _ in range(3)]
        await post_events(client, events)
        r = await client.get("/stores/STORE_TEST/anomalies")
        dead = [a for a in r.json()["anomalies"] if a["anomaly_type"] == "DEAD_ZONE"]
        zone_ids = [a["zone_id"] for a in dead]
        assert "PERFUME_ZONE" in zone_ids

    @pytest.mark.asyncio
    async def test_active_zone_not_flagged(self, client):
        """A zone with recent activity must NOT be flagged."""
        recent_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        events = [make_evt("ZONE_ENTER", zone_id="ACTIVE_ZONE", timestamp=recent_ts)
                  for _ in range(3)]
        await post_events(client, events)
        r = await client.get("/stores/STORE_TEST/anomalies")
        dead = [a for a in r.json()["anomalies"] if a["anomaly_type"] == "DEAD_ZONE"]
        zone_ids = [a["zone_id"] for a in dead]
        assert "ACTIVE_ZONE" not in zone_ids


class TestAllStaffClip:
    @pytest.mark.asyncio
    async def test_all_staff_no_customer_anomalies(self, client):
        """Clip with only staff movement should not trigger queue or conversion anomalies."""
        events = [
            make_evt("ENTRY", visitor_id=f"STAFF_{i}", is_staff=True)
            for i in range(5)
        ]
        events += [
            make_evt("BILLING_QUEUE_JOIN", visitor_id=f"STAFF_{i}",
                     zone_id="BILLING", is_staff=True, queue_depth=5)
            for i in range(5)
        ]
        await post_events(client, events)
        r = await client.get("/stores/STORE_TEST/anomalies")
        anomalies = r.json()["anomalies"]
        # Queue spike uses queue_depth from events regardless of staff flag,
        # but conversion drop should not fire (no customer data)
        conv_drops = [a for a in anomalies if a["anomaly_type"] == "CONVERSION_DROP"]
        assert len(conv_drops) == 0


class TestAnomalyStructure:
    @pytest.mark.asyncio
    async def test_anomaly_response_always_valid(self, client):
        """Even with no events, anomalies response must be valid."""
        r = await client.get("/stores/STORE_BRAND_NEW/anomalies")
        assert r.status_code == 200
        data = r.json()
        assert "store_id" in data
        assert "anomalies" in data
        assert "as_of" in data
        assert isinstance(data["anomalies"], list)

    @pytest.mark.asyncio
    async def test_anomaly_id_is_unique(self, client):
        events = [make_evt("BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=10)
                  for _ in range(3)]
        await post_events(client, events)
        r = await client.get("/stores/STORE_TEST/anomalies")
        anomalies = r.json()["anomalies"]
        ids = [a["anomaly_id"] for a in anomalies]
        assert len(ids) == len(set(ids)), "Anomaly IDs must be unique"
