"""SQLite store: idempotent event and dead-letter writes, atomic batches, thread safety."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from helpers import make_event

from event_tracking_pipeline import DeadLetter, EventStore
from event_tracking_pipeline.store import MAX_DEAD_PAYLOAD

T0 = "2026-11-14T10:00:00.000Z"
T1 = "2026-11-14T11:00:00.000Z"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    """Fresh file-backed store."""
    s = EventStore(tmp_path / "events.sqlite3")
    yield s
    s.close()


def test_writing_the_same_batch_twice_stores_it_once(store: EventStore) -> None:
    events = [make_event(), make_event()]
    first = store.write_batch(events, [], T0)
    second = store.write_batch(events, [], T1)
    assert (first.inserted, first.duplicates) == (2, 0)
    assert (second.inserted, second.duplicates) == (0, 2)
    assert store.count_events() == 2


def test_duplicate_ids_inside_one_batch(store: EventStore) -> None:
    e = make_event()
    result = store.write_batch([e, e, make_event()], [], T0)
    assert (result.inserted, result.duplicates) == (2, 1)


def test_first_write_wins_for_an_event_id(store: EventStore) -> None:
    e = make_event(name="first")
    store.write_batch([e], [], T0)
    store.write_batch([{**e, "name": "second"}], [], T1)
    assert store.events_by_name() == [("first", 1)]


def test_dead_letters_are_stored_once_and_counted(store: EventStore) -> None:
    d = DeadLetter("schema", "/name: bad", '{"name": ""}')
    store.write_batch([], [d], T0)
    store.write_batch([], [d], T1)
    store.write_batch([], [DeadLetter("invalid_json", "x", "{oops")], T1)
    assert store.dead_letter_summary() == [("invalid_json", 1, 1), ("schema", 1, 2)]
    assert store.dead_letters(10)[0][0] in {"schema", "invalid_json"}


def test_same_payload_for_a_different_reason_is_a_separate_dead_letter() -> None:
    assert DeadLetter("a", "", "p").fingerprint != DeadLetter("b", "", "p").fingerprint
    assert DeadLetter("a", "x", "p").fingerprint == DeadLetter("a", "y", "p").fingerprint


def test_dead_letter_payload_is_capped(store: EventStore) -> None:
    store.write_batch([], [DeadLetter("invalid_json", "big", "x" * (MAX_DEAD_PAYLOAD * 2))], T0)
    row = store._conn.execute("SELECT length(payload) FROM dead_letters").fetchone()
    assert row[0] == MAX_DEAD_PAYLOAD


def test_dead_letter_with_lone_surrogate_payload(store: EventStore) -> None:
    store.write_batch([], [DeadLetter("invalid_json", "x", "\ud800")], T0)
    assert store.dead_letter_summary() == [("invalid_json", 1, 1)]


def test_failed_batch_writes_nothing(store: EventStore) -> None:
    broken = make_event()
    del broken["name"]
    with pytest.raises(KeyError):
        store.write_batch([make_event(), broken], [DeadLetter("schema", "x", "y")], T0)
    assert store.count_events() == 0
    assert store.dead_letter_summary() == []


def test_stored_row_keeps_client_and_server_times(store: EventStore) -> None:
    e = make_event(user_id="u1")
    store.write_batch([e], [], T1)
    row = store._conn.execute("SELECT user_id, client_ts, received_at FROM events").fetchone()
    assert row == ("u1", e["timestamp"], T1)


def test_concurrent_writers_store_each_event_exactly_once(store: EventStore) -> None:
    events = [make_event() for _ in range(200)]
    errors: list[BaseException] = []

    def writer(offset: int) -> None:
        try:
            for i in range(0, 200, 10):
                chunk = events[(i + offset) % 200 : (i + offset) % 200 + 10]
                store.write_batch(chunk, [], T0)
        except Exception as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(k * 10,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert store.count_events() == 200


def test_data_survives_reopening(tmp_path: Path) -> None:
    path = tmp_path / "e.sqlite3"
    s = EventStore(path)
    s.write_batch([make_event()], [], T0)
    s.close()
    s2 = EventStore(path)
    assert s2.count_events() == 1
    s2.close()


def test_in_memory_store() -> None:
    s = EventStore(":memory:")
    assert s.write_batch([make_event()], [], T0).inserted == 1
    s.close()


@pytest.mark.parametrize("limit", [0, 1001])
def test_dead_letter_limit_is_checked(store: EventStore, limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        store.dead_letters(limit)
