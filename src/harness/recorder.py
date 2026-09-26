"""
harness/recorder.py
───────────────────
EventRecorder – local-only telemetry for operations.
Records: run_id, operation, duration, attempt_count, http_status,
         rate_limit_remaining, cache_outcome.
Never records credentials, authorization headers, or issue bodies.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Module-level run_id per process
_RUN_ID = str(uuid.uuid4())


@dataclass
class Event:
    run_id: str
    operation: str
    started_at: str
    duration_ms: float
    status: str                      # ok | error | partial | cancelled
    attempt_count: int = 1
    http_status: int | None = None
    rate_limit_remaining: str | None = None
    cache_outcome: str | None = None  # hit | miss | saved
    # Never include: auth tokens, issue bodies, full URLs with credentials


class EventRecorder:
    def __init__(self) -> None:
        self._events: list[Event] = []

    def record_event(
        self,
        operation: str,
        start_time: float,
        status: str,
        attempt_count: int = 1,
        http_status: int | None = None,
        rate_limit_remaining: str | None = None,
        cache_outcome: str | None = None,
    ) -> Event:
        duration_ms = (time.monotonic() - start_time) * 1000
        event = Event(
            run_id=_RUN_ID,
            operation=operation,
            started_at=datetime.now(tz=timezone.utc).isoformat(),
            duration_ms=round(duration_ms, 2),
            status=status,
            attempt_count=attempt_count,
            http_status=http_status,
            rate_limit_remaining=rate_limit_remaining,
            cache_outcome=cache_outcome,
        )
        self._events.append(event)
        logger.debug(
            "event op=%s status=%s duration=%.1fms attempts=%d cache=%s",
            operation,
            status,
            duration_ms,
            attempt_count,
            cache_outcome,
        )
        return event

    def get_events(self) -> list[Event]:
        return list(self._events)

    @property
    def run_id(self) -> str:
        return _RUN_ID
