"""Main HTTP server entry point and connection lifecycle orchestration."""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import random
import selectors
import signal
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from config import (
    CLI_REFRESH_MS_DEFAULT,
    DEFAULT_CHAOS_SEED,
    DEMO_TOKEN,
    DRAIN_TIMEOUT_SECS,
    ENABLE_EXPECT_CONTINUE,
    ENABLE_INCIDENT_MODE,
    ENABLE_LIVE_EVENTS,
    ENABLE_PLAYGROUND,
    ENABLE_SCENARIOS,
    ENABLE_TARGET_PROXY,
    ENABLE_TLS,
    EVENT_BUFFER_SIZE,
    EVENT_HEARTBEAT_SECS,
    HISTORY_LIMIT,
    HOST,
    HTTPS_PORT,
    IDLE_SWEEP_INTERVAL_SECS,
    INCIDENT_DEFAULT_LATENCY_MS,
    INCIDENT_DEFAULT_PROBABILITY,
    INCIDENT_MAX_LATENCY_MS,
    KEEPALIVE_TIMEOUT_SECS,
    LIVE_EVENTS_REQUIRE_SELECTORS,
    LOG_FORMAT,
    MAX_ACTIVE_CONNECTIONS,
    MAX_ASSERTIONS_PER_STEP,
    MAX_KEEPALIVE_REQUESTS,
    MAX_MOCK_BODY_BYTES,
    MAX_MOCK_ROUTES,
    MAX_SCENARIOS,
    MAX_STEPS_PER_SCENARIO,
    MAX_TARGETS,
    PORT,
    PUBLIC_BASE_URL,
    REDIRECT_HTTP_TO_HTTPS,
    REQUEST_QUEUE_SIZE,
    REQUIRE_DEMO_TOKEN,
    SCENARIO_STATE_FILE,
    SELECT_TIMEOUT_SECS,
    SERVER_ENGINE,
    SHUTDOWN_POLL_INTERVAL_SECS,
    SOCKET_TIMEOUT_SECS,
    STATE_FILE,
    TARGET_STATE_FILE,
    TLS_CERT_FILE,
    TLS_KEY_FILE,
    WORKER_COUNT,
    WRITE_CHUNK_SIZE,
)
from dynamic_mock_dispatch import response_from_mock
from event_hub import EventHub
from handlers.example_handlers import home, serve_static, stream_demo, submit
from metrics import MetricsRegistry
from playground_api import PlaygroundAPI
from playground_store import PlaygroundStore
from request import KNOWN_METHODS, HTTPRequest, HTTPRequestParseError
from response import REASON_PHRASES, HTTPResponse, iter_chunked_encoded, prepare_response
from router import Router
from scenario_api import ScenarioAPI
from scenario_engine import ScenarioEngine
from scenario_store import ScenarioStore
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
from target_store import TargetStore
from target_store import utc_now as target_utc_now
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


@dataclass(slots=True)
class IncidentProfile:
    active: bool = False
    mode: str = "none"
    probability: float = INCIDENT_DEFAULT_PROBABILITY
    latency_ms: int = INCIDENT_DEFAULT_LATENCY_MS
    seed: int = 1337
    started_at: float = 0.0
    affected_requests: int = 0


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
        enable_scenarios: bool = ENABLE_SCENARIOS,
        scenario_state_file: str = SCENARIO_STATE_FILE,
        max_scenarios: int = MAX_SCENARIOS,
        max_steps_per_scenario: int = MAX_STEPS_PER_SCENARIO,
        max_assertions_per_step: int = MAX_ASSERTIONS_PER_STEP,
        default_chaos_seed: int = DEFAULT_CHAOS_SEED,
        enable_live_events: bool = ENABLE_LIVE_EVENTS,
        event_buffer_size: int = EVENT_BUFFER_SIZE,
        event_heartbeat_secs: int = EVENT_HEARTBEAT_SECS,
        live_events_require_selectors: bool = LIVE_EVENTS_REQUIRE_SELECTORS,
        cli_refresh_ms_default: int = CLI_REFRESH_MS_DEFAULT,
        enable_target_proxy: bool = ENABLE_TARGET_PROXY,
        target_state_file: str = TARGET_STATE_FILE,
        max_targets: int = MAX_TARGETS,
        demo_token: str = DEMO_TOKEN,
        require_demo_token: bool = REQUIRE_DEMO_TOKEN,
        public_base_url: str = PUBLIC_BASE_URL,
        codecrafters_mode: bool = False,
        files_directory: str | None = None,
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
        self.enable_scenarios = enable_scenarios
        self.scenario_state_file = scenario_state_file
        self.max_scenarios = max_scenarios
        self.max_steps_per_scenario = max_steps_per_scenario
        self.max_assertions_per_step = max_assertions_per_step
        self.default_chaos_seed = default_chaos_seed
        self.enable_live_events = enable_live_events
        self.event_heartbeat_secs = event_heartbeat_secs
        self.live_events_require_selectors = live_events_require_selectors
        self.cli_refresh_ms_default = cli_refresh_ms_default
        self.enable_target_proxy = enable_target_proxy
        self.target_state_file = target_state_file
        self.max_targets = max_targets
        self.demo_token = demo_token
        self.require_demo_token = require_demo_token
        self.public_base_url = public_base_url
        self.codecrafters_mode = codecrafters_mode
        self.files_directory = (
            Path(files_directory).resolve() if files_directory is not None else None
        )
        self.enable_incident_mode = ENABLE_INCIDENT_MODE

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
        self._incident_lock = threading.Lock()
        self._incident_profile = IncidentProfile()
        self._incident_request_seq = 0
        self.metrics = MetricsRegistry()
        self.metrics.set_incident_state(state="inactive", profile="none")
        self.event_hub: EventHub | None = (
            EventHub(buffer_size=event_buffer_size) if self.enable_live_events else None
        )
        self.playground_store: PlaygroundStore | None = None
        self.playground_api: PlaygroundAPI | None = None
        self.scenario_store: ScenarioStore | None = None
        self.scenario_engine: ScenarioEngine | None = None
        self.scenario_api: ScenarioAPI | None = None
        self.target_store: TargetStore | None = None
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
        if self.enable_scenarios:
            self.scenario_store = ScenarioStore(
                state_file=self.scenario_state_file,
                max_scenarios=self.max_scenarios,
            )
            self.scenario_engine = ScenarioEngine(
                dispatch_request=self._dispatch_internal_request,
                max_steps_per_scenario=self.max_steps_per_scenario,
                max_assertions_per_step=self.max_assertions_per_step,
                default_seed=self.default_chaos_seed,
            )
            self.scenario_api = ScenarioAPI(
                store=self.scenario_store,
                engine=self.scenario_engine,
                emit_event=self._emit_event,
            )
        if self.enable_target_proxy:
            self.target_store = TargetStore(
                state_file=self.target_state_file,
                max_targets=self.max_targets,
            )
        self._emit_event(
            "system.health",
            "",
            {
                "state": "server_initialized",
                "engine": self.engine,
                "live_events": self.enable_live_events,
            },
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

    def _emit_event(self, event_type: str, trace_id: str, payload: dict[str, object]) -> None:
        if self.event_hub is None:
            return
        self.event_hub.publish(
            event_type=event_type,
            source="server",
            trace_id=trace_id,
            payload=payload,
        )

    def _is_protected_path(self, path: str) -> bool:
        protected_prefixes = (
            "/api/mocks",
            "/api/history",
            "/api/replay",
            "/api/scenarios",
            "/api/targets",
            "/api/proxy/request",
            "/api/events",
            "/api/incidents",
        )
        return any(path.startswith(prefix) for prefix in protected_prefixes)

    def _is_authorized(self, request: HTTPRequest) -> bool:
        auth_enabled = self.require_demo_token or bool(self.demo_token)
        if not auth_enabled:
            return True

        expected = self.demo_token
        if not expected:
            return False

        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            presented = auth_header.removeprefix("Bearer ").strip()
            if presented == expected:
                return True

        query_token = request.query_params.get("token", [])
        if query_token and query_token[0] == expected:
            return True

        return False

    def _unauthorized_response(self) -> HTTPResponse:
        return HTTPResponse(
            status_code=401,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"error": {"code": "unauthorized", "message": "unauthorized"}}),
        )

    def _incident_state_payload(self) -> dict[str, object]:
        with self._incident_lock:
            return {
                "active": self._incident_profile.active,
                "mode": self._incident_profile.mode,
                "probability": self._incident_profile.probability,
                "latency_ms": self._incident_profile.latency_ms,
                "seed": self._incident_profile.seed,
                "started_at": (
                    0.0
                    if self._incident_profile.started_at <= 0
                    else self._incident_profile.started_at
                ),
                "affected_requests": self._incident_profile.affected_requests,
            }

    def _set_incident_profile(
        self,
        *,
        mode: str,
        probability: float,
        latency_ms: int,
        seed: int,
    ) -> dict[str, object]:
        with self._incident_lock:
            self._incident_profile.active = True
            self._incident_profile.mode = mode
            self._incident_profile.probability = probability
            self._incident_profile.latency_ms = latency_ms
            self._incident_profile.seed = seed
            self._incident_profile.started_at = time.time()
            self._incident_profile.affected_requests = 0
        self.metrics.set_incident_state(state="active", profile=mode)
        payload = self._incident_state_payload()
        self._emit_event("incident.started", "", payload)
        return payload

    def _stop_incident_profile(self) -> dict[str, object]:
        with self._incident_lock:
            self._incident_profile = IncidentProfile()
        self.metrics.set_incident_state(state="inactive", profile="none")
        payload = self._incident_state_payload()
        self._emit_event("incident.stopped", "", payload)
        return payload

    def _handle_incident_request(self, request: HTTPRequest) -> HTTPResponse:
        if not self.enable_incident_mode:
            return HTTPResponse(
                status_code=404,
                headers={"Content-Type": "application/json"},
                body=json.dumps(
                    {"error": {"code": "disabled", "message": "incident mode is disabled"}}
                ),
            )
        if request.path == "/api/incidents/state":
            if request.method not in {"GET", "HEAD"}:
                return HTTPResponse(
                    status_code=405,
                    headers={"Allow": "GET, HEAD"},
                    body="Method Not Allowed",
                )
            return HTTPResponse(
                status_code=200,
                headers={"Content-Type": "application/json"},
                body=json.dumps({"incident": self._incident_state_payload()}, sort_keys=True),
            )
        if request.path == "/api/incidents/stop":
            if request.method != "POST":
                return HTTPResponse(
                    status_code=405,
                    headers={"Allow": "POST"},
                    body="Method Not Allowed",
                )
            return HTTPResponse(
                status_code=200,
                headers={"Content-Type": "application/json"},
                body=json.dumps(
                    {"incident": self._stop_incident_profile(), "status": "stopped"},
                    sort_keys=True,
                ),
            )
        if request.path != "/api/incidents/start":
            return HTTPResponse(status_code=404, body="Not Found")
        if request.method != "POST":
            return HTTPResponse(
                status_code=405,
                headers={"Allow": "POST"},
                body="Method Not Allowed",
            )
        payload, error = self._read_json_payload(request)
        if error is not None:
            return error
        mode = str(payload.get("mode", "status_spike_503")).strip().lower()
        valid_modes = {"status_spike_503", "fixed_latency", "body_drop"}
        if mode not in valid_modes:
            return self._targets_response(
                400,
                {
                    "error": {
                        "code": "invalid_incident",
                        "message": "mode must be status_spike_503, fixed_latency, or body_drop",
                    }
                },
            )
        probability = float(payload.get("probability", INCIDENT_DEFAULT_PROBABILITY))
        probability = max(0.0, min(1.0, probability))
        latency_ms = int(payload.get("latency_ms", INCIDENT_DEFAULT_LATENCY_MS))
        latency_ms = max(0, min(INCIDENT_MAX_LATENCY_MS, latency_ms))
        seed = int(payload.get("seed", 1337))
        state = self._set_incident_profile(
            mode=mode,
            probability=probability,
            latency_ms=latency_ms,
            seed=seed,
        )
        return HTTPResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"incident": state, "status": "started"}, sort_keys=True),
        )

    def _incident_should_skip_path(self, path: str) -> bool:
        return (
            path.startswith("/api/")
            or path.startswith("/static/")
            or path == "/_metrics"
            or path == "/playground"
        )

    def _incident_decision(self, profile: IncidentProfile) -> bool:
        with self._incident_lock:
            self._incident_request_seq += 1
            seq = self._incident_request_seq
        value = random.Random(profile.seed + seq).random()
        return value <= profile.probability

    def _apply_incident_profile(self, request: HTTPRequest, response: HTTPResponse) -> HTTPResponse:
        if not self.enable_incident_mode:
            return response
        if self._incident_should_skip_path(request.path):
            return response

        with self._incident_lock:
            profile = IncidentProfile(
                active=self._incident_profile.active,
                mode=self._incident_profile.mode,
                probability=self._incident_profile.probability,
                latency_ms=self._incident_profile.latency_ms,
                seed=self._incident_profile.seed,
                started_at=self._incident_profile.started_at,
                affected_requests=self._incident_profile.affected_requests,
            )

        if not profile.active:
            return response

        applied = False
        mode = profile.mode

        if mode == "fixed_latency" and profile.latency_ms > 0:
            time.sleep(profile.latency_ms / 1000)
            applied = True

        if mode == "status_spike_503" and self._incident_decision(profile):
            response = HTTPResponse(
                status_code=503,
                headers={"Content-Type": "text/plain; charset=utf-8"},
                body="Incident Injected: upstream unavailable",
            )
            applied = True

        if mode == "body_drop" and self._incident_decision(profile):
            response = HTTPResponse(
                status_code=response.status_code,
                reason_phrase=response.reason_phrase,
                headers=dict(response.headers),
                body=b"",
            )
            applied = True

        if not applied:
            return response

        with self._incident_lock:
            self._incident_profile.affected_requests += 1
            affected = self._incident_profile.affected_requests
            current_mode = self._incident_profile.mode
        self.metrics.record_incident_fault()
        self._emit_event(
            "incident.affected",
            str(response.headers.get("X-Request-ID", "")),
            {
                "mode": current_mode,
                "path": request.path,
                "method": request.method,
                "status": response.status_code,
                "affected_requests": affected,
            },
        )
        return response

    def _event_topics(self, request: HTTPRequest) -> set[str] | None:
        raw_topics = request.query_params.get("topics", [])
        if not raw_topics:
            return None
        tokens = [item.strip() for item in raw_topics[0].split(",") if item.strip()]
        if not tokens:
            return None
        return set(tokens)

    def _event_since_id(self, request: HTTPRequest) -> int:
        since_values = request.query_params.get("since_id", [])
        if not since_values:
            last_event_values = request.headers.get("last-event-id", "")
            if not last_event_values:
                return 0
            try:
                return int(last_event_values)
            except ValueError:
                return 0
        try:
            return max(0, int(since_values[0]))
        except ValueError:
            return 0

    def _event_limit(self, request: HTTPRequest, default: int = 200) -> int:
        values = request.query_params.get("limit", [])
        if not values:
            return default
        try:
            return max(1, min(1000, int(values[0])))
        except ValueError:
            return default

    def _events_snapshot_response(self, request: HTTPRequest) -> HTTPResponse:
        if self.event_hub is None:
            return HTTPResponse(
                status_code=404,
                headers={"Content-Type": "application/json"},
                body=json.dumps({"error": {"code": "disabled", "message": "live events disabled"}}),
            )
        snapshot = self.event_hub.snapshot(
            since_id=self._event_since_id(request),
            limit=self._event_limit(request, default=200),
            topics=self._event_topics(request),
        )
        snapshot["refresh_ms"] = self.cli_refresh_ms_default
        return HTTPResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps(snapshot, sort_keys=True),
        )

    def _events_stream_response(self, request: HTTPRequest) -> HTTPResponse:
        if self.event_hub is None:
            return HTTPResponse(status_code=404, body="Not Found")
        if self.live_events_require_selectors and self.engine != "selectors":
            return HTTPResponse(
                status_code=409,
                headers={"Content-Type": "application/json"},
                body=json.dumps(
                    {
                        "error": {
                            "code": "use_selectors_engine",
                            "message": "live events require selectors engine",
                        }
                    }
                ),
            )

        snapshot = self.event_hub.snapshot(
            since_id=self._event_since_id(request),
            limit=self._event_limit(request, default=200),
            topics=self._event_topics(request),
        )
        lines = [f"retry: {max(100, self.cli_refresh_ms_default)}\n\n"]
        if snapshot.get("overflowed", False):
            lines.append("event: stream.gap\n")
            lines.append(
                "data: "
                + json.dumps(
                    {
                        "overflowed": True,
                        "oldest_id": snapshot.get("oldest_id", 0),
                        "latest_id": snapshot.get("latest_id", 0),
                    },
                    sort_keys=True,
                )
                + "\n\n"
            )
        for event in snapshot["events"]:
            lines.append(f"id: {event['id']}\n")
            lines.append(f"event: {event['type']}\n")
            lines.append(f"data: {json.dumps(event, sort_keys=True)}\n\n")
        if not snapshot["events"]:
            lines.append(": heartbeat\n\n")

        return HTTPResponse(
            status_code=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "close",
                "X-Events-Latest-ID": str(snapshot["latest_id"]),
                "X-Event-Heartbeat-Secs": str(max(1, int(self.event_heartbeat_secs))),
            },
            body="".join(lines),
        )

    def _validate_target_payload(
        self,
        payload: dict[str, object],
        *,
        target_id: str | None = None,
    ) -> tuple[bool, str]:
        if target_id is None:
            target_id = str(payload.get("id", "") or uuid.uuid4().hex[:8])
        name = str(payload.get("name", "")).strip()
        base_url = str(payload.get("base_url", "")).strip()
        if not name:
            return False, "name is required"
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            return False, "base_url must start with http:// or https://"
        return True, ""

    def _target_headers(self, request: HTTPRequest) -> dict[str, str]:
        headers_raw = request.headers.get("content-type", "")
        _ = headers_raw
        return {"Content-Type": "application/json"}

    def _proxy_request_to_target(
        self,
        *,
        target: dict[str, object],
        method: str,
        path: str,
        headers: dict[str, str],
        body: str,
        timeout_ms: int,
    ) -> tuple[int, dict[str, str], str]:
        target_url = str(target["base_url"]).rstrip("/")
        full_url = f"{target_url}{path}"
        data = body.encode("utf-8") if body else None
        req = urllib.request.Request(
            url=full_url,
            method=method,
            headers={str(k): str(v) for k, v in headers.items()},
            data=data,
        )
        try:
            with urllib.request.urlopen(req, timeout=max(1, timeout_ms) / 1000) as resp:
                response_body = resp.read().decode("utf-8", errors="replace")
                response_headers = {str(k): str(v) for k, v in resp.headers.items()}
                return int(resp.status), response_headers, response_body
        except urllib.error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            response_headers = {str(k): str(v) for k, v in exc.headers.items()}
            return int(exc.code), response_headers, response_body
        except urllib.error.URLError as exc:
            return 504, {"Content-Type": "application/json"}, json.dumps(
                {"error": {"code": "proxy_timeout", "message": str(exc)}}
            )

    def _dispatch_to_target(self, request: HTTPRequest, *, target_id: str) -> HTTPResponse:
        if self.target_store is None:
            return HTTPResponse(status_code=404, body="Target store disabled")
        target = self.target_store.get_target(target_id)
        if target is None:
            return HTTPResponse(
                status_code=404,
                headers={"Content-Type": "application/json"},
                body=json.dumps(
                    {
                        "error": {
                            "code": "target_not_found",
                            "message": "target not found",
                        }
                    }
                ),
            )
        status, response_headers, response_body = self._proxy_request_to_target(
            target=target,
            method=request.method,
            path=request.raw_target,
            headers=dict(request.headers),
            body=request.body.decode("utf-8", errors="replace"),
            timeout_ms=int(target.get("timeout_ms", 5000)),
        )
        trace_id = self._new_trace_id()
        self._emit_event(
            "proxy.request.completed",
            trace_id,
            {
                "target_id": target_id,
                "method": request.method,
                "path": request.raw_target,
                "status": status,
            },
        )
        proxy_response = HTTPResponse(
            status_code=status,
            headers=response_headers,
            body=response_body,
        )
        proxy_response.headers.setdefault("X-Request-ID", trace_id)
        return proxy_response

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
                    response = self._apply_incident_profile(request, response)
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
            response = self._apply_incident_profile(request, response)
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

    def _dispatch_internal_request(
        self,
        request: HTTPRequest,
        *,
        target_id: str = "local",
    ) -> HTTPResponse:
        if target_id != "local":
            response = self._dispatch_to_target(request, target_id=target_id)
            response = self._apply_incident_profile(request, response)
            if response.headers.get("X-Request-ID") is None:
                response.headers["X-Request-ID"] = self._new_trace_id()
            return response
        response = self._dispatch(request)
        response = self._apply_incident_profile(request, response)
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

    def _read_json_payload(
        self,
        request: HTTPRequest,
    ) -> tuple[dict[str, object], HTTPResponse | None]:
        try:
            decoded = request.body.decode("utf-8") if request.body else "{}"
            payload = json.loads(decoded)
        except UnicodeDecodeError:
            return {}, HTTPResponse(
                status_code=400,
                headers={"Content-Type": "application/json"},
                body=json.dumps(
                    {
                        "error": {
                            "code": "invalid_json",
                            "message": "body must be utf-8",
                        }
                    }
                ),
            )
        except json.JSONDecodeError:
            return {}, HTTPResponse(
                status_code=400,
                headers={"Content-Type": "application/json"},
                body=json.dumps(
                    {"error": {"code": "invalid_json", "message": "body must be valid json"}}
                ),
            )

        if not isinstance(payload, dict):
            return {}, HTTPResponse(
                status_code=400,
                headers={"Content-Type": "application/json"},
                body=json.dumps(
                    {"error": {"code": "invalid_json", "message": "top-level must be object"}}
                ),
            )
        return payload, None

    def _targets_response(self, status_code: int, payload: dict[str, object]) -> HTTPResponse:
        return HTTPResponse(
            status_code=status_code,
            headers={"Content-Type": "application/json"},
            body=json.dumps(payload, sort_keys=True),
        )

    def _handle_targets_request(self, request: HTTPRequest) -> HTTPResponse:
        if not self.enable_target_proxy or self.target_store is None:
            return HTTPResponse(status_code=404, body="Not Found")

        if request.path == "/api/targets":
            if request.method == "GET":
                return self._targets_response(200, {"targets": self.target_store.list_targets()})
            if request.method != "POST":
                return HTTPResponse(
                    status_code=405,
                    headers={"Allow": "GET, POST"},
                    body="Method Not Allowed",
                )

            payload, error = self._read_json_payload(request)
            if error is not None:
                return error
            target_id = str(payload.get("id") or uuid.uuid4().hex[:8])
            ok, message = self._validate_target_payload(payload, target_id=target_id)
            if not ok:
                return self._targets_response(
                    400,
                    {"error": {"code": "invalid_target", "message": message}},
                )
            target = {
                "id": target_id,
                "name": str(payload.get("name", "")),
                "base_url": str(payload.get("base_url", "")),
                "enabled": bool(payload.get("enabled", True)),
                "timeout_ms": int(payload.get("timeout_ms", 5000)),
                "created_at": target_utc_now(),
                "updated_at": target_utc_now(),
            }
            try:
                created = self.target_store.create_target(target)
            except ValueError as exc:
                reason = str(exc)
                if reason == "max_targets_exceeded":
                    return self._targets_response(
                        413,
                        {"error": {"code": "target_limit", "message": "max targets reached"}},
                    )
                return self._targets_response(
                    409,
                    {"error": {"code": "duplicate_target", "message": "target already exists"}},
                )
            return self._targets_response(201, {"target": created})

        target_id = request.path.removeprefix("/api/targets/")
        if not target_id:
            return self._targets_response(
                404,
                {"error": {"code": "not_found", "message": "not found"}},
            )

        if request.method == "DELETE":
            deleted = self.target_store.delete_target(target_id)
            if not deleted:
                return self._targets_response(
                    404,
                    {"error": {"code": "not_found", "message": "target not found"}},
                )
            return self._targets_response(200, {"deleted": True, "id": target_id})

        if request.method != "PUT":
            return HTTPResponse(
                status_code=405,
                headers={"Allow": "PUT, DELETE"},
                body="Method Not Allowed",
            )

        existing = self.target_store.get_target(target_id)
        if existing is None:
            return self._targets_response(
                404,
                {"error": {"code": "not_found", "message": "target not found"}},
            )
        payload, error = self._read_json_payload(request)
        if error is not None:
            return error
        merged = dict(existing)
        merged.update(payload)
        ok, message = self._validate_target_payload(merged, target_id=target_id)
        if not ok:
            return self._targets_response(
                400,
                {"error": {"code": "invalid_target", "message": message}},
            )
        merged["id"] = target_id
        merged["updated_at"] = target_utc_now()
        try:
            updated = self.target_store.update_target(target_id, merged)
        except ValueError:
            return self._targets_response(
                409,
                {"error": {"code": "duplicate_target", "message": "target already exists"}},
            )
        if updated is None:
            return self._targets_response(
                404,
                {"error": {"code": "not_found", "message": "target not found"}},
            )
        return self._targets_response(200, {"target": updated})

    def _handle_proxy_request(self, request: HTTPRequest) -> HTTPResponse:
        if not self.enable_target_proxy or self.target_store is None:
            return HTTPResponse(status_code=404, body="Not Found")
        if request.method != "POST":
            return HTTPResponse(
                status_code=405,
                headers={"Allow": "POST"},
                body="Method Not Allowed",
            )

        payload, error = self._read_json_payload(request)
        if error is not None:
            return error

        target_id = str(payload.get("target_id", "")).strip()
        method = str(payload.get("method", "GET")).upper()
        path = str(payload.get("path", "/")).strip()
        if not target_id:
            return self._targets_response(
                400,
                {"error": {"code": "invalid_proxy_request", "message": "target_id is required"}},
            )
        if method not in {"GET", "POST", "PUT", "DELETE"}:
            return self._targets_response(
                400,
                {"error": {"code": "invalid_proxy_request", "message": "invalid method"}},
            )
        if not path.startswith("/"):
            return self._targets_response(
                400,
                {"error": {"code": "invalid_proxy_request", "message": "path must start with /"}},
            )

        headers_raw = payload.get("headers", {})
        if headers_raw is None:
            headers_raw = {}
        if not isinstance(headers_raw, dict):
            return self._targets_response(
                400,
                {"error": {"code": "invalid_proxy_request", "message": "headers must be object"}},
            )

        timeout_ms = int(payload.get("timeout_ms", 5000))
        body_text = str(payload.get("body", ""))

        target = self.target_store.get_target(target_id)
        if target is None:
            return self._targets_response(
                404,
                {"error": {"code": "target_not_found", "message": "target not found"}},
            )

        status, response_headers, response_body = self._proxy_request_to_target(
            target=target,
            method=method,
            path=path,
            headers={str(k): str(v) for k, v in headers_raw.items()},
            body=body_text,
            timeout_ms=timeout_ms,
        )
        trace_id = self._new_trace_id()
        self._emit_event(
            "proxy.request.completed",
            trace_id,
            {
                "target_id": target_id,
                "method": method,
                "path": path,
                "status": status,
            },
        )
        return self._targets_response(
            200,
            {
                "request": {
                    "target_id": target_id,
                    "method": method,
                    "path": path,
                },
                "response": {
                    "status": status,
                    "headers": response_headers,
                    "body": response_body,
                },
                "trace_id": trace_id,
            },
        )

    def _is_api_admin_path(self, path: str) -> bool:
        return (
            path == "/api/playground/state"
            or path == "/api/mocks"
            or path == "/api/history"
            or path == "/api/playground/request"
            or path.startswith("/api/mocks/")
            or path.startswith("/api/replay/")
            or path == "/api/scenarios"
            or path.startswith("/api/scenarios/")
            or path.startswith("/api/incidents/")
        )

    def _codecrafters_file_path(self, request_path: str) -> Path | None:
        if self.files_directory is None:
            return None
        if not self.files_directory.exists() or not self.files_directory.is_dir():
            return None
        if not request_path.startswith("/files/"):
            return None

        filename = request_path.removeprefix("/files/")
        if not filename:
            return None
        if "/" in filename or "\\" in filename:
            return None
        if filename in {".", ".."}:
            return None

        candidate = (self.files_directory / filename).resolve()
        try:
            candidate.relative_to(self.files_directory)
        except ValueError:
            return None
        return candidate

    def _codecrafters_accepts_gzip(self, request: HTTPRequest) -> bool:
        raw_header = request.headers.get("accept-encoding", "")
        if not raw_header:
            return False

        for token in raw_header.split(","):
            encoding = token.strip().split(";", 1)[0].strip().lower()
            if encoding == "gzip":
                return True
        return False

    def _dispatch_codecrafters_base(self, request: HTTPRequest) -> HTTPResponse | None:
        if not self.codecrafters_mode:
            return None

        if request.path.startswith("/echo/"):
            if request.method not in {"GET", "HEAD"}:
                return None
            echo_payload = request.path.removeprefix("/echo/")
            body_bytes = echo_payload.encode("utf-8")
            headers = {"Content-Type": "text/plain"}
            if self._codecrafters_accepts_gzip(request):
                body_bytes = gzip.compress(body_bytes, mtime=0)
                headers["Content-Encoding"] = "gzip"
            response = HTTPResponse(
                status_code=200,
                headers=headers,
                body=body_bytes,
            )
            if request.method == "HEAD":
                return self._as_head_response(response)
            return response

        if request.path == "/user-agent":
            if request.method not in {"GET", "HEAD"}:
                return None
            user_agent = request.headers.get("user-agent", "")
            response = HTTPResponse(
                status_code=200,
                headers={"Content-Type": "text/plain"},
                body=user_agent,
            )
            if request.method == "HEAD":
                return self._as_head_response(response)
            return response

        if request.path.startswith("/files/"):
            if request.method in {"GET", "HEAD"}:
                file_path = self._codecrafters_file_path(request.path)
                if file_path is None or not file_path.exists() or not file_path.is_file():
                    not_found = HTTPResponse(status_code=404, body="Not Found")
                    if request.method == "HEAD":
                        return self._as_head_response(not_found)
                    return not_found

                response = HTTPResponse(
                    status_code=200,
                    headers={"Content-Type": "application/octet-stream"},
                    file_path=file_path,
                )
                if request.method == "HEAD":
                    return self._as_head_response(response)
                return response

            if request.method == "POST":
                file_path = self._codecrafters_file_path(request.path)
                if file_path is None:
                    return HTTPResponse(status_code=404, body="Not Found")
                if file_path.exists() and not file_path.is_file():
                    return HTTPResponse(status_code=404, body="Not Found")
                try:
                    file_path.write_bytes(request.body)
                except OSError:
                    self.metrics.record_error_class("dispatch")
                    logger.exception("Failed to write file in codecrafters mode")
                    return HTTPResponse(status_code=500, body="Internal Server Error")
                return HTTPResponse(status_code=201, reason_phrase="Created", body=b"")

        return None

    def _dispatch(self, request: HTTPRequest) -> HTTPResponse:
        if request.method not in KNOWN_METHODS:
            return HTTPResponse(status_code=501, body="Not Implemented")
        if self._is_protected_path(request.path) and not self._is_authorized(request):
            return self._unauthorized_response()
        compatibility_response = self._dispatch_codecrafters_base(request)
        if compatibility_response is not None:
            return compatibility_response

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
        if request.path == "/api/events/snapshot":
            if request.method not in {"GET", "HEAD"}:
                return HTTPResponse(
                    status_code=405,
                    headers={"Allow": "GET, HEAD"},
                    body="Method Not Allowed",
                )
            snapshot_response = self._events_snapshot_response(request)
            if request.method == "HEAD":
                return self._as_head_response(snapshot_response)
            return snapshot_response
        if request.path == "/api/events/stream":
            if request.method not in {"GET", "HEAD"}:
                return HTTPResponse(
                    status_code=405,
                    headers={"Allow": "GET, HEAD"},
                    body="Method Not Allowed",
                )
            stream_response = self._events_stream_response(request)
            if request.method == "HEAD":
                return self._as_head_response(stream_response)
            return stream_response
        if request.path == "/api/targets" or request.path.startswith("/api/targets/"):
            response = self._handle_targets_request(request)
            if request.method == "HEAD":
                return self._as_head_response(response)
            return response
        if request.path == "/api/proxy/request":
            response = self._handle_proxy_request(request)
            if request.method == "HEAD":
                return self._as_head_response(response)
            return response
        if request.path.startswith("/api/incidents/"):
            response = self._handle_incident_request(request)
            if request.method == "HEAD":
                return self._as_head_response(response)
            return response

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

        if self._is_api_admin_path(request.path):
            if request.path == "/api/scenarios" or request.path.startswith("/api/scenarios/"):
                if not self.enable_scenarios or self.scenario_api is None:
                    return HTTPResponse(status_code=404, body="Not Found")
                response, run = self.scenario_api.handle(request)
                if response.status_code >= 400:
                    self.metrics.record_playground_admin_error()
                if run is not None:
                    failed_assertions = int(run.get("failed_assertions", 0))
                    summary = run.get("summary", {})
                    self.metrics.record_scenario_run(
                        passed=str(run.get("status", "fail")) == "pass",
                        step_count=int(summary.get("passed", 0)) + int(summary.get("failed", 0)),
                        failed_assertions=failed_assertions,
                        duration_ms=float(summary.get("duration_ms", 0.0)),
                    )
                    for step in run.get("step_results", []):
                        scenario_event = {
                            "scenario_id": str(run.get("scenario_id", "")),
                            "run_id": str(run.get("run_id", "")),
                            "step_id": str(step.get("step_id", "")),
                            "assertion_fail_count": sum(
                                1
                                for assertion in step.get("assertions", [])
                                if not assertion.get("passed", False)
                            ),
                            "chaos_applied": bool(step.get("chaos_applied", False)),
                            "scenario_seed": str(run.get("seed", "")),
                        }
                        if self.log_format == "json":
                            logger.info(json.dumps(scenario_event, sort_keys=True))
                        else:
                            logger.info(
                                "scenario_id=%s run_id=%s step_id=%s "
                                "assertion_fail_count=%s chaos_applied=%s scenario_seed=%s",
                                scenario_event["scenario_id"],
                                scenario_event["run_id"],
                                scenario_event["step_id"],
                                scenario_event["assertion_fail_count"],
                                scenario_event["chaos_applied"],
                                scenario_event["scenario_seed"],
                            )
                if request.method == "HEAD":
                    return self._as_head_response(response)
                return response

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
        elif path.startswith("/api/scenarios/") and path.endswith("/run") and method == "POST":
            event["scenario_id"] = path.removeprefix("/api/scenarios/").removesuffix("/run")
            event["run_id"] = response.headers.get("X-Scenario-Run-ID", "")
            event["scenario_seed"] = response.headers.get("X-Scenario-Seed", "")

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
        else:
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

        self._emit_event(
            "request.completed",
            str(event["trace_id"]),
            {
                "method": method,
                "path": path,
                "status": response.status_code,
                "bytes_in": bytes_in,
                "bytes_out": payload_size,
                "latency_ms": round(duration_ms, 3),
                "route_key": event["route_key"],
                "connection_reused": connection_reused,
                "engine": self.engine,
            },
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
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--codecrafters-mode", action="store_true", default=False)
    parser.add_argument("--directory", default=None)
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
    parser.add_argument(
        "--enable-scenarios",
        action=argparse.BooleanOptionalAction,
        default=ENABLE_SCENARIOS,
    )
    parser.add_argument("--scenario-state-file", default=SCENARIO_STATE_FILE)
    parser.add_argument("--max-scenarios", type=int, default=MAX_SCENARIOS)
    parser.add_argument("--max-steps-per-scenario", type=int, default=MAX_STEPS_PER_SCENARIO)
    parser.add_argument(
        "--max-assertions-per-step",
        type=int,
        default=MAX_ASSERTIONS_PER_STEP,
    )
    parser.add_argument("--default-chaos-seed", type=int, default=DEFAULT_CHAOS_SEED)
    parser.add_argument(
        "--enable-live-events",
        action=argparse.BooleanOptionalAction,
        default=ENABLE_LIVE_EVENTS,
    )
    parser.add_argument("--event-buffer-size", type=int, default=EVENT_BUFFER_SIZE)
    parser.add_argument("--event-heartbeat-secs", type=int, default=EVENT_HEARTBEAT_SECS)
    parser.add_argument(
        "--live-events-require-selectors",
        action=argparse.BooleanOptionalAction,
        default=LIVE_EVENTS_REQUIRE_SELECTORS,
    )
    parser.add_argument("--cli-refresh-ms", type=int, default=CLI_REFRESH_MS_DEFAULT)
    parser.add_argument(
        "--enable-target-proxy",
        action=argparse.BooleanOptionalAction,
        default=ENABLE_TARGET_PROXY,
    )
    parser.add_argument("--target-state-file", default=TARGET_STATE_FILE)
    parser.add_argument("--max-targets", type=int, default=MAX_TARGETS)
    parser.add_argument("--demo-token", default=DEMO_TOKEN)
    parser.add_argument(
        "--require-demo-token",
        action=argparse.BooleanOptionalAction,
        default=REQUIRE_DEMO_TOKEN,
    )
    parser.add_argument("--public-base-url", default=PUBLIC_BASE_URL)
    args = parser.parse_args()
    if args.port is None:
        args.port = 4221 if args.codecrafters_mode else PORT
    return args


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
        enable_scenarios=args.enable_scenarios,
        scenario_state_file=args.scenario_state_file,
        max_scenarios=args.max_scenarios,
        max_steps_per_scenario=args.max_steps_per_scenario,
        max_assertions_per_step=args.max_assertions_per_step,
        default_chaos_seed=args.default_chaos_seed,
        enable_live_events=args.enable_live_events,
        event_buffer_size=args.event_buffer_size,
        event_heartbeat_secs=args.event_heartbeat_secs,
        live_events_require_selectors=args.live_events_require_selectors,
        cli_refresh_ms_default=args.cli_refresh_ms,
        enable_target_proxy=args.enable_target_proxy,
        target_state_file=args.target_state_file,
        max_targets=args.max_targets,
        demo_token=args.demo_token,
        require_demo_token=args.require_demo_token,
        public_base_url=args.public_base_url,
        codecrafters_mode=args.codecrafters_mode,
        files_directory=args.directory,
    )
    _install_signal_handlers(server)
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()
