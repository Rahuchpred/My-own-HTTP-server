"""V31 tests for stdlib load generator outputs."""

import asyncio
import threading
import time

from server import HTTPServer
from tools.loadgen import LoadResult, percentile, run_load


def _start_server() -> tuple[HTTPServer, threading.Thread]:
    server = HTTPServer(port=0)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()

    deadline = time.time() + 2
    while server.port == 0 and time.time() < deadline:
        time.sleep(0.01)
    if server.port == 0:
        raise RuntimeError("Server did not bind to a port")
    return server, thread


def _stop_server(server: HTTPServer, thread: threading.Thread) -> None:
    server.stop()
    thread.join(timeout=2)


def test_percentile_interpolation() -> None:
    values = [10.0, 20.0, 30.0, 40.0]
    assert percentile(values, 0) == 10.0
    assert percentile(values, 100) == 40.0
    assert percentile(values, 50) == 25.0


def test_load_result_summary_fields() -> None:
    result = LoadResult(
        total_requests=10,
        errors=1,
        status_counts={"200": 9},
        latencies_ms=[10.0, 20.0, 30.0, 40.0],
        duration_secs=2.0,
    )
    summary = result.summary()
    assert summary["requests"] == 10
    assert summary["errors"] == 1
    assert summary["status_counts"] == {"200": 9}
    assert summary["rps"] == 5.0
    assert summary["p50_ms"] == 25.0


def test_run_load_generates_requests_against_server() -> None:
    server, thread = _start_server()
    try:
        result = asyncio.run(
            run_load(
                host=server.host,
                port=server.port,
                path="/",
                concurrency=4,
                duration_secs=0.3,
                timeout_secs=2.0,
            )
        )
    finally:
        _stop_server(server, thread)

    assert result.total_requests > 0
    assert result.status_counts.get("200", 0) > 0
