"""Main HTTP server entry point and connection lifecycle orchestration."""

import json
import logging
import socket
import time

from config import (
    HOST,
    KEEPALIVE_TIMEOUT_SECS,
    MAX_KEEPALIVE_REQUESTS,
    PORT,
    REQUEST_QUEUE_SIZE,
    SOCKET_TIMEOUT_SECS,
    WORKER_COUNT,
)
from handlers.example_handlers import home, serve_static, stream_demo, submit
from metrics import MetricsRegistry
from request import HTTPRequest
from response import HTTPResponse
from router import Router
from socket_handler import (
    HeaderTooLargeError,
    MalformedRequestError,
    PayloadTooLargeError,
    SocketTimeoutError,
    read_http_request_message,
    write_http_response,
)
from thread_pool import ThreadPool

logger = logging.getLogger(__name__)


class HTTPServer:
    def __init__(
        self,
        host: str = HOST,
        port: int = PORT,
        router: Router | None = None,
        worker_count: int = WORKER_COUNT,
        request_queue_size: int = REQUEST_QUEUE_SIZE,
    ) -> None:
        self.host = host
        self.port = port
        self.router = router or self._build_default_router()
        self.worker_count = worker_count
        self.request_queue_size = request_queue_size
        self._server_socket: socket.socket | None = None
        self._pool: ThreadPool | None = None
        self._running = False
        self.metrics = MetricsRegistry()

    def _build_default_router(self) -> Router:
        router = Router()
        router.add_route("GET", "/", home)
        router.add_route("GET", "/stream", stream_demo)
        router.add_route("POST", "/submit", submit)
        return router

    def start(self) -> None:
        """Start listening and process clients through a fixed worker pool."""
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
            payload = response.to_bytes()
            write_http_response(client_socket, payload)
            duration_ms = (time.perf_counter() - started_at) * 1000
            self.metrics.record_request(
                status_code=response.status_code,
                duration_ms=duration_ms,
                bytes_sent=len(payload),
            )

    def _handle_client(self, client_socket: socket.socket, address: tuple[str, int]) -> None:
        with client_socket:
            self.metrics.connection_opened()
            client_socket.settimeout(min(SOCKET_TIMEOUT_SECS, KEEPALIVE_TIMEOUT_SECS))
            request_count = 0
            carry = b""
            try:
                while request_count < MAX_KEEPALIVE_REQUESTS:
                    started_at = time.perf_counter()
                    try:
                        raw_request, carry = read_http_request_message(client_socket, carry)
                    except PayloadTooLargeError:
                        response = HTTPResponse(status_code=413, body="Payload Too Large")
                        response.headers.setdefault("Connection", "close")
                        payload = response.to_bytes()
                        write_http_response(client_socket, payload)
                        self._record_and_log(
                            address=address,
                            method="-",
                            path="-",
                            response=response,
                            payload_size=len(payload),
                            started_at=started_at,
                            connection_reused=False,
                        )
                        return
                    except (HeaderTooLargeError, MalformedRequestError, SocketTimeoutError):
                        response = HTTPResponse(status_code=400, body="Bad Request")
                        response.headers.setdefault("Connection", "close")
                        payload = response.to_bytes()
                        write_http_response(client_socket, payload)
                        self._record_and_log(
                            address=address,
                            method="-",
                            path="-",
                            response=response,
                            payload_size=len(payload),
                            started_at=started_at,
                            connection_reused=False,
                        )
                        return
                    except OSError:
                        return

                    if not raw_request:
                        return

                    try:
                        request = HTTPRequest.from_bytes(raw_request)
                    except ValueError:
                        response = HTTPResponse(status_code=400, body="Bad Request")
                        response.headers.setdefault("Connection", "close")
                        payload = response.to_bytes()
                        write_http_response(client_socket, payload)
                        self._record_and_log(
                            address=address,
                            method="-",
                            path="-",
                            response=response,
                            payload_size=len(payload),
                            started_at=started_at,
                            connection_reused=False,
                        )
                        return

                    request_count += 1
                    connection_reused = request_count > 1
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
                                f"timeout={KEEPALIVE_TIMEOUT_SECS}, "
                                f"max={MAX_KEEPALIVE_REQUESTS - request_count}"
                            ),
                        )

                    payload = response.to_bytes()
                    write_http_response(client_socket, payload)
                    self._record_and_log(
                        address=address,
                        method=request.method,
                        path=request.path,
                        response=response,
                        payload_size=len(payload),
                        started_at=started_at,
                        connection_reused=connection_reused,
                    )
                    if should_close:
                        return
            finally:
                self.metrics.connection_closed()

    def _dispatch(self, request: HTTPRequest) -> HTTPResponse:
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
        started_at: float,
        connection_reused: bool,
    ) -> None:
        duration_ms = (time.perf_counter() - started_at) * 1000
        self.metrics.record_request(
            status_code=response.status_code,
            duration_ms=duration_ms,
            bytes_sent=payload_size,
        )
        logger.info(
            "client=%s method=%s path=%s status=%s bytes=%s duration_ms=%.2f connection_reused=%s",
            address[0],
            method,
            path,
            response.status_code,
            payload_size,
            duration_ms,
            connection_reused,
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    server = HTTPServer()
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()
