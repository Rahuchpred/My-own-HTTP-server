"""Main HTTP server entry point and connection lifecycle orchestration."""

from __future__ import annotations

import argparse
import json
import logging
import os
import selectors
import signal
import socket
import ssl
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from config import (
    DRAIN_TIMEOUT_SECS,
    ENABLE_EXPECT_CONTINUE,
    ENABLE_PLAYGROUND,
    ENABLE_TLS,
    HISTORY_LIMIT,
    HOST,
    HTTPS_PORT,
    IDLE_SWEEP_INTERVAL_SECS,
    KEEPALIVE_TIMEOUT_SECS,
    LOG_FORMAT,
    MAX_ACTIVE_CONNECTIONS,
    MAX_KEEPALIVE_REQUESTS,
    MAX_MOCK_BODY_BYTES,
    MAX_MOCK_ROUTES,
    PORT,
    REDIRECT_HTTP_TO_HTTPS,
    REQUEST_QUEUE_SIZE,
    SELECT_TIMEOUT_SECS,
    SERVER_ENGINE,
    SHUTDOWN_POLL_INTERVAL_SECS,
    SOCKET_TIMEOUT_SECS,
    STATE_FILE,
    TLS_CERT_FILE,
    TLS_KEY_FILE,
    WORKER_COUNT,
    WRITE_CHUNK_SIZE,
)
from dynamic_mock_dispatch import response_from_mock
from handlers.example_handlers import home, serve_static, stream_demo, submit
from metrics import MetricsRegistry
from playground_api import PlaygroundAPI
from playground_store import PlaygroundStore
from request import KNOWN_METHODS, HTTPRequest, HTTPRequestParseError
from response import REASON_PHRASES, HTTPResponse, iter_chunked_encoded, prepare_response
from router import Router
from socket_handler import (
    HeaderTooLargeError,
    MalformedRequestError,
    PayloadTooLargeError,
    SocketTimeoutError,
    extract_http_request_message,
    inspect_http_request_head,
    read_http_request_message,
    write_http_response,
    write_http_response_message,
)
from thread_pool import ThreadPool

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OutboundResponse:
    """Tracks incremental write state for a queued HTTP response."""

    response: HTTPResponse
    method: str
    path: str
    started_at: float
    connection_reused: bool
    connection_id: int
    request_id: int
    bytes_in: int
    route_key: str
    trace_id: str
    close_after: bool
    metrics_started: bool
    replay_input: dict[str, object] | None = None
    pending_chunks: deque[memoryview] = field(default_factory=deque)
    stream_iter: Iterator[bytes] | None = None
    file_obj: BinaryIO | None = None
    file_remaining: int = 0
    file_offset: int = 0
    bytes_sent: int = 0

    @classmethod
    def from_http_response(
        cls,
        *,
        response: HTTPResponse,
        method: str,
        path: str,
        started_at: float,
        connection_reused: bool,
        connection_id: int,
        request_id: int,
        bytes_in: int,
        route_key: str,
        trace_id: str,
        close_after: bool,
        metrics_started: bool,
        replay_input: dict[str, object] | None = None,
    ) -> "OutboundResponse":
        prepared = prepare_response(response)
        outbound = cls(
            response=response,
            method=method,
            path=path,
            started_at=started_at,
            connection_reused=connection_reused,
            connection_id=connection_id,
            request_id=request_id,
            bytes_in=bytes_in,
            route_key=route_key,
            trace_id=trace_id,
            close_after=close_after,
            metrics_started=metrics_started,
            replay_input=replay_input,
        )
        outbound.pending_chunks.append(memoryview(prepared.head))
        if prepared.body is not None and prepared.body:
            outbound.pending_chunks.append(memoryview(prepared.body))
        elif prepared.stream is not None:
            outbound.stream_iter = iter_chunked_encoded(prepared.stream)
        elif prepared.file_path is not None:
            outbound.file_obj = prepared.file_path.open("rb")
            outbound.file_remaining = prepared.file_path.stat().st_size
        return outbound

    def close_resources(self) -> None:
        if self.file_obj is not None:
            self.file_obj.close()
            self.file_obj = None


@dataclass(slots=True)
class ConnectionState:
    sock: socket.socket
    address: tuple[str, int]
    connection_id: int
    recv_buffer: bytearray = field(default_factory=bytearray)
    raw_writes: deque[memoryview] = field(default_factory=deque)
    queued_responses: deque[OutboundResponse] = field(default_factory=deque)
    current_response: OutboundResponse | None = None
    requests_served: int = 0
    request_seq: int = 0
    last_activity: float = field(default_factory=time.monotonic)
    closing: bool = False
    continue_sent_headers: set[int] = field(default_factory=set)


@dataclass(slots=True)
class TLSConfig:
    enabled: bool
    cert_file: str
    key_file: str
    https_port: int
    redirect_http_to_https: bool


class HTTPServer:
    def __init__(
        self,
        host: str = HOST,
        port: int = PORT,
        router: Router | None = None,
        worker_count: int = WORKER_COUNT,
        request_queue_size: int = REQUEST_QUEUE_SIZE,
        *,
        engine: str = SERVER_ENGINE,
        max_active_connections: int = MAX_ACTIVE_CONNECTIONS,
        keepalive_timeout_secs: int = KEEPALIVE_TIMEOUT_SECS,
        enable_tls: bool = ENABLE_TLS,
        tls_cert_file: str = TLS_CERT_FILE,
        tls_key_file: str = TLS_KEY_FILE,
        https_port: int = HTTPS_PORT,
        redirect_http_to_https: bool = REDIRECT_HTTP_TO_HTTPS,
        drain_timeout_secs: float = DRAIN_TIMEOUT_SECS,
        log_format: str = LOG_FORMAT,
        enable_playground: bool = ENABLE_PLAYGROUND,
        state_file: str = STATE_FILE,
        history_limit: int = HISTORY_LIMIT,
        max_mock_body_bytes: int = MAX_MOCK_BODY_BYTES,
        max_mock_routes: int = MAX_MOCK_ROUTES,
    ) -> None:
        self.host = host
        self.port = port
        self.http_port = port
        self.https_port = https_port
        self.router = router or self._build_default_router()
        self.worker_count = worker_count
        self.request_queue_size = request_queue_size
        self.engine = engine
        self.max_active_connections = max_active_connections
        self.keepalive_timeout_secs = keepalive_timeout_secs
        self.drain_timeout_secs = drain_timeout_secs
        self.log_format = log_format
        self.enable_playground = enable_playground
        self.state_file = state_file
        self.history_limit = history_limit
        self.max_mock_body_bytes = max_mock_body_bytes
        self.max_mock_routes = max_mock_routes

        self.tls_config = TLSConfig(
            enabled=enable_tls,
            cert_file=tls_cert_file,
            key_file=tls_key_file,
            https_port=https_port,
            redirect_http_to_https=redirect_http_to_https,
        )
        self._ssl_context: ssl.SSLContext | None = None
        self._server_socket: socket.socket | None = None
        self._redirect_socket: socket.socket | None = None
        self._redirect_thread: threading.Thread | None = None
        self._pool: ThreadPool | None = None
        self._selector: selectors.BaseSelector | None = None
        self._selector_connections: dict[int, ConnectionState] = {}
        self._next_connection_id = 0
        self._running = False
        self._accepting = False
        self._lifecycle_state = "stopped"
        self._drain_started_at = 0.0
        self._drain_elapsed_ms = 0.0
        self._state_lock = threading.Lock()
        self.metrics = MetricsRegistry()
        self.playground_store: PlaygroundStore | None = None
        self.playground_api: PlaygroundAPI | None = None
        if self.enable_playground:
            self.playground_store = PlaygroundStore(
                state_file=self.state_file,
                history_limit=self.history_limit,
                max_mock_routes=self.max_mock_routes,
            )
            self.playground_api = PlaygroundAPI(
                store=self.playground_store,
                max_mock_body_bytes=self.max_mock_body_bytes,
                dispatch_request=self._dispatch_internal_request,
            )
            snapshot = self.playground_store.snapshot()
            self.metrics.set_playground_counts(
                mock_count=len(snapshot.get("mocks", [])),
                history_count=len(snapshot.get("history", [])),
            )

    def _build_default_router(self) -> Router:
        router = Router()
        router.add_route("GET", "/", home)
        router.add_route("GET", "/stream", stream_demo)
        router.add_route("POST", "/submit", submit)
        return router

    def _build_ssl_context(self) -> ssl.SSLContext:
        if not self.tls_config.enabled:
            raise RuntimeError("TLS is disabled")
        cert_file = self.tls_config.cert_file
        key_file = self.tls_config.key_file
        if not os.path.exists(cert_file):
            raise FileNotFoundError(f"TLS certificate file not found: {cert_file}")
        if not os.path.exists(key_file):
            raise FileNotFoundError(f"TLS private key file not found: {key_file}")

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20")
        context.load_cert_chain(certfile=cert_file, keyfile=key_file)
        return context

    def _set_lifecycle_state(self, state: str) -> None:
        with self._state_lock:
            self._lifecycle_state = state
            self.metrics.set_drain_state(
                state=state,
                inflight_remaining=self._inflight_remaining(),
                elapsed_ms=self._drain_elapsed_ms,
            )

    def _inflight_remaining(self) -> int:
        snapshot = self.metrics.snapshot()
        inflight = int(snapshot.get("inflight_requests", 0))
        if self.engine == "selectors":
            inflight += len(self._selector_connections)
        return inflight

    def _is_draining(self) -> bool:
        return self._lifecycle_state == "draining"

    def _start_redirect_listener(self) -> None:
        if not self.tls_config.enabled or not self.tls_config.redirect_http_to_https:
            return

        redirect_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        redirect_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        redirect_socket.bind((self.host, self.http_port))
        redirect_socket.listen(128)
        redirect_socket.settimeout(0.2)
        self._redirect_socket = redirect_socket
        self.http_port = redirect_socket.getsockname()[1]

        thread = threading.Thread(target=self._redirect_loop, daemon=True)
        thread.start()
        self._redirect_thread = thread

    def _redirect_loop(self) -> None:
        assert self._redirect_socket is not None
        while self._running and self._accepting:
            try:
                client_socket, _address = self._redirect_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            with client_socket:
                try:
                    client_socket.settimeout(1.0)
                    data = bytearray()
                    while b"\r\n\r\n" not in data and len(data) < 8192:
                        chunk = client_socket.recv(1024)
                        if not chunk:
                            break
                        data.extend(chunk)
                    location = self._redirect_location(bytes(data))
                    response = HTTPResponse(
                        status_code=308,
                        reason_phrase="Permanent Redirect",
                        headers={
                            "Location": location,
                            "Connection": "close",
                            "Cache-Control": "no-store",
                        },
                        body="Use HTTPS",
                    )
                    write_http_response_message(client_socket, response)
                except OSError:
                    continue

    def _redirect_location(self, raw_request: bytes) -> str:
        host = self.host
        target = "/"
        if raw_request:
            try:
                head = raw_request.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1")
                lines = head.split("\r\n")
                if lines and lines[0]:
                    parts = lines[0].split(" ")
                    if len(parts) >= 2 and parts[1]:
                        target = parts[1]
                for line in lines[1:]:
                    if ":" not in line:
                        continue
                    name, value = line.split(":", 1)
                    if name.strip().lower() == "host":
                        host = value.strip().split(":")[0]
                        break
            except Exception:
                pass
        return f"https://{host}:{self.https_port}{target}"

    def start(self) -> None:
        """Start listening and process clients according to selected engine."""
        self._running = True
        self._accepting = True
        self._drain_elapsed_ms = 0.0
        self._set_lifecycle_state("running")
        if self.tls_config.enabled:
            self._ssl_context = self._build_ssl_context()
            self.https_port = self.tls_config.https_port
            self._start_redirect_listener()

        if self.engine == "threadpool":
            self._start_threadpool()
            return
        if self.engine == "selectors":
            self._start_selectors()
            return
        raise ValueError(f"Unsupported engine: {self.engine}")

    def _start_threadpool(self) -> None:
        bind_port = self.https_port if self.tls_config.enabled else self.port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
            self._server_socket = server_socket
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind((self.host, bind_port))
            server_socket.listen(128)
            server_socket.settimeout(0.2)
            if self.tls_config.enabled:
                self.https_port = server_socket.getsockname()[1]
                self.port = self.https_port
            else:
                self.port = server_socket.getsockname()[1]
            self._pool = ThreadPool(
                worker_count=self.worker_count,
                queue_size=self.request_queue_size,
                handler=self._handle_client,
            )
            self._pool.start()

            try:
                while self._running:
                    try:
                        client_socket, address = server_socket.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break

                    if self._is_draining():
                        self._send_drain_reject_response(client_socket)
                        continue

                    if self.tls_config.enabled:
                        wrapped = self._wrap_server_socket(client_socket)
                        if wrapped is None:
                            continue
                        client_socket = wrapped

                    if self._pool is None or not self._pool.submit(client_socket, address):
                        self._send_queue_full_response(client_socket)
            finally:
                if self._pool is not None:
                    self._pool.shutdown(graceful=True, timeout=self.drain_timeout_secs)
                    self._pool = None
                self._stop_redirect_listener()
                self._set_lifecycle_state("stopped")

    def _start_selectors(self) -> None:
        bind_port = self.https_port if self.tls_config.enabled else self.port
        with (
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket,
            selectors.DefaultSelector() as selector,
        ):
            self._server_socket = server_socket
            self._selector = selector
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind((self.host, bind_port))
            server_socket.listen(128)
            server_socket.setblocking(False)
            selector.register(server_socket, selectors.EVENT_READ, data=None)
            if self.tls_config.enabled:
                self.https_port = server_socket.getsockname()[1]
                self.port = self.https_port
            else:
                self.port = server_socket.getsockname()[1]

            last_idle_sweep = time.monotonic()
            try:
                while self._running:
                    try:
                        events = selector.select(timeout=SELECT_TIMEOUT_SECS)
                    except OSError:
                        if not self._running:
                            break
                        raise

                    for key, mask in events:
                        if key.data is None:
                            if self._accepting:
                                self._accept_selector_clients(server_socket, selector)
                            continue

                        state: ConnectionState = key.data
                        if mask & selectors.EVENT_READ:
                            self._handle_selector_read(state, selector)
                        if mask & selectors.EVENT_WRITE:
                            self._handle_selector_write(state, selector)

                    now = time.monotonic()
                    if now - last_idle_sweep >= IDLE_SWEEP_INTERVAL_SECS:
                        self._sweep_idle_connections(selector, now)
                        last_idle_sweep = now
                    if self._is_draining():
                        self._update_drain_metrics()
                        if now - self._drain_started_at >= self.drain_timeout_secs:
                            self._running = False
                            break
                        if not self._selector_connections and self.metrics.snapshot().get(
                            "inflight_requests", 0
                        ) == 0:
                            self._running = False
                            break
            finally:
                for state in list(self._selector_connections.values()):
                    self._close_selector_connection(state, selector)
                self._selector_connections.clear()
                self._selector = None
                self._stop_redirect_listener()
                self._set_lifecycle_state("stopped")

    def stop(self) -> None:
        if not self._running:
            return

        self._accepting = False
        self._set_lifecycle_state("draining")
        self._drain_started_at = time.monotonic()
        self._update_drain_metrics()

        if self._server_socket is not None:
            self._server_socket.close()
            self._server_socket = None
        self._stop_redirect_listener()

        deadline = time.monotonic() + self.drain_timeout_secs
        while time.monotonic() < deadline:
            inflight_remaining = self._inflight_remaining()
            self._update_drain_metrics()
            if inflight_remaining == 0:
                break
            time.sleep(SHUTDOWN_POLL_INTERVAL_SECS)

        self._running = False
        self._drain_elapsed_ms = max(0.0, (time.monotonic() - self._drain_started_at) * 1000)
        self._update_drain_metrics()

        if self._pool is not None:
            self._pool.shutdown(graceful=True, timeout=self.drain_timeout_secs)
            self._pool = None
        if self._selector is not None:
            for state in list(self._selector_connections.values()):
                self._close_selector_connection(state, self._selector)
            self._selector_connections.clear()

    def _update_drain_metrics(self) -> None:
        if self._lifecycle_state != "draining":
            return
        self._drain_elapsed_ms = max(0.0, (time.monotonic() - self._drain_started_at) * 1000)
        self.metrics.set_drain_state(
            state="draining",
            inflight_remaining=self._inflight_remaining(),
            elapsed_ms=self._drain_elapsed_ms,
        )

    def _stop_redirect_listener(self) -> None:
        if self._redirect_socket is not None:
            try:
                self._redirect_socket.close()
            except OSError:
                pass
            self._redirect_socket = None
        if self._redirect_thread is not None:
            self._redirect_thread.join(timeout=1.0)
            self._redirect_thread = None

    def _wrap_server_socket(self, client_socket: socket.socket) -> ssl.SSLSocket | None:
        if self._ssl_context is None:
            return None
        try:
            client_socket.settimeout(SOCKET_TIMEOUT_SECS)
            wrapped = self._ssl_context.wrap_socket(client_socket, server_side=True)
            wrapped.settimeout(SOCKET_TIMEOUT_SECS)
            return wrapped
        except ssl.SSLError as exc:
            self.metrics.record_error_class("tls_handshake")
            logger.warning("tls_handshake_failed error=%s", exc)
            try:
                client_socket.close()
            except OSError:
                pass
            return None

    def _send_drain_reject_response(self, client_socket: socket.socket) -> None:
        with client_socket:
            started_at = time.perf_counter()
            response = HTTPResponse(
                status_code=503,
                reason_phrase="Service Unavailable",
                headers={
                    "Connection": "close",
                    "Retry-After": "2",
                },
                body="Server Draining",
            )
            bytes_sent = write_http_response_message(client_socket, response)
            trace_id = self._new_trace_id()
            self._record_and_log(
                address=("-", 0),
                method="-",
                path="-",
                response=response,
                payload_size=bytes_sent,
                bytes_in=0,
                started_at=started_at,
                connection_reused=False,
                connection_id=0,
                request_id=0,
                route_key="drain/reject",
                trace_id=trace_id,
                shutdown_phase="draining",
            )

    def _send_queue_full_response(self, client_socket: socket.socket) -> None:
        with client_socket:
            started_at = time.perf_counter()
            response = HTTPResponse(
                status_code=503,
                reason_phrase="Service Unavailable",
                headers={"Connection": "close"},
                body="Service Unavailable",
            )
            bytes_sent = write_http_response_message(client_socket, response)
            trace_id = self._new_trace_id()
            self._record_and_log(
                address=("-", 0),
                method="-",
                path="-",
                response=response,
                payload_size=bytes_sent,
                bytes_in=0,
                started_at=started_at,
                connection_reused=False,
                connection_id=0,
                request_id=0,
                route_key="queue/full",
                trace_id=trace_id,
                shutdown_phase=self._lifecycle_state,
            )

    def _new_trace_id(self) -> str:
        return uuid.uuid4().hex[:16]

    def _build_replay_input(self, request: HTTPRequest) -> dict[str, object]:
        return {
            "method": request.method,
            "path": request.path,
            "raw_target": request.raw_target,
            "http_version": request.http_version,
            "headers": dict(request.headers),
            "body": request.body.decode("utf-8", errors="replace"),
        }

    def _handle_client(self, client_socket: socket.socket, address: tuple[str, int]) -> None:
        with client_socket:
            self.metrics.connection_opened()
            client_socket.settimeout(min(SOCKET_TIMEOUT_SECS, self.keepalive_timeout_secs))
            request_count = 0
            carry = b""
            try:
                while request_count < MAX_KEEPALIVE_REQUESTS:
                    started_at = time.perf_counter()
                    trace_id = self._new_trace_id()
                    sent_continue = False

                    def send_continue(_head_info: object) -> None:
                        nonlocal sent_continue
                        if ENABLE_EXPECT_CONTINUE and not sent_continue:
                            write_http_response(client_socket, b"HTTP/1.1 100 Continue\r\n\r\n")
                            sent_continue = True

                    try:
                        raw_request, carry = read_http_request_message(
                            client_socket,
                            carry,
                            expect_continue_callback=send_continue,
                        )
                    except PayloadTooLargeError as exc:
                        self.metrics.record_read_error(exc.__class__.__name__)
                        response = HTTPResponse(status_code=413, body="Payload Too Large")
                        response.headers.setdefault("Connection", "close")
                        bytes_sent = write_http_response_message(client_socket, response)
                        self._record_and_log(
                            address=address,
                            method="-",
                            path="-",
                            response=response,
                            payload_size=bytes_sent,
                            bytes_in=0,
                            started_at=started_at,
                            connection_reused=False,
                            connection_id=0,
                            request_id=0,
                            route_key="request/too_large",
                            trace_id=trace_id,
                        )
                        return
                    except HeaderTooLargeError as exc:
                        self.metrics.record_read_error(exc.__class__.__name__)
                        response = HTTPResponse(
                            status_code=431,
                            body="Request Header Fields Too Large",
                        )
                        response.headers.setdefault("Connection", "close")
                        bytes_sent = write_http_response_message(client_socket, response)
                        self._record_and_log(
                            address=address,
                            method="-",
                            path="-",
                            response=response,
                            payload_size=bytes_sent,
                            bytes_in=0,
                            started_at=started_at,
                            connection_reused=False,
                            connection_id=0,
                            request_id=0,
                            route_key="request/header_too_large",
                            trace_id=trace_id,
                        )
                        return
                    except SocketTimeoutError as exc:
                        self.metrics.record_read_error(exc.__class__.__name__)
                        response = HTTPResponse(status_code=408, body="Request Timeout")
                        response.headers.setdefault("Connection", "close")
                        bytes_sent = write_http_response_message(client_socket, response)
                        self._record_and_log(
                            address=address,
                            method="-",
                            path="-",
                            response=response,
                            payload_size=bytes_sent,
                            bytes_in=0,
                            started_at=started_at,
                            connection_reused=False,
                            connection_id=0,
                            request_id=0,
                            route_key="request/timeout",
                            trace_id=trace_id,
                        )
                        return
                    except MalformedRequestError as exc:
                        self.metrics.record_read_error(exc.__class__.__name__)
                        response = HTTPResponse(status_code=400, body="Bad Request")
                        response.headers.setdefault("Connection", "close")
                        bytes_sent = write_http_response_message(client_socket, response)
                        self._record_and_log(
                            address=address,
                            method="-",
                            path="-",
                            response=response,
                            payload_size=bytes_sent,
                            bytes_in=0,
                            started_at=started_at,
                            connection_reused=False,
                            connection_id=0,
                            request_id=0,
                            route_key="request/malformed",
                            trace_id=trace_id,
                        )
                        return
                    except OSError:
                        return

                    if not raw_request:
                        return

                    try:
                        request = HTTPRequest.from_bytes(raw_request)
                    except HTTPRequestParseError as exc:
                        self.metrics.record_error_class("parse")
                        response = HTTPResponse(
                            status_code=exc.status_code,
                            body=REASON_PHRASES.get(exc.status_code, "Bad Request"),
                        )
                        response.headers.setdefault("Connection", "close")
                        bytes_sent = write_http_response_message(client_socket, response)
                        self._record_and_log(
                            address=address,
                            method="-",
                            path="-",
                            response=response,
                            payload_size=bytes_sent,
                            bytes_in=len(raw_request),
                            started_at=started_at,
                            connection_reused=False,
                            connection_id=0,
                            request_id=0,
                            route_key="request/parse_error",
                            trace_id=trace_id,
                        )
                        return
                    except ValueError:
                        self.metrics.record_error_class("parse")
                        response = HTTPResponse(status_code=400, body="Bad Request")
                        response.headers.setdefault("Connection", "close")
                        bytes_sent = write_http_response_message(client_socket, response)
                        self._record_and_log(
                            address=address,
                            method="-",
                            path="-",
                            response=response,
                            payload_size=bytes_sent,
                            bytes_in=len(raw_request),
                            started_at=started_at,
                            connection_reused=False,
                            connection_id=0,
                            request_id=0,
                            route_key="request/bad_request",
                            trace_id=trace_id,
                        )
                        return

                    request_count += 1
                    connection_reused = request_count > 1
                    if (self._is_draining() or not self._accepting) and request_count > 1:
                        response = HTTPResponse(
                            status_code=503,
                            headers={"Retry-After": "2"},
                            body="Server Draining",
                        )
                        response.headers.setdefault("Connection", "close")
                        response.headers["X-Request-ID"] = trace_id
                        bytes_sent = write_http_response_message(client_socket, response)
                        self._record_and_log(
                            address=address,
                            method=request.method,
                            path=request.path,
                            response=response,
                            payload_size=bytes_sent,
                            bytes_in=len(raw_request),
                            started_at=started_at,
                            connection_reused=connection_reused,
                            connection_id=0,
                            request_id=request_count,
                            route_key=f"{request.method} {request.path}",
                            trace_id=trace_id,
                            shutdown_phase="draining",
                            replay_input=self._build_replay_input(request),
                        )
                        return
                    self.metrics.request_started(connection_reused=connection_reused)
                    response = self._dispatch(request)
                    response.headers.setdefault("X-Request-ID", trace_id)
                    should_close = (
                        (not request.keep_alive)
                        or request_count >= MAX_KEEPALIVE_REQUESTS
                    )
                    if should_close:
                        response.headers.setdefault("Connection", "close")
                    else:
                        response.headers.setdefault("Connection", "keep-alive")
                        response.headers.setdefault(
                            "Keep-Alive",
                            (
                                f"timeout={self.keepalive_timeout_secs}, "
                                f"max={MAX_KEEPALIVE_REQUESTS - request_count}"
                            ),
                        )

                    try:
                        bytes_sent = write_http_response_message(client_socket, response)
                    except OSError as exc:
                        self.metrics.record_write_error(exc.__class__.__name__)
                        self.metrics.request_finished()
                        return

                    self.metrics.request_finished()
                    self._record_and_log(
                        address=address,
                        method=request.method,
                        path=request.path,
                        response=response,
                        payload_size=bytes_sent,
                        bytes_in=len(raw_request),
                        started_at=started_at,
                        connection_reused=connection_reused,
                        connection_id=0,
                        request_id=request_count,
                        route_key=f"{request.method} {request.path}",
                        trace_id=trace_id,
                        replay_input=self._build_replay_input(request),
                    )
                    if should_close:
                        return
            finally:
                self.metrics.connection_closed()

    def _accept_selector_clients(
        self,
        server_socket: socket.socket,
        selector: selectors.BaseSelector,
    ) -> None:
        while True:
            try:
                client_socket, address = server_socket.accept()
            except BlockingIOError:
                return
            except OSError:
                return

            if len(self._selector_connections) >= self.max_active_connections:
                if self.tls_config.enabled:
                    wrapped_full = self._wrap_server_socket(client_socket)
                    if wrapped_full is not None:
                        self._send_queue_full_response(wrapped_full)
                    continue
                self._send_queue_full_response(client_socket)
                continue

            if self._is_draining():
                if self.tls_config.enabled:
                    wrapped_drain = self._wrap_server_socket(client_socket)
                    if wrapped_drain is not None:
                        self._send_drain_reject_response(wrapped_drain)
                    continue
                self._send_drain_reject_response(client_socket)
                continue

            if self.tls_config.enabled:
                wrapped = self._wrap_server_socket(client_socket)
                if wrapped is None:
                    continue
                client_socket = wrapped

            client_socket.setblocking(False)
            self._next_connection_id += 1
            state = ConnectionState(
                sock=client_socket,
                address=address,
                connection_id=self._next_connection_id,
            )
            self._selector_connections[client_socket.fileno()] = state
            selector.register(client_socket, selectors.EVENT_READ, data=state)
            self.metrics.connection_opened()

    def _handle_selector_read(
        self,
        state: ConnectionState,
        selector: selectors.BaseSelector,
    ) -> None:
        try:
            chunk = state.sock.recv(8192)
        except BlockingIOError:
            return
        except OSError as exc:
            self.metrics.record_read_error(exc.__class__.__name__)
            self._close_selector_connection(state, selector)
            return

        if not chunk:
            state.closing = True
            self._update_selector_interest(state, selector)
            return

        state.last_activity = time.monotonic()
        state.recv_buffer.extend(chunk)

        while True:
            try:
                extracted = extract_http_request_message(bytes(state.recv_buffer))
            except HeaderTooLargeError as exc:
                self.metrics.record_read_error(exc.__class__.__name__)
                self._queue_selector_response(
                    state,
                    HTTPResponse(status_code=431, body="Request Header Fields Too Large"),
                    method="-",
                    path="-",
                    started_at=time.perf_counter(),
                    connection_reused=state.requests_served > 0,
                    request_id=state.request_seq + 1,
                    bytes_in=0,
                    close_after=True,
                    metrics_started=False,
                )
                state.closing = True
                break
            except PayloadTooLargeError as exc:
                self.metrics.record_read_error(exc.__class__.__name__)
                self._queue_selector_response(
                    state,
                    HTTPResponse(status_code=413, body="Payload Too Large"),
                    method="-",
                    path="-",
                    started_at=time.perf_counter(),
                    connection_reused=state.requests_served > 0,
                    request_id=state.request_seq + 1,
                    bytes_in=0,
                    close_after=True,
                    metrics_started=False,
                )
                state.closing = True
                break
            except MalformedRequestError as exc:
                self.metrics.record_read_error(exc.__class__.__name__)
                self._queue_selector_response(
                    state,
                    HTTPResponse(status_code=400, body="Bad Request"),
                    method="-",
                    path="-",
                    started_at=time.perf_counter(),
                    connection_reused=state.requests_served > 0,
                    request_id=state.request_seq + 1,
                    bytes_in=0,
                    close_after=True,
                    metrics_started=False,
                )
                state.closing = True
                break

            if extracted is None:
                self._maybe_queue_continue(state)
                break

            raw_request, leftover = extracted
            state.recv_buffer = bytearray(leftover)
            state.continue_sent_headers.clear()
            started_at = time.perf_counter()
            trace_id = self._new_trace_id()

            try:
                request = HTTPRequest.from_bytes(raw_request)
            except HTTPRequestParseError as exc:
                self.metrics.record_error_class("parse")
                self._queue_selector_response(
                    state,
                    HTTPResponse(
                        status_code=exc.status_code,
                        body=REASON_PHRASES.get(exc.status_code, "Bad Request"),
                    ),
                    method="-",
                    path="-",
                    started_at=started_at,
                    connection_reused=state.requests_served > 0,
                    request_id=state.request_seq + 1,
                    bytes_in=len(raw_request),
                    route_key="request/parse_error",
                    trace_id=trace_id,
                    close_after=True,
                    metrics_started=False,
                )
                state.closing = True
                break
            except ValueError:
                self.metrics.record_error_class("parse")
                self._queue_selector_response(
                    state,
                    HTTPResponse(status_code=400, body="Bad Request"),
                    method="-",
                    path="-",
                    started_at=started_at,
                    connection_reused=state.requests_served > 0,
                    request_id=state.request_seq + 1,
                    bytes_in=len(raw_request),
                    route_key="request/bad_request",
                    trace_id=trace_id,
                    close_after=True,
                    metrics_started=False,
                )
                state.closing = True
                break

            state.requests_served += 1
            state.request_seq += 1
            connection_reused = state.requests_served > 1
            if (self._is_draining() or not self._accepting) and state.requests_served > 1:
                draining_response = HTTPResponse(
                    status_code=503,
                    headers={"Retry-After": "2"},
                    body="Server Draining",
                )
                draining_response.headers["X-Request-ID"] = trace_id
                self._queue_selector_response(
                    state,
                    draining_response,
                    method=request.method,
                    path=request.path,
                    started_at=started_at,
                    connection_reused=connection_reused,
                    request_id=state.request_seq,
                    bytes_in=len(raw_request),
                    route_key=f"{request.method} {request.path}",
                    trace_id=trace_id,
                    close_after=True,
                    metrics_started=False,
                    replay_input=self._build_replay_input(request),
                )
                state.closing = True
                break
            self.metrics.request_started(connection_reused=connection_reused)

            response = self._dispatch(request)
            response.headers.setdefault("X-Request-ID", trace_id)
            should_close = (
                (not request.keep_alive)
                or state.requests_served >= MAX_KEEPALIVE_REQUESTS
            )
            if should_close:
                response.headers.setdefault("Connection", "close")
            else:
                response.headers.setdefault("Connection", "keep-alive")
                response.headers.setdefault(
                    "Keep-Alive",
                    (
                        f"timeout={self.keepalive_timeout_secs}, "
                        f"max={MAX_KEEPALIVE_REQUESTS - state.requests_served}"
                    ),
                )

            self._queue_selector_response(
                state,
                response,
                method=request.method,
                path=request.path,
                started_at=started_at,
                connection_reused=connection_reused,
                request_id=state.request_seq,
                bytes_in=len(raw_request),
                route_key=f"{request.method} {request.path}",
                trace_id=trace_id,
                close_after=should_close,
                metrics_started=True,
                replay_input=self._build_replay_input(request),
            )
            if should_close:
                state.closing = True

            if state.closing:
                break

        self._update_selector_interest(state, selector)

    def _maybe_queue_continue(self, state: ConnectionState) -> None:
        if not ENABLE_EXPECT_CONTINUE:
            return
        try:
            head_info = inspect_http_request_head(bytes(state.recv_buffer))
        except (HeaderTooLargeError, MalformedRequestError, PayloadTooLargeError):
            return

        if head_info is None:
            return
        if not head_info.expect_continue:
            return
        if not head_info.uses_chunked_transfer and head_info.expected_body_length == 0:
            return
        if head_info.header_end_index in state.continue_sent_headers:
            return

        state.raw_writes.append(memoryview(b"HTTP/1.1 100 Continue\r\n\r\n"))
        state.continue_sent_headers.add(head_info.header_end_index)

    def _queue_selector_response(
        self,
        state: ConnectionState,
        response: HTTPResponse,
        *,
        method: str,
        path: str,
        started_at: float,
        connection_reused: bool,
        request_id: int,
        bytes_in: int,
        route_key: str | None = None,
        trace_id: str | None = None,
        close_after: bool,
        metrics_started: bool,
        replay_input: dict[str, object] | None = None,
    ) -> None:
        if close_after:
            response.headers.setdefault("Connection", "close")
        outbound = OutboundResponse.from_http_response(
            response=response,
            method=method,
            path=path,
            started_at=started_at,
            connection_reused=connection_reused,
            connection_id=state.connection_id,
            request_id=request_id,
            bytes_in=bytes_in,
            route_key=route_key or f"{method} {path}",
            trace_id=trace_id or self._new_trace_id(),
            close_after=close_after,
            metrics_started=metrics_started,
            replay_input=replay_input,
        )
        state.queued_responses.append(outbound)

    def _handle_selector_write(
        self,
        state: ConnectionState,
        selector: selectors.BaseSelector,
    ) -> None:
        while state.raw_writes:
            view = state.raw_writes[0]
            try:
                sent = state.sock.send(view)
            except BlockingIOError:
                return
            except OSError as exc:
                self.metrics.record_write_error(exc.__class__.__name__)
                self._close_selector_connection(state, selector)
                return

            if sent <= 0:
                return
            state.last_activity = time.monotonic()
            if sent < len(view):
                state.raw_writes[0] = view[sent:]
                return
            state.raw_writes.popleft()

        while True:
            if state.current_response is None:
                if not state.queued_responses:
                    break
                state.current_response = state.queued_responses.popleft()

            outbound = state.current_response
            if outbound is None:
                break

            if outbound.pending_chunks:
                view = outbound.pending_chunks[0]
                try:
                    sent = state.sock.send(view)
                except BlockingIOError:
                    return
                except OSError as exc:
                    self.metrics.record_write_error(exc.__class__.__name__)
                    self._close_selector_connection(state, selector)
                    return

                if sent <= 0:
                    return
                state.last_activity = time.monotonic()
                outbound.bytes_sent += sent
                if sent < len(view):
                    outbound.pending_chunks[0] = view[sent:]
                    return
                outbound.pending_chunks.popleft()
                continue

            if outbound.stream_iter is not None:
                try:
                    next_chunk = next(outbound.stream_iter)
                except StopIteration:
                    outbound.stream_iter = None
                    continue
                outbound.pending_chunks.append(memoryview(next_chunk))
                continue

            if outbound.file_obj is not None and outbound.file_remaining > 0:
                can_use_sendfile = hasattr(os, "sendfile") and not isinstance(
                    state.sock, ssl.SSLSocket
                )
                if can_use_sendfile:
                    try:
                        sent = os.sendfile(
                            state.sock.fileno(),
                            outbound.file_obj.fileno(),
                            outbound.file_offset,
                            min(WRITE_CHUNK_SIZE, outbound.file_remaining),
                        )
                    except BlockingIOError:
                        return
                    except OSError as exc:
                        self.metrics.record_write_error(exc.__class__.__name__)
                        outbound.close_resources()
                        self._close_selector_connection(state, selector)
                        return

                    if sent is None:
                        sent = 0
                    if sent <= 0:
                        return
                    state.last_activity = time.monotonic()
                    outbound.bytes_sent += sent
                    outbound.file_offset += sent
                    outbound.file_remaining -= sent
                    if outbound.file_remaining > 0:
                        return
                    outbound.close_resources()
                    continue

                chunk = outbound.file_obj.read(min(WRITE_CHUNK_SIZE, outbound.file_remaining))
                if not chunk:
                    outbound.close_resources()
                    continue
                outbound.pending_chunks.append(memoryview(chunk))
                outbound.file_remaining -= len(chunk)
                continue

            if outbound.file_obj is not None:
                outbound.close_resources()

            self._finalize_selector_response(state, outbound, selector)
            if state.sock.fileno() not in self._selector_connections:
                return
            state.current_response = None
            if state.closing:
                break

        self._update_selector_interest(state, selector)

    def _finalize_selector_response(
        self,
        state: ConnectionState,
        outbound: OutboundResponse,
        selector: selectors.BaseSelector,
    ) -> None:
        if outbound.metrics_started:
            self.metrics.request_finished()

        self._record_and_log(
            address=state.address,
            method=outbound.method,
            path=outbound.path,
            response=outbound.response,
            payload_size=outbound.bytes_sent,
            bytes_in=outbound.bytes_in,
            started_at=outbound.started_at,
            connection_reused=outbound.connection_reused,
            connection_id=outbound.connection_id,
            request_id=outbound.request_id,
            route_key=outbound.route_key,
            trace_id=outbound.trace_id,
            replay_input=outbound.replay_input,
        )

        if outbound.close_after:
            self._close_selector_connection(state, selector)

    def _sweep_idle_connections(
        self,
        selector: selectors.BaseSelector,
        now: float,
    ) -> None:
        for state in list(self._selector_connections.values()):
            if state.closing:
                continue
            if now - state.last_activity <= self.keepalive_timeout_secs:
                continue
            self._queue_selector_response(
                state,
                HTTPResponse(status_code=408, body="Request Timeout"),
                method="-",
                path="-",
                started_at=time.perf_counter(),
                connection_reused=state.requests_served > 0,
                request_id=state.request_seq + 1,
                bytes_in=0,
                close_after=True,
                metrics_started=False,
            )
            state.closing = True
            self._update_selector_interest(state, selector)

    def _update_selector_interest(
        self,
        state: ConnectionState,
        selector: selectors.BaseSelector,
    ) -> None:
        fileno = state.sock.fileno()
        if fileno not in self._selector_connections:
            return

        has_pending_write = bool(
            state.raw_writes or state.current_response is not None or state.queued_responses
        )
        if state.closing and not has_pending_write:
            self._close_selector_connection(state, selector)
            return

        events = selectors.EVENT_READ
        if has_pending_write:
            events |= selectors.EVENT_WRITE
        if state.closing:
            events = selectors.EVENT_WRITE

        try:
            selector.modify(state.sock, events, data=state)
        except (KeyError, ValueError, OSError):
            self._close_selector_connection(state, selector)

    def _close_selector_connection(
        self,
        state: ConnectionState,
        selector: selectors.BaseSelector,
    ) -> None:
        fileno = state.sock.fileno()
        if fileno not in self._selector_connections:
            return

        try:
            selector.unregister(state.sock)
        except Exception:
            pass

        if state.current_response is not None:
            state.current_response.close_resources()
            state.current_response = None
        while state.queued_responses:
            state.queued_responses.popleft().close_resources()

        try:
            state.sock.close()
        except OSError:
            pass

        self._selector_connections.pop(fileno, None)
        self.metrics.connection_closed()

    def _dispatch_internal_request(self, request: HTTPRequest) -> HTTPResponse:
        response = self._dispatch(request)
        if response.headers.get("X-Request-ID") is None:
            response.headers["X-Request-ID"] = self._new_trace_id()
        return response

    def _serve_playground_page(self, *, as_head: bool) -> HTTPResponse:
        playground_path = Path("static") / "playground.html"
        if not playground_path.exists() or not playground_path.is_file():
            return HTTPResponse(status_code=404, body="Not Found")
        response = HTTPResponse(
            status_code=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            file_path=playground_path,
        )
        if as_head:
            return self._as_head_response(response)
        return response

    def _is_playground_admin_path(self, path: str) -> bool:
        return (
            path == "/api/playground/state"
            or path == "/api/mocks"
            or path == "/api/history"
            or path == "/api/playground/request"
            or path.startswith("/api/mocks/")
            or path.startswith("/api/replay/")
        )

    def _dispatch(self, request: HTTPRequest) -> HTTPResponse:
        if request.method not in KNOWN_METHODS:
            return HTTPResponse(status_code=501, body="Not Implemented")

        if request.path == "/_metrics":
            if request.method not in {"GET", "HEAD"}:
                return HTTPResponse(
                    status_code=405,
                    headers={"Allow": "GET, HEAD"},
                    body="Method Not Allowed",
                )
            metrics_snapshot = self.metrics.snapshot()
            metrics_snapshot["engine"] = self.engine
            snapshot = json.dumps(metrics_snapshot, sort_keys=True)
            metrics_response = HTTPResponse(
                status_code=200,
                headers={"Content-Type": "application/json"},
                body=snapshot,
            )
            if request.method == "HEAD":
                return self._as_head_response(metrics_response)
            return metrics_response

        if request.path == "/playground":
            if not self.enable_playground:
                return HTTPResponse(status_code=404, body="Not Found")
            if request.method not in {"GET", "HEAD"}:
                return HTTPResponse(
                    status_code=405,
                    headers={"Allow": "GET, HEAD"},
                    body="Method Not Allowed",
                )
            return self._serve_playground_page(as_head=request.method == "HEAD")

        if self._is_playground_admin_path(request.path):
            if not self.enable_playground or self.playground_api is None:
                return HTTPResponse(status_code=404, body="Not Found")
            response = self.playground_api.handle(request)
            if response.status_code >= 400:
                self.metrics.record_playground_admin_error()
            if request.path.startswith("/api/replay/") and request.method == "POST":
                self.metrics.record_playground_replay()
            snapshot = (
                self.playground_store.snapshot()
                if self.playground_store is not None
                else {"mocks": [], "history": []}
            )
            self.metrics.set_playground_counts(
                mock_count=len(snapshot.get("mocks", [])),
                history_count=len(snapshot.get("history", [])),
            )
            if request.method == "HEAD":
                return self._as_head_response(response)
            return response

        if request.path.startswith("/static/"):
            if request.method not in {"GET", "HEAD"}:
                return HTTPResponse(
                    status_code=405,
                    headers={"Allow": "GET, HEAD"},
                    body="Method Not Allowed",
                )
            if request.method == "HEAD":
                get_request = self._request_with_method(request, method="GET")
                return self._as_head_response(serve_static(get_request))
            return serve_static(request)

        if self.enable_playground and self.playground_store is not None:
            mock_method = "GET" if request.method == "HEAD" else request.method
            mock = self.playground_store.find_mock(method=mock_method, path=request.path)
            if mock is not None:
                response = response_from_mock(request, mock)
                if request.method == "HEAD":
                    return self._as_head_response(response)
                return response

        allowed_methods = {"GET", "HEAD", "POST"}
        if request.method not in allowed_methods:
            return HTTPResponse(
                status_code=405,
                headers={"Allow": "GET, HEAD, POST"},
                body="Method Not Allowed",
            )

        if request.method == "HEAD":
            handler = self.router.resolve("GET", request.path)
            if handler is None:
                return self._as_head_response(HTTPResponse(status_code=404, body="Not Found"))
            try:
                return self._as_head_response(handler(request))
            except Exception:
                self.metrics.record_error_class("dispatch")
                logger.exception("Unhandled error in HEAD route handler")
                return self._as_head_response(
                    HTTPResponse(status_code=500, body="Internal Server Error")
                )

        handler = self.router.resolve(request.method, request.path)
        if handler is None:
            return HTTPResponse(status_code=404, body="Not Found")

        try:
            return handler(request)
        except Exception:
            self.metrics.record_error_class("dispatch")
            logger.exception("Unhandled error in route handler")
            return HTTPResponse(status_code=500, body="Internal Server Error")

    def _record_and_log(
        self,
        *,
        address: tuple[str, int],
        method: str,
        path: str,
        response: HTTPResponse,
        payload_size: int,
        bytes_in: int,
        started_at: float,
        connection_reused: bool,
        connection_id: int,
        request_id: int,
        route_key: str | None = None,
        trace_id: str | None = None,
        shutdown_phase: str | None = None,
        replay_input: dict[str, object] | None = None,
    ) -> None:
        duration_ms = (time.perf_counter() - started_at) * 1000
        self.metrics.record_request(
            status_code=response.status_code,
            duration_ms=duration_ms,
            bytes_sent=payload_size,
            route_key=route_key,
            trace_id=trace_id,
        )
        drain_elapsed_ms = round(
            max(0.0, (time.monotonic() - self._drain_started_at) * 1000)
            if self._lifecycle_state == "draining"
            else 0.0,
            3,
        )
        event = {
            "client": address[0],
            "method": method,
            "path": path,
            "status": response.status_code,
            "engine": self.engine,
            "connection_id": connection_id,
            "request_id": request_id,
            "bytes_in": bytes_in,
            "bytes_out": payload_size,
            "latency_ms": round(duration_ms, 3),
            "connection_reused": connection_reused,
            "route_key": route_key or f"{method} {path}",
            "trace_id": trace_id or "",
            "shutdown_phase": shutdown_phase or self._lifecycle_state,
            "drain_elapsed_ms": drain_elapsed_ms,
        }
        if path.startswith("/api/mocks") and method == "POST":
            event["playground_action"] = "mock_create"
        elif path.startswith("/api/mocks/") and method == "PUT":
            event["playground_action"] = "mock_update"
            event["mock_id"] = path.removeprefix("/api/mocks/")
        elif path.startswith("/api/mocks/") and method == "DELETE":
            event["playground_action"] = "mock_delete"
            event["mock_id"] = path.removeprefix("/api/mocks/")
        elif path.startswith("/api/replay/") and method == "POST":
            event["playground_action"] = "replay"
            event["replayed_request_id"] = path.removeprefix("/api/replay/")

        should_capture_history = (
            path not in {"/_metrics", "/api/playground/state"}
            and not path.startswith("/static/")
        )
        if (
            self.enable_playground
            and self.playground_store is not None
            and trace_id
            and should_capture_history
        ):
            history_record = {
                "request_id": trace_id,
                "trace_id": trace_id,
                "method": method,
                "path": path,
                "status": response.status_code,
                "latency_ms": round(duration_ms, 3),
                "bytes_in": bytes_in,
                "bytes_out": payload_size,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "replay_input": replay_input or {},
            }
            self.playground_store.add_history(history_record)
            snapshot = self.playground_store.snapshot()
            self.metrics.set_playground_counts(
                mock_count=len(snapshot.get("mocks", [])),
                history_count=len(snapshot.get("history", [])),
            )

        if self.log_format == "json":
            logger.info(json.dumps(event, sort_keys=True))
            return

        logger.info(
            (
                "client=%s method=%s path=%s status=%s engine=%s "
                "connection_id=%s request_id=%s bytes_in=%s bytes_out=%s "
                "duration_ms=%.2f connection_reused=%s route_key=%s "
                "trace_id=%s shutdown_phase=%s drain_elapsed_ms=%.2f"
            ),
            event["client"],
            event["method"],
            event["path"],
            event["status"],
            event["engine"],
            event["connection_id"],
            event["request_id"],
            event["bytes_in"],
            event["bytes_out"],
            duration_ms,
            event["connection_reused"],
            event["route_key"],
            event["trace_id"],
            event["shutdown_phase"],
            event["drain_elapsed_ms"],
        )

    def _request_with_method(self, request: HTTPRequest, method: str) -> HTTPRequest:
        return HTTPRequest(
            method=method,
            path=request.path,
            raw_target=request.raw_target,
            http_version=request.http_version,
            headers=dict(request.headers),
            body=request.body,
            query_params=dict(request.query_params),
            keep_alive=request.keep_alive,
            transfer_encoding=request.transfer_encoding,
        )

    def _as_head_response(self, get_response: HTTPResponse) -> HTTPResponse:
        headers = dict(get_response.headers)
        if get_response.stream is not None:
            headers.setdefault("Transfer-Encoding", "chunked")
            return HTTPResponse(
                status_code=get_response.status_code,
                reason_phrase=get_response.reason_phrase,
                headers=headers,
                body=b"",
            )

        if get_response.file_path is not None:
            body_size = get_response.file_path.stat().st_size
            return HTTPResponse(
                status_code=get_response.status_code,
                reason_phrase=get_response.reason_phrase,
                headers=headers,
                body=b"",
                content_length_override=body_size,
            )

        body_bytes = get_response.body
        if isinstance(body_bytes, str):
            body_bytes = body_bytes.encode("utf-8")
        return HTTPResponse(
            status_code=get_response.status_code,
            reason_phrase=get_response.reason_phrase,
            headers=headers,
            body=b"",
            content_length_override=len(body_bytes),
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run custom HTTP server")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--enable-tls", action="store_true", default=ENABLE_TLS)
    parser.add_argument("--cert-file", default=TLS_CERT_FILE)
    parser.add_argument("--key-file", default=TLS_KEY_FILE)
    parser.add_argument("--https-port", type=int, default=HTTPS_PORT)
    parser.add_argument(
        "--redirect-http",
        action=argparse.BooleanOptionalAction,
        default=REDIRECT_HTTP_TO_HTTPS,
    )
    parser.add_argument("--drain-timeout", type=float, default=DRAIN_TIMEOUT_SECS)
    parser.add_argument("--engine", choices=["threadpool", "selectors"], default=SERVER_ENGINE)
    parser.add_argument("--max-connections", type=int, default=MAX_ACTIVE_CONNECTIONS)
    parser.add_argument("--keepalive-timeout", type=int, default=KEEPALIVE_TIMEOUT_SECS)
    parser.add_argument("--log-format", choices=["plain", "json"], default=LOG_FORMAT)
    parser.add_argument(
        "--enable-playground",
        action=argparse.BooleanOptionalAction,
        default=ENABLE_PLAYGROUND,
    )
    parser.add_argument("--state-file", default=STATE_FILE)
    parser.add_argument("--history-limit", type=int, default=HISTORY_LIMIT)
    parser.add_argument("--max-mock-body-bytes", type=int, default=MAX_MOCK_BODY_BYTES)
    return parser.parse_args()


def _install_signal_handlers(server: HTTPServer) -> None:
    def _handler(signum: int, _frame: object) -> None:
        signal_name = signal.Signals(signum).name
        logger.info("signal_received signal=%s action=drain_and_stop", signal_name)
        server.stop()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


if __name__ == "__main__":
    args = _parse_args()
    logging.basicConfig(level=logging.INFO)
    server = HTTPServer(
        host=args.host,
        port=args.port,
        engine=args.engine,
        max_active_connections=args.max_connections,
        keepalive_timeout_secs=args.keepalive_timeout,
        enable_tls=args.enable_tls,
        tls_cert_file=args.cert_file,
        tls_key_file=args.key_file,
        https_port=args.https_port,
        redirect_http_to_https=args.redirect_http,
        drain_timeout_secs=args.drain_timeout,
        log_format=args.log_format,
        enable_playground=args.enable_playground,
        state_file=args.state_file,
        history_limit=args.history_limit,
        max_mock_body_bytes=args.max_mock_body_bytes,
    )
    _install_signal_handlers(server)
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()
