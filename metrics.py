"""Thread-safe in-memory metrics for HTTP server requests."""

from __future__ import annotations

import threading
from collections import Counter

LATENCY_BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2000, 5000)


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total_requests = 0
        self._active_connections = 0
        self._open_connections = 0
        self._inflight_requests = 0
        self._requests_reused_connection = 0
        self._status_counts: Counter[str] = Counter()
        self._latency_buckets: Counter[str] = Counter()
        self._bytes_sent_total = 0
        self._read_errors_by_type: Counter[str] = Counter()
        self._write_errors_by_type: Counter[str] = Counter()

    def connection_opened(self) -> None:
        with self._lock:
            self._active_connections += 1
            self._open_connections += 1

    def connection_closed(self) -> None:
        with self._lock:
            self._active_connections = max(0, self._active_connections - 1)
            self._open_connections = max(0, self._open_connections - 1)

    def request_started(self, *, connection_reused: bool) -> None:
        with self._lock:
            self._inflight_requests += 1
            if connection_reused:
                self._requests_reused_connection += 1

    def request_finished(self) -> None:
        with self._lock:
            self._inflight_requests = max(0, self._inflight_requests - 1)

    def record_request(self, status_code: int, duration_ms: float, bytes_sent: int) -> None:
        with self._lock:
            self._total_requests += 1
            self._status_counts[str(status_code)] += 1
            self._bytes_sent_total += bytes_sent
            self._latency_buckets[self._bucket_label(duration_ms)] += 1

    def record_read_error(self, error_type: str) -> None:
        with self._lock:
            self._read_errors_by_type[error_type] += 1

    def record_write_error(self, error_type: str) -> None:
        with self._lock:
            self._write_errors_by_type[error_type] += 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "total_requests": self._total_requests,
                "active_connections": self._active_connections,
                "open_connections": self._open_connections,
                "inflight_requests": self._inflight_requests,
                "requests_reused_connection": self._requests_reused_connection,
                "status_counts": dict(self._status_counts),
                "latency_buckets_ms": dict(self._latency_buckets),
                "bytes_sent_total": self._bytes_sent_total,
                "read_errors_by_type": dict(self._read_errors_by_type),
                "write_errors_by_type": dict(self._write_errors_by_type),
            }

    def _bucket_label(self, duration_ms: float) -> str:
        for limit in LATENCY_BUCKETS_MS:
            if duration_ms <= limit:
                return f"<= {limit}ms"
        return "> 5000ms"
