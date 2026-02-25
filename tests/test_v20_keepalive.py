"""V20 tests for persistent keep-alive request handling."""

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

    header_block = bytes(buffer[:header_end])
    body = bytes(buffer[header_end + 4 :])
    content_length = 0
    for line in header_block.split(b"\r\n")[1:]:
        name, value = line.split(b":", 1)
        if name.strip().lower() == b"content-length":
            content_length = int(value.strip())
            break

    while len(body) < content_length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk

    return header_block + b"\r\n\r\n" + body


def test_keep_alive_supports_multiple_requests_on_same_socket() -> None:
    server, thread = _start_server()

    with socket.create_connection((server.host, server.port), timeout=2) as sock:
        sock.sendall(
            b"GET / HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: keep-alive\r\n"
            b"\r\n"
        )
        response_one = _recv_http_response(sock)

        sock.sendall(
            b"GET /unknown HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        response_two = _recv_http_response(sock)

    _stop_server(server, thread)

    assert response_one.startswith(b"HTTP/1.1 200 OK")
    assert b"Connection: keep-alive" in response_one
    assert response_two.startswith(b"HTTP/1.1 404 Not Found")
    assert b"Connection: close" in response_two
