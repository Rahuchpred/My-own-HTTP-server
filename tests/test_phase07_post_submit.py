"""P07 test for POST /submit handling."""

import socket
import threading
import time

from server import HTTPServer


def test_post_submit_returns_echoed_body() -> None:
    server = HTTPServer(port=0)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()

    deadline = time.time() + 2
    while server.port == 0 and time.time() < deadline:
        time.sleep(0.01)

    body = b"name=test"
    request = (
        b"POST /submit HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Type: application/x-www-form-urlencoded\r\n"
        + f"Content-Length: {len(body)}\r\n".encode("ascii")
        + b"\r\n"
        + body
    )

    with socket.create_connection((server.host, server.port), timeout=1.0) as client:
        client.sendall(request)
        response = client.recv(4096)

    server.stop()
    thread.join(timeout=1.0)

    assert response.startswith(b"HTTP/1.1 200 OK")
    assert b"Received POST body: name=test" in response
