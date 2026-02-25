"""V23 tests for chunked response streaming."""

import socket
import threading
import time

from response import HTTPResponse
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


def test_response_stream_serializes_with_chunked_transfer() -> None:
    response = HTTPResponse(status_code=200, stream=[b"hello", b"world"])
    raw = response.to_bytes()

    assert b"Transfer-Encoding: chunked\r\n" in raw
    assert b"5\r\nhello\r\n" in raw
    assert b"5\r\nworld\r\n" in raw
    assert raw.endswith(b"0\r\n\r\n")


def test_stream_route_returns_chunked_body() -> None:
    server, thread = _start_server()
    try:
        payload = (
            b"GET /stream HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        with socket.create_connection((server.host, server.port), timeout=2) as sock:
            sock.sendall(payload)
            sock.settimeout(2)
            chunks: list[bytes] = []
            while True:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            response = b"".join(chunks)
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 200 OK")
    assert b"Transfer-Encoding: chunked\r\n" in response
    assert b"A\r\nchunk-one\n\r\n" in response
    assert response.endswith(b"0\r\n\r\n")
