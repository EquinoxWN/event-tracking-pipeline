"""Real HTTP on 127.0.0.1: requests, concurrent clients and the port check."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from helpers import batch_body, make_event

from event_tracking_pipeline import Collector, EventStore
from event_tracking_pipeline.server import build_server


@pytest.fixture
def served() -> Iterator[tuple[str, EventStore]]:
    """Collector on a free loopback port, stopped after the test."""
    store = EventStore(":memory:")
    server = build_server(Collector(store), "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", store
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    store.close()


def post(url: str, body: bytes) -> tuple[int, Any]:
    """POST JSON with a 5 s timeout; returns status and parsed body."""
    req = urllib.request.Request(  # noqa: S310 - loopback URL built by the test
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as res:  # noqa: S310
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_post_and_health_over_http(served: tuple[str, EventStore]) -> None:
    base, store = served
    assert post(f"{base}/v1/batch", batch_body(make_event())) == (
        200,
        {"accepted": 1, "duplicates": 0, "rejected": 0},
    )
    assert post(f"{base}/v1/batch", b"{bad")[0] == 400
    with urllib.request.urlopen(f"{base}/healthz", timeout=5) as res:  # noqa: S310
        assert json.loads(res.read()) == {"status": "ok"}
    assert store.count_events() == 1


def test_concurrent_clients_with_overlapping_retries(served: tuple[str, EventStore]) -> None:
    base, store = served
    events = [make_event() for _ in range(120)]
    bodies = [batch_body(*events[i : i + 10]) for i in range(0, 120, 10)]

    def send_all(_: int) -> list[int]:
        return [post(f"{base}/v1/batch", b)[1]["accepted"] for b in bodies]

    with ThreadPoolExecutor(max_workers=6) as pool:
        accepted = sum(sum(r) for r in pool.map(send_all, range(6)))
    assert accepted == 120
    assert store.count_events() == 120


@pytest.mark.parametrize("port", [-1, 65536])
def test_port_range(port: int) -> None:
    with pytest.raises(ValueError, match="port"):
        build_server(Collector(EventStore(":memory:")), "127.0.0.1", port)
