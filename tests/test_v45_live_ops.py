"""V45 tests for live events, target proxy, and demo token guards."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from server import HTTPServer


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        payload = {
            "ok": True,
            "method": "GET",
            "path": self.path,
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        _ = (format, args)


def _start_fixture_server() -> tuple[ThreadingHTTPServer, threading.Thread]:
    fixture = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    return fixture, thread


def _stop_fixture_server(server: ThreadingHTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _start_server(
    tmp_path: Path,
    *,
    engine: str,
    require_demo_token: bool = False,
    demo_token: str = "",
) -> tuple[HTTPServer, threading.Thread]:
    server = HTTPServer(
        port=0,
        engine=engine,
        enable_tls=False,
        enable_playground=True,
        enable_scenarios=True,
        enable_live_events=True,
        enable_target_proxy=True,
        state_file=str(tmp_path / f"playground-{engine}.json"),
        scenario_state_file=str(tmp_path / f"scenario-{engine}.json"),
        target_state_file=str(tmp_path / f"targets-{engine}.json"),
        require_demo_token=require_demo_token,
        demo_token=demo_token,
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


def _json_body(response: bytes) -> dict[str, object]:
    _head, body = response.split(b"\r\n\r\n", 1)
    return json.loads(body.decode("utf-8"))


@pytest.mark.parametrize("engine", ["selectors"])
def test_live_snapshot_contains_request_and_scenario_events(tmp_path: Path, engine: str) -> None:
    server, thread = _start_server(tmp_path, engine=engine)
    try:
        scenario_payload = {
            "name": "live-events",
            "steps": [
                {
                    "method": "GET",
                    "path": "/",
                    "expect": {"status": 200, "body_contains": ["Hello World"]},
                }
            ],
        }

        create_response = _request(
            server.host,
            server.port,
            (
                "POST /api/scenarios HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(json.dumps(scenario_payload))}\r\n"
                "Connection: close\r\n\r\n"
                f"{json.dumps(scenario_payload)}"
            ).encode("utf-8"),
        )
        scenario_id = str(_json_body(create_response)["scenario"]["id"])

        _request(
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

        snapshot_response = _request(
            server.host,
            server.port,
            b"GET /api/events/snapshot?since_id=0&limit=500 HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: close\r\n\r\n",
        )
        payload = _json_body(snapshot_response)
        event_types = {item["type"] for item in payload["events"]}

        stream_response = _request(
            server.host,
            server.port,
            b"GET /api/events/stream?since_id=0 HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: close\r\n\r\n",
        )
    finally:
        _stop_server(server, thread)

    assert snapshot_response.startswith(b"HTTP/1.1 200")
    assert "request.completed" in event_types
    assert "scenario.run.started" in event_types
    assert "scenario.step.completed" in event_types
    assert stream_response.startswith(b"HTTP/1.1 200")
    assert b"Content-Type: text/event-stream" in stream_response


def test_events_stream_rejects_threadpool_when_selectors_required(tmp_path: Path) -> None:
    server, thread = _start_server(tmp_path, engine="threadpool")
    try:
        response = _request(
            server.host,
            server.port,
            b"GET /api/events/stream HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 409")
    assert b"use_selectors_engine" in response


def test_target_proxy_request_and_events(tmp_path: Path) -> None:
    fixture, fixture_thread = _start_fixture_server()
    server, thread = _start_server(tmp_path, engine="selectors")
    try:
        base_url = f"http://127.0.0.1:{fixture.server_address[1]}"
        create_target_payload = {
            "name": "fixture",
            "base_url": base_url,
            "enabled": True,
            "timeout_ms": 4000,
        }
        create_target_response = _request(
            server.host,
            server.port,
            (
                "POST /api/targets HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(json.dumps(create_target_payload))}\r\n"
                "Connection: close\r\n\r\n"
                f"{json.dumps(create_target_payload)}"
            ).encode("utf-8"),
        )
        target_id = str(_json_body(create_target_response)["target"]["id"])

        proxy_payload = {
            "target_id": target_id,
            "method": "GET",
            "path": "/hello",
            "headers": {},
            "body": "",
        }
        proxy_response = _request(
            server.host,
            server.port,
            (
                "POST /api/proxy/request HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(json.dumps(proxy_payload))}\r\n"
                "Connection: close\r\n\r\n"
                f"{json.dumps(proxy_payload)}"
            ).encode("utf-8"),
        )

        snapshot_response = _request(
            server.host,
            server.port,
            b"GET /api/events/snapshot?since_id=0&limit=500 HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: close\r\n\r\n",
        )
    finally:
        _stop_server(server, thread)
        _stop_fixture_server(fixture, fixture_thread)

    proxy_json = _json_body(proxy_response)
    events_json = _json_body(snapshot_response)

    assert create_target_response.startswith(b"HTTP/1.1 201")
    assert proxy_response.startswith(b"HTTP/1.1 200")
    assert int(proxy_json["response"]["status"]) == 200
    assert "hello" in str(proxy_json["response"]["body"])
    assert any(item["type"] == "proxy.request.completed" for item in events_json["events"])


def test_demo_token_protects_admin_apis(tmp_path: Path) -> None:
    server, thread = _start_server(
        tmp_path,
        engine="selectors",
        require_demo_token=True,
        demo_token="secret",
    )
    try:
        unauthorized = _request(
            server.host,
            server.port,
            b"GET /api/targets HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
        authorized = _request(
            server.host,
            server.port,
            (
                b"GET /api/targets HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Authorization: Bearer secret\r\n"
                b"Connection: close\r\n\r\n"
            ),
        )
        public_page = _request(
            server.host,
            server.port,
            b"GET /playground HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
    finally:
        _stop_server(server, thread)

    assert unauthorized.startswith(b"HTTP/1.1 401")
    assert authorized.startswith(b"HTTP/1.1 200")
    assert public_page.startswith(b"HTTP/1.1 200")
