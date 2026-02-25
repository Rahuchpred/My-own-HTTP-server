"""V24 tests for Date/Server/Connection protocol headers."""

import socket
import threading
import time
from email.utils import parsedate_to_datetime

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


def test_response_adds_date_and_server_headers() -> None:
    response = HTTPResponse(status_code=200, body="ok")
    raw = response.to_bytes()
    head = raw.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1")
    headers = {}
    for line in head.split("\r\n")[1:]:
        key, value = line.split(": ", 1)
        headers[key] = value

    assert "Date" in headers
    assert "Server" in headers
    parsedate_to_datetime(headers["Date"])


def test_explicit_server_header_is_preserved() -> None:
    response = HTTPResponse(status_code=200, headers={"Server": "MyServer"}, body="ok")
    raw = response.to_bytes()
    assert b"Server: MyServer\r\n" in raw


def test_server_sets_connection_header_on_keepalive_response() -> None:
    server, thread = _start_server()
    try:
        payload = (
            b"GET / HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: keep-alive\r\n"
            b"\r\n"
        )
        with socket.create_connection((server.host, server.port), timeout=2) as sock:
            sock.sendall(payload)
            response = sock.recv(4096)
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 200 OK")
    assert b"Connection: keep-alive\r\n" in response
    assert b"Date: " in response
    assert b"Server: " in response
