"""Main HTTP server entry point and connection lifecycle orchestration."""

import socket
from typing import Final

from config import HOST, PORT
from router import Router
from socket_handler import write_http_response

BOOTSTRAP_RESPONSE: Final[bytes] = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"


class HTTPServer:
    def __init__(self, host: str = HOST, port: int = PORT, router: Router | None = None) -> None:
        self.host = host
        self.port = port
        self.router = router or Router()
        self._server_socket: socket.socket | None = None
        self._running = False

    def start(self) -> None:
        """Start listening for one client connection (P01 baseline)."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
            self._server_socket = server_socket
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind((self.host, self.port))
            server_socket.listen(5)
            self.port = server_socket.getsockname()[1]

            self._running = True
            client_socket, _address = server_socket.accept()
            self._handle_client(client_socket)
            self._running = False

    def stop(self) -> None:
        self._running = False
        if self._server_socket is not None:
            self._server_socket.close()
            self._server_socket = None

    def _handle_client(self, client_socket: socket.socket) -> None:
        with client_socket:
            write_http_response(client_socket, BOOTSTRAP_RESPONSE)
