"""V27 protocol regression tests for keep-alive, chunked, and queue behavior."""

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from server import HTTPServer


class SlowHTTPServer(HTTPServer):
    def _handle_client(self, client_socket: socket.socket, address: tuple[str, int]) -> None:
        time.sleep(0.3)
        super()._handle_client(client_socket, address)


def _start_server(server: HTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    deadline = time.time() + 2
    while server.port == 0 and time.time() < deadline:
        time.sleep(0.01)
    if server.port == 0:
        raise RuntimeError("Server did not bind to a port")
    return thread


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


def test_keepalive_and_chunked_request_work_together() -> None:
    server = HTTPServer(port=0)
    thread = _start_server(server)
    try:
        with socket.create_connection((server.host, server.port), timeout=2) as sock:
            first = (
                b"POST /submit HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Connection: keep-alive\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"\r\n"
                b"4\r\nname\r\n"
                b"5\r\n=test\r\n"
                b"0\r\n\r\n"
            )
            sock.sendall(first)
            response_one = _recv_http_response(sock)

            second = (
                b"GET / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            sock.sendall(second)
            response_two = _recv_http_response(sock)
    finally:
        _stop_server(server, thread)

    assert response_one.startswith(b"HTTP/1.1 200 OK")
    assert b"Received POST body: name=test" in response_one
    assert response_two.startswith(b"HTTP/1.1 200 OK")


def test_queue_saturation_still_returns_503() -> None:
    server = SlowHTTPServer(port=0, worker_count=1, request_queue_size=1)
    thread = _start_server(server)
    try:
        payload = b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"

        def send() -> bytes:
            with socket.create_connection((server.host, server.port), timeout=2) as sock:
                sock.sendall(payload)
                return sock.recv(4096)

        with ThreadPoolExecutor(max_workers=3) as executor:
            responses = list(executor.map(lambda _i: send(), range(3)))
    finally:
        _stop_server(server, thread)

    assert any(response.startswith(b"HTTP/1.1 503 Service Unavailable") for response in responses)


def test_head_metrics_returns_no_body() -> None:
    server = HTTPServer(port=0)
    thread = _start_server(server)
    try:
        payload = (
            b"HEAD /_metrics HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        with socket.create_connection((server.host, server.port), timeout=2) as sock:
            sock.sendall(payload)
            response = sock.recv(8192)
    finally:
        _stop_server(server, thread)

    head, body = response.split(b"\r\n\r\n", 1)
    assert head.startswith(b"HTTP/1.1 200 OK")
    assert b"Content-Type: application/json\r\n" in head + b"\r\n"
    assert body == b""
