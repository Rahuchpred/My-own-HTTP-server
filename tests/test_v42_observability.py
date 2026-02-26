"""V42 tests for V3 observability metrics and trace IDs."""

from __future__ import annotations

import json
import socket
import threading
import time

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


def _recv_http_response(sock: socket.socket) -> bytes:
    buffer = bytearray()
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buffer.extend(chunk)

    header_end = buffer.find(b"\r\n\r\n")
    if header_end == -1:
        return bytes(buffer)

    head = bytes(buffer[:header_end])
    body = bytes(buffer[header_end + 4 :])
    headers = {}
    for line in head.split(b"\r\n")[1:]:
        key, value = line.split(b":", 1)
        headers[key.strip().lower()] = value.strip().lower()

    content_length = int(headers.get(b"content-length", b"0"))
    while len(body) < content_length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk

    return head + b"\r\n\r\n" + body


def _send(host: str, port: int, payload: bytes) -> bytes:
    with socket.create_connection((host, port), timeout=2) as sock:
        sock.sendall(payload)
        return _recv_http_response(sock)


def test_trace_id_header_is_present_and_metrics_include_route_latency() -> None:
    server, thread = _start_server()
    try:
        home = _send(
            server.host,
            server.port,
            b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
        _send(
            server.host,
            server.port,
            b"GET /unknown HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
        metrics_response = _send(
            server.host,
            server.port,
            b"GET /_metrics HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
    finally:
        _stop_server(server, thread)

    assert b"X-Request-ID: " in home

    _head, body = metrics_response.split(b"\r\n\r\n", 1)
    snapshot = json.loads(body.decode("utf-8"))

    assert "requests_by_route" in snapshot
    assert snapshot["requests_by_route"].get("GET /", 0) >= 1

    assert "latency_by_route_ms" in snapshot
    assert "GET /" in snapshot["latency_by_route_ms"]
    assert set(snapshot["latency_by_route_ms"]["GET /"].keys()) == {"p50", "p95", "p99"}

    assert "latency_p99_ms" in snapshot
    assert isinstance(snapshot["latency_p99_ms"], (float, int))

    assert "error_counts_by_class" in snapshot
    assert "request_trace_id" in snapshot
    assert isinstance(snapshot["request_trace_id"], str)

    assert snapshot["drain_state"] in {"running", "draining", "stopped"}
    assert "drain_inflight_remaining" in snapshot
    assert "drain_elapsed_ms" in snapshot
