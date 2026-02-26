"""In-memory event buffer for live ops feeds (CLI + web)."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any


class EventHub:
    def __init__(self, *, buffer_size: int) -> None:
        self._buffer_size = max(100, buffer_size)
        self._lock = threading.Lock()
        self._events: deque[dict[str, Any]] = deque(maxlen=self._buffer_size)
        self._next_id = 1

    def publish(
        self,
        *,
        event_type: str,
        source: str,
        trace_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            event = {
                "id": self._next_id,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "type": event_type,
                "source": source,
                "trace_id": trace_id,
                "payload": payload,
            }
            self._next_id += 1
            self._events.append(event)
            return dict(event)

    def snapshot(
        self,
        *,
        since_id: int,
        limit: int,
        topics: set[str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            events = list(self._events)
            latest_id = self._next_id - 1
            oldest_id = events[0]["id"] if events else latest_id + 1

        filtered = [
            event
            for event in events
            if event["id"] > since_id and (topics is None or event["type"] in topics)
        ]
        if limit > 0 and len(filtered) > limit:
            filtered = filtered[-limit:]

        return {
            "events": filtered,
            "latest_id": latest_id,
            "oldest_id": oldest_id,
            "overflowed": since_id < (oldest_id - 1),
        }
