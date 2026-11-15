"""Make untrusted text safe to put in messages and print to a terminal."""

from __future__ import annotations

import json
from collections.abc import Iterable

MAX_VALUE = 64
MAX_LISTED = 5


def printable(text: str, limit: int | None = None) -> str:
    """Escape every non-printable character (ANSI ESC, newlines, bidi controls); optionally cap length."""
    out = "".join(
        c if c.isprintable() else c.encode("unicode_escape").decode("ascii") for c in text
    )
    if limit is not None and len(out) > limit:
        return out[:limit] + "..."
    return out


def quote(value: str, limit: int = MAX_VALUE) -> str:
    """JSON-quoted, escaped and length-capped form of one untrusted value."""
    return printable(json.dumps(value), limit)


def quote_list(values: Iterable[str], limit: int = MAX_LISTED) -> str:
    """Up to `limit` quoted values, sorted, then how many were left out."""
    items = sorted(values)
    shown = ", ".join(quote(v) for v in items[:limit])
    rest = len(items) - limit
    return f"{shown} and {rest} more" if rest > 0 else shown
