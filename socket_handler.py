"""Low-level socket read/write utilities."""

import socket


def read_http_request(client_socket: socket.socket) -> bytes:
    raise NotImplementedError("Implemented in phase P02")


def write_http_response(client_socket: socket.socket, payload: bytes) -> None:
    """Write the complete response payload to a client socket."""
    client_socket.sendall(payload)
