"""The schema accepts tracker events and rejects each kind of bad event with a clear path."""

from __future__ import annotations

from typing import Any

import pytest
from helpers import make_event
from jsonschema.exceptions import SchemaError

from event_tracking_pipeline import EventValidator, load_schema
from event_tracking_pipeline.schema import MAX_MESSAGE, MAX_PROBLEMS

V = EventValidator()


def paths(event: object) -> list[str]:
    """Problem paths for an event."""
    return [p.path for p in V.problems(event)]


def test_valid_event_has_no_problems() -> None:
    assert V.problems(make_event()) == []
    assert V.is_valid(make_event())


def test_minimal_event_is_valid() -> None:
    event = make_event()
    del event["context"], event["properties"]
    assert V.is_valid(event)


def test_identify_with_user_id_is_valid() -> None:
    assert V.is_valid(make_event(type="identify", name="identify", user_id="user-1"))


@pytest.mark.parametrize(
    "field", ["schema_version", "event_id", "type", "name", "anonymous_id", "timestamp"]
)
def test_each_required_field_is_required(field: str) -> None:
    event = make_event()
    del event[field]
    problems = V.problems(event)
    assert len(problems) == 1
    assert f"'{field}' is a required property" in problems[0].message


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("event_id", "not-a-uuid"),
        ("event_id", "00000000-0000-1000-8000-000000000001"),
        ("event_id", "00000000-0000-4000-8000-00000000000G"),
        ("type", "click"),
        ("name", ""),
        ("name", "1st_click"),
        ("name", "x" * 129),
        ("anonymous_id", "short"),
        ("anonymous_id", "has space in it"),
        ("user_id", ""),
        ("timestamp", "2026-11-14 10:00:00"),
        ("timestamp", "2026-11-14T10:00:00+02:00"),
        ("timestamp", 1731578400),
        ("properties", {"nested": {"a": 1}}),
        ("properties", {"bad-key": 1}),
        ("properties", {"list": [1, 2]}),
        ("properties", {"s": "x" * 1025}),
        ("context", {"unknown": 1}),
    ],
)
def test_bad_field_is_reported_at_its_path(field: str, value: Any) -> None:
    assert any(p.startswith(f"/{field}") for p in paths(make_event(**{field: value})))


def test_unknown_top_level_field_is_rejected() -> None:
    assert "Additional properties are not allowed" in V.problems(make_event(extra=1))[0].message


def test_identify_requires_a_user_id() -> None:
    assert V.problems(make_event(type="identify", name="identify")) != []
    assert V.problems(make_event(type="identify", name="identify", user_id=None)) != []


def test_too_many_properties() -> None:
    props = {f"k{i}": i for i in range(51)}
    assert paths(make_event(properties=props)) == ["/properties"]
    assert V.is_valid(make_event(properties={f"k{i}": i for i in range(50)}))


@pytest.mark.parametrize(
    "ts", ["2026-02-30T10:00:00Z", "2026-13-01T10:00:00Z", "2026-11-14T25:00:00Z"]
)
def test_timestamp_must_be_a_real_date(ts: str) -> None:
    problems = V.problems(make_event(timestamp=ts))
    assert [(p.path, p.message) for p in problems] == [
        ("/timestamp", "not a calendar date and time")
    ]


@pytest.mark.parametrize("ts", ["2024-02-29T23:59:59Z", "2026-11-14T10:00:00.123456Z"])
def test_valid_timestamps(ts: str) -> None:
    assert V.is_valid(make_event(timestamp=ts))


@pytest.mark.parametrize("event", [None, [], "event", 42])
def test_non_object_event(event: object) -> None:
    assert paths(event) == [""]
    assert str(V.problems(event)[0]).startswith("/: ")


def test_problems_are_capped_and_messages_truncated() -> None:
    event = make_event(properties={f"bad-{i}-" + "y" * 300: [i] for i in range(30)})
    problems = V.problems(event)
    assert len(problems) == MAX_PROBLEMS
    assert all(len(p.message) <= MAX_MESSAGE for p in problems)


def test_problems_are_sorted_by_path() -> None:
    found = paths(make_event(name="", anonymous_id="x", event_id="y"))
    assert found == sorted(found)
    assert found == ["/anonymous_id", "/event_id", "/name"]


def test_schema_loads_and_rejects_unknown_versions() -> None:
    schema = load_schema()
    assert schema["properties"]["schema_version"] == {"const": 1}
    with pytest.raises(ValueError, match="unknown schema version"):
        load_schema(2)


def test_invalid_schema_fails_at_construction() -> None:
    with pytest.raises(SchemaError):
        EventValidator({"type": "no-such-type"})


@pytest.mark.parametrize("field", ["user_id", "properties"])
def test_lone_surrogates_are_rejected(field: str) -> None:
    value: Any = "\udc00x" if field == "user_id" else {"s": "a\ud800"}
    assert [p.message for p in V.problems(make_event(**{field: value}))] == [
        "contains a lone surrogate (not valid Unicode text)"
    ]
