"""P01 tests for basic TCP listener and one request handling."""

import socket
import threading
import time

from server import HTTPServer


def test_server_accepts_one_connection_and_replies() -> None:
    server = HTTPServer(port=0)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()

    deadline = time.time() + 2
    while server.port == 0 and time.time() < deadline:
        time.sleep(0.01)

    with socket.create_connection((server.host, server.port), timeout=1.0) as client:
        client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        response = client.recv(1024)

    thread.join(timeout=1.0)

    assert response.startswith(b"HTTP/1.1 200 OK")
    assert b"Content-Length: 0" in response
