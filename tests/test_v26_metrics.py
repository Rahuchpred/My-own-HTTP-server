"""V26 tests for metrics endpoint and counters."""

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


def _send(host: str, port: int, payload: bytes) -> bytes:
    with socket.create_connection((host, port), timeout=2) as sock:
        sock.sendall(payload)
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
        headers: dict[bytes, bytes] = {}
        for line in head.split(b"\r\n")[1:]:
            if b":" not in line:
                continue
            key, value = line.split(b":", 1)
            headers[key.strip().lower()] = value.strip().lower()

        content_length = int(headers.get(b"content-length", b"0"))
        while len(body) < content_length:
            chunk = sock.recv(4096)
            if not chunk:
                break
            body += chunk

        return head + b"\r\n\r\n" + body


def test_metrics_endpoint_reports_request_counters() -> None:
    server, thread = _start_server()
    try:
        _send(
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

    assert metrics_response.startswith(b"HTTP/1.1 200 OK")
    head, body = metrics_response.split(b"\r\n\r\n", 1)
    assert b"Content-Type: application/json\r\n" in head + b"\r\n"

    snapshot = json.loads(body.decode("utf-8"))
    assert snapshot["total_requests"] >= 2
    assert snapshot["bytes_sent_total"] > 0
    assert "status_counts" in snapshot
    assert snapshot["status_counts"].get("200", 0) >= 1
    assert snapshot["status_counts"].get("404", 0) >= 1
    assert "latency_buckets_ms" in snapshot
