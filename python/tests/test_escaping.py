"""Untrusted text (ANSI escapes, newlines, long keys) never reaches messages or the terminal raw."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from helpers import make_event

from event_tracking_pipeline import Collector, DeadLetter, EventStore, EventValidator
from event_tracking_pipeline.cli import main
from event_tracking_pipeline.text import printable, quote, quote_list

ANSI = "\x1b[31mred\x1b[0m"
CLEAR = "\x1b[2J\x1b[H"
NASTY = [ANSI, "line1\nline2\r", "bidi" + chr(0x202E) + "evil", "bell" + chr(7), "nul" + chr(0)]


def is_safe(text: str) -> bool:
    """True when every character is printable (no ESC, CR, LF, bidi controls)."""
    return text.isprintable()


@pytest.mark.parametrize("raw", NASTY)
def test_printable_escapes_control_characters(raw: str) -> None:
    out = printable(raw)
    assert is_safe(out)
    assert out != raw


def test_printable_keeps_normal_text_and_caps_length() -> None:
    assert printable("José 山田 ok") == "José 山田 ok"
    assert printable("x" * 100, 10) == "x" * 10 + "..."
    assert printable("\ud800") == "\\ud800"


def test_quote_and_quote_list() -> None:
    assert quote(ANSI) == '"\\u001b[31mred\\u001b[0m"'
    assert len(quote("k" * 10_000)) == 64 + 3
    assert quote_list(["b", "a"]) == '"a", "b"'
    assert quote_list([f"k{i:02}" for i in range(8)]).endswith('"k04" and 3 more')


@pytest.fixture
def store() -> Iterator[EventStore]:
    """In-memory store."""
    s = EventStore(":memory:")
    yield s
    s.close()


@pytest.mark.parametrize("key", NASTY)
def test_unknown_envelope_key_is_escaped(store: EventStore, key: str) -> None:
    body = json.dumps({"events": [make_event()], key: 1}).encode()
    r = Collector(store).ingest(body)
    assert r.status == 400
    assert r.error is not None
    assert r.error.startswith("unknown envelope fields: ")
    assert is_safe(r.error)
    for _, detail, _ in store.dead_letters():
        assert is_safe(detail)


def test_many_long_envelope_keys_give_a_bounded_message(store: EventStore) -> None:
    keys = {f"{i:02}" + "k" * 10_000: i for i in range(50)}
    body = json.dumps({"events": [make_event()], **keys}).encode()
    r = Collector(store).ingest(body)
    assert r.error is not None
    assert r.error.endswith(" and 45 more")
    assert len(r.error) < 500


@pytest.mark.parametrize("raw", NASTY)
def test_schema_problems_are_escaped(raw: str) -> None:
    problems = EventValidator().problems(make_event(name=raw, properties={raw: 1}, extra_field=raw))
    assert problems
    for p in problems:
        assert is_safe(p.path)
        assert is_safe(p.message)


def test_long_property_name_path_is_capped() -> None:
    key = "a" * 5000
    problems = EventValidator().problems(make_event(properties={key: [1]}))
    assert all(len(p.path) < 200 for p in problems)


def test_report_escapes_stored_text(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "e.sqlite3"
    s = EventStore(db)
    # Rows as an older version (or a manual edit) could have left them: raw control characters.
    s.write_batch(
        [],
        [DeadLetter(f"schema{ANSI}", f"{CLEAR}/name: bad\nFAKE LINE", "{}")],
        "2026-11-14T10:00:00Z",
    )
    s.close()
    assert main(["report", "--db", str(db)], env={}) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "\nFAKE LINE" not in out
    assert "\\x1b[2J" in out


def test_validate_escapes_problems(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "b.json"
    f.write_text(json.dumps([make_event(name=CLEAR, properties={ANSI: 1})]), encoding="utf-8")
    assert main(["validate", str(f)], env={}) == 1
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert len(out.splitlines()) == 2
