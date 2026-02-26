"""Compatibility tests for CodeCrafters-style HTTP server behavior."""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from server import HTTPServer, _parse_args

PROJECT_ROOT = Path(__file__).resolve().parent.parent
YOUR_PROGRAM_PATH = PROJECT_ROOT / "your_program.sh"


def _start_server(*, files_directory: Path | None = None) -> tuple[HTTPServer, threading.Thread]:
    server = HTTPServer(
        port=0,
        codecrafters_mode=True,
        files_directory=str(files_directory) if files_directory is not None else None,
        enable_playground=False,
        enable_scenarios=False,
        enable_live_events=False,
        enable_target_proxy=False,
    )
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

    head = bytes(buffer[:header_end])
    body = bytes(buffer[header_end + 4 :])
    headers = _parse_headers_from_head(head)
    content_length = int(headers.get(b"content-length", b"0"))

    while len(body) < content_length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk

    return head + b"\r\n\r\n" + body


def _send_request(host: str, port: int, payload: bytes) -> bytes:
    with socket.create_connection((host, port), timeout=2) as sock:
        sock.sendall(payload)
        return _recv_http_response(sock)


def _parse_headers_from_head(head: bytes) -> dict[bytes, bytes]:
    headers: dict[bytes, bytes] = {}
    for line in head.split(b"\r\n")[1:]:
        if b":" not in line:
            continue
        key, value = line.split(b":", 1)
        headers[key.strip().lower()] = value.strip()
    return headers


def _split_response(response: bytes) -> tuple[bytes, dict[bytes, bytes], bytes]:
    head, body = response.split(b"\r\n\r\n", 1)
    return head, _parse_headers_from_head(head), body


def test_cc_mode_default_port_4221(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["server.py", "--codecrafters-mode"])
    args = _parse_args()
    assert args.port == 4221

    monkeypatch.setattr(sys, "argv", ["server.py", "--codecrafters-mode", "--port", "9099"])
    args = _parse_args()
    assert args.port == 9099


def test_cc_root_200_and_unknown_404() -> None:
    server, thread = _start_server()
    try:
        root_response = _send_request(
            server.host,
            server.port,
            b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
        unknown_response = _send_request(
            server.host,
            server.port,
            b"GET /not-found HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
    finally:
        _stop_server(server, thread)

    assert root_response.startswith(b"HTTP/1.1 200 OK")
    assert unknown_response.startswith(b"HTTP/1.1 404 Not Found")


def test_cc_echo_returns_plain_text_and_length() -> None:
    server, thread = _start_server()
    try:
        response = _send_request(
            server.host,
            server.port,
            b"GET /echo/abc123 HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
    finally:
        _stop_server(server, thread)

    head, headers, body = _split_response(response)
    assert head.startswith(b"HTTP/1.1 200 OK")
    assert headers.get(b"content-type") == b"text/plain"
    assert headers.get(b"content-length") == b"6"
    assert body == b"abc123"


def test_cc_user_agent_echoes_header() -> None:
    server, thread = _start_server()
    try:
        response = _send_request(
            server.host,
            server.port,
            (
                b"GET /user-agent HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"User-Agent: foobar/1.2.3\r\n"
                b"Connection: close\r\n\r\n"
            ),
        )
    finally:
        _stop_server(server, thread)

    head, headers, body = _split_response(response)
    assert head.startswith(b"HTTP/1.1 200 OK")
    assert headers.get(b"content-type") == b"text/plain"
    assert headers.get(b"content-length") == b"12"
    assert body == b"foobar/1.2.3"


def test_cc_files_get_existing_returns_octet_stream(tmp_path: Path) -> None:
    (tmp_path / "foo").write_bytes(b"Hello, World!")
    server, thread = _start_server(files_directory=tmp_path)
    try:
        response = _send_request(
            server.host,
            server.port,
            b"GET /files/foo HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
    finally:
        _stop_server(server, thread)

    head, headers, body = _split_response(response)
    assert head.startswith(b"HTTP/1.1 200 OK")
    assert headers.get(b"content-type") == b"application/octet-stream"
    assert headers.get(b"content-length") == b"13"
    assert body == b"Hello, World!"


def test_cc_files_get_missing_returns_404(tmp_path: Path) -> None:
    server, thread = _start_server(files_directory=tmp_path)
    try:
        response = _send_request(
            server.host,
            server.port,
            b"GET /files/missing HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 404 Not Found")


def test_cc_files_post_creates_file_and_returns_201(tmp_path: Path) -> None:
    server, thread = _start_server(files_directory=tmp_path)
    try:
        body = b"12345"
        response = _send_request(
            server.host,
            server.port,
            (
                b"POST /files/file_123 HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Content-Type: application/octet-stream\r\n"
                b"Connection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                + body
            ),
        )
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 201 Created")
    assert (tmp_path / "file_123").read_bytes() == b"12345"


def test_cc_files_rejects_path_traversal(tmp_path: Path) -> None:
    server, thread = _start_server(files_directory=tmp_path)
    try:
        response = _send_request(
            server.host,
            server.port,
            b"GET /files/../secret HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 404 Not Found")


def test_cc_mode_accepts_directory_flag_passthrough_from_your_program(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            "bash",
            str(YOUR_PROGRAM_PATH),
            "--directory",
            str(tmp_path),
            "--help",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--directory" in result.stdout
    assert "--codecrafters-mode" in result.stdout
