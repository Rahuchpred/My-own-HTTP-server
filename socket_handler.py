"""Low-level socket read/write utilities."""

import socket

from config import BUFFER_SIZE, MAX_BODY_BYTES, MAX_HEADER_BYTES, MAX_REQUEST_BYTES


class HTTPReadError(Exception):
    """Raised when a client request cannot be safely read from the socket."""


class MalformedRequestError(HTTPReadError):
    """Raised when socket bytes do not form a complete HTTP request."""


class HeaderTooLargeError(HTTPReadError):
    """Raised when HTTP headers exceed configured maximum size."""


class PayloadTooLargeError(HTTPReadError):
    """Raised when request body exceeds configured maximum size."""


class SocketTimeoutError(HTTPReadError):
    """Raised when a client times out while sending request bytes."""


def _extract_content_length(header_bytes: bytes) -> int:
    headers = header_bytes.decode("iso-8859-1").split("\r\n")
    for line in headers[1:]:
        if not line:
            continue
        if ":" not in line:
            raise MalformedRequestError("Malformed header while reading request")
        name, value = line.split(":", 1)
        if name.strip().lower() == "content-length":
            try:
                parsed_length = int(value.strip())
            except ValueError as exc:
                raise MalformedRequestError("Invalid Content-Length header") from exc
            if parsed_length < 0:
                raise MalformedRequestError("Negative Content-Length header")
            return parsed_length
    return 0


def read_http_request_message(
    client_socket: socket.socket,
    initial_buffer: bytes = b"",
) -> tuple[bytes, bytes]:
    """Read one HTTP/1.1 request and return (request_bytes, leftover_bytes)."""
    buffer = bytearray(initial_buffer)
    header_end_index = -1
    expected_body_length = 0

    while True:
        if header_end_index == -1:
            header_end_index = buffer.find(b"\r\n\r\n")
            if header_end_index != -1:
                header_section_length = header_end_index + 4
                if header_section_length > MAX_HEADER_BYTES:
                    raise HeaderTooLargeError("Headers exceeded MAX_HEADER_BYTES")
                expected_body_length = _extract_content_length(bytes(buffer[:header_end_index]))
                if expected_body_length > MAX_BODY_BYTES:
                    raise PayloadTooLargeError("Body exceeded MAX_BODY_BYTES")

        if header_end_index != -1:
            request_length = header_end_index + 4 + expected_body_length
            if len(buffer) >= request_length:
                request_bytes = bytes(buffer[:request_length])
                leftover_bytes = bytes(buffer[request_length:])
                return request_bytes, leftover_bytes

        if len(buffer) > MAX_REQUEST_BYTES:
            raise PayloadTooLargeError("Request exceeded MAX_REQUEST_BYTES")
        if header_end_index == -1 and len(buffer) > MAX_HEADER_BYTES:
            raise HeaderTooLargeError("Headers exceeded MAX_HEADER_BYTES")

        try:
            chunk = client_socket.recv(BUFFER_SIZE)
        except socket.timeout as exc:
            raise SocketTimeoutError("Timed out waiting for request bytes") from exc

        if not chunk:
            if not buffer:
                return b"", b""
            raise MalformedRequestError("Connection closed before request completed")

        buffer.extend(chunk)


def read_http_request(client_socket: socket.socket) -> bytes:
    """Compatibility helper to read a single request without carry-over."""
    request_bytes, _leftover = read_http_request_message(client_socket)
    return request_bytes


def write_http_response(client_socket: socket.socket, payload: bytes) -> None:
    """Write the complete response payload to a client socket."""
    client_socket.sendall(payload)
