import argparse
import csv
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("normalise_pos")

STORE_CODE_MAP = {
    "ST1008": "STORE_MUM_001",
    "ST1076": "STORE_BLR_002",
    "ST1012": "STORE_DEL_001",
    "ST1021": "STORE_HYD_001",
    "ST1033": "STORE_CHE_001",
}


def parse_timestamp(date_str: str, time_str: str) -> str:
    """Convert DD-MM-YYYY + HH:MM:SS → ISO-8601 UTC Z."""
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%d-%m-%Y %H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalise(input_path: Path, output_path: Path):
    # Group rows by (order_id, store_id, date, time) and sum basket value
    orders: dict[str, dict] = {}

    with open(input_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            order_id = row.get("order_id", "").strip()
            if not order_id:
                continue
            key = order_id
            if key not in orders:
                raw_store = row.get("store_id", "").strip()
                orders[key] = {
                    "transaction_id": f"TXN_{order_id.zfill(6)}",
                    "store_id": STORE_CODE_MAP.get(raw_store, raw_store),
                    "timestamp": parse_timestamp(
                        row.get("order_date", "").strip(),
                        row.get("order_time", "").strip(),
                    ),
                    "basket_value_inr": 0.0,
                }
            try:
                orders[key]["basket_value_inr"] += float(row.get("total_amount", 0))
            except ValueError:
                pass

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["store_id", "transaction_id", "timestamp", "basket_value_inr"]
        )
        writer.writeheader()
        for order in sorted(orders.values(), key=lambda x: x["timestamp"]):
            order["basket_value_inr"] = round(order["basket_value_inr"], 2)
            writer.writerow(order)

    logger.info("Wrote %d transactions to %s", len(orders), output_path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",  default="data/pos_transactions.csv")
    parser.add_argument("--output", default="data/pos_transactions_normalised.csv")
    args = parser.parse_args()
    normalise(Path(args.input), Path(args.output))


if __name__ == "__main__":
    main()
