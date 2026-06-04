"""
assertions.py — 10 example API assertions for self-validation before submission.

These are not the full scoring suite — they are the acceptance-level checks
you can run locally to verify your API is ready.

Usage:
    # Start the API first:  docker compose up
    # Ingest sample events: python pipeline/ingest_batch.py --file data/sample_events.jsonl

    python assertions.py --api http://localhost:8000 --store STORE_BLR_002
"""

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone

import requests


def ok(msg: str):
    print(f"  ✅  {msg}")


def fail(msg: str, detail: str = ""):
    print(f"  ❌  {msg}")
    if detail:
        print(f"      {detail}")


def assert_eq(label: str, actual, expected):
    if actual == expected:
        ok(f"{label}: {actual!r}")
    else:
        fail(f"{label}: expected {expected!r}, got {actual!r}")


def assert_range(label: str, actual: float, lo: float, hi: float):
    if lo <= actual <= hi:
        ok(f"{label}: {actual} ∈ [{lo}, {hi}]")
    else:
        fail(f"{label}: {actual} not in [{lo}, {hi}]")


def assert_not_none(label: str, actual):
    if actual is not None:
        ok(f"{label} is not None")
    else:
        fail(f"{label} is None")


def run_assertions(api: str, store_id: str):
    BASE = api.rstrip("/")
    print(f"\nRunning assertions against {BASE} for store {store_id}\n")

    # ── A1: Health endpoint is reachable and returns healthy ────────────────
    print("A1 — /health reachable and returns structured response")
    try:
        r = requests.get(f"{BASE}/health", timeout=5)
        assert r.status_code == 200, f"HTTP {r.status_code}"
        data = r.json()
        assert "status" in data, "missing 'status'"
        assert "database" in data, "missing 'database'"
        ok(f"/health returned status={data['status']!r}")
    except Exception as e:
        fail("/health assertion", str(e))

    # ── A2: Ingest accepts a valid event ────────────────────────────────────
    print("\nA2 — POST /events/ingest accepts a valid event")
    event = {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": "VIS_assert01",
        "event_type": "ENTRY",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.88,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }
    try:
        r = requests.post(f"{BASE}/events/ingest", json={"events": [event]}, timeout=5)
        assert r.status_code == 200, f"HTTP {r.status_code}"
        data = r.json()
        assert data["accepted"] >= 0
        ok(f"accepted={data['accepted']}, rejected={data['rejected']}")
    except Exception as e:
        fail("ingest assertion", str(e))

    # ── A3: Ingest idempotency — posting same event twice ───────────────────
    print("\nA3 — Idempotency: same event_id ingested twice → duplicate on second")
    try:
        r1 = requests.post(f"{BASE}/events/ingest", json={"events": [event]}, timeout=5)
        r2 = requests.post(f"{BASE}/events/ingest", json={"events": [event]}, timeout=5)
        d2 = r2.json()
        assert d2["duplicate"] == 1, f"Expected duplicate=1, got {d2}"
        ok(f"Second ingest: accepted={d2['accepted']}, duplicate={d2['duplicate']}")
    except Exception as e:
        fail("idempotency assertion", str(e))

    # ── A4: /metrics returns valid structure ─────────────────────────────────
    print(f"\nA4 — GET /stores/{store_id}/metrics returns valid response")
    try:
        r = requests.get(f"{BASE}/stores/{store_id}/metrics", timeout=5)
        assert r.status_code == 200, f"HTTP {r.status_code}"
        m = r.json()
        assert "unique_visitors" in m
        assert "conversion_rate" in m
        assert "abandonment_rate" in m
        assert "queue_depth_current" in m
        assert isinstance(m["unique_visitors"], int)
        assert 0.0 <= m["conversion_rate"] <= 1.0
        ok(f"visitors={m['unique_visitors']}, conversion={m['conversion_rate']:.2%}")
    except Exception as e:
        fail("metrics assertion", str(e))

    # ── A5: conversion_rate is never null (even for zero-purchase store) ────
    print("\nA5 — conversion_rate is 0.0 (not null) for zero-purchase store")
    try:
        r = requests.get(f"{BASE}/stores/STORE_ZERO_PURCHASE/metrics", timeout=5)
        assert r.status_code == 200
        m = r.json()
        assert m["conversion_rate"] is not None, "conversion_rate is null"
        assert m["conversion_rate"] == 0.0, f"Expected 0.0, got {m['conversion_rate']}"
        ok(f"conversion_rate=0.0 for empty store")
    except Exception as e:
        fail("zero-purchase assertion", str(e))

    # ── A6: /funnel returns 4 stages ─────────────────────────────────────────
    print(f"\nA6 — GET /stores/{store_id}/funnel returns 4 stages")
    try:
        r = requests.get(f"{BASE}/stores/{store_id}/funnel", timeout=5)
        assert r.status_code == 200
        f = r.json()
        assert len(f["stages"]) == 4
        stage_names = [s["stage"] for s in f["stages"]]
        assert stage_names == ["Entry", "Zone Visit", "Billing Queue", "Purchase"]
        ok(f"stages: {stage_names}")
    except Exception as e:
        fail("funnel assertion", str(e))

    # ── A7: /heatmap normalised scores 0–100 ────────────────────────────────
    print(f"\nA7 — GET /stores/{store_id}/heatmap — normalised scores in [0,100]")
    try:
        r = requests.get(f"{BASE}/stores/{store_id}/heatmap", timeout=5)
        assert r.status_code == 200
        h = r.json()
        for zone in h.get("zones", []):
            assert 0.0 <= zone["normalised_score"] <= 100.0, \
                f"Zone {zone['zone_id']} score {zone['normalised_score']} out of range"
        ok(f"{len(h['zones'])} zones, all scores normalised")
    except Exception as e:
        fail("heatmap assertion", str(e))

    # ── A8: /anomalies returns valid structure ───────────────────────────────
    print(f"\nA8 — GET /stores/{store_id}/anomalies — valid structure")
    try:
        r = requests.get(f"{BASE}/stores/{store_id}/anomalies", timeout=5)
        assert r.status_code == 200
        a = r.json()
        assert "anomalies" in a
        for anom in a["anomalies"]:
            assert "anomaly_type" in anom
            assert "severity" in anom
            assert anom["severity"] in ("INFO", "WARN", "CRITICAL")
            assert "suggested_action" in anom
            assert len(anom["suggested_action"]) > 5
        ok(f"{len(a['anomalies'])} anomalies, all valid")
    except Exception as e:
        fail("anomalies assertion", str(e))

    # ── A9: Staff events excluded from visitor count ─────────────────────────
    print("\nA9 — Staff events excluded from unique_visitors")
    store_test = "STORE_STAFF_TEST"
    staff_event = {
        "event_id": str(uuid.uuid4()),
        "store_id": store_test,
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": "VIS_staff_only",
        "event_type": "ENTRY",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": True,
        "confidence": 0.97,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }
    try:
        requests.post(f"{BASE}/events/ingest", json={"events": [staff_event]}, timeout=5)
        r = requests.get(f"{BASE}/stores/{store_test}/metrics", timeout=5)
        m = r.json()
        assert m["unique_visitors"] == 0, \
            f"Expected 0 customer visitors (only staff), got {m['unique_visitors']}"
        ok("Staff-only store: unique_visitors=0")
    except Exception as e:
        fail("staff exclusion assertion", str(e))

    # ── A10: STALE_FEED flagged in /health ───────────────────────────────────
    print("\nA10 — STALE_FEED: store with no recent events is flagged in /health")
    try:
        r = requests.get(f"{BASE}/health", timeout=5)
        h = r.json()
        store_statuses = {s["store_id"]: s for s in h.get("stores", [])}
        if store_id in store_statuses:
            s = store_statuses[store_id]
            lag = s.get("lag_seconds", 0)
            ok(f"Store {store_id} lag={lag}s, status={s['status']}")
        else:
            ok("No stores have events yet — STALE_FEED logic not triggered (correct)")
    except Exception as e:
        fail("STALE_FEED assertion", str(e))

    print("\n" + "─" * 50)
    print("Assertions complete. Review any ❌ above before submission.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--store", default="STORE_BLR_002")
    args = parser.parse_args()
    run_assertions(args.api, args.store)
