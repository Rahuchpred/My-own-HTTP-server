"""V43 tests for API playground routes, dynamic mocks, and replay."""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from server import HTTPServer


def _start_server(tmp_path: Path, *, engine: str) -> tuple[HTTPServer, threading.Thread]:
    state_file = tmp_path / f"state-{engine}.json"
    server = HTTPServer(
        port=0,
        engine=engine,
        enable_playground=True,
        state_file=str(state_file),
        history_limit=100,
        enable_tls=False,
    )
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
    thread.join(timeout=3)


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


def _request(host: str, port: int, payload: bytes) -> bytes:
    with socket.create_connection((host, port), timeout=3) as sock:
        sock.sendall(payload)
        return _recv_http_response(sock)


@pytest.mark.parametrize("engine", ["threadpool", "selectors"])
def test_playground_mock_create_dispatch_history_and_replay(
    tmp_path: Path,
    engine: str,
) -> None:
    server, thread = _start_server(tmp_path, engine=engine)
    try:
        create_payload = {
            "method": "POST",
            "path_pattern": "/api/users",
            "status": 201,
            "headers": {"X-Mock-Server": "custom"},
            "body": '{"created":true}',
            "content_type": "application/json",
        }
        create_raw = (
            b"POST /api/mocks HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(json.dumps(create_payload))}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + json.dumps(create_payload).encode("utf-8")
        )
        create_response = _request(server.host, server.port, create_raw)

        mock_request = (
            b"POST /api/users HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: 0\r\n"
            b"Connection: close\r\n\r\n"
        )
        mock_response = _request(server.host, server.port, mock_request)

        history_response = _request(
            server.host,
            server.port,
            b"GET /api/history HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )

        _head, body = history_response.split(b"\r\n\r\n", 1)
        history = json.loads(body.decode("utf-8"))["history"]
        user_record = next(item for item in history if item.get("path") == "/api/users")

        replay_response = _request(
            server.host,
            server.port,
            (
                f"POST /api/replay/{user_record['request_id']} HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Connection: close\r\n"
                "Content-Length: 0\r\n\r\n"
            ).encode("utf-8"),
        )

        state_response = _request(
            server.host,
            server.port,
            b"GET /api/playground/state HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
    finally:
        _stop_server(server, thread)

    assert create_response.startswith(b"HTTP/1.1 201")
    assert mock_response.startswith(b"HTTP/1.1 201")
    assert b"X-Mock-Server: custom" in mock_response

    assert history_response.startswith(b"HTTP/1.1 200")
    assert user_record["trace_id"]
    assert isinstance(user_record["latency_ms"], float | int)

    assert replay_response.startswith(b"HTTP/1.1 200")
    assert b"replayed_request_id" in replay_response

    assert state_response.startswith(b"HTTP/1.1 200")
    assert b'"mocks"' in state_response


@pytest.mark.parametrize("engine", ["threadpool", "selectors"])
def test_playground_blocks_reserved_paths_and_serves_ui(tmp_path: Path, engine: str) -> None:
    server, thread = _start_server(tmp_path, engine=engine)
    try:
        payload = {
            "method": "GET",
            "path_pattern": "/_metrics",
            "status": 200,
            "headers": {},
            "body": "blocked",
            "content_type": "text/plain",
        }
        raw = (
            b"POST /api/mocks HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(json.dumps(payload))}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + json.dumps(payload).encode("utf-8")
        )
        reserved_response = _request(server.host, server.port, raw)
        playground_response = _request(
            server.host,
            server.port,
            b"GET /playground HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
    finally:
        _stop_server(server, thread)

    assert reserved_response.startswith(b"HTTP/1.1 403")
    assert playground_response.startswith(b"HTTP/1.1 200")
    assert b"API Playground" in playground_response
