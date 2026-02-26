"""V34 deterministic performance smoke checks."""

import asyncio
import threading
import time

from server import HTTPServer
from tools.loadgen import run_load


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


def test_perf_smoke_run_has_low_error_rate() -> None:
    server, thread = _start_server()
    try:
        result = asyncio.run(
            run_load(
                host=server.host,
                port=server.port,
                path="/",
                concurrency=20,
                duration_secs=0.6,
                timeout_secs=2.0,
            )
        )
    finally:
        _stop_server(server, thread)

    summary = result.summary()
    assert summary["requests"] >= 20
    assert summary["error_rate"] <= 0.05
