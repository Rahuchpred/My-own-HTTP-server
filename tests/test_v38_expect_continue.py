"""V38 tests for Expect: 100-continue behavior."""

import socket
import threading
import time

import pytest

from config import MAX_BODY_BYTES
from server import HTTPServer


@pytest.fixture(params=["threadpool", "selectors"])
def engine(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _start_server(engine: str) -> tuple[HTTPServer, threading.Thread]:
    server = HTTPServer(port=0, engine=engine)
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


def _recv_until_headers(sock: socket.socket) -> bytes:
    buffer = bytearray()
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buffer.extend(chunk)
    return bytes(buffer)


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


def test_expect_continue_sends_interim_then_final(engine: str) -> None:
    server, thread = _start_server(engine)
    try:
        with socket.create_connection((server.host, server.port), timeout=2) as sock:
            headers_only = (
                b"POST /submit HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Expect: 100-continue\r\n"
                b"Content-Length: 4\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            sock.sendall(headers_only)
            interim = _recv_until_headers(sock)

            sock.sendall(b"name")
            final_response = _recv_http_response(sock)
    finally:
        _stop_server(server, thread)

    assert interim.startswith(b"HTTP/1.1 100 Continue")
    assert final_response.startswith(b"HTTP/1.1 200 OK")
    assert b"Received POST body: name" in final_response


def test_expect_continue_oversized_body_is_rejected_without_interim(engine: str) -> None:
    server, thread = _start_server(engine)
    try:
        with socket.create_connection((server.host, server.port), timeout=2) as sock:
            headers_only = (
                b"POST /submit HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Expect: 100-continue\r\n"
                + f"Content-Length: {MAX_BODY_BYTES + 1}\r\n".encode("ascii")
                + b"Connection: close\r\n"
                + b"\r\n"
            )
            sock.sendall(headers_only)
            response = _recv_http_response(sock)
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 413 Payload Too Large")
    assert b"HTTP/1.1 100 Continue" not in response
