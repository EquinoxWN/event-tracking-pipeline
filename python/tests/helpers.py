"""Shared builders for valid events and batches."""

from __future__ import annotations

import json
import uuid
from typing import Any


def make_event(**overrides: Any) -> dict[str, Any]:
    """A valid schema v1 track event; keyword arguments replace or add fields."""
    event: dict[str, Any] = {
        "schema_version": 1,
        "event_id": str(uuid.uuid4()),
        "type": "track",
        "name": "item_viewed",
        "anonymous_id": "anon-0001-abcd",
        "timestamp": "2026-11-14T10:00:00.000Z",
        "context": {"locale": "en-GB", "library": {"name": "etp-js", "version": "0.1.0"}},
        "properties": {"sku": "a1", "price": 9.5},
    }
    event.update(overrides)
    return event


def batch_body(*events: dict[str, Any], sent_at: str | None = "2026-11-14T10:00:01.000Z") -> bytes:
    """JSON request body for a batch."""
    body: dict[str, Any] = {"events": list(events)}
    if sent_at is not None:
        body["sent_at"] = sent_at
    return json.dumps(body).encode()
