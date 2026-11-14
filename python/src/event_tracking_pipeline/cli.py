"""Command line: serve the collector, report on a database, validate a batch file."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from .collector import Collector
from .schema import EventValidator
from .server import build_server
from .store import EventStore

MAX_FILE_BYTES = 10_000_000


def _origins(text: str) -> list[str]:
    """Comma-separated http(s) origins, validated."""
    out = [o.strip() for o in text.split(",") if o.strip()]
    for o in out:
        if not o.startswith(("http://", "https://")) or o.count("/") != 2:
            raise argparse.ArgumentTypeError(f"not an origin (scheme://host[:port]): {o!r}")
    return out


def _port(text: str) -> int:
    """TCP port number 0..65535."""
    if not text.isdigit() or not 0 <= int(text) <= 65535:
        raise argparse.ArgumentTypeError(f"not a port: {text!r}")
    return int(text)


def build_parser(env: Mapping[str, str]) -> argparse.ArgumentParser:
    """Argument parser; defaults come from ETP_* environment variables."""
    p = argparse.ArgumentParser(prog="etp-collector", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the HTTP collector")
    serve.add_argument("--db", default=env.get("ETP_DB", "events.sqlite3"))
    serve.add_argument("--host", default=env.get("ETP_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=_port, default=env.get("ETP_PORT", "8787"))
    serve.add_argument(
        "--allow-origins",
        type=_origins,
        default=env.get("ETP_ALLOWED_ORIGINS", ""),
        help="comma-separated browser origins allowed by CORS",
    )
    report = sub.add_parser("report", help="event and dead-letter counts")
    report.add_argument("--db", default=env.get("ETP_DB", "events.sqlite3"))
    report.add_argument("--recent", type=int, default=5, help="dead letters to show (1-1000)")
    validate = sub.add_parser("validate", help="check a batch or event JSON file offline")
    validate.add_argument("file", type=Path)
    return p


def _serve(args: argparse.Namespace) -> int:
    """Run until interrupted."""
    store = EventStore(args.db)
    server = build_server(
        Collector(store, allowed_origins=args.allow_origins), args.host, args.port
    )
    print(
        f"collector on http://{args.host}:{server.server_port}/v1/batch, db {args.db}", flush=True
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopping", flush=True)
    finally:
        server.server_close()
        store.close()
    return 0


def _report(args: argparse.Namespace) -> int:
    """Print counts from an existing database."""
    if not Path(args.db).is_file():
        print(f"error: no database at {args.db}", file=sys.stderr)
        return 2
    store = EventStore(args.db)
    try:
        print(f"events stored: {store.count_events()}")
        for name, count in store.events_by_name():
            print(f"  {name}: {count}")
        summary = store.dead_letter_summary()
        print(f"dead letters: {sum(n for _, n, _ in summary)} rows")
        for reason, rows, seen in summary:
            print(f"  {reason}: {rows} rows, seen {seen} times")
        if summary:
            for reason, detail, seen in store.dead_letters(args.recent):
                print(f"  - [{reason}] x{seen} {detail[:150]}")
    finally:
        store.close()
    return 0


def _validate(args: argparse.Namespace) -> int:
    """Validate a file holding one event, a list of events or a batch envelope."""
    path: Path = args.file
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        print(f"error: {path} is missing or larger than {MAX_FILE_BYTES} bytes", file=sys.stderr)
        return 2
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        print(f"error: {path} is not JSON: {exc}", file=sys.stderr)
        return 2
    events = data.get("events") if isinstance(data, dict) and "events" in data else data
    events = events if isinstance(events, list) else [events]
    validator = EventValidator()
    bad = 0
    for i, event in enumerate(events):
        problems = validator.problems(event)
        bad += bool(problems)
        status = "ok" if not problems else "; ".join(str(p) for p in problems)
        print(f"event {i}: {status}")
    print(f"{len(events) - bad} valid, {bad} invalid")
    return 1 if bad else 0


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """CLI entry point; returns the exit code."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = build_parser(os.environ if env is None else env)
    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    if args.command == "report":
        if not 1 <= args.recent <= 1000:
            parser.error("--recent must be between 1 and 1000")
        return _report(args)
    return _validate(args)


if __name__ == "__main__":
    raise SystemExit(main())
