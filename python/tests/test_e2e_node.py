"""End to end: the JavaScript tracker sends to the collector over loopback HTTP (scripts/e2e.py)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from e2e import run


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js 24+ is needed for the JS tracker")
def test_tracker_to_collector(tmp_path: Path) -> None:
    summary, problems = run(60, tmp_path / "e2e.sqlite3")
    assert problems == []
    assert summary["stored_events"] == 61
    assert summary["dead_letters"] == {"invalid_json": 1, "schema": 3}
    assert summary["replay"]["body"]["duplicates"] >= 1
