"""V30 tests for static cache validators and conditional responses."""

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


def _parse_headers(raw_response: bytes) -> tuple[bytes, dict[str, str], bytes]:
    head, body = raw_response.split(b"\r\n\r\n", 1)
    lines = head.decode("iso-8859-1").split("\r\n")
    status_line = lines[0].encode("iso-8859-1")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, value = line.split(": ", 1)
        headers[key.lower()] = value
    return status_line, headers, body


def test_static_file_returns_etag_and_last_modified_and_supports_304() -> None:
    server, thread = _start_server()
    try:
        first = _send_and_recv(
            server.host,
            server.port,
            b"GET /static/test.html HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
        status_one, headers_one, _body_one = _parse_headers(first)
        etag = headers_one["etag"]
        last_modified = headers_one["last-modified"]

        second = _send_and_recv(
            server.host,
            server.port,
            (
                b"GET /static/test.html HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                + f"If-None-Match: {etag}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
            ),
        )
        status_two, _headers_two, body_two = _parse_headers(second)

        third = _send_and_recv(
            server.host,
            server.port,
            (
                b"GET /static/test.html HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                + f"If-Modified-Since: {last_modified}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
            ),
        )
        status_three, _headers_three, body_three = _parse_headers(third)
    finally:
        _stop_server(server, thread)

    assert status_one.startswith(b"HTTP/1.1 200 OK")
    assert etag
    assert last_modified
    assert status_two.startswith(b"HTTP/1.1 304 Not Modified")
    assert body_two == b""
    assert status_three.startswith(b"HTTP/1.1 304 Not Modified")
    assert body_three == b""
