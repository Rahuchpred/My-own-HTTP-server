"""V41 tests for graceful draining shutdown semantics."""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass

from request import HTTPRequest
from response import HTTPResponse
from router import Router
from server import HTTPServer


@dataclass
class SlowHandler:
    delay_secs: float

    def __call__(self, _request: HTTPRequest) -> HTTPResponse:
        time.sleep(self.delay_secs)
        return HTTPResponse(status_code=200, body="slow-ok")


def _start_server(*, engine: str) -> tuple[HTTPServer, threading.Thread]:
    router = Router()
    router.add_route("GET", "/slow", SlowHandler(delay_secs=0.35))
    server = HTTPServer(
        port=0,
        engine=engine,
        router=router,
        drain_timeout_secs=1.5,
    )
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()

    deadline = time.time() + 3
    while server.port == 0 and time.time() < deadline:
        time.sleep(0.01)

    if server.port == 0:
        raise RuntimeError("Server did not bind to a port")
    return server, thread


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


def test_inflight_request_completes_while_server_is_draining() -> None:
    server, thread = _start_server(engine="threadpool")
    with socket.create_connection((server.host, server.port), timeout=3) as sock:
        sock.sendall(
            b"GET /slow HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        deadline = time.time() + 1.0
        while time.time() < deadline:
            if server.metrics.snapshot().get("inflight_requests", 0) > 0:
                break
            time.sleep(0.01)
        stopper = threading.Thread(target=server.stop)
        stopper.start()
        response = _recv_http_response(sock)
        stopper.join(timeout=3)

    thread.join(timeout=3)

    assert response.startswith(b"HTTP/1.1 200 OK")
    assert b"slow-ok" in response


def test_keepalive_request_during_drain_returns_503() -> None:
    server, thread = _start_server(engine="threadpool")
    try:
        with socket.create_connection((server.host, server.port), timeout=3) as sock:
            sock.sendall(
                b"GET /slow HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Connection: keep-alive\r\n"
                b"\r\n"
            )
            first = _recv_http_response(sock)

            stopper = threading.Thread(target=server.stop)
            stopper.start()
            time.sleep(0.05)

            sock.sendall(
                b"GET /slow HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            second = _recv_http_response(sock)
            stopper.join(timeout=3)
    finally:
        thread.join(timeout=3)

    assert first.startswith(b"HTTP/1.1 200 OK")
    assert second.startswith(b"HTTP/1.1 503 Service Unavailable")
    assert b"Retry-After: 2\r\n" in second
