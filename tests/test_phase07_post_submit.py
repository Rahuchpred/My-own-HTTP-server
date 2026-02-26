"""P07 test for POST /submit handling."""

import socket
import threading
import time

from server import HTTPServer


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
        response = _recv_http_response(client)

    server.stop()
    thread.join(timeout=1.0)

    assert response.startswith(b"HTTP/1.1 200 OK")
    assert b"Received POST body: name=test" in response
