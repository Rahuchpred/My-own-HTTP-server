"""Low-level socket read/write utilities."""

from __future__ import annotations

import os
import socket
import ssl
from dataclasses import dataclass
from typing import Callable

from config import (
    BUFFER_SIZE,
    MAX_BODY_BYTES,
    MAX_HEADER_BYTES,
    MAX_REQUEST_BYTES,
    READ_CHUNK_SIZE,
    WRITE_CHUNK_SIZE,
)
from response import HTTPResponse, iter_chunked_encoded, prepare_response


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


@dataclass(slots=True)
class RequestHeadInfo:
    header_end_index: int
    expected_body_length: int
    uses_chunked_transfer: bool
    expect_continue: bool


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


def _has_expect_continue(header_bytes: bytes) -> bool:
    headers = header_bytes.decode("iso-8859-1").split("\r\n")
    for line in headers[1:]:
        if not line:
            continue
        if ":" not in line:
            raise MalformedRequestError("Malformed header while reading request")
        name, value = line.split(":", 1)
        if name.strip().lower() == "expect":
            return value.strip().lower() == "100-continue"
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


def inspect_http_request_head(buffer: bytes) -> RequestHeadInfo | None:
    """Inspect request headers from an in-memory buffer, if complete."""
    if len(buffer) > MAX_REQUEST_BYTES:
        raise PayloadTooLargeError("Request exceeded MAX_REQUEST_BYTES")

    header_end_index = buffer.find(b"\r\n\r\n")
    if header_end_index == -1:
        if len(buffer) > MAX_HEADER_BYTES:
            raise HeaderTooLargeError("Headers exceeded MAX_HEADER_BYTES")
        return None

    header_section_length = header_end_index + 4
    if header_section_length > MAX_HEADER_BYTES:
        raise HeaderTooLargeError("Headers exceeded MAX_HEADER_BYTES")

    header_bytes = bytes(buffer[:header_end_index])
    transfer_encoding = _extract_transfer_encoding(header_bytes)
    uses_chunked_transfer = bool(transfer_encoding and "chunked" in transfer_encoding)
    if uses_chunked_transfer and _has_content_length_header(header_bytes):
        raise MalformedRequestError("Content-Length cannot be combined with chunked transfer")

    expected_body_length = 0
    if not uses_chunked_transfer:
        expected_body_length = _extract_content_length(header_bytes)
        if expected_body_length > MAX_BODY_BYTES:
            raise PayloadTooLargeError("Body exceeded MAX_BODY_BYTES")

    return RequestHeadInfo(
        header_end_index=header_end_index,
        expected_body_length=expected_body_length,
        uses_chunked_transfer=uses_chunked_transfer,
        expect_continue=_has_expect_continue(header_bytes),
    )


def extract_http_request_message(buffer: bytes) -> tuple[bytes, bytes] | None:
    """Extract one complete HTTP request from a bytes buffer."""
    head_info = inspect_http_request_head(buffer)
    if head_info is None:
        return None

    body_start = head_info.header_end_index + 4
    if head_info.uses_chunked_transfer:
        complete_body_length = _chunked_body_complete_length(buffer[body_start:])
        if complete_body_length is None:
            return None
        request_length = body_start + complete_body_length
    else:
        request_length = body_start + head_info.expected_body_length
        if len(buffer) < request_length:
            return None

    request_bytes = buffer[:request_length]
    leftover = buffer[request_length:]
    return request_bytes, leftover


def read_http_request_message(
    client_socket: socket.socket,
    initial_buffer: bytes = b"",
    expect_continue_callback: Callable[[RequestHeadInfo], None] | None = None,
) -> tuple[bytes, bytes]:
    """Read one HTTP/1.1 request and return (request_bytes, leftover_bytes)."""
    buffer = bytearray(initial_buffer)
    continue_sent = False

    while True:
        extracted = extract_http_request_message(bytes(buffer))
        if extracted is not None:
            request_bytes, leftover_bytes = extracted
            return request_bytes, leftover_bytes

        if expect_continue_callback is not None and not continue_sent:
            head_info = inspect_http_request_head(bytes(buffer))
            if head_info is not None and head_info.expect_continue:
                body_start = head_info.header_end_index + 4
                body_complete = False
                if head_info.uses_chunked_transfer:
                    body_complete = (
                        _chunked_body_complete_length(bytes(buffer[body_start:]))
                        is not None
                    )
                else:
                    body_complete = len(buffer) >= body_start + head_info.expected_body_length

                if not body_complete:
                    expect_continue_callback(head_info)
                    continue_sent = True

        try:
            chunk = client_socket.recv(max(BUFFER_SIZE, READ_CHUNK_SIZE))
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


def write_http_response_message(
    client_socket: socket.socket,
    response: HTTPResponse,
    *,
    write_chunk_size: int = WRITE_CHUNK_SIZE,
) -> int:
    """Write an HTTPResponse with incremental body/file transfer."""
    prepared = prepare_response(response)
    bytes_sent = 0
    client_socket.sendall(prepared.head)
    bytes_sent += len(prepared.head)

    if prepared.body is not None:
        if prepared.body:
            client_socket.sendall(prepared.body)
            bytes_sent += len(prepared.body)
        return bytes_sent

    if prepared.stream is not None:
        for encoded_chunk in iter_chunked_encoded(prepared.stream):
            client_socket.sendall(encoded_chunk)
            bytes_sent += len(encoded_chunk)
        return bytes_sent

    if prepared.file_path is not None:
        with prepared.file_path.open("rb") as file_obj:
            file_size = prepared.file_path.stat().st_size
            remaining = file_size
            offset = 0
            can_use_sendfile = hasattr(os, "sendfile") and not isinstance(
                client_socket, ssl.SSLSocket
            )
            if can_use_sendfile:
                while remaining > 0:
                    sent = os.sendfile(
                        client_socket.fileno(),
                        file_obj.fileno(),
                        offset,
                        min(write_chunk_size, remaining),
                    )
                    if sent <= 0:
                        break
                    remaining -= sent
                    offset += sent
                    bytes_sent += sent
            else:
                while True:
                    chunk = file_obj.read(write_chunk_size)
                    if not chunk:
                        break
                    client_socket.sendall(chunk)
                    bytes_sent += len(chunk)
        return bytes_sent

    return bytes_sent
