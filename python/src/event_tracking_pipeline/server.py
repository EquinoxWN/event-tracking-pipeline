"""Serve the collector over HTTP with the standard library's threaded WSGI server."""

from __future__ import annotations

import logging
from socketserver import ThreadingMixIn
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from .collector import Collector

LOG = logging.getLogger("event_tracking_pipeline.server")
REQUEST_TIMEOUT_S = 10.0


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """One thread per request; threads do not block shutdown."""

    daemon_threads = True


class QuietHandler(WSGIRequestHandler):
    """Request handler with a socket timeout that logs through `logging`."""

    timeout = REQUEST_TIMEOUT_S

    def log_message(self, format: str, *args: Any) -> None:
        """Access log line at debug level."""
        LOG.debug("%s %s", self.address_string(), format % args)


def build_server(collector: Collector, host: str = "127.0.0.1", port: int = 8787) -> WSGIServer:
    """Bound server for the collector; port 0 picks a free port."""
    if not 0 <= port <= 65535:
        raise ValueError(f"port out of range: {port}")
    return make_server(
        host, port, collector, server_class=ThreadingWSGIServer, handler_class=QuietHandler
    )
