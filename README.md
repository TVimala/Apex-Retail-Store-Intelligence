# Apex Retail Store Intelligence API

End-to-end CCTV analytics pipeline: raw footage → live store metrics API.

---

## Quick Start (5 commands)

```bash
# 1. Clone and enter the project
git clone <your-repo-url> store-intelligence && cd store-intelligence

# 2. Start the API (Docker)
docker compose up --build -d

# 3. Wait for the API to be ready, then ingest the sample events
python pipeline/translate_events.py \
    --input  data/raw_sample_events.jsonl \
    --output data/sample_events.jsonl \
    --store  STORE_BLR_002 \
    --api    http://localhost:8000

# 4. Verify the API is working
python assertions.py --api http://localhost:8000 --store STORE_BLR_002

# 5. Launch the live dashboard
python dashboard/live_dashboard.py \
    --store STORE_BLR_002 \
    --api   http://localhost:8000 \
    --replay-events data/sample_events.jsonl
```

---

## Project Structure

```
store-intelligence/
├── pipeline/
│   ├── detect.py              # YOLOv8 + ByteTrack detection pipeline
│   ├── tracker.py             # Re-ID and multi-object tracking
│   ├── emit.py                # Event schema + JSONL emitter
│   ├── translate_events.py    # ★ Converts real Purplle events → API schema
│   ├── normalise_pos.py       # ★ Converts real POS CSV → challenge format
│   ├── ingest_batch.py        # Batch ingest JSONL → API
│   └── run.sh                 # One-command pipeline runner
├── app/
│   ├── main.py                # FastAPI entrypoint
│   ├── models.py              # Pydantic event schema
│   ├── routers/               # API endpoint handlers
│   │   ├── events.py          # POST /events/ingest
│   │   ├── stores.py          # GET /stores/{id}/metrics|funnel|heatmap|anomalies
│   │   └── health.py          # GET /health
│   ├── services/              # Business logic
│   │   ├── metrics.py
│   │   ├── funnel.py
│   │   ├── heatmap.py
│   │   └── anomalies.py
│   └── db/                    # SQLAlchemy async ORM + database setup
├── dashboard/
│   └── live_dashboard.py      # Rich terminal live dashboard
├── data/
│   ├── store_layout.json             # Zone definitions for each store
│   ├── raw_sample_events.jsonl       # ★ Real Purplle event format (input)
│   ├── sample_events.jsonl           # Translated events in API schema (generated)
│   ├── pos_transactions.csv          # ★ Real POS data (raw)
│   └── pos_transactions_normalised.csv  # Normalised POS (generated)
├── tests/
│   ├── test_pipeline.py
│   ├── test_metrics.py
│   └── test_anomalies.py
├── docs/
│   ├── DESIGN.md
│   └── CHOICES.md
├── assertions.py              # 10 acceptance assertions
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

---

## Data Pipeline: Real Purplle Events → API

The provided `raw_sample_events.jsonl` and `pos_transactions.csv` use
Purplle's internal field names, which differ from the challenge API schema.

Two translation scripts handle this automatically:

### translate_events.py — Event Schema Translator

Converts raw Purplle JSONL events into the Apex Retail API schema.

**Field mapping:**

| Raw field (Purplle) | API field | Notes |
|---|---|---|
| `id_token` | `visitor_id` | Prefixed with `VIS_` |
| `store_code` / `store_id` | `store_id` | `store_1076` → `STORE_BLR_002` |
| `event_timestamp` / `event_time` | `timestamp` | ISO-8601 UTC normalised |
| `event_type: entry` | `event_type: ENTRY` | |
| `event_type: zone_entered` | `event_type: ZONE_ENTER` | |
| `event_type: queue_completed` | `BILLING_QUEUE_JOIN` | |
| `event_type: queue_abandoned` | `BILLING_QUEUE_JOIN` + `BILLING_QUEUE_ABANDON` | Two events emitted |
| `queue_position_at_join` | `metadata.queue_depth` | |

```bash
# Translate only (no API)
python pipeline/translate_events.py \
    --input  data/raw_sample_events.jsonl \
    --output data/sample_events.jsonl \
    --store  STORE_BLR_002

# Translate + immediately POST to API
python pipeline/translate_events.py \
    --input  data/raw_sample_events.jsonl \
    --output data/sample_events.jsonl \
    --store  STORE_BLR_002 \
    --api    http://localhost:8000
```

### normalise_pos.py — POS Data Normaliser

Converts `pos_transactions.csv` (real format: per-product-line rows) into
the challenge format (per-order basket totals).

```bash
python pipeline/normalise_pos.py \
    --input  data/pos_transactions.csv \
    --output data/pos_transactions_normalised.csv
```

---

## Running the Detection Pipeline (CCTV Clips)

Place your clips in `data/clips/` named as `<STORE_ID>_<CAM_ID>.mp4`:

```
data/clips/
  STORE_BLR_002_CAM_ENTRY_01.mp4
  STORE_BLR_002_CAM_FLOOR_01.mp4
  STORE_BLR_002_CAM_BILLING_01.mp4
```

Install pipeline dependencies (outside Docker):

```bash
pip install ultralytics supervision opencv-python-headless numpy
```

Run the full pipeline:

```bash
# Max speed (batch mode)
bash pipeline/run.sh ./data/clips ./data/store_layout.json ./data/events http://localhost:8000 0

# Real-time (for live dashboard demo)
bash pipeline/run.sh ./data/clips ./data/store_layout.json ./data/events http://localhost:8000 1.0

# Single clip
python pipeline/detect.py \
    --clip    data/clips/STORE_BLR_002_CAM_ENTRY_01.mp4 \
    --store   STORE_BLR_002 \
    --camera  CAM_ENTRY_01 \
    --layout  data/store_layout.json \
    --output  data/events/STORE_BLR_002_CAM_ENTRY_01.jsonl \
    --api-url http://localhost:8000
```

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/events/ingest` | Ingest up to 500 events (idempotent) |
| `GET`  | `/stores/{id}/metrics` | Visitors, conversion, queue depth, dwell |
| `GET`  | `/stores/{id}/funnel` | Entry → Zone → Billing → Purchase funnel |
| `GET`  | `/stores/{id}/heatmap` | Zone visit frequency normalised 0–100 |
| `GET`  | `/stores/{id}/anomalies` | Active anomalies with severity + action |
| `GET`  | `/health` | Service health + per-store feed lag |

### Quick API test

```bash
# Check health
curl http://localhost:8000/health | python3 -m json.tool

# Get metrics for STORE_BLR_002
curl http://localhost:8000/stores/STORE_BLR_002/metrics | python3 -m json.tool

# Get funnel
curl http://localhost:8000/stores/STORE_BLR_002/funnel | python3 -m json.tool

# Get anomalies
curl http://localhost:8000/stores/STORE_BLR_002/anomalies | python3 -m json.tool
```

---

## Running Tests

```bash
# Install test dependencies
pip install -r requirements.txt

# Run all tests with coverage
pytest

# Run specific test file
pytest tests/test_metrics.py -v

# Run without coverage (faster)
pytest --no-cov
```

---

## Live Dashboard

```bash
# Terminal dashboard — polls API every 2 seconds
python dashboard/live_dashboard.py \
    --store STORE_BLR_002 \
    --api   http://localhost:8000

# With simulated live event replay
python dashboard/live_dashboard.py \
    --store         STORE_BLR_002 \
    --api           http://localhost:8000 \
    --replay-events data/sample_events.jsonl \
    --poll          2.0
```

Dashboard URL: terminal only (no browser needed). Shows metrics, funnel,
heatmap, and anomalies updating live.

---

## Docker

```bash
# Start API
docker compose up --build

# Start in background
docker compose up --build -d

# View logs
docker compose logs -f

# Stop
docker compose down
```

Switch to PostgreSQL by editing `docker-compose.yml`:
```yaml
environment:
  - DATABASE_URL=postgresql+asyncpg://apex:apex_secret@postgres/store_intelligence
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/store_intelligence.db` | Database connection string |
| `LOG_LEVEL` | `INFO` | Logging level |

---

## Running Assertions (Self-Validation)

```bash
# Start API first, then ingest sample events, then run assertions
docker compose up -d
python pipeline/translate_events.py --input data/raw_sample_events.jsonl --output data/sample_events.jsonl --api http://localhost:8000
python assertions.py --api http://localhost:8000 --store STORE_BLR_002
```

---

## Architecture

See `docs/DESIGN.md` for full architecture overview and AI-assisted decisions.
See `docs/CHOICES.md` for model selection, schema design, and API architecture rationale.
