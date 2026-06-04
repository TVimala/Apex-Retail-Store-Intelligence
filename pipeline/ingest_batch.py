"""
ingest_batch.py — Batch-ingest all JSONL event files into the Intelligence API.

Usage:
    python ingest_batch.py --dir ./data/events --api http://localhost:8000
    python ingest_batch.py --file events.jsonl --api http://localhost:8000
"""

import argparse
import json
import logging
import sys
from pathlib import Path
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ingest_batch")

BATCH_SIZE = 500  # API max


def load_jsonl(path: Path) -> list[dict]:
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError as e:
                    logger.warning("Skipping malformed line in %s: %s", path.name, e)
    return events


def post_batch(events: list[dict], api_url: str) -> dict:
    r = requests.post(
        f"{api_url}/events/ingest",
        json={"events": events},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def ingest_file(path: Path, api_url: str) -> int:
    events = load_jsonl(path)
    total = len(events)
    logger.info("Ingesting %d events from %s", total, path.name)
    ingested = 0
    for i in range(0, total, BATCH_SIZE):
        batch = events[i : i + BATCH_SIZE]
        try:
            result = post_batch(batch, api_url)
            ingested += result.get("accepted", len(batch))
        except requests.RequestException as e:
            logger.error("Batch %d failed: %s", i // BATCH_SIZE, e)
    logger.info("  → %d / %d accepted", ingested, total)
    return ingested


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", help="Directory of JSONL files")
    parser.add_argument("--file", help="Single JSONL file")
    parser.add_argument("--api", default="http://localhost:8000", help="API base URL")
    args = parser.parse_args()

    if not args.dir and not args.file:
        parser.error("Provide --dir or --file")

    paths: list[Path] = []
    if args.dir:
        paths.extend(sorted(Path(args.dir).glob("*.jsonl")))
    if args.file:
        paths.append(Path(args.file))

    if not paths:
        logger.error("No JSONL files found")
        sys.exit(1)

    total_ingested = 0
    for p in paths:
        total_ingested += ingest_file(p, args.api)

    logger.info("Done. Total events ingested: %d", total_ingested)


if __name__ == "__main__":
    main()
