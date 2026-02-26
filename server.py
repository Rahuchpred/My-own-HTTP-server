"""Main HTTP server entry point and connection lifecycle orchestration."""

from __future__ import annotations

import argparse
import json
import logging
import os
import selectors
import socket
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import BinaryIO

from config import (
    ENABLE_EXPECT_CONTINUE,
    HOST,
    IDLE_SWEEP_INTERVAL_SECS,
    KEEPALIVE_TIMEOUT_SECS,
    LOG_FORMAT,
    MAX_ACTIVE_CONNECTIONS,
    MAX_KEEPALIVE_REQUESTS,
    PORT,
    REQUEST_QUEUE_SIZE,
    SELECT_TIMEOUT_SECS,
    SERVER_ENGINE,
    SOCKET_TIMEOUT_SECS,
    WORKER_COUNT,
    WRITE_CHUNK_SIZE,
)
from handlers.example_handlers import home, serve_static, stream_demo, submit
from metrics import MetricsRegistry
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
    close_after: bool
    metrics_started: bool
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
        close_after: bool,
        metrics_started: bool,
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
            close_after=close_after,
            metrics_started=metrics_started,
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
        log_format: str = LOG_FORMAT,
    ) -> None:
        self.host = host
        self.port = port
        self.router = router or self._build_default_router()
        self.worker_count = worker_count
        self.request_queue_size = request_queue_size
        self.engine = engine
        self.max_active_connections = max_active_connections
        self.keepalive_timeout_secs = keepalive_timeout_secs
        self.log_format = log_format

        self._server_socket: socket.socket | None = None
        self._pool: ThreadPool | None = None
        self._selector: selectors.BaseSelector | None = None
        self._selector_connections: dict[int, ConnectionState] = {}
        self._next_connection_id = 0
        self._running = False
        self.metrics = MetricsRegistry()

    def _build_default_router(self) -> Router:
        router = Router()
        router.add_route("GET", "/", home)
        router.add_route("GET", "/stream", stream_demo)
        router.add_route("POST", "/submit", submit)
        return router

    def start(self) -> None:
        """Start listening and process clients according to selected engine."""
        if self.engine == "threadpool":
            self._start_threadpool()
            return
        if self.engine == "selectors":
            self._start_selectors()
            return
        raise ValueError(f"Unsupported engine: {self.engine}")

    def _start_threadpool(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
            self._server_socket = server_socket
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind((self.host, self.port))
            server_socket.listen(128)
            server_socket.settimeout(0.2)
            self.port = server_socket.getsockname()[1]
            self._pool = ThreadPool(
                worker_count=self.worker_count,
                queue_size=self.request_queue_size,
                handler=self._handle_client,
            )
            self._pool.start()

            self._running = True
            try:
                while self._running:
                    try:
                        client_socket, address = server_socket.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break

                    if self._pool is None or not self._pool.submit(client_socket, address):
                        self._send_queue_full_response(client_socket)
            finally:
                if self._pool is not None:
                    self._pool.shutdown()
                    self._pool = None

    def _start_selectors(self) -> None:
        with (
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket,
            selectors.DefaultSelector() as selector,
        ):
            self._server_socket = server_socket
            self._selector = selector
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind((self.host, self.port))
            server_socket.listen(128)
            server_socket.setblocking(False)
            selector.register(server_socket, selectors.EVENT_READ, data=None)
            self.port = server_socket.getsockname()[1]
            self._running = True

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
            finally:
                for state in list(self._selector_connections.values()):
                    self._close_selector_connection(state, selector)
                self._selector_connections.clear()
                self._selector = None

    def stop(self) -> None:
        self._running = False
        if self._server_socket is not None:
            self._server_socket.close()
            self._server_socket = None
        if self._pool is not None:
            self._pool.shutdown()
            self._pool = None

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
            )

    def _handle_client(self, client_socket: socket.socket, address: tuple[str, int]) -> None:
        with client_socket:
            self.metrics.connection_opened()
            client_socket.settimeout(min(SOCKET_TIMEOUT_SECS, self.keepalive_timeout_secs))
            request_count = 0
            carry = b""
            try:
                while request_count < MAX_KEEPALIVE_REQUESTS:
                    started_at = time.perf_counter()
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
                        )
                        return
                    except OSError:
                        return

                    if not raw_request:
                        return

                    try:
                        request = HTTPRequest.from_bytes(raw_request)
                    except HTTPRequestParseError as exc:
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
                        )
                        return
                    except ValueError:
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
                        )
                        return

                    request_count += 1
                    connection_reused = request_count > 1
                    self.metrics.request_started(connection_reused=connection_reused)
                    response = self._dispatch(request)
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
                self._send_queue_full_response(client_socket)
                continue

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

            try:
                request = HTTPRequest.from_bytes(raw_request)
            except HTTPRequestParseError as exc:
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
                    close_after=True,
                    metrics_started=False,
                )
                state.closing = True
                break
            except ValueError:
                self._queue_selector_response(
                    state,
                    HTTPResponse(status_code=400, body="Bad Request"),
                    method="-",
                    path="-",
                    started_at=started_at,
                    connection_reused=state.requests_served > 0,
                    request_id=state.request_seq + 1,
                    bytes_in=len(raw_request),
                    close_after=True,
                    metrics_started=False,
                )
                state.closing = True
                break

            state.requests_served += 1
            state.request_seq += 1
            connection_reused = state.requests_served > 1
            self.metrics.request_started(connection_reused=connection_reused)

            response = self._dispatch(request)
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
                close_after=should_close,
                metrics_started=True,
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
        close_after: bool,
        metrics_started: bool,
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
            close_after=close_after,
            metrics_started=metrics_started,
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
                if hasattr(os, "sendfile"):
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

    def _dispatch(self, request: HTTPRequest) -> HTTPResponse:
        if request.method not in KNOWN_METHODS:
            return HTTPResponse(status_code=501, body="Not Implemented")

        allowed_methods = {"GET", "HEAD", "POST"}
        if request.method not in allowed_methods:
            return HTTPResponse(
                status_code=405,
                headers={"Allow": "GET, HEAD, POST"},
                body="Method Not Allowed",
            )

        if request.path == "/_metrics":
            if request.method not in {"GET", "HEAD"}:
                return HTTPResponse(
                    status_code=405,
                    headers={"Allow": "GET, HEAD"},
                    body="Method Not Allowed",
                )
            snapshot = json.dumps(self.metrics.snapshot(), sort_keys=True)
            metrics_response = HTTPResponse(
                status_code=200,
                headers={"Content-Type": "application/json"},
                body=snapshot,
            )
            if request.method == "HEAD":
                return self._as_head_response(metrics_response)
            return metrics_response

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

        if request.method == "HEAD":
            handler = self.router.resolve("GET", request.path)
            if handler is None:
                return self._as_head_response(HTTPResponse(status_code=404, body="Not Found"))
            try:
                return self._as_head_response(handler(request))
            except Exception:
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
    ) -> None:
        duration_ms = (time.perf_counter() - started_at) * 1000
        self.metrics.record_request(
            status_code=response.status_code,
            duration_ms=duration_ms,
            bytes_sent=payload_size,
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
        }
        if self.log_format == "json":
            logger.info(json.dumps(event, sort_keys=True))
            return

        logger.info(
            (
                "client=%s method=%s path=%s status=%s engine=%s "
                "connection_id=%s request_id=%s bytes_in=%s bytes_out=%s "
                "duration_ms=%.2f connection_reused=%s"
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
    parser.add_argument("--engine", choices=["threadpool", "selectors"], default=SERVER_ENGINE)
    parser.add_argument("--max-connections", type=int, default=MAX_ACTIVE_CONNECTIONS)
    parser.add_argument("--keepalive-timeout", type=int, default=KEEPALIVE_TIMEOUT_SECS)
    parser.add_argument("--log-format", choices=["plain", "json"], default=LOG_FORMAT)
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    logging.basicConfig(level=logging.INFO)
    server = HTTPServer(
        host=args.host,
        port=args.port,
        engine=args.engine,
        max_active_connections=args.max_connections,
        keepalive_timeout_secs=args.keepalive_timeout,
        log_format=args.log_format,
    )
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()
