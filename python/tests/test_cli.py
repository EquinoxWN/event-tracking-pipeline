"""CLI: validate files, report on a database, argument and environment checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import make_event

from event_tracking_pipeline import DeadLetter, EventStore
from event_tracking_pipeline.cli import build_parser, main


def write(path: Path, data: object) -> Path:
    """Write JSON to a file."""
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_validate_batch_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = write(tmp_path / "b.json", {"events": [make_event(), make_event()]})
    assert main(["validate", str(f)], env={}) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "2 valid, 0 invalid"


def test_validate_reports_each_bad_event(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = write(tmp_path / "b.json", [make_event(), make_event(name="")])
    assert main(["validate", str(f)], env={}) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "event 0: ok"
    assert out[1].startswith("event 1: /name:")
    assert out[-1] == "1 valid, 1 invalid"


def test_validate_single_event(tmp_path: Path) -> None:
    assert main(["validate", str(write(tmp_path / "e.json", make_event()))], env={}) == 0


def test_validate_missing_or_non_json_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["validate", str(tmp_path / "none.json")], env={}) == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    assert main(["validate", str(bad)], env={}) == 2
    assert "is not JSON" in capsys.readouterr().err


def test_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "e.sqlite3"
    s = EventStore(db)
    s.write_batch(
        [make_event(name="a"), make_event(name="a"), make_event(name="b")],
        [DeadLetter("schema", "/name: bad", "{}")],
        "2026-11-14T10:00:00.000Z",
    )
    s.close()
    assert main(["report", "--db", str(db)], env={}) == 0
    out = capsys.readouterr().out
    assert "events stored: 3\n  a: 2\n  b: 1\n" in out
    assert "dead letters: 1 rows\n  schema: 1 rows, seen 1 times\n" in out
    assert "[schema] x1 /name: bad" in out


def test_report_without_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["report", "--db", str(tmp_path / "none.sqlite3")], env={}) == 2
    assert "no database" in capsys.readouterr().err
    assert not (tmp_path / "none.sqlite3").exists()


def test_report_recent_is_bounded(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["report", "--db", str(tmp_path / "x"), "--recent", "0"], env={})


def test_environment_defaults() -> None:
    env = {
        "ETP_DB": "x.sqlite3",
        "ETP_HOST": "0.0.0.0",  # noqa: S104 - parsing only, nothing is bound
        "ETP_PORT": "9000",
        "ETP_ALLOWED_ORIGINS": "https://a.example.test, http://localhost:3000",
    }
    args = build_parser(env).parse_args(["serve"])
    assert (args.db, args.host, args.port) == ("x.sqlite3", "0.0.0.0", 9000)  # noqa: S104
    assert args.allow_origins == ["https://a.example.test", "http://localhost:3000"]
    defaults = build_parser({}).parse_args(["serve"])
    assert (defaults.host, defaults.port, defaults.allow_origins) == ("127.0.0.1", 8787, [])


@pytest.mark.parametrize(
    "argv",
    [
        ["serve", "--port", "70000"],
        ["serve", "--port", "http"],
        ["serve", "--allow-origins", "*"],
        ["serve", "--allow-origins", "https://a.example.test/path"],
        [],
    ],
)
def test_bad_arguments_exit(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as err:
        build_parser({}).parse_args(argv)
    assert err.value.code == 2


def test_bad_port_in_environment() -> None:
    with pytest.raises(SystemExit):
        build_parser({"ETP_PORT": "-1"}).parse_args(["serve"])
