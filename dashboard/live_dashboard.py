"""
dashboard/live_dashboard.py — Terminal live dashboard for Apex Retail Store Intelligence.

Uses the `rich` library for a styled terminal UI that polls the API every 2 seconds.
Shows: visitor count, conversion rate, queue depth, active anomalies, zone heatmap.

Usage:
    python dashboard/live_dashboard.py --store STORE_BLR_002 --api http://localhost:8000
    python dashboard/live_dashboard.py --store STORE_BLR_002 --api http://localhost:8000 \
        --replay-events ./data/events/STORE_BLR_002_CAM_ENTRY_01.jsonl

The --replay-events flag simulates live event ingestion by drip-feeding a JSONL file
at 1-event-per-second to demonstrate the connected pipeline.
"""

import argparse
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich import box
except ImportError:
    print("Install rich: pip install rich")
    sys.exit(1)

console = Console()


# ─────────────────────────────────────────────────────────────────────────────
# API client
# ─────────────────────────────────────────────────────────────────────────────

class StoreAPIClient:
    def __init__(self, base_url: str, store_id: str):
        self.base_url = base_url.rstrip("/")
        self.store_id = store_id
        self._session = requests.Session()

    def metrics(self) -> dict | None:
        return self._get(f"/stores/{self.store_id}/metrics")

    def funnel(self) -> dict | None:
        return self._get(f"/stores/{self.store_id}/funnel")

    def heatmap(self) -> dict | None:
        return self._get(f"/stores/{self.store_id}/heatmap")

    def anomalies(self) -> dict | None:
        return self._get(f"/stores/{self.store_id}/anomalies")

    def health(self) -> dict | None:
        return self._get("/health")

    def _get(self, path: str) -> dict | None:
        try:
            r = self._session.get(f"{self.base_url}{path}", timeout=3)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard builder
# ─────────────────────────────────────────────────────────────────────────────

SEVERITY_COLOUR = {"INFO": "cyan", "WARN": "yellow", "CRITICAL": "red"}


def render_header(store_id: str, health: dict | None) -> Panel:
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    db_status = health.get("database", "?") if health else "?"
    overall = health.get("status", "?") if health else "?"
    colour = "green" if overall == "healthy" else "yellow" if overall == "degraded" else "red"
    text = Text()
    text.append("  APEX RETAIL — STORE INTELLIGENCE  ", style="bold white on blue")
    text.append(f"  {store_id}  ", style="bold cyan")
    text.append(f"  {now}  ", style="dim")
    text.append(f"  ●  {overall.upper()}", style=f"bold {colour}")
    return Panel(text, box=box.DOUBLE_EDGE)


def render_metrics(m: dict | None) -> Panel:
    if not m:
        return Panel("[red]No metrics data[/]", title="📊 Metrics", border_style="red")
    cv = m.get("conversion_rate", 0)
    cv_colour = "green" if cv >= 0.3 else "yellow" if cv >= 0.15 else "red"
    q = m.get("queue_depth_current", 0)
    q_colour = "red" if q >= 8 else "yellow" if q >= 4 else "green"

    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    table.add_column("Metric", style="bold")
    table.add_column("Value")
    table.add_row("Unique Visitors", f"[bold white]{m.get('unique_visitors', 0)}[/]")
    table.add_row("Conversion Rate", f"[bold {cv_colour}]{cv:.1%}[/]")
    table.add_row("Queue Depth", f"[bold {q_colour}]{q}[/]")
    table.add_row("Abandonment Rate", f"{m.get('abandonment_rate', 0):.1%}")
    table.add_row("Transactions", str(m.get("total_transactions", 0)))
    return Panel(table, title="📊 Metrics", border_style="cyan")


def render_funnel(f: dict | None) -> Panel:
    if not f:
        return Panel("[red]No funnel data[/]", title="🔻 Funnel", border_style="red")

    table = Table(box=box.SIMPLE)
    table.add_column("Stage", style="bold")
    table.add_column("Count", justify="right")
    table.add_column("Drop-off", justify="right")

    for stage in f.get("stages", []):
        drop = stage.get("drop_off_pct", 0)
        drop_colour = "red" if drop >= 50 else "yellow" if drop >= 25 else "green"
        table.add_row(
            stage["stage"],
            str(stage["count"]),
            f"[{drop_colour}]{drop:.1f}%[/]",
        )
    return Panel(table, title="🔻 Funnel", border_style="blue")


def render_heatmap(h: dict | None) -> Panel:
    if not h or not h.get("zones"):
        return Panel("[dim]No zone data[/]", title="🗺️  Heatmap", border_style="dim")

    table = Table(box=box.SIMPLE)
    table.add_column("Zone", style="bold", width=20)
    table.add_column("Score", justify="right")
    table.add_column("Visits", justify="right")
    table.add_column("Confidence")

    BAR_CHARS = "▁▂▃▄▅▆▇█"
    for zone in h["zones"][:8]:  # top 8
        score = zone.get("normalised_score", 0)
        bar_idx = int(score / 100 * (len(BAR_CHARS) - 1))
        bar = BAR_CHARS[bar_idx]
        conf_style = "dim red" if zone.get("data_confidence") == "LOW" else "green"
        table.add_row(
            zone["zone_id"],
            f"[bold cyan]{bar} {score:.0f}[/]",
            str(zone.get("visit_frequency", 0)),
            f"[{conf_style}]{zone.get('data_confidence', '?')}[/]",
        )
    return Panel(table, title="🗺️  Zone Heatmap", border_style="magenta")


def render_anomalies(a: dict | None) -> Panel:
    if not a:
        return Panel("[red]No anomaly data[/]", title="⚠️  Anomalies", border_style="red")
    anomalies = a.get("anomalies", [])
    if not anomalies:
        return Panel("[green]  No active anomalies[/]", title="⚠️  Anomalies", border_style="green")

    table = Table(box=box.SIMPLE)
    table.add_column("Severity", width=8)
    table.add_column("Type", width=22)
    table.add_column("Description")

    for an in anomalies[:5]:
        sev = an.get("severity", "INFO")
        colour = SEVERITY_COLOUR.get(sev, "white")
        table.add_row(
            f"[bold {colour}]{sev}[/]",
            an.get("anomaly_type", ""),
            an.get("description", "")[:60],
        )
    return Panel(table, title=f"⚠️  Anomalies ({len(anomalies)})", border_style="yellow")


def build_layout(
    store_id: str,
    health: dict | None,
    metrics: dict | None,
    funnel: dict | None,
    heatmap: dict | None,
    anomalies: dict | None,
    event_count: int,
) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(render_header(store_id, health), size=3),
        Layout(name="main"),
        Layout(Panel(f"[dim]Events ingested this session: {event_count}[/]"), size=3),
    )
    layout["main"].split_row(
        Layout(name="left"),
        Layout(name="right"),
    )
    layout["left"].split_column(
        Layout(render_metrics(metrics)),
        Layout(render_funnel(funnel)),
    )
    layout["right"].split_column(
        Layout(render_heatmap(heatmap)),
        Layout(render_anomalies(anomalies)),
    )
    return layout


# ─────────────────────────────────────────────────────────────────────────────
# Event replay thread
# ─────────────────────────────────────────────────────────────────────────────

class EventReplayer:
    def __init__(self, jsonl_path: str, api_url: str, events_per_second: float = 5.0):
        self.path = jsonl_path
        self.api_url = api_url
        self.eps = events_per_second
        self.count = 0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        interval = 1.0 / self.eps
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    requests.post(
                        f"{self.api_url}/events/ingest",
                        json={"events": [event]},
                        timeout=2,
                    )
                    self.count += 1
                except Exception:
                    pass
                time.sleep(interval)


# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────

def run_dashboard(store_id: str, api_url: str, replay_path: str | None, poll_interval: float):
    client = StoreAPIClient(api_url, store_id)
    replayer = None
    if replay_path:
        replayer = EventReplayer(replay_path, api_url)
        replayer.start()
        console.print(f"[cyan]Replaying events from {replay_path}[/]")

    with Live(console=console, refresh_per_second=2, screen=True) as live:
        while True:
            health = client.health()
            metrics = client.metrics()
            funnel = client.funnel()
            heatmap = client.heatmap()
            anomalies = client.anomalies()
            event_count = replayer.count if replayer else 0

            live.update(build_layout(
                store_id, health, metrics, funnel, heatmap, anomalies, event_count
            ))
            time.sleep(poll_interval)


def main():
    parser = argparse.ArgumentParser(description="Apex Retail Live Dashboard")
    parser.add_argument("--store", default="STORE_BLR_002", help="Store ID to display")
    parser.add_argument("--api", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--replay-events", help="Path to JSONL events file for live replay")
    parser.add_argument("--poll", type=float, default=2.0, help="Poll interval in seconds")
    args = parser.parse_args()

    try:
        run_dashboard(args.store, args.api, args.replay_events, args.poll)
    except KeyboardInterrupt:
        console.print("\n[dim]Dashboard closed.[/]")


if __name__ == "__main__":
    main()
