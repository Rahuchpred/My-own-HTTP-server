"""V22 tests for chunked transfer request decoding."""

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


def _send_raw(host: str, port: int, payload: bytes) -> bytes:
    with socket.create_connection((host, port), timeout=2) as sock:
        sock.sendall(payload)
        return sock.recv(8192)


def test_chunked_post_body_is_decoded() -> None:
    server, thread = _start_server()
    try:
        payload = (
            b"POST /submit HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"4\r\nname\r\n"
            b"5\r\n=test\r\n"
            b"0\r\n\r\n"
        )
        response = _send_raw(server.host, server.port, payload)
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 200 OK")
    assert b"Received POST body: name=test" in response


def test_chunked_plus_content_length_returns_400() -> None:
    server, thread = _start_server()
    try:
        payload = (
            b"POST /submit HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Content-Length: 4\r\n"
            b"\r\n"
            b"4\r\nname\r\n0\r\n\r\n"
        )
        response = _send_raw(server.host, server.port, payload)
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 400 Bad Request")
