"""Load the versioned event schema and validate events against it."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from typing import Any, cast

from jsonschema import Draft202012Validator

SCHEMA_VERSION = 1
MAX_PROBLEMS = 10
MAX_MESSAGE = 200


@dataclass(frozen=True)
class Problem:
    """One reason an event does not match the schema."""

    path: str
    message: str

    def __str__(self) -> str:
        """Readable `path: message` form."""
        return f"{self.path or '/'}: {self.message}"


def load_schema(version: int = SCHEMA_VERSION) -> dict[str, Any]:
    """Event schema shipped with the package."""
    if version != SCHEMA_VERSION:
        raise ValueError(f"unknown schema version {version!r}; this build has {SCHEMA_VERSION}")
    text = (
        resources.files("event_tracking_pipeline")
        .joinpath(f"schemas/event.v{version}.json")
        .read_text(encoding="utf-8")
    )
    return cast(dict[str, Any], json.loads(text))


def _pointer(parts: Any) -> str:
    """JSON-pointer-like path of an error location."""
    return "".join(f"/{p}" for p in parts)


def _parse_utc(value: str) -> datetime | None:
    """Calendar-valid UTC timestamp, or None when the date does not exist."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_unicode(event: object) -> bool:
    """True when every string in the event encodes to UTF-8."""
    try:
        json.dumps(event, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


class EventValidator:
    """Checks events against a JSON Schema (Draft 2020-12) plus a real-date check."""

    def __init__(self, schema: dict[str, Any] | None = None) -> None:
        """Compile the schema once; an invalid schema fails here, not per event."""
        schema = load_schema() if schema is None else schema
        Draft202012Validator.check_schema(schema)
        self._validator = Draft202012Validator(schema)

    def problems(self, event: object) -> list[Problem]:
        """Every problem with one event (at most MAX_PROBLEMS); empty when valid."""
        errors = sorted(
            self._validator.iter_errors(event), key=lambda e: [str(p) for p in e.absolute_path]
        )
        found = [Problem(_pointer(e.absolute_path), e.message[:MAX_MESSAGE]) for e in errors]
        if not found and isinstance(event, dict):
            if _parse_utc(event["timestamp"]) is None:
                found.append(Problem("/timestamp", "not a calendar date and time"))
            if not _is_unicode(event):
                found.append(Problem("", "contains a lone surrogate (not valid Unicode text)"))
        return found[:MAX_PROBLEMS]

    def is_valid(self, event: object) -> bool:
        """True when the event has no problems."""
        return not self.problems(event)
