"""The collector: parse a batch, validate each event, store good ones, dead-letter the rest."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .schema import EventValidator
from .store import DeadLetter, EventStore

LOG = logging.getLogger("event_tracking_pipeline.collector")

MAX_BODY_BYTES = 262_144
MAX_EVENTS = 100
TS_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$")
JSON_TYPES = ("application/json", "text/plain")

StartResponse = Callable[[str, list[tuple[str, str]]], object]

REASONS = {
    200: "200 OK",
    204: "204 No Content",
    400: "400 Bad Request",
    404: "404 Not Found",
    405: "405 Method Not Allowed",
    411: "411 Length Required",
    413: "413 Content Too Large",
    415: "415 Unsupported Media Type",
    503: "503 Service Unavailable",
}


def utc_now() -> str:
    """Current UTC time as RFC 3339 with a Z suffix."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class IngestResult:
    """Outcome of one batch: HTTP status and counts."""

    status: int
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    error: str | None = None

    def body(self) -> dict[str, Any]:
        """JSON response body."""
        out: dict[str, Any] = {
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
        }
        if self.error:
            out["error"] = self.error
        return out


def _reject_constant(name: str) -> Any:
    """Refuse NaN and Infinity, which are not JSON."""
    raise ValueError(f"{name} is not valid JSON")


class Collector:
    """Validates batches and writes them to an EventStore; also a WSGI app."""

    def __init__(
        self,
        store: EventStore,
        validator: EventValidator | None = None,
        *,
        clock: Callable[[], str] = utc_now,
        allowed_origins: Iterable[str] = (),
        max_body_bytes: int = MAX_BODY_BYTES,
        max_events: int = MAX_EVENTS,
    ) -> None:
        """Wire the store, validator, clock and limits."""
        if max_body_bytes < 1 or max_events < 1:
            raise ValueError("limits must be positive")
        self.store = store
        self.validator = validator or EventValidator()
        self.clock = clock
        self.allowed_origins = frozenset(allowed_origins)
        self.max_body_bytes = max_body_bytes
        self.max_events = max_events

    def ingest(self, body: bytes) -> IngestResult:
        """Process one request body; bad input is dead-lettered, never dropped."""
        received_at = self.clock()
        raw = body.decode("utf-8", "replace")
        try:
            batch = json.loads(body, parse_constant=_reject_constant)
        except (ValueError, RecursionError) as exc:
            dead = DeadLetter("invalid_json", str(exc)[:200], raw)
            return self._write([], [dead], received_at, error="body is not valid JSON")
        problem = self._envelope_problem(batch)
        if problem:
            dead = DeadLetter("invalid_envelope", problem, raw)
            return self._write([], [dead], received_at, error=problem)
        good: list[dict[str, Any]] = []
        bad: list[DeadLetter] = []
        for event in batch["events"]:
            problems = self.validator.problems(event)
            if problems:
                detail = "; ".join(str(p) for p in problems)
                bad.append(DeadLetter("schema", detail, json.dumps(event, sort_keys=True)))
            else:
                good.append(event)
        return self._write(good, bad, received_at)

    def _envelope_problem(self, batch: object) -> str | None:
        """Why the batch envelope is unusable, or None."""
        if not isinstance(batch, dict):
            return "body must be a JSON object"
        unknown = set(batch) - {"events", "sent_at"}
        if unknown:
            return f"unknown envelope fields: {', '.join(sorted(unknown))}"
        events = batch.get("events")
        if not isinstance(events, list) or not events:
            return "events must be a non-empty array"
        if len(events) > self.max_events:
            return f"at most {self.max_events} events per batch, got {len(events)}"
        sent_at = batch.get("sent_at")
        if sent_at is not None and (not isinstance(sent_at, str) or not TS_PATTERN.match(sent_at)):
            return "sent_at must be an RFC 3339 UTC timestamp"
        return None

    def _write(
        self,
        good: list[dict[str, Any]],
        bad: list[DeadLetter],
        received_at: str,
        error: str | None = None,
    ) -> IngestResult:
        """Store one batch; a store failure is a 503 so the client retries."""
        try:
            result = self.store.write_batch(good, bad, received_at)
        except sqlite3.Error:
            LOG.exception("store write failed")
            return IngestResult(503, error="storage unavailable, retry later")
        status = 400 if error else 200
        return IngestResult(status, result.inserted, result.duplicates, len(bad), error)

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> list[bytes]:
        """WSGI entry point: /v1/batch (POST, OPTIONS) and /healthz (GET)."""
        path = environ.get("PATH_INFO", "")
        method = environ.get("REQUEST_METHOD", "GET")
        cors = self._cors_headers(environ.get("HTTP_ORIGIN"))
        if path == "/healthz":
            if method != "GET":
                return _respond(start_response, 405, {"error": "use GET"}, [("Allow", "GET")])
            return _respond(start_response, 200, {"status": "ok"})
        if path != "/v1/batch":
            return _respond(start_response, 404, {"error": "not found"})
        if method == "OPTIONS":
            return _respond(start_response, 204, None, cors)
        if method != "POST":
            return _respond(
                start_response, 405, {"error": "use POST"}, [("Allow", "POST, OPTIONS")]
            )
        ctype = environ.get("CONTENT_TYPE", "").split(";")[0].strip().lower()
        if ctype not in JSON_TYPES:
            return _respond(start_response, 415, {"error": "send application/json"}, cors)
        length_text = environ.get("CONTENT_LENGTH", "")
        if not length_text.isdigit():
            return _respond(start_response, 411, {"error": "Content-Length required"}, cors)
        length = int(length_text)
        if length > self.max_body_bytes:
            return _respond(
                start_response, 413, {"error": f"body over {self.max_body_bytes} bytes"}, cors
            )
        body = environ["wsgi.input"].read(length)
        result = self.ingest(body)
        return _respond(start_response, result.status, result.body(), cors)

    def _cors_headers(self, origin: str | None) -> list[tuple[str, str]]:
        """CORS headers for an allowed browser origin, none otherwise."""
        if not origin or origin not in self.allowed_origins:
            return []
        return [
            ("Access-Control-Allow-Origin", origin),
            ("Access-Control-Allow-Methods", "POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type"),
            ("Access-Control-Max-Age", "600"),
            ("Vary", "Origin"),
        ]


def _respond(
    start_response: StartResponse,
    status: int,
    body: dict[str, Any] | None,
    headers: list[tuple[str, str]] | None = None,
) -> list[bytes]:
    """Send a JSON response (or an empty one for 204)."""
    data = b"" if body is None else json.dumps(body).encode()
    base = [("Content-Length", str(len(data))), ("Cache-Control", "no-store")]
    if body is not None:
        base.append(("Content-Type", "application/json"))
    start_response(REASONS[status], base + (headers or []))
    return [data]
