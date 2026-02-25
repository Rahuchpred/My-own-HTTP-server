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


def _extract_transfer_encoding(header_bytes: bytes) -> str | None:
    headers = header_bytes.decode("iso-8859-1").split("\r\n")
    for line in headers[1:]:
        if not line:
            continue
        if ":" not in line:
            raise MalformedRequestError("Malformed header while reading request")
        name, value = line.split(":", 1)
        if name.strip().lower() == "transfer-encoding":
            return value.strip().lower()
    return None


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


def _has_content_length_header(header_bytes: bytes) -> bool:
    headers = header_bytes.decode("iso-8859-1").split("\r\n")
    for line in headers[1:]:
        if not line:
            continue
        if ":" not in line:
            raise MalformedRequestError("Malformed header while reading request")
        name, _value = line.split(":", 1)
        if name.strip().lower() == "content-length":
            return True
    return False


def _chunked_body_complete_length(encoded_body: bytes) -> int | None:
    position = 0
    decoded_size = 0
    while True:
        line_end = encoded_body.find(b"\r\n", position)
        if line_end == -1:
            return None
        size_token = encoded_body[position:line_end].split(b";", 1)[0].strip()
        if not size_token:
            raise MalformedRequestError("Missing chunk size")
        try:
            chunk_size = int(size_token, 16)
        except ValueError as exc:
            raise MalformedRequestError("Malformed chunk size") from exc
        position = line_end + 2

        if chunk_size == 0:
            while True:
                trailer_end = encoded_body.find(b"\r\n", position)
                if trailer_end == -1:
                    return None
                if trailer_end == position:
                    return trailer_end + 2
                trailer_line = encoded_body[position:trailer_end]
                if b":" not in trailer_line:
                    raise MalformedRequestError("Malformed chunked trailer")
                position = trailer_end + 2

        if len(encoded_body) < position + chunk_size + 2:
            return None
        decoded_size += chunk_size
        if decoded_size > MAX_BODY_BYTES:
            raise PayloadTooLargeError("Decoded chunked body exceeded MAX_BODY_BYTES")
        if encoded_body[position + chunk_size : position + chunk_size + 2] != b"\r\n":
            raise MalformedRequestError("Chunk missing CRLF terminator")
        position += chunk_size + 2


def read_http_request_message(
    client_socket: socket.socket,
    initial_buffer: bytes = b"",
) -> tuple[bytes, bytes]:
    """Read one HTTP/1.1 request and return (request_bytes, leftover_bytes)."""
    buffer = bytearray(initial_buffer)
    header_end_index = -1
    expected_body_length = 0
    uses_chunked_transfer = False

    while True:
        if header_end_index == -1:
            header_end_index = buffer.find(b"\r\n\r\n")
            if header_end_index != -1:
                header_section_length = header_end_index + 4
                if header_section_length > MAX_HEADER_BYTES:
                    raise HeaderTooLargeError("Headers exceeded MAX_HEADER_BYTES")
                header_bytes = bytes(buffer[:header_end_index])
                transfer_encoding = _extract_transfer_encoding(header_bytes)
                uses_chunked_transfer = bool(
                    transfer_encoding and "chunked" in transfer_encoding
                )
                if uses_chunked_transfer:
                    expected_body_length = 0
                    if _has_content_length_header(header_bytes):
                        raise MalformedRequestError(
                            "Content-Length cannot be combined with chunked transfer"
                        )
                else:
                    expected_body_length = _extract_content_length(header_bytes)
                    if expected_body_length > MAX_BODY_BYTES:
                        raise PayloadTooLargeError("Body exceeded MAX_BODY_BYTES")

        if header_end_index != -1:
            if uses_chunked_transfer:
                body_start = header_end_index + 4
                complete_body_length = _chunked_body_complete_length(bytes(buffer[body_start:]))
                if complete_body_length is not None:
                    request_length = body_start + complete_body_length
                    request_bytes = bytes(buffer[:request_length])
                    leftover_bytes = bytes(buffer[request_length:])
                    return request_bytes, leftover_bytes
            else:
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
