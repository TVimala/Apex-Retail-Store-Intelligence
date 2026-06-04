# DESIGN.md — Store Intelligence System Architecture

## Overview

This system converts raw CCTV footage from Apex Retail stores into a live, queryable analytics API. The pipeline has four stages: detection, event streaming, intelligence API, and live dashboard.

```
Raw CCTV Clips
    │
    ▼
Detection Layer (detect.py + tracker.py)
  - YOLOv8n person detection @ 0.35 confidence threshold
  - ByteTrack multi-object tracking (supervision library)
  - Re-ID via trajectory + colour histogram proximity
  - Zone classification via polygon intersection
  - Staff detection via torso colour histogram proxy
    │
    ▼ structured JSONL events (emit.py)
    │
    ├── File: events/*.jsonl          (offline / batch replay)
    └── HTTP POST: /events/ingest     (live streaming, optional)
         │
         ▼
Intelligence API (FastAPI + SQLite/PostgreSQL)
  - Idempotent ingest (event_id deduplication)
  - Real-time metric computation (no caching layer needed at single-store scale)
  - Session-based funnel analytics
  - Anomaly detection with severity tiers
  - /health with per-store feed lag monitoring
         │
         ▼
Live Dashboard (rich terminal UI)
  - Polls API every 2 seconds
  - Can replay JSONL events at configurable speed to simulate live pipeline
```

## Component Decisions

### Detection Layer

**Model choice: YOLOv8n**
YOLOv8n was chosen over larger variants (YOLOv8m, YOLOv8x) and alternatives (RT-DETR, MediaPipe) because:
1. It achieves >40fps on CPU-only hardware at 1080p, which is critical since this challenge runs without guaranteed GPU.
2. It has strong out-of-the-box performance on the COCO `person` class (AP ~37.3 on COCO val), which is our only detection target.
3. The confidence threshold (0.35) is tuned low intentionally — we emit low-confidence events with the flag rather than suppressing them, preserving information for downstream calibration.

For a production deployment with GPU, YOLOv8m or RT-DETR-L would be the upgrade path.

**Tracking: ByteTrack**
ByteTrack (via the `supervision` library) was chosen over DeepSORT because:
- ByteTrack uses low-confidence detections as "second associations," making it more robust during partial occlusion (e.g., people behind displays).
- It has no appearance model dependency, which keeps the pipeline self-contained and fast.
- DeepSORT requires a re-identification CNN at each frame; ByteTrack only needs bounding boxes.

A fallback IoU-based tracker is included for environments where `supervision` cannot be installed.

**Re-ID Strategy**
Rather than a full OSNet/torchreid model (which requires GPU and substantial model weights), the Re-ID registry uses:
1. **Spatial proximity**: if a new track appears within 120px of an exited visitor's last foot position, within a 120-second cooldown window, it is flagged as REENTRY.
2. **Colour histogram**: optional secondary check on torso region. Prevents mismatches when a different person enters from the same direction shortly after an exit.
3. **Trajectory hash**: for stable cross-frame identity within a continuous track, the foot trajectory is used.

This approach was validated against the key re-entry edge case: a customer who exits briefly (e.g., takes a phone call outside) and re-enters within ~2 minutes. The 120-second cooldown and 120px radius were calibrated against the sample_events.jsonl data.

**Staff Detection**
Staff detection uses a colour histogram proxy on the torso region. The assumption is that Apex Retail staff wear dark, low-saturation uniforms (navy/black). A score is computed from the ratio of dark pixels (V channel < 80 in HSV) and low-saturation pixels (S channel < 60). Any track scoring above 0.72 is flagged `is_staff=true`.

This is a proxy, not a trained classifier. A production system would use one of:
- A fine-tuned binary classifier on staff vs. customer crops
- A VLM (e.g., Claude Vision) with a prompt describing the uniform

I evaluated using a VLM for staff detection (see AI-Assisted Decisions below) and chose the histogram approach for latency and offline capability.

**Zone Classification**
Zone classification uses point-in-polygon (ray-casting) on the foot point of each bounding box. Zone definitions come from `store_layout.json`. This is O(n_zones × n_people) per frame — negligible for typical retail store sizes (< 10 zones, < 30 people).

### Event Stream

The event schema was designed with two goals:
1. **Sufficient for all analytics queries** — every field needed by the API is present at emit time, avoiding expensive joins at query time.
2. **Idempotent by event_id** — UUID v4 per event enables safe at-least-once delivery from the pipeline to the API.

Key design choices:
- `is_staff` is set at emit time, not at ingest time, because the detection layer has visual context that the API does not.
- `confidence` is always emitted, even for low-confidence detections. The API can filter if needed but cannot recover suppressed data.
- `session_seq` tracks the ordinal position of each event in a visitor's session. This allows the API to reconstruct session timelines without sorting by timestamp.

### Intelligence API

**Framework: FastAPI + SQLAlchemy async**
FastAPI was chosen for:
- Native async support (critical for I/O-bound DB queries)
- Automatic OpenAPI schema generation
- Pydantic v2 validation with detailed error responses

**Database: SQLite (with WAL mode) / PostgreSQL**
SQLite with WAL mode is the default because:
- It requires zero configuration and works immediately after `docker compose up`
- WAL mode allows concurrent reads with a single writer, which matches our workload (one pipeline writer, multiple API readers)
- For 5 stores × 1hr clips × ~1000 events/hour = ~5000 events, SQLite is more than adequate

Switching to PostgreSQL requires only changing `DATABASE_URL` in the environment.

**Metric computation: Real-time SQL aggregation**
Metrics are computed from live SQL aggregation queries on every request rather than being cached in a materialized view. This ensures the metrics are always current. The trade-off is that at 40 stores with high event volume, these queries may need to be moved to a pre-aggregated table with an incremental update pattern. This is documented in the scalability note in CHOICES.md.

**Ingest idempotency**
The `POST /events/ingest` endpoint uses a pre-check query for existing `event_id`s followed by bulk `INSERT OR IGNORE` (SQLite) or `INSERT ... ON CONFLICT DO NOTHING` (PostgreSQL). This makes the endpoint safe to retry and safe to call twice with the same payload.

### Live Dashboard

The terminal dashboard (`rich` library) polls the API every 2 seconds and renders:
- Store metrics panel (visitors, conversion, queue depth, abandonment)
- Conversion funnel with drop-off percentages
- Zone heatmap (top 8 zones by normalised score)
- Active anomalies with severity colour-coding

It includes an `--replay-events` mode that drip-feeds a JSONL events file to the API at configurable speed, providing proof that the pipeline and API are genuinely connected end-to-end.

---

## AI-Assisted Decisions

### 1. Re-ID Architecture: Trajectory Hash vs. Full OSNet Model

I used Claude to evaluate the trade-off between a trajectory-based Re-ID approach (what I built) and integrating a full OSNet/torchreid appearance model.

**What AI suggested**: Use OSNet with a sliding window of appearance embeddings per track. It provided code for integrating `torchreid` and noted that appearance-based Re-ID is significantly more accurate for re-entry detection after >2 minutes.

**What I chose and why**: The trajectory + colour histogram approach. The primary reason is deployment reliability — OSNet requires GPU for real-time use, and the challenge specifies no guaranteed GPU. The `torchreid` package also has complex CUDA version dependencies that break `docker compose up` on arbitrary machines. For the specific re-entry edge case in the footage (customers who step outside for <2 minutes), spatial proximity within a 120-second window is sufficient.

**Where AI was right**: At production scale (>1hr clips, >2 min re-entry gaps), OSNet would outperform trajectory-based Re-ID. I documented this in CHOICES.md as the primary production upgrade path.

### 2. Anomaly Detection: Rule-Based vs. ML-Based

I asked an LLM to design the anomaly detection system. It suggested a two-track approach:
- Rule-based for operational anomalies (queue spike, feed staleness)
- A lightweight time-series model (ARIMA or Prophet) for conversion drop detection

**What I overrode**: The ARIMA/Prophet suggestion for conversion drop. The challenge has 20-minute clips — there is insufficient historical data to fit a meaningful time-series model. I implemented a simpler 7-day rolling average comparison, which is more interpretable and more robust on sparse data.

**What I kept**: The severity escalation logic (INFO/WARN/CRITICAL at 1x/1.5x thresholds) was directly from the AI suggestion and is a clear improvement over fixed binary thresholds.

### 3. Database Schema: Normalised vs. Denormalised

I consulted an LLM on whether to normalise the events table (separate `metadata` table) or denormalise (flatten metadata fields into the main events table).

**AI suggestion**: Normalise into separate tables for `sessions`, `zones`, and `events` for better data integrity.

**My decision**: Denormalise into a single `events` table with indexed columns. The rationale:
- All analytics queries read one entity (events), so joins would add latency without benefit.
- Event schema is fixed at emit time, so the "flexibility" of normalisation is not needed.
- For the scale of this challenge (~5 stores, hours of footage), query performance on a flat table with indexes is superior to multi-table joins.

I agreed with the AI that a sessions table would be useful at 40-store scale (to avoid re-deriving session boundaries on every funnel query), and documented this as a future optimisation.
