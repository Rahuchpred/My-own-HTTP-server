"""V46 tests for incident mode controls and response injection."""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

from server import HTTPServer


def _start_server(tmp_path: Path) -> tuple[HTTPServer, threading.Thread]:
    server = HTTPServer(
        port=0,
        engine="selectors",
        enable_tls=False,
        state_file=str(tmp_path / "playground.json"),
        scenario_state_file=str(tmp_path / "scenario.json"),
        target_state_file=str(tmp_path / "targets.json"),
    )
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()

    deadline = time.time() + 3
    while server.port == 0 and time.time() < deadline:
        time.sleep(0.01)

    if server.port == 0:
        raise RuntimeError("server did not start")
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
        if b":" not in line:
            continue
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


def test_incident_mode_start_stop_and_injects_503(tmp_path: Path) -> None:
    server, thread = _start_server(tmp_path)
    try:
        start_payload = {
            "mode": "status_spike_503",
            "probability": 1.0,
            "latency_ms": 0,
            "seed": 7,
        }
        start_response = _request(
            server.host,
            server.port,
            (
                "POST /api/incidents/start HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(json.dumps(start_payload))}\r\n"
                "Connection: close\r\n\r\n"
                f"{json.dumps(start_payload)}"
            ).encode("utf-8"),
        )

        during_response = _request(
            server.host,
            server.port,
            b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )

        stop_response = _request(
            server.host,
            server.port,
            (
                "POST /api/incidents/stop HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Content-Length: 2\r\n"
                "Content-Type: application/json\r\n"
                "Connection: close\r\n\r\n{}"
            ).encode("utf-8"),
        )

        after_response = _request(
            server.host,
            server.port,
            b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
    finally:
        _stop_server(server, thread)

    assert start_response.startswith(b"HTTP/1.1 200")
    assert during_response.startswith(b"HTTP/1.1 503")
    assert stop_response.startswith(b"HTTP/1.1 200")
    assert after_response.startswith(b"HTTP/1.1 200")
    assert b"Hello World" in after_response
