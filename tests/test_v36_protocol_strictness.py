"""V36 tests for stricter HTTP/1.1 request validation and status mapping."""

import socket
import threading
import time

from config import MAX_HEADER_BYTES
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


def _send_and_recv(host: str, port: int, payload: bytes) -> bytes:
    with socket.create_connection((host, port), timeout=2) as sock:
        sock.sendall(payload)
        return _recv_http_response(sock)


def test_http11_without_host_returns_400() -> None:
    server, thread = _start_server()
    try:
        response = _send_and_recv(
            server.host,
            server.port,
            b"GET / HTTP/1.1\r\nConnection: close\r\n\r\n",
        )
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 400 Bad Request")


def test_unsupported_http_version_returns_505() -> None:
    server, thread = _start_server()
    try:
        response = _send_and_recv(
            server.host,
            server.port,
            b"GET / HTTP/2.0\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 505 HTTP Version Not Supported")


def test_overlong_request_target_returns_414() -> None:
    server, thread = _start_server()
    try:
        target = "/" + ("a" * 3000)
        payload = (
            f"GET {target} HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        response = _send_and_recv(server.host, server.port, payload)
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 414 URI Too Long")


def test_unknown_method_returns_501() -> None:
    server, thread = _start_server()
    try:
        response = _send_and_recv(
            server.host,
            server.port,
            b"BREW / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 501 Not Implemented")


def test_oversized_header_returns_431() -> None:
    server, thread = _start_server()
    try:
        large_value = b"a" * (MAX_HEADER_BYTES + 512)
        payload = (
            b"GET / HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            + b"X-Large: "
            + large_value
            + b"\r\nConnection: close\r\n\r\n"
        )
        response = _send_and_recv(server.host, server.port, payload)
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 431 Request Header Fields Too Large")
