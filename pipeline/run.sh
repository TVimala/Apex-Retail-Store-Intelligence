#!/usr/bin/env bash
# run.sh — Process all CCTV clips and emit events to JSONL + live API
#
# Usage:
#   ./pipeline/run.sh [CLIPS_DIR] [LAYOUT_JSON] [OUTPUT_DIR] [API_URL] [REPLAY_SPEED]
#
# Defaults:
#   CLIPS_DIR     = ./data/clips
#   LAYOUT_JSON   = ./data/store_layout.json
#   OUTPUT_DIR    = ./data/events
#   API_URL       = http://localhost:8000  (set to "" to skip live ingest)
#   REPLAY_SPEED  = 0  (0 = max speed, 1.0 = real-time for live dashboard)

set -euo pipefail

CLIPS_DIR="${1:-./data/clips}"
LAYOUT_JSON="${2:-./data/store_layout.json}"
OUTPUT_DIR="${3:-./data/events}"
API_URL="${4:-http://localhost:8000}"
REPLAY_SPEED="${5:-0}"

mkdir -p "$OUTPUT_DIR"

# ── Step 1: Translate raw sample events (if present) ──────────────────────
RAW_EVENTS="./data/raw_sample_events.jsonl"
TRANSLATED_EVENTS="./data/sample_events.jsonl"
if [[ -f "$RAW_EVENTS" ]]; then
    echo "[INFO] Translating raw Purplle events → challenge schema …"
    python pipeline/translate_events.py \
        --input  "$RAW_EVENTS" \
        --output "$TRANSLATED_EVENTS" \
        --store  STORE_BLR_002
fi

# ── Step 2: Normalise POS transactions (if present) ───────────────────────
RAW_POS="./data/pos_transactions.csv"
NORM_POS="./data/pos_transactions_normalised.csv"
if [[ -f "$RAW_POS" ]]; then
    echo "[INFO] Normalising POS transactions …"
    python pipeline/normalise_pos.py \
        --input  "$RAW_POS" \
        --output "$NORM_POS"
fi

# ── Step 3: Ingest translated sample events (quick validation) ────────────
if [[ -f "$TRANSLATED_EVENTS" ]] && [[ -n "$API_URL" ]]; then
    echo "[INFO] Ingesting sample_events.jsonl into API at $API_URL …"
    python pipeline/ingest_batch.py \
        --file "$TRANSLATED_EVENTS" \
        --api  "$API_URL" || echo "[WARN] API not yet running — skipping ingest"
fi

# ── Step 4: Process actual CCTV clips (if any exist) ─────────────────────
process_clip() {
    local clip="$1"
    local filename
    filename=$(basename "$clip" .mp4)

    local store_id camera_id
    store_id=$(echo "$filename" | sed 's/_CAM_.*//')
    camera_id=$(echo "$filename" | grep -oP 'CAM_\w+')

    if [[ -z "$store_id" || -z "$camera_id" ]]; then
        echo "[WARN] Cannot parse store/camera from filename: $filename — skipping"
        return
    fi

    local output_file="$OUTPUT_DIR/${filename}.jsonl"
    echo "[INFO] Processing $filename → $output_file"

    python pipeline/detect.py \
        --clip    "$clip" \
        --store   "$store_id" \
        --camera  "$camera_id" \
        --layout  "$LAYOUT_JSON" \
        --output  "$output_file" \
        ${API_URL:+--api-url "$API_URL"} \
        --replay-speed "$REPLAY_SPEED"
}

export -f process_clip

CLIP_COUNT=$(find "$CLIPS_DIR" -name "*.mp4" 2>/dev/null | wc -l || echo 0)
if [[ "$CLIP_COUNT" -gt 0 ]]; then
    echo "[INFO] Found $CLIP_COUNT clips in $CLIPS_DIR — processing …"
    find "$CLIPS_DIR" -name "*.mp4" -print0 | \
        xargs -0 -P 4 -I {} bash -c 'process_clip "$@"' _ {}
    echo ""
    echo "✅ All clips processed. Events written to: $OUTPUT_DIR"
    if [[ -n "$API_URL" ]]; then
        echo "[INFO] Ingesting all JSONL event files into API …"
        python pipeline/ingest_batch.py --dir "$OUTPUT_DIR" --api "$API_URL"
    fi
else
    echo "[INFO] No .mp4 clips found in $CLIPS_DIR — skipping video processing."
    echo "       To process clips, place them at: $CLIPS_DIR/<STORE_ID>_<CAM_ID>.mp4"
fi

echo ""
echo "Done. Run the dashboard with:"
echo "  python dashboard/live_dashboard.py --store STORE_BLR_002 --api $API_URL"
