"""event-tracking-pipeline: a collector that validates every tracked event and keeps the bad ones."""

from .collector import Collector, IngestResult
from .schema import EventValidator, Problem, load_schema
from .store import DeadLetter, EventStore, WriteResult

__version__ = "0.1.0"

__all__ = [
    "Collector",
    "DeadLetter",
    "EventStore",
    "EventValidator",
    "IngestResult",
    "Problem",
    "WriteResult",
    "__version__",
    "load_schema",
]
