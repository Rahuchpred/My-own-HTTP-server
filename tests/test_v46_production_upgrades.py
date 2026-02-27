"""V46 tests for live fallback, RBAC auth sessions, and persistent trend storage."""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time
from pathlib import Path

from auth_store import hash_password
from server import HTTPServer


def _start_server(
    tmp_path: Path,
    *,
    engine: str = "selectors",
    auth_users_file: str | None = None,
    require_demo_token: bool = False,
    demo_token: str = "",
    metrics_sqlite_file: str | None = None,
    metrics_retention_days: int = 30,
    metrics_flush_interval_secs: int = 1,
) -> tuple[HTTPServer, threading.Thread]:
    sqlite_file = metrics_sqlite_file or str(tmp_path / "metrics.sqlite3")
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
        auth_users_file=auth_users_file or str(tmp_path / "auth_users.json"),
        require_demo_token=require_demo_token,
        demo_token=demo_token,
        metrics_backend="sqlite",
        metrics_sqlite_file=sqlite_file,
        metrics_retention_days=metrics_retention_days,
        metrics_flush_interval_secs=metrics_flush_interval_secs,
    )
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()

    deadline = time.time() + 4
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
    if not body:
        return {}
    return json.loads(body.decode("utf-8"))


def _make_users_file(path: Path) -> Path:
    payload = {
        "version": 1,
        "users": [
            {
                "username": "viewer",
                "password_hash": hash_password("viewer123"),
                "role": "viewer",
                "enabled": True,
            },
            {
                "username": "operator",
                "password_hash": hash_password("operator123"),
                "role": "operator",
                "enabled": True,
            },
            {
                "username": "admin",
                "password_hash": hash_password("admin123"),
                "role": "admin",
                "enabled": True,
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _login(host: str, port: int, username: str, password: str) -> tuple[bytes, str]:
    payload = json.dumps({"username": username, "password": password})
    response = _request(
        host,
        port,
        (
            "POST /api/auth/login HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n"
            f"{payload}"
        ).encode("utf-8"),
    )
    token = ""
    if response.startswith(b"HTTP/1.1 200"):
        token = str(_json_body(response)["session"]["token"])
    return response, token


def test_live_mode_reports_unavailable_on_threadpool_when_selectors_required(
    tmp_path: Path,
) -> None:
    server, thread = _start_server(tmp_path, engine="threadpool")
    try:
        response = _request(
            server.host,
            server.port,
            b"GET /api/events/live-mode HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
    finally:
        _stop_server(server, thread)

    body = _json_body(response)
    assert response.startswith(b"HTTP/1.1 200")
    assert body["available"] is False
    assert body["reason"] == "use_selectors_engine"


def test_live_mode_reports_available_on_selectors(tmp_path: Path) -> None:
    server, thread = _start_server(tmp_path, engine="selectors")
    try:
        response = _request(
            server.host,
            server.port,
            b"GET /api/events/live-mode HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
    finally:
        _stop_server(server, thread)

    body = _json_body(response)
    assert response.startswith(b"HTTP/1.1 200")
    assert body["available"] is True
    assert body["engine"] == "selectors"


def test_metrics_trends_persist_across_restart_sqlite(tmp_path: Path) -> None:
    sqlite_file = tmp_path / "metrics.sqlite3"
    server, thread = _start_server(
        tmp_path,
        engine="selectors",
        metrics_sqlite_file=str(sqlite_file),
    )
    try:
        _request(
            server.host,
            server.port,
            b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
        _request(
            server.host,
            server.port,
            b"GET /unknown HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
        time.sleep(1.2)
    finally:
        _stop_server(server, thread)

    server2, thread2 = _start_server(
        tmp_path,
        engine="selectors",
        metrics_sqlite_file=str(sqlite_file),
    )
    try:
        trends_payload = (
            b"GET /api/metrics/trends?window=24h&route=__all__ HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: close\r\n\r\n"
        )
        trends = _request(
            server2.host,
            server2.port,
            trends_payload,
        )
    finally:
        _stop_server(server2, thread2)

    body = _json_body(trends)
    assert trends.startswith(b"HTTP/1.1 200")
    assert int(body["summary"]["request_count"]) >= 2


def test_metrics_prunes_rows_older_than_retention(tmp_path: Path) -> None:
    sqlite_file = tmp_path / "metrics.sqlite3"
    old_bucket = int((time.time() - (3 * 24 * 60 * 60)) // 60) * 60

    conn = sqlite3.connect(str(sqlite_file))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metric_trends (
            bucket_start INTEGER NOT NULL,
            route_key TEXT NOT NULL,
            request_count INTEGER NOT NULL,
            status_4xx INTEGER NOT NULL,
            status_5xx INTEGER NOT NULL,
            lat_bucket_0 INTEGER NOT NULL,
            lat_bucket_1 INTEGER NOT NULL,
            lat_bucket_2 INTEGER NOT NULL,
            lat_bucket_3 INTEGER NOT NULL,
            lat_bucket_4 INTEGER NOT NULL,
            lat_bucket_5 INTEGER NOT NULL,
            lat_bucket_6 INTEGER NOT NULL,
            lat_bucket_7 INTEGER NOT NULL,
            lat_bucket_8 INTEGER NOT NULL,
            lat_bucket_9 INTEGER NOT NULL,
            lat_bucket_10 INTEGER NOT NULL,
            PRIMARY KEY (bucket_start, route_key)
        )
        """
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO metric_trends (
            bucket_start, route_key, request_count, status_4xx, status_5xx,
            lat_bucket_0, lat_bucket_1, lat_bucket_2, lat_bucket_3, lat_bucket_4,
            lat_bucket_5, lat_bucket_6, lat_bucket_7, lat_bucket_8, lat_bucket_9, lat_bucket_10
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (old_bucket, "__all__", 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0),
    )
    conn.commit()
    conn.close()

    server, thread = _start_server(
        tmp_path,
        engine="selectors",
        metrics_sqlite_file=str(sqlite_file),
        metrics_retention_days=1,
        metrics_flush_interval_secs=1,
    )
    try:
        time.sleep(1.5)
        trends_payload = (
            b"GET /api/metrics/trends?window=24h&route=__all__ HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: close\r\n\r\n"
        )
        trends = _request(
            server.host,
            server.port,
            trends_payload,
        )
    finally:
        _stop_server(server, thread)

    body = _json_body(trends)
    buckets = [int(item["bucket_start"]) for item in body.get("points", [])]
    assert old_bucket not in buckets


def test_metrics_trends_endpoint_returns_percentiles(tmp_path: Path) -> None:
    server, thread = _start_server(tmp_path, engine="selectors")
    try:
        _request(
            server.host,
            server.port,
            b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
        time.sleep(1.1)
        trends_payload = (
            b"GET /api/metrics/trends?window=24h&route=__all__ HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: close\r\n\r\n"
        )
        trends = _request(
            server.host,
            server.port,
            trends_payload,
        )
    finally:
        _stop_server(server, thread)

    payload = _json_body(trends)
    summary = payload["summary"]
    assert trends.startswith(b"HTTP/1.1 200")
    assert "p50" in summary
    assert "p95" in summary
    assert "p99" in summary
    assert "error_rate" in summary


def test_auth_login_success_and_me(tmp_path: Path) -> None:
    users = _make_users_file(tmp_path / "users.json")
    server, thread = _start_server(tmp_path, auth_users_file=str(users))
    try:
        login_response, token = _login(server.host, server.port, "operator", "operator123")
        me_response = _request(
            server.host,
            server.port,
            (
                "GET /api/auth/me HTTP/1.1\r\n"
                "Host: localhost\r\n"
                f"Authorization: Bearer {token}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("utf-8"),
        )
    finally:
        _stop_server(server, thread)

    assert login_response.startswith(b"HTTP/1.1 200")
    assert token
    me_body = _json_body(me_response)
    assert me_response.startswith(b"HTTP/1.1 200")
    assert me_body["auth"]["role"] == "operator"


def test_auth_login_invalid_credentials_401(tmp_path: Path) -> None:
    users = _make_users_file(tmp_path / "users.json")
    server, thread = _start_server(tmp_path, auth_users_file=str(users))
    try:
        response, _token = _login(server.host, server.port, "operator", "wrong")
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 401")


def test_viewer_cannot_start_incident_and_operator_can(tmp_path: Path) -> None:
    users = _make_users_file(tmp_path / "users.json")
    server, thread = _start_server(tmp_path, auth_users_file=str(users))
    try:
        _viewer_response, viewer_token = _login(
            server.host,
            server.port,
            "viewer",
            "viewer123",
        )
        _operator_response, operator_token = _login(
            server.host,
            server.port,
            "operator",
            "operator123",
        )
        payload = json.dumps(
            {
                "mode": "status_spike_503",
                "probability": 0.1,
                "latency_ms": 0,
                "seed": 1337,
            }
        )

        viewer_start = _request(
            server.host,
            server.port,
            (
                "POST /api/incidents/start HTTP/1.1\r\n"
                "Host: localhost\r\n"
                f"Authorization: Bearer {viewer_token}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Connection: close\r\n\r\n"
                f"{payload}"
            ).encode("utf-8"),
        )
        operator_start = _request(
            server.host,
            server.port,
            (
                "POST /api/incidents/start HTTP/1.1\r\n"
                "Host: localhost\r\n"
                f"Authorization: Bearer {operator_token}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Connection: close\r\n\r\n"
                f"{payload}"
            ).encode("utf-8"),
        )
    finally:
        _stop_server(server, thread)

    assert viewer_start.startswith(b"HTTP/1.1 403")
    assert operator_start.startswith(b"HTTP/1.1 200")


def test_operator_cannot_admin_crud_and_admin_can(tmp_path: Path) -> None:
    users = _make_users_file(tmp_path / "users.json")
    server, thread = _start_server(tmp_path, auth_users_file=str(users))
    try:
        _op_resp, operator_token = _login(server.host, server.port, "operator", "operator123")
        _ad_resp, admin_token = _login(server.host, server.port, "admin", "admin123")
        mock_payload = json.dumps(
            {
                "method": "GET",
                "path_pattern": "/api/test",
                "status": 200,
                "headers": {"X-Mock-Server": "test"},
                "body": "{\"ok\":true}",
                "content_type": "application/json",
            }
        )
        operator_create = _request(
            server.host,
            server.port,
            (
                "POST /api/mocks HTTP/1.1\r\n"
                "Host: localhost\r\n"
                f"Authorization: Bearer {operator_token}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(mock_payload)}\r\n"
                "Connection: close\r\n\r\n"
                f"{mock_payload}"
            ).encode("utf-8"),
        )
        admin_create = _request(
            server.host,
            server.port,
            (
                "POST /api/mocks HTTP/1.1\r\n"
                "Host: localhost\r\n"
                f"Authorization: Bearer {admin_token}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(mock_payload)}\r\n"
                "Connection: close\r\n\r\n"
                f"{mock_payload}"
            ).encode("utf-8"),
        )
    finally:
        _stop_server(server, thread)

    assert operator_create.startswith(b"HTTP/1.1 403")
    assert admin_create.startswith(b"HTTP/1.1 201")


def test_legacy_demo_token_still_authorizes_admin_paths(tmp_path: Path) -> None:
    server, thread = _start_server(
        tmp_path,
        require_demo_token=True,
        demo_token="legacy-secret",
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
            b"GET /api/targets HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Authorization: Bearer legacy-secret\r\n"
            b"Connection: close\r\n\r\n",
        )
    finally:
        _stop_server(server, thread)

    assert unauthorized.startswith(b"HTTP/1.1 401")
    assert authorized.startswith(b"HTTP/1.1 200")


def test_openapi_json_served_and_contains_core_paths(tmp_path: Path) -> None:
    server, thread = _start_server(tmp_path)
    try:
        response = _request(
            server.host,
            server.port,
            b"GET /openapi.json HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 200")
    payload = _json_body(response)
    paths = payload.get("paths", {})
    assert "/api/events/live-mode" in paths
    assert "/api/metrics/trends" in paths
    assert "/api/auth/login" in paths


def test_playground_html_includes_demo_mode_and_live_controls(tmp_path: Path) -> None:
    server, thread = _start_server(tmp_path)
    try:
        response = _request(
            server.host,
            server.port,
            b"GET /playground HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 200")
    body = response.split(b"\r\n\r\n", 1)[1]
    assert b"Enable Live Mode" in body
    assert b"Demo Presets" in body


def test_playground_js_has_fallback_and_enable_live_mode_hooks() -> None:
    script = (Path(__file__).resolve().parent.parent / "static" / "playground.js").read_text(
        encoding="utf-8"
    )
    assert "startSnapshotFallback" in script
    assert "/api/events/live-mode" in script
    assert "DEMO_PRESETS" in script
