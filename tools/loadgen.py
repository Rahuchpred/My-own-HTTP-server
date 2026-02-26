"""Async stdlib load generator for HTTP server stress testing."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from dataclasses import dataclass, field


@dataclass(slots=True)
class LoadResult:
    total_requests: int
    errors: int
    status_counts: dict[str, int]
    latencies_ms: list[float] = field(default_factory=list)
    duration_secs: float = 0.0

    def summary(self) -> dict[str, float | int | dict[str, int]]:
        rps = self.total_requests / self.duration_secs if self.duration_secs > 0 else 0.0
        error_rate = self.errors / self.total_requests if self.total_requests > 0 else 0.0
        return {
            "requests": self.total_requests,
            "errors": self.errors,
            "error_rate": round(error_rate, 6),
            "rps": round(rps, 2),
            "p50_ms": round(percentile(self.latencies_ms, 50), 2),
            "p95_ms": round(percentile(self.latencies_ms, 95), 2),
            "status_counts": self.status_counts,
        }


async def issue_request(host: str, port: int, path: str, timeout: float) -> tuple[int, float, bool]:
    started = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        writer.write(request)
        await writer.drain()

        status_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not status_line.startswith(b"HTTP/"):
            raise ValueError("Invalid status line")
        parts = status_line.decode("iso-8859-1").strip().split(" ")
        if len(parts) < 2:
            raise ValueError("Malformed status line")
        status = int(parts[1])

        headers: dict[str, str] = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if line in {b"\r\n", b"\n", b""}:
                break
            key, value = line.decode("iso-8859-1").strip().split(":", 1)
            headers[key.strip().lower()] = value.strip()

        if headers.get("transfer-encoding", "").lower() == "chunked":
            await _read_chunked_body(reader, timeout=timeout)
        else:
            content_length = int(headers.get("content-length", "0"))
            if content_length > 0:
                await asyncio.wait_for(reader.readexactly(content_length), timeout=timeout)

        writer.close()
        await writer.wait_closed()
        return status, (time.perf_counter() - started) * 1000, False
    except Exception:
        return 0, (time.perf_counter() - started) * 1000, True


async def run_load(
    host: str,
    port: int,
    path: str,
    *,
    concurrency: int,
    duration_secs: float,
    timeout_secs: float,
) -> LoadResult:
    status_counts: Counter[str] = Counter()
    latencies_ms: list[float] = []
    total_requests = 0
    errors = 0
    stop_at = time.perf_counter() + duration_secs
    lock = asyncio.Lock()

    async def worker() -> None:
        nonlocal total_requests, errors
        while time.perf_counter() < stop_at:
            status, latency_ms, is_error = await issue_request(host, port, path, timeout_secs)
            async with lock:
                total_requests += 1
                latencies_ms.append(latency_ms)
                if is_error:
                    errors += 1
                else:
                    status_counts[str(status)] += 1

    started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    duration = time.perf_counter() - started

    return LoadResult(
        total_requests=total_requests,
        errors=errors,
        status_counts=dict(status_counts),
        latencies_ms=latencies_ms,
        duration_secs=duration,
    )


async def _read_chunked_body(reader: asyncio.StreamReader, timeout: float) -> None:
    while True:
        size_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not size_line:
            raise ValueError("Unexpected EOF in chunked body")
        size_token = size_line.strip().split(b";", 1)[0]
        chunk_size = int(size_token, 16)
        if chunk_size == 0:
            # Consume the terminating empty trailer line.
            await asyncio.wait_for(reader.readline(), timeout=timeout)
            return
        await asyncio.wait_for(reader.readexactly(chunk_size), timeout=timeout)
        await asyncio.wait_for(reader.readexactly(2), timeout=timeout)


def percentile(values: list[float], pct: int) -> float:
    if not values:
        return 0.0
    if pct <= 0:
        return min(values)
    if pct >= 100:
        return max(values)

    ordered = sorted(values)
    rank = (len(ordered) - 1) * (pct / 100)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run HTTP load against local server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--path", default="/")
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=2.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = asyncio.run(
        run_load(
            host=args.host,
            port=args.port,
            path=args.path,
            concurrency=args.concurrency,
            duration_secs=args.duration,
            timeout_secs=args.timeout,
        )
    )
    print(json.dumps(result.summary(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
