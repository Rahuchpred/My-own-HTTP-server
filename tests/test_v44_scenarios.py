"""V44 tests for scenario APIs and execution."""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from server import HTTPServer


def _start_server(tmp_path: Path, *, engine: str) -> tuple[HTTPServer, threading.Thread]:
    server = HTTPServer(
        port=0,
        engine=engine,
        enable_playground=True,
        enable_scenarios=True,
        state_file=str(tmp_path / f"playground-{engine}.json"),
        scenario_state_file=str(tmp_path / f"scenarios-{engine}.json"),
        enable_tls=False,
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
def test_scenario_crud_and_run(engine: str, tmp_path: Path) -> None:
    server, thread = _start_server(tmp_path, engine=engine)
    try:
        scenario_payload = {
            "name": "User flow",
            "description": "basic",
            "steps": [
                {
                    "id": "home",
                    "method": "GET",
                    "path": "/",
                    "expect": {"status": 200, "body_contains": ["Hello World"]},
                }
            ],
        }

        create_raw = (
            b"POST /api/scenarios HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(json.dumps(scenario_payload))}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + json.dumps(scenario_payload).encode("utf-8")
        )
        create_response = _request(server.host, server.port, create_raw)
        assert create_response.startswith(b"HTTP/1.1 201")

        _head, body = create_response.split(b"\r\n\r\n", 1)
        created = json.loads(body.decode("utf-8"))["scenario"]
        scenario_id = created["id"]

        run_response = _request(
            server.host,
            server.port,
            (
                f"POST /api/scenarios/{scenario_id}/run HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 2\r\n"
                "Connection: close\r\n\r\n{}"
            ).encode("utf-8"),
        )
        assert run_response.startswith(b"HTTP/1.1 200")

        _head, run_body = run_response.split(b"\r\n\r\n", 1)
        run = json.loads(run_body.decode("utf-8"))["run"]
        assert run["status"] == "pass"
        run_id = run["run_id"]

        runs_response = _request(
            server.host,
            server.port,
            (
                f"GET /api/scenarios/{scenario_id}/runs?limit=5 HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Connection: close\r\n\r\n"
            ).encode("utf-8"),
        )
        assert runs_response.startswith(b"HTTP/1.1 200")
        assert run_id.encode("utf-8") in runs_response

        run_detail_response = _request(
            server.host,
            server.port,
            (
                f"GET /api/scenarios/runs/{run_id} HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Connection: close\r\n\r\n"
            ).encode("utf-8"),
        )
        assert run_detail_response.startswith(b"HTTP/1.1 200")

        metrics_response = _request(
            server.host,
            server.port,
            b"GET /_metrics HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
        assert b"scenario_runs_total" in metrics_response

        delete_response = _request(
            server.host,
            server.port,
            (
                f"DELETE /api/scenarios/{scenario_id} HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Connection: close\r\n\r\n"
            ).encode("utf-8"),
        )
        assert delete_response.startswith(b"HTTP/1.1 200")
    finally:
        _stop_server(server, thread)
