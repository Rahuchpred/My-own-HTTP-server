"""V37 tests for selectors-based engine parity and pipelined ordering."""

import socket
import threading
import time

from server import HTTPServer


def _start_server() -> tuple[HTTPServer, threading.Thread]:
    server = HTTPServer(port=0, engine="selectors")
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


def _recv_one_http_response(sock: socket.socket) -> bytes:
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

    if headers.get(b"transfer-encoding") == b"chunked":
        while not body.endswith(b"0\r\n\r\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            body += chunk
    else:
        content_length = int(headers.get(b"content-length", b"0"))
        while len(body) < content_length:
            chunk = sock.recv(4096)
            if not chunk:
                break
            body += chunk

    return head + b"\r\n\r\n" + body


def test_selectors_engine_serves_basic_request() -> None:
    server, thread = _start_server()
    try:
        with socket.create_connection((server.host, server.port), timeout=2) as sock:
            sock.sendall(
                b"GET / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            response = _recv_one_http_response(sock)
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 200 OK")
    assert b"Hello World" in response


def test_selectors_engine_keepalive_handles_two_requests() -> None:
    server, thread = _start_server()
    try:
        with socket.create_connection((server.host, server.port), timeout=2) as sock:
            sock.sendall(
                b"GET / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Connection: keep-alive\r\n"
                b"\r\n"
            )
            first = _recv_one_http_response(sock)

            sock.sendall(
                b"GET /unknown HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            second = _recv_one_http_response(sock)
    finally:
        _stop_server(server, thread)

    assert first.startswith(b"HTTP/1.1 200 OK")
    assert b"Connection: keep-alive" in first
    assert second.startswith(b"HTTP/1.1 404 Not Found")


def test_selectors_engine_preserves_pipelined_response_order() -> None:
    server, thread = _start_server()
    try:
        with socket.create_connection((server.host, server.port), timeout=2) as sock:
            sock.sendall(
                b"GET / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Connection: keep-alive\r\n"
                b"\r\n"
                b"GET /missing HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            first = _recv_one_http_response(sock)
            second = _recv_one_http_response(sock)
    finally:
        _stop_server(server, thread)

    assert first.startswith(b"HTTP/1.1 200 OK")
    assert second.startswith(b"HTTP/1.1 404 Not Found")
