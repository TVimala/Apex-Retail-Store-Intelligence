# CHOICES.md — Three Key Engineering Decisions

---

## Decision 1: Detection Model — YOLOv8n with ByteTrack

### The Problem
The detection layer needs to: (1) detect individual people in 1080p@15fps CCTV footage, (2) handle group entry (count 3 people as 3, not 1), (3) handle partial occlusion, and (4) run fast enough that detection doesn't become a bottleneck.

### Options Considered

| Option | Pros | Cons |
|---|---|---|
| **YOLOv8n** | Fast (40+ fps CPU), well-documented, COCO-pretrained person class | Smaller model, lower AP on occluded/small people |
| YOLOv8m/x | Better accuracy on occlusion, higher AP | 2–5× slower, GPU dependency in practice |
| RT-DETR-L | State-of-the-art detection accuracy, transformer architecture | Significantly slower on CPU, complex setup |
| MediaPipe Pose | Fast, runs fully offline | Pose not detection — misses occluded people, no bounding box area for zone inference |
| GPT-4V / Claude Vision | Can understand scene semantics (staff vs customer) | Far too slow for frame-by-frame (API latency 500ms+), cost-prohibitive at 15fps |

### What AI Suggested
When I asked Claude to evaluate model options for retail CCTV analytics, it recommended YOLOv8m as a better baseline over YOLOv8n, citing that the ~2× speed penalty is acceptable on modern hardware with GPU. It also suggested using GPT-4V for the first frame of each clip to classify staff uniforms, then using that signature for per-frame filtering.

### What I Chose and Why
**YOLOv8n + ByteTrack**, with the GPU recommendation overridden.

The challenge specification requires `docker compose up` to start everything on any machine without manual steps. GPU availability is not guaranteed. YOLOv8n at 0.35 confidence threshold produces sufficient detections for entry/exit counting and zone classification. For the group entry edge case, the bounding box NMS (non-maximum suppression) in YOLO with IoU threshold 0.45 correctly separates people who are adjacent but not overlapping.

I partially adopted the AI's VLM suggestion — using GPT-4V or Claude Vision for **staff uniform calibration** (one-time per store, not per-frame) is a valid production approach I would pursue with more time. For this submission, the colour histogram proxy is used instead.

**ByteTrack over DeepSORT**: ByteTrack's secondary association pass on low-confidence detections specifically addresses the partial occlusion case (people behind shelf displays). DeepSORT's appearance model is an additional dependency that would complicate the Docker setup without clear benefit for entry/exit counting accuracy.

### Confidence in This Decision: High
The primary risk is accuracy on the held-out clip for the partial occlusion test cases. If I had a GPU environment, I would upgrade to YOLOv8m for the final submission.

---

## Decision 2: Event Schema Design

### The Problem
The event schema must support: real-time metric computation, funnel analytics, session reconstruction, cross-camera deduplication, and staff exclusion — all from a single event stream.

### Options Considered

**Option A: Minimal schema (just detection + timestamp)**
```json
{"visitor_id": "V1", "event_type": "ENTRY", "timestamp": "...", "store_id": "..."}
```
Pro: Simple to produce. Con: Cannot compute zone dwell, funnel stages, or queue depth from this alone.

**Option B: Full session object (entire session as one record)**
```json
{"session_id": "...", "events": [...], "zones_visited": [...], "purchased": true}
```
Pro: Analytically clean. Con: Cannot stream in real-time — sessions are only complete at EXIT. Cannot detect anomalies mid-session.

**Option C: Per-event schema with denormalised session context (what I built)**
Each event carries its own identity plus session context (`session_seq`, `visitor_id`, `is_staff`) and zone context (`zone_id`, `sku_zone`). Events are atomic and streamable.

### What AI Suggested
When I asked an LLM to design the schema, it initially proposed a minimal schema (Option A) and suggested computing zone dwell and session context in the API from raw position data. It revised this after I pointed out that the API receives events, not raw video frames — it cannot compute zone membership from an event stream alone.

The AI's revised suggestion was close to my final schema. The main thing I added that it didn't suggest: `session_seq` (the ordinal position of each event in a visitor's session). This field makes session timeline reconstruction O(n log n) sort rather than a full scan by timestamp, and is especially valuable for the funnel calculation.

### What I Chose and Why
**Option C** with the following specific design decisions:
1. `is_staff` at emit time, not inference time — the detection layer has the frame; the API does not.
2. `confidence` always emitted — the API layer (or downstream) decides what to do with low-confidence events. Suppressing at emit time loses information permanently.
3. `event_id` as UUID v4 — globally unique, generation is O(1), no sequence number coordination needed across multiple cameras.
4. `zone_id: null` for ENTRY/EXIT — enforced by Pydantic validators in the API. The schema allows null zone but requires non-null zone for zone-type events.
5. `dwell_ms: 0` for instantaneous events — avoids null handling in aggregation queries. `AVG(dwell_ms)` on ZONE_DWELL events gives meaningful results without filtering.

### Confidence in This Decision: High
The schema has been validated against all 10 example assertions in `assertions.py`. The one area I would revisit: `dwell_ms` on `ZONE_DWELL` events currently represents total dwell since zone entry, not the interval since the last dwell event. This is consistent with the spec ("emit every 30s of continued dwell") but means the API must use the most recent `ZONE_DWELL.dwell_ms` value, not sum them.

---

## Decision 3: API Architecture — Real-Time SQL Aggregation vs. Pre-Computed State

### The Problem
The `/metrics`, `/funnel`, and `/heatmap` endpoints must return "real-time" data. There are two approaches: compute from raw events on every request, or maintain pre-computed state that is updated incrementally as events are ingested.

### Options Considered

**Option A: Real-time SQL aggregation (what I built)**
Every API request runs SQL aggregation queries (COUNT, AVG, GROUP BY) over the events table for today's window.

Pro: Always current. Simple to reason about. No cache invalidation problem.
Con: Query cost grows linearly with event count. At 40 stores × 1000+ events/hour × 8 hours = ~320,000 events/day, some queries take 50–200ms.

**Option B: Incremental materialised views (Redis counters)**
On each ingest, increment Redis counters: `store:{id}:visitors`, `store:{id}:zone:{z}:dwell_sum`, etc.

Pro: O(1) reads. Scales to 40 stores with no degradation.
Con: Requires Redis in the Docker stack (complicates `docker compose up`). Counter recovery after crash requires replaying events. Logic for session deduplication (re-entry) is complex in counter-based models.

**Option C: Scheduled batch recomputation (e.g., every 30 seconds)**
A background task rewrites a `metrics_cache` table every 30 seconds from the events table.

Pro: Fast reads, simple cache invalidation (time-based).
Con: Latency up to 30 seconds. Makes the API technically stale, not real-time. The `/anomalies` endpoint (queue spike detection) specifically needs <30s latency.

### What AI Suggested
I asked an LLM (Claude) to design the storage and query architecture. It suggested Option B (Redis counters) as the production-correct choice, with Option A as the development starting point. It provided a detailed Redis data model including sorted sets for session tracking and hash maps for zone aggregation.

I agreed with the AI that Option B is the right long-term architecture. However, for this submission I chose **Option A** for the following reasons:

1. **Acceptance gate compliance**: `docker compose up` must start everything with no manual steps. Adding Redis increases the setup surface area and risk of failure on the reviewer's machine.
2. **SQLite with WAL mode** handles concurrent reads with negligible lock contention at 5-store, single-machine scale.
3. **Index coverage**: The `events` table has composite indexes on `(store_id, timestamp)` and `(store_id, event_type, timestamp)`, making today's aggregation queries hit index-only scans.

### Production Scale Path
The first thing that breaks at 40 live stores is the `COUNT(DISTINCT visitor_id)` query on the funnel endpoint — it does a full table scan even with indexes when the session count exceeds ~50,000. The fix is a materialised sessions table that is updated incrementally:

```sql
CREATE TABLE sessions (
    visitor_id TEXT,
    store_id TEXT,
    date DATE,
    entered_at TIMESTAMP,
    exited_at TIMESTAMP,
    reached_billing BOOLEAN,
    purchased BOOLEAN,
    PRIMARY KEY (visitor_id, store_id, date)
);
```

This table would be updated in the ingest path, making funnel queries O(1) per store per day. I would implement this first if scaling beyond 10 stores.

### Confidence in This Decision: High for submission, Medium for production
Option A is the right choice for this challenge. Option B (Redis) is the right choice for production. The architectural boundary between them is clean — the service layer (`metrics.py`, `funnel.py`) can be repointed to Redis counters without changing the API surface.
