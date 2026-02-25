"""Main HTTP server entry point and connection lifecycle orchestration."""

import socket
import threading

from config import HOST, PORT
from handlers.example_handlers import home, serve_static, submit
from request import HTTPRequest
from response import HTTPResponse
from router import Router
from socket_handler import read_http_request, write_http_response


class HTTPServer:
    def __init__(self, host: str = HOST, port: int = PORT, router: Router | None = None) -> None:
        self.host = host
        self.port = port
        self.router = router or self._build_default_router()
        self._server_socket: socket.socket | None = None
        self._running = False
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def _build_default_router(self) -> Router:
        router = Router()
        router.add_route("GET", "/", home)
        router.add_route("POST", "/submit", submit)
        return router

    def start(self) -> None:
        """Start listening and process each client in a dedicated thread."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
            self._server_socket = server_socket
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind((self.host, self.port))
            server_socket.listen(128)
            server_socket.settimeout(0.2)
            self.port = server_socket.getsockname()[1]

            self._running = True
            while self._running:
                try:
                    client_socket, address = server_socket.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break

                client_thread = threading.Thread(
                    target=self._handle_client,
                    args=(client_socket, address),
                    daemon=True,
                )
                with self._lock:
                    self._threads.append(client_thread)
                client_thread.start()

            self._join_client_threads()

    def stop(self) -> None:
        self._running = False
        if self._server_socket is not None:
            self._server_socket.close()
            self._server_socket = None

    def _join_client_threads(self) -> None:
        with self._lock:
            threads = list(self._threads)
            self._threads.clear()

        for thread in threads:
            thread.join(timeout=1.0)

    def _handle_client(self, client_socket: socket.socket, address: tuple[str, int]) -> None:
        _ = address
        with client_socket:
            raw_request = read_http_request(client_socket)
            if not raw_request:
                return

            try:
                request = HTTPRequest.from_bytes(raw_request)
            except ValueError:
                response = HTTPResponse(status_code=400, body="Bad Request")
            else:
                response = self._dispatch(request)

            write_http_response(client_socket, response.to_bytes())

    def _dispatch(self, request: HTTPRequest) -> HTTPResponse:
        if request.path.startswith("/static/"):
            return serve_static(request)

        handler = self.router.resolve(request.method, request.path)
        if handler is None:
            return HTTPResponse(status_code=404, body="Not Found")
        return handler(request)


if __name__ == "__main__":
    server = HTTPServer()
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()
