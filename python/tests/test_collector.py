"""Collector: good events stored, bad ones dead-lettered, HTTP edge cases answered correctly."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from typing import Any

import pytest
from helpers import batch_body, make_event

from event_tracking_pipeline import Collector, EventStore
from event_tracking_pipeline.collector import utc_now

NOW = "2026-11-14T12:00:00.000Z"


@pytest.fixture
def store() -> Iterator[EventStore]:
    """In-memory store."""
    s = EventStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def collector(store: EventStore) -> Collector:
    """Collector with a fixed clock and one allowed origin."""
    return Collector(store, clock=lambda: NOW, allowed_origins=["https://shop.example.test"])


def call(
    app: Collector, method: str, path: str, body: bytes = b"", **headers: str
) -> tuple[int, dict[str, str], Any]:
    """Run one WSGI request and return status, headers and parsed body."""
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_TYPE": headers.pop("content_type", "application/json"),
        "CONTENT_LENGTH": headers.pop("content_length", str(len(body))),
        "wsgi.input": io.BytesIO(body),
    }
    environ.update({f"HTTP_{k.upper()}": v for k, v in headers.items()})
    seen: dict[str, Any] = {}

    def start_response(status: str, hdrs: list[tuple[str, str]]) -> None:
        seen["status"], seen["headers"] = int(status.split()[0]), dict(hdrs)

    data = b"".join(app(environ, start_response))
    assert seen["headers"]["Content-Length"] == str(len(data))
    return seen["status"], seen["headers"], json.loads(data) if data else None


def test_valid_batch_is_stored(collector: Collector, store: EventStore) -> None:
    r = collector.ingest(batch_body(make_event(), make_event()))
    assert (r.status, r.accepted, r.duplicates, r.rejected) == (200, 2, 0, 0)
    assert store.count_events() == 2
    row = store._conn.execute("SELECT received_at FROM events").fetchone()
    assert row == (NOW,)


def test_mixed_batch_keeps_good_events_and_dead_letters_bad_ones(
    collector: Collector, store: EventStore
) -> None:
    body = batch_body(make_event(), make_event(name=""), make_event(event_id="x"), make_event())
    r = collector.ingest(body)
    assert (r.status, r.accepted, r.rejected) == (200, 2, 2)
    assert store.count_events() == 2
    assert store.dead_letter_summary() == [("schema", 2, 2)]
    details = sorted(d for _, d, _ in store.dead_letters())
    assert details[0].startswith("/event_id:")
    assert details[1].startswith("/name:")


def test_retried_batch_is_counted_as_duplicates(collector: Collector, store: EventStore) -> None:
    body = batch_body(make_event(), make_event())
    collector.ingest(body)
    r = collector.ingest(body)
    assert (r.status, r.accepted, r.duplicates) == (200, 0, 2)
    assert store.count_events() == 2


@pytest.mark.parametrize(
    "body", [b"{not json", b"", b"\xff\xfe", b'{"events": [NaN]}', b"[" * 100_000]
)
def test_unparseable_body_is_dead_lettered(
    collector: Collector, store: EventStore, body: bytes
) -> None:
    r = collector.ingest(body)
    assert r.status == 400
    assert r.error == "body is not valid JSON"
    assert store.dead_letter_summary() == [("invalid_json", 1, 1)]


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (b"[]", "body must be a JSON object"),
        (b'{"events": []}', "events must be a non-empty array"),
        (b'{"events": {}}', "events must be a non-empty array"),
        (b'{"events": [{}], "extra": 1}', 'unknown envelope fields: "extra"'),
        (b'{"events": [{}], "sent_at": "yesterday"}', "sent_at must be an RFC 3339 UTC timestamp"),
        (b'{"events": [{}], "sent_at": 5}', "sent_at must be an RFC 3339 UTC timestamp"),
    ],
)
def test_bad_envelope_is_dead_lettered(
    collector: Collector, store: EventStore, body: bytes, error: str
) -> None:
    r = collector.ingest(body)
    assert (r.status, r.error) == (400, error)
    assert store.dead_letter_summary() == [("invalid_envelope", 1, 1)]


def test_too_many_events(store: EventStore) -> None:
    c = Collector(store, max_events=3)
    r = c.ingest(batch_body(*[make_event() for _ in range(4)]))
    assert (r.status, r.error) == (400, "at most 3 events per batch, got 4")
    assert c.ingest(batch_body(*[make_event() for _ in range(3)])).accepted == 3


def test_sent_at_is_optional(collector: Collector) -> None:
    assert collector.ingest(batch_body(make_event(), sent_at=None)).status == 200


def test_store_failure_answers_503_so_clients_retry(store: EventStore) -> None:
    c = Collector(store)
    store.close()
    r = c.ingest(batch_body(make_event()))
    assert (r.status, r.error) == (503, "storage unavailable, retry later")


def test_limits_must_be_positive(store: EventStore) -> None:
    with pytest.raises(ValueError, match="positive"):
        Collector(store, max_events=0)


def test_utc_now_format() -> None:
    assert utc_now().endswith("Z")
    assert len(utc_now()) == len("2026-11-14T12:00:00.000Z")


# HTTP layer


def test_post_batch(collector: Collector) -> None:
    status, headers, body = call(collector, "POST", "/v1/batch", batch_body(make_event()))
    assert status == 200
    assert body == {"accepted": 1, "duplicates": 0, "rejected": 0}
    assert headers["Content-Type"] == "application/json"
    assert headers["Cache-Control"] == "no-store"


def test_post_with_text_plain_and_charset(collector: Collector) -> None:
    status, _, _ = call(
        collector,
        "POST",
        "/v1/batch",
        batch_body(make_event()),
        content_type="text/plain;charset=UTF-8",
    )
    assert status == 200


def test_rejected_batch_answers_400_with_counts(collector: Collector) -> None:
    status, _, body = call(collector, "POST", "/v1/batch", b"nope")
    assert status == 400
    assert body["error"] == "body is not valid JSON"


def test_healthz(collector: Collector) -> None:
    assert call(collector, "GET", "/healthz")[0::2] == (200, {"status": "ok"})
    assert call(collector, "POST", "/healthz")[0] == 405


@pytest.mark.parametrize(
    ("method", "path", "kwargs", "status"),
    [
        ("GET", "/nope", {}, 404),
        ("GET", "/v1/batch", {}, 405),
        ("PUT", "/v1/batch", {}, 405),
        ("POST", "/v1/batch", {"content_type": "application/xml"}, 415),
        ("POST", "/v1/batch", {"content_type": ""}, 415),
        ("POST", "/v1/batch", {"content_length": ""}, 411),
        ("POST", "/v1/batch", {"content_length": "-5"}, 411),
        ("POST", "/v1/batch", {"content_length": "abc"}, 411),
        ("POST", "/v1/batch", {"content_length": "262145"}, 413),
    ],
)
def test_http_errors(
    collector: Collector, method: str, path: str, kwargs: dict[str, str], status: int
) -> None:
    assert call(collector, method, path, b"{}", **kwargs)[0] == status


def test_body_at_the_size_limit_is_read(store: EventStore) -> None:
    c = Collector(store, max_body_bytes=200)
    body = b'{"events": [{}]}'.ljust(200)
    assert call(c, "POST", "/v1/batch", body)[0] == 200
    assert call(c, "POST", "/v1/batch", body + b" ")[0] == 413


def test_405_lists_allowed_methods(collector: Collector) -> None:
    assert call(collector, "GET", "/v1/batch")[1]["Allow"] == "POST, OPTIONS"


def test_cors_for_allowed_origin_only(collector: Collector) -> None:
    status, headers, body = call(
        collector, "OPTIONS", "/v1/batch", origin="https://shop.example.test"
    )
    assert (status, body) == (204, None)
    assert headers["Access-Control-Allow-Origin"] == "https://shop.example.test"
    assert headers["Vary"] == "Origin"
    _, headers, _ = call(collector, "OPTIONS", "/v1/batch", origin="https://evil.example.test")
    assert "Access-Control-Allow-Origin" not in headers
    _, headers, _ = call(
        collector, "POST", "/v1/batch", batch_body(make_event()), origin="https://shop.example.test"
    )
    assert headers["Access-Control-Allow-Origin"] == "https://shop.example.test"


def test_lone_surrogate_is_dead_lettered_instead_of_crashing(
    collector: Collector, store: EventStore
) -> None:
    r = collector.ingest(batch_body(make_event(user_id="\ud800")))
    assert (r.status, r.accepted, r.rejected) == (200, 0, 1)
    assert store.dead_letter_summary() == [("schema", 1, 1)]
