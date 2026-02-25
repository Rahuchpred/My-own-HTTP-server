"""P04 test for the hardcoded GET / home route."""

import socket
import threading
import time

from server import HTTPServer


def test_get_root_returns_home_response() -> None:
    server = HTTPServer(port=0)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()

    deadline = time.time() + 2
    while server.port == 0 and time.time() < deadline:
        time.sleep(0.01)

    with socket.create_connection((server.host, server.port), timeout=1.0) as client:
        client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        raw_response = client.recv(4096)

    server.stop()
    thread.join(timeout=1.0)

    assert raw_response.startswith(b"HTTP/1.1 200 OK")
    assert b"Hello World" in raw_response
