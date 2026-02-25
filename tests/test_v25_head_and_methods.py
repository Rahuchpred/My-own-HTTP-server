"""V25 tests for HEAD handling and method rules."""

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


def _send_and_recv(host: str, port: int, payload: bytes) -> bytes:
    with socket.create_connection((host, port), timeout=2) as sock:
        sock.sendall(payload)
        return sock.recv(8192)


def _split_head_body(raw_response: bytes) -> tuple[bytes, bytes]:
    head, body = raw_response.split(b"\r\n\r\n", 1)
    return head, body


def test_head_root_returns_headers_without_body() -> None:
    server, thread = _start_server()
    try:
        payload = (
            b"HEAD / HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        response = _send_and_recv(server.host, server.port, payload)
    finally:
        _stop_server(server, thread)

    head, body = _split_head_body(response)
    assert head.startswith(b"HTTP/1.1 200 OK")
    assert b"Content-Length: 11\r\n" in head + b"\r\n"
    assert body == b""


def test_head_static_returns_headers_without_body() -> None:
    server, thread = _start_server()
    try:
        payload = (
            b"HEAD /static/test.html HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        response = _send_and_recv(server.host, server.port, payload)
    finally:
        _stop_server(server, thread)

    head, body = _split_head_body(response)
    assert head.startswith(b"HTTP/1.1 200 OK")
    assert b"Content-Length:" in head
    assert body == b""


def test_unknown_method_returns_405_with_allow_header_including_head() -> None:
    server, thread = _start_server()
    try:
        payload = (
            b"DELETE / HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        response = _send_and_recv(server.host, server.port, payload)
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 405 Method Not Allowed")
    assert b"Allow: GET, HEAD, POST\r\n" in response
