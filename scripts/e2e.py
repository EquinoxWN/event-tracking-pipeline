"""End-to-end check: the real JS tracker sends to the real collector on 127.0.0.1, then counts are asserted.

usage: python scripts/e2e.py [--events N] [--db PATH]   (needs Node.js 24+ on PATH)
Prints a summary and `E2E PASS`, or the mismatches and exits 1.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from event_tracking_pipeline import Collector, EventStore
from event_tracking_pipeline.server import build_server

ROOT = Path(__file__).resolve().parent.parent
SENDER = ROOT / "js" / "scripts" / "e2e-send.js"
NODE_TIMEOUT_S = 60


def run(events: int, db: Path) -> tuple[dict[str, Any], list[str]]:
    """Start the collector, run the sender, return its summary and any mismatches."""
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("node is not on PATH")
    store = EventStore(db)
    server = build_server(Collector(store), "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1/batch"
        proc = subprocess.run(  # noqa: S603 - fixed argv: node, this repo's script, loopback URL
            [node, str(SENDER), url, str(events)],
            capture_output=True,
            text=True,
            timeout=NODE_TIMEOUT_S,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"sender failed ({proc.returncode}): {proc.stderr.strip()[-500:]}")
        summary: dict[str, Any] = json.loads(proc.stdout.strip().splitlines()[-1])
        summary["stored_events"] = store.count_events()
        summary["dead_letters"] = {r: n for r, n, _ in store.dead_letter_summary()}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        store.close()
    expected = {
        "sent": events,
        "queued": 0,
        "deniedDropped": 5,
        "offlineStatus": "offline",
        "stored_events": events + 1,
        "dead_letters": {"invalid_json": 1, "schema": 3},
    }
    problems = [
        f"{k}: want {v!r}, got {summary.get(k)!r}"
        for k, v in expected.items()
        if summary.get(k) != v
    ]
    dropped = summary.get("dropped", {})
    if any(dropped.values()):
        problems.append(f"tracker dropped events: {dropped}")
    replay = summary.get("replay", {})
    if (
        replay.get("status") != 200
        or replay["body"]["accepted"] != 0
        or replay["body"]["duplicates"] < 1
    ):
        problems.append(f"replayed batch was not fully deduplicated: {replay}")
    if summary.get("mixed", {}).get("body") != {"accepted": 1, "duplicates": 0, "rejected": 3}:
        problems.append(f"mixed batch: {summary.get('mixed')}")
    if summary.get("garbage", {}).get("status") != 400:
        problems.append(f"invalid JSON body: {summary.get('garbage')}")
    if summary.get("requests", 0) < 2:
        problems.append("the simulated network drop was not retried")
    return summary, problems


def main(argv: list[str] | None = None) -> int:
    """Run once and print the result."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--events", type=int, default=200)
    p.add_argument("--db", type=Path)
    args = p.parse_args(argv)
    if not 3 <= args.events <= 10_000:
        p.error("--events must be between 3 and 10000")
    with tempfile.TemporaryDirectory(
        dir=ROOT / ".tmp" if (ROOT / ".tmp").is_dir() else None
    ) as tmp:
        db = args.db or Path(tmp) / "e2e.sqlite3"
        summary, problems = run(args.events, db)
    print(
        f"tracker: {summary['sent']} sent in {summary['requests']} requests, "
        f"{summary['deniedDropped']} dropped for denied consent, first flush {summary['offlineStatus']}"
    )
    print(
        f"collector: {summary['stored_events']} events stored, dead letters {summary['dead_letters']}"
    )
    print(f"replayed batch: {summary['replay']['body']}; mixed batch: {summary['mixed']['body']}")
    for line in problems:
        print(f"MISMATCH {line}")
    print("E2E FAIL" if problems else "E2E PASS")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
