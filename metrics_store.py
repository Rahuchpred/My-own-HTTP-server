"""Persistent trend storage for request and latency aggregates."""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

LATENCY_BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2000, 5000)
_BUCKET_COLUMNS = [f"lat_bucket_{idx}" for idx in range(len(LATENCY_BUCKETS_MS) + 1)]


class MetricsTrendStore:
    backend_name: str = "memory"

    def record_request(
        self,
        *,
        route_key: str,
        status_code: int,
        duration_ms: float,
        observed_at: float | None = None,
    ) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        raise NotImplementedError

    def prune_older_than(self, *, cutoff_epoch: int) -> None:
        raise NotImplementedError

    def query(self, *, window_seconds: int, route_key: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def close(self) -> None:
        self.flush()


class MemoryMetricsTrendStore(MetricsTrendStore):
    backend_name = "memory"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[tuple[int, str], dict[str, Any]] = {}
        self._rows: dict[tuple[int, str], dict[str, Any]] = {}

    def record_request(
        self,
        *,
        route_key: str,
        status_code: int,
        duration_ms: float,
        observed_at: float | None = None,
    ) -> None:
        bucket_start = _minute_bucket(observed_at)
        route = route_key or "-"
        with self._lock:
            _accumulate(self._pending, bucket_start, route, status_code, duration_ms)
            _accumulate(self._pending, bucket_start, "__all__", status_code, duration_ms)

    def flush(self) -> None:
        with self._lock:
            if not self._pending:
                return
            for key, row in self._pending.items():
                existing = self._rows.get(key)
                if existing is None:
                    self._rows[key] = _copy_row(row)
                    continue
                _merge_row(existing, row)
            self._pending = {}

    def prune_older_than(self, *, cutoff_epoch: int) -> None:
        with self._lock:
            for mapping in (self._rows, self._pending):
                for key in list(mapping.keys()):
                    bucket_start, _route = key
                    if bucket_start < cutoff_epoch:
                        mapping.pop(key, None)

    def query(self, *, window_seconds: int, route_key: str) -> list[dict[str, Any]]:
        self.flush()
        now = int(time.time())
        since = max(0, now - max(60, window_seconds))
        with self._lock:
            rows = [
                _copy_row(row)
                for (bucket_start, route), row in self._rows.items()
                if route == route_key and bucket_start >= since
            ]
        rows.sort(key=lambda row: int(row["bucket_start"]))
        return [_row_to_point(row) for row in rows]


class SqliteMetricsTrendStore(MetricsTrendStore):
    backend_name = "sqlite"

    def __init__(self, *, db_file: str) -> None:
        self._db_file = Path(db_file)
        self._db_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._pending: dict[tuple[int, str], dict[str, Any]] = {}
        self._conn = sqlite3.connect(str(self._db_file), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        bucket_columns = ",\n                ".join(
            f"{name} INTEGER NOT NULL" for name in _BUCKET_COLUMNS
        )
        self._conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS metric_trends (
                bucket_start INTEGER NOT NULL,
                route_key TEXT NOT NULL,
                request_count INTEGER NOT NULL,
                status_4xx INTEGER NOT NULL,
                status_5xx INTEGER NOT NULL,
                {bucket_columns},
                PRIMARY KEY (bucket_start, route_key)
            )
            """
        )
        self._conn.commit()

    def record_request(
        self,
        *,
        route_key: str,
        status_code: int,
        duration_ms: float,
        observed_at: float | None = None,
    ) -> None:
        bucket_start = _minute_bucket(observed_at)
        route = route_key or "-"
        with self._lock:
            _accumulate(self._pending, bucket_start, route, status_code, duration_ms)
            _accumulate(self._pending, bucket_start, "__all__", status_code, duration_ms)

    def flush(self) -> None:
        with self._lock:
            if not self._pending:
                return
            pending = self._pending
            self._pending = {}

        cols = ", ".join(_BUCKET_COLUMNS)
        placeholders = ", ".join("?" for _ in range(5 + len(_BUCKET_COLUMNS)))
        update_bucket = ", ".join(f"{name}={name}+excluded.{name}" for name in _BUCKET_COLUMNS)
        sql = (
            "INSERT INTO metric_trends "
            f"(bucket_start, route_key, request_count, status_4xx, status_5xx, {cols}) "
            f"VALUES ({placeholders}) "
            "ON CONFLICT(bucket_start, route_key) DO UPDATE SET "
            "request_count=request_count+excluded.request_count, "
            "status_4xx=status_4xx+excluded.status_4xx, "
            "status_5xx=status_5xx+excluded.status_5xx, "
            f"{update_bucket}"
        )
        rows: list[tuple[Any, ...]] = []
        for row in pending.values():
            values = [
                int(row["bucket_start"]),
                str(row["route_key"]),
                int(row["request_count"]),
                int(row["status_4xx"]),
                int(row["status_5xx"]),
            ]
            values.extend(int(value) for value in row["latency_buckets"])
            rows.append(tuple(values))

        with self._conn:
            self._conn.executemany(sql, rows)

    def prune_older_than(self, *, cutoff_epoch: int) -> None:
        self.flush()
        with self._conn:
            self._conn.execute(
                "DELETE FROM metric_trends WHERE bucket_start < ?",
                (int(cutoff_epoch),),
            )

    def query(self, *, window_seconds: int, route_key: str) -> list[dict[str, Any]]:
        self.flush()
        now = int(time.time())
        since = max(0, now - max(60, window_seconds))
        select_cols = ", ".join(_BUCKET_COLUMNS)
        cursor = self._conn.execute(
            f"""
            SELECT bucket_start, route_key, request_count, status_4xx, status_5xx, {select_cols}
            FROM metric_trends
            WHERE route_key = ? AND bucket_start >= ?
            ORDER BY bucket_start ASC
            """,
            (route_key, since),
        )
        rows = []
        for db_row in cursor.fetchall():
            bucket_values = [int(value) for value in db_row[5:]]
            row = {
                "bucket_start": int(db_row[0]),
                "route_key": str(db_row[1]),
                "request_count": int(db_row[2]),
                "status_4xx": int(db_row[3]),
                "status_5xx": int(db_row[4]),
                "latency_buckets": bucket_values,
            }
            rows.append(_row_to_point(row))
        return rows

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self._conn.close()


def create_metrics_store(*, backend: str, sqlite_file: str) -> MetricsTrendStore:
    normalized = backend.strip().lower()
    if normalized == "sqlite":
        return SqliteMetricsTrendStore(db_file=sqlite_file)
    return MemoryMetricsTrendStore()


def summarize_points(points: list[dict[str, Any]]) -> dict[str, Any]:
    if not points:
        return {
            "request_count": 0,
            "status_4xx": 0,
            "status_5xx": 0,
            "p50": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "error_rate": 0.0,
        }

    aggregate = {
        "bucket_start": int(points[0]["bucket_start"]),
        "route_key": str(points[0].get("route_key", "__all__")),
        "request_count": 0,
        "status_4xx": 0,
        "status_5xx": 0,
        "latency_buckets": [0 for _ in range(len(_BUCKET_COLUMNS))],
    }
    for point in points:
        aggregate["request_count"] += int(point.get("request_count", 0))
        aggregate["status_4xx"] += int(point.get("status_4xx", 0))
        aggregate["status_5xx"] += int(point.get("status_5xx", 0))
        buckets = point.get("latency_buckets", [])
        for idx in range(min(len(aggregate["latency_buckets"]), len(buckets))):
            aggregate["latency_buckets"][idx] += int(buckets[idx])

    return _row_to_point(aggregate)


def _minute_bucket(observed_at: float | None) -> int:
    ts = observed_at if observed_at is not None else time.time()
    return int(ts // 60) * 60


def _new_row(bucket_start: int, route_key: str) -> dict[str, Any]:
    return {
        "bucket_start": bucket_start,
        "route_key": route_key,
        "request_count": 0,
        "status_4xx": 0,
        "status_5xx": 0,
        "latency_buckets": [0 for _ in range(len(_BUCKET_COLUMNS))],
    }


def _copy_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "bucket_start": int(row["bucket_start"]),
        "route_key": str(row["route_key"]),
        "request_count": int(row["request_count"]),
        "status_4xx": int(row["status_4xx"]),
        "status_5xx": int(row["status_5xx"]),
        "latency_buckets": [int(value) for value in row["latency_buckets"]],
    }


def _bucket_index(duration_ms: float) -> int:
    duration = max(0.0, float(duration_ms))
    for idx, limit in enumerate(LATENCY_BUCKETS_MS):
        if duration <= limit:
            return idx
    return len(LATENCY_BUCKETS_MS)


def _accumulate(
    mapping: dict[tuple[int, str], dict[str, Any]],
    bucket_start: int,
    route_key: str,
    status_code: int,
    duration_ms: float,
) -> None:
    key = (bucket_start, route_key)
    row = mapping.get(key)
    if row is None:
        row = _new_row(bucket_start, route_key)
        mapping[key] = row

    row["request_count"] += 1
    if 400 <= status_code <= 499:
        row["status_4xx"] += 1
    if 500 <= status_code <= 599:
        row["status_5xx"] += 1
    row["latency_buckets"][_bucket_index(duration_ms)] += 1


def _merge_row(destination: dict[str, Any], source: dict[str, Any]) -> None:
    destination["request_count"] += int(source["request_count"])
    destination["status_4xx"] += int(source["status_4xx"])
    destination["status_5xx"] += int(source["status_5xx"])
    source_buckets = source["latency_buckets"]
    for idx in range(len(destination["latency_buckets"])):
        destination["latency_buckets"][idx] += int(source_buckets[idx])


def _hist_percentile(latency_buckets: list[int], percentile: int) -> float:
    total = sum(int(value) for value in latency_buckets)
    if total <= 0:
        return 0.0
    target = max(1, math.ceil(total * (percentile / 100)))
    running = 0
    for idx, count in enumerate(latency_buckets):
        running += int(count)
        if running >= target:
            if idx < len(LATENCY_BUCKETS_MS):
                return float(LATENCY_BUCKETS_MS[idx])
            return float(LATENCY_BUCKETS_MS[-1] + 1)
    return float(LATENCY_BUCKETS_MS[-1] + 1)


def _row_to_point(row: dict[str, Any]) -> dict[str, Any]:
    request_count = int(row["request_count"])
    status_4xx = int(row["status_4xx"])
    status_5xx = int(row["status_5xx"])
    buckets = [int(value) for value in row["latency_buckets"]]
    error_rate = 0.0
    if request_count > 0:
        error_rate = ((status_4xx + status_5xx) / request_count) * 100.0

    return {
        "bucket_start": int(row["bucket_start"]),
        "route_key": str(row["route_key"]),
        "request_count": request_count,
        "status_4xx": status_4xx,
        "status_5xx": status_5xx,
        "p50": round(_hist_percentile(buckets, 50), 3),
        "p95": round(_hist_percentile(buckets, 95), 3),
        "p99": round(_hist_percentile(buckets, 99), 3),
        "error_rate": round(error_rate, 4),
        "latency_buckets": buckets,
    }
