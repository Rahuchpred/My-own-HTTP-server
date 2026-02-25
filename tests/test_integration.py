"""Socket-level integration tests for the HTTP server."""

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from config import MAX_BODY_BYTES
from server import HTTPServer


def _start_server() -> tuple[HTTPServer, threading.Thread]:
    server = HTTPServer(port=0)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()

    deadline = time.time() + 3
    while server.port == 0 and time.time() < deadline:
        time.sleep(0.01)

    if server.port == 0:
        raise RuntimeError("Server did not bind to a port")

    return server, thread



def _stop_server(server: HTTPServer, thread: threading.Thread) -> None:
    server.stop()
    thread.join(timeout=2.0)



def _send_raw(host: str, port: int, payload: bytes) -> bytes:
    with socket.create_connection((host, port), timeout=2.0) as client:
        client.sendall(payload)
        return client.recv(8192)



def test_concurrent_requests() -> None:
    server, thread = _start_server()

    payload = b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n"

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [
            executor.submit(_send_raw, server.host, server.port, payload)
            for _ in range(20)
        ]
        responses = [future.result() for future in futures]

    _stop_server(server, thread)

    assert len(responses) == 20
    assert all(response.startswith(b"HTTP/1.1 200 OK") for response in responses)



def test_malformed_request_returns_400() -> None:
    server, thread = _start_server()

    response = _send_raw(server.host, server.port, b"BROKEN\r\n\r\n")

    _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 400 Bad Request")



def test_unsupported_method_returns_405() -> None:
    server, thread = _start_server()

    response = _send_raw(server.host, server.port, b"PUT / HTTP/1.1\r\nHost: localhost\r\n\r\n")

    _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 405 Method Not Allowed")



def test_oversized_body_returns_413() -> None:
    server, thread = _start_server()

    content_length = MAX_BODY_BYTES + 1
    payload = (
        b"POST /submit HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        + f"Content-Length: {content_length}\r\n".encode("ascii")
        + b"\r\n"
    )

    response = _send_raw(server.host, server.port, payload)

    _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 413 Payload Too Large")
