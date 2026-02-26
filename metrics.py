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
        self._error_counts_by_class: Counter[str] = Counter()
        self._requests_by_route: Counter[str] = Counter()
        self._status_by_route: dict[str, Counter[str]] = {}
        self._latencies_by_route_ms: dict[str, list[float]] = {}
        self._last_request_trace_id = ""
        self._drain_state = "running"
        self._drain_inflight_remaining = 0
        self._drain_elapsed_ms = 0.0
        self._playground_mock_count = 0
        self._playground_history_count = 0
        self._playground_replay_total = 0
        self._playground_admin_errors_total = 0
        self._scenario_runs_total = 0
        self._scenario_runs_passed = 0
        self._scenario_runs_failed = 0
        self._scenario_steps_total = 0
        self._scenario_assertions_failed_total = 0
        self._scenario_last_run_duration_ms = 0.0
        self._incident_state = "inactive"
        self._incident_profile = "none"
        self._incident_faults_total = 0

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

    def record_request(
        self,
        status_code: int,
        duration_ms: float,
        bytes_sent: int,
        *,
        route_key: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        with self._lock:
            self._total_requests += 1
            self._status_counts[str(status_code)] += 1
            self._bytes_sent_total += bytes_sent
            self._latency_buckets[self._bucket_label(duration_ms)] += 1
            if route_key is not None:
                self._requests_by_route[route_key] += 1
                if route_key not in self._status_by_route:
                    self._status_by_route[route_key] = Counter()
                self._status_by_route[route_key][str(status_code)] += 1
                latencies = self._latencies_by_route_ms.setdefault(route_key, [])
                latencies.append(duration_ms)
                # Keep bounded memory usage for long runs.
                if len(latencies) > 4096:
                    del latencies[: len(latencies) - 4096]
            if trace_id is not None:
                self._last_request_trace_id = trace_id

    def record_read_error(self, error_type: str) -> None:
        with self._lock:
            self._read_errors_by_type[error_type] += 1
            self._error_counts_by_class["read"] += 1

    def record_write_error(self, error_type: str) -> None:
        with self._lock:
            self._write_errors_by_type[error_type] += 1
            self._error_counts_by_class["write"] += 1

    def record_error_class(self, error_class: str) -> None:
        with self._lock:
            self._error_counts_by_class[error_class] += 1

    def set_drain_state(
        self,
        *,
        state: str,
        inflight_remaining: int,
        elapsed_ms: float,
    ) -> None:
        with self._lock:
            self._drain_state = state
            self._drain_inflight_remaining = inflight_remaining
            self._drain_elapsed_ms = round(elapsed_ms, 3)

    def set_playground_counts(self, *, mock_count: int, history_count: int) -> None:
        with self._lock:
            self._playground_mock_count = max(0, mock_count)
            self._playground_history_count = max(0, history_count)

    def record_playground_replay(self) -> None:
        with self._lock:
            self._playground_replay_total += 1

    def record_playground_admin_error(self) -> None:
        with self._lock:
            self._playground_admin_errors_total += 1

    def record_scenario_run(
        self,
        *,
        passed: bool,
        step_count: int,
        failed_assertions: int,
        duration_ms: float,
    ) -> None:
        with self._lock:
            self._scenario_runs_total += 1
            if passed:
                self._scenario_runs_passed += 1
            else:
                self._scenario_runs_failed += 1
            self._scenario_steps_total += max(0, step_count)
            self._scenario_assertions_failed_total += max(0, failed_assertions)
            self._scenario_last_run_duration_ms = round(max(0.0, duration_ms), 3)

    def set_incident_state(self, *, state: str, profile: str) -> None:
        with self._lock:
            self._incident_state = state
            self._incident_profile = profile

    def record_incident_fault(self) -> None:
        with self._lock:
            self._incident_faults_total += 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            latency_by_route = {
                route_key: self._route_latency_summary(latencies)
                for route_key, latencies in self._latencies_by_route_ms.items()
            }
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
                "error_counts_by_class": dict(self._error_counts_by_class),
                "requests_by_route": dict(self._requests_by_route),
                "status_by_route": {
                    route_key: dict(statuses)
                    for route_key, statuses in self._status_by_route.items()
                },
                "latency_by_route_ms": latency_by_route,
                "latency_p99_ms": self._overall_latency_percentile(99),
                "request_trace_id": self._last_request_trace_id,
                "drain_state": self._drain_state,
                "drain_inflight_remaining": self._drain_inflight_remaining,
                "drain_elapsed_ms": self._drain_elapsed_ms,
                "playground_mock_count": self._playground_mock_count,
                "playground_history_count": self._playground_history_count,
                "playground_replay_total": self._playground_replay_total,
                "playground_admin_errors_total": self._playground_admin_errors_total,
                "scenario_runs_total": self._scenario_runs_total,
                "scenario_runs_passed": self._scenario_runs_passed,
                "scenario_runs_failed": self._scenario_runs_failed,
                "scenario_steps_total": self._scenario_steps_total,
                "scenario_assertions_failed_total": self._scenario_assertions_failed_total,
                "scenario_last_run_duration_ms": self._scenario_last_run_duration_ms,
                "incident_state": self._incident_state,
                "incident_profile": self._incident_profile,
                "incident_faults_total": self._incident_faults_total,
            }

    def _bucket_label(self, duration_ms: float) -> str:
        for limit in LATENCY_BUCKETS_MS:
            if duration_ms <= limit:
                return f"<= {limit}ms"
        return "> 5000ms"

    def _route_latency_summary(self, latencies: list[float]) -> dict[str, float]:
        return {
            "p50": round(self._percentile(latencies, 50), 3),
            "p95": round(self._percentile(latencies, 95), 3),
            "p99": round(self._percentile(latencies, 99), 3),
        }

    def _overall_latency_percentile(self, percentile: int) -> float:
        all_values: list[float] = []
        for values in self._latencies_by_route_ms.values():
            all_values.extend(values)
        return round(self._percentile(all_values, percentile), 3)

    def _percentile(self, values: list[float], percentile: int) -> float:
        if not values:
            return 0.0
        if percentile <= 0:
            return min(values)
        if percentile >= 100:
            return max(values)

        ordered = sorted(values)
        rank = (len(ordered) - 1) * (percentile / 100)
        lower = int(rank)
        upper = min(lower + 1, len(ordered) - 1)
        weight = rank - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight
