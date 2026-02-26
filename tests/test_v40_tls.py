"""V40 tests for HTTPS listener and HTTP redirect behavior."""

from __future__ import annotations

import shutil
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

import pytest

from server import HTTPServer


@pytest.fixture()
def tls_files(tmp_path: Path) -> tuple[Path, Path]:
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available")

    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-sha256",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-keyout",
            str(key_file),
            "-out",
            str(cert_file),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return cert_file, key_file


def _start_server(
    *,
    engine: str,
    cert_file: Path,
    key_file: Path,
) -> tuple[HTTPServer, threading.Thread]:
    server = HTTPServer(
        port=0,
        https_port=0,
        engine=engine,
        enable_tls=True,
        tls_cert_file=str(cert_file),
        tls_key_file=str(key_file),
        redirect_http_to_https=True,
    )
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()

    deadline = time.time() + 3
    while (server.port == 0 or server.http_port == 0) and time.time() < deadline:
        time.sleep(0.01)

    if server.port == 0 or server.http_port == 0:
        raise RuntimeError("TLS server did not bind to ports")
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


@pytest.mark.parametrize("engine", ["threadpool", "selectors"])
def test_https_request_succeeds_with_tls_enabled(
    engine: str,
    tls_files: tuple[Path, Path],
) -> None:
    cert_file, key_file = tls_files
    server, thread = _start_server(engine=engine, cert_file=cert_file, key_file=key_file)
    try:
        client_context = ssl.create_default_context()
        client_context.check_hostname = False
        client_context.verify_mode = ssl.CERT_NONE
        with socket.create_connection((server.host, server.port), timeout=3) as raw_sock:
            with client_context.wrap_socket(raw_sock, server_hostname="localhost") as tls_sock:
                tls_sock.sendall(
                    b"GET / HTTP/1.1\r\n"
                    b"Host: localhost\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                )
                response = _recv_http_response(tls_sock)
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 200 OK")
    assert b"Hello World" in response


def test_http_listener_redirects_to_https(tls_files: tuple[Path, Path]) -> None:
    cert_file, key_file = tls_files
    server, thread = _start_server(engine="threadpool", cert_file=cert_file, key_file=key_file)
    try:
        with socket.create_connection((server.host, server.http_port), timeout=3) as sock:
            sock.sendall(
                b"GET /static/test.html HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            response = _recv_http_response(sock)
    finally:
        _stop_server(server, thread)

    assert response.startswith(b"HTTP/1.1 308 Permanent Redirect")
    assert f"Location: https://localhost:{server.port}/static/test.html".encode("ascii") in response


def test_tls_missing_files_fail_fast() -> None:
    server = HTTPServer(
        enable_tls=True,
        tls_cert_file="/tmp/does-not-exist-cert.pem",
        tls_key_file="/tmp/does-not-exist-key.pem",
    )

    with pytest.raises(FileNotFoundError):
        server.start()
