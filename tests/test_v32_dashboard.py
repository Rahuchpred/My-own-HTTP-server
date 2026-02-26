"""V32 tests for live dashboard page availability."""

import socket
import threading
import time
from pathlib import Path

from server import HTTPServer


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


def test_dashboard_static_file_contains_metrics_polling() -> None:
    dashboard = (Path(__file__).resolve().parent.parent / "static" / "dashboard.html").read_text(
        encoding="utf-8"
    )
    assert "HTTP Server Live Metrics" in dashboard
    assert "fetch('/_metrics'" in dashboard
    assert "setInterval(refreshMetrics, 1000);" in dashboard


def test_dashboard_is_served_by_static_route() -> None:
    server, thread = _start_server()
    try:
        payload = (
            b"GET /static/dashboard.html HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        with socket.create_connection((server.host, server.port), timeout=2) as sock:
            sock.sendall(payload)
            response = sock.recv(8192)
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 200 OK")
    assert b"Content-Type: text/html\r\n" in response
    assert b"HTTP Server Live Metrics" in response
