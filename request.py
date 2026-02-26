"""HTTP request model and parser."""

from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from config import MAX_BODY_BYTES, MAX_TARGET_LENGTH

ALLOWED_HTTP_VERSIONS = {"HTTP/1.1", "HTTP/1.0"}
KNOWN_METHODS = {
    "GET",
    "HEAD",
    "POST",
    "PUT",
    "DELETE",
    "OPTIONS",
    "PATCH",
    "TRACE",
    "CONNECT",
}


class HTTPRequestParseError(ValueError):
    """Request parse error carrying an HTTP status code."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True)
class HTTPRequest:
    method: str
    path: str
    http_version: str
    raw_target: str = "/"
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    query_params: dict[str, list[str]] = field(default_factory=dict)
    keep_alive: bool = False
    transfer_encoding: str | None = None

    @classmethod
    def from_bytes(cls, raw: bytes) -> "HTTPRequest":
        """Parse raw HTTP request bytes into a structured request object."""
        try:
            header_bytes, body = raw.split(b"\r\n\r\n", 1)
        except ValueError as exc:
            raise HTTPRequestParseError("Missing CRLF CRLF request separator") from exc

        lines = header_bytes.decode("iso-8859-1").split("\r\n")
        if not lines or not lines[0]:
            raise HTTPRequestParseError("Missing request line")

        first_line_parts = lines[0].split(" ")
        if len(first_line_parts) != 3:
            raise HTTPRequestParseError("Invalid request line")

        method, target, http_version = first_line_parts
        if not method or not target or not http_version:
            raise HTTPRequestParseError("Request line contains empty tokens")

        normalized_method = method.upper()
        if normalized_method not in KNOWN_METHODS:
            raise HTTPRequestParseError("Method not implemented", status_code=501)

        if http_version not in ALLOWED_HTTP_VERSIONS:
            raise HTTPRequestParseError("Unsupported HTTP version", status_code=505)

        if len(target) > MAX_TARGET_LENGTH:
            raise HTTPRequestParseError("Request target too long", status_code=414)

        parsed_target = urlsplit(target)
        path = parsed_target.path or "/"
        query_params = parse_qs(parsed_target.query, keep_blank_values=True)

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            if ":" not in line:
                raise HTTPRequestParseError("Malformed header line")
            name, value = line.split(":", 1)
            header_name = name.strip().lower()
            if not header_name:
                raise HTTPRequestParseError("Header name cannot be empty")
            headers[header_name] = value.strip()

        if http_version == "HTTP/1.1" and "host" not in headers:
            raise HTTPRequestParseError("Host header required for HTTP/1.1")

        transfer_encoding = headers.get("transfer-encoding")
        has_chunked_transfer = bool(transfer_encoding and "chunked" in transfer_encoding.lower())
        has_content_length = "content-length" in headers
        if has_chunked_transfer and has_content_length:
            raise HTTPRequestParseError("Content-Length cannot be combined with chunked transfer")

        if has_chunked_transfer:
            body = _decode_chunked_body(body)
        elif has_content_length:
            content_length_value = headers["content-length"]
            try:
                expected_body_length = int(content_length_value)
            except ValueError as exc:
                raise HTTPRequestParseError("Invalid Content-Length") from exc

            if expected_body_length < 0:
                raise HTTPRequestParseError("Negative Content-Length is invalid")

            if len(body) != expected_body_length:
                raise HTTPRequestParseError("Body length does not match Content-Length")

        if len(body) > MAX_BODY_BYTES:
            raise HTTPRequestParseError("Decoded body exceeded MAX_BODY_BYTES", status_code=413)

        connection_header = headers.get("connection", "")
        keep_alive = _is_keep_alive(http_version, connection_header)

        return cls(
            method=normalized_method,
            path=path,
            raw_target=target,
            http_version=http_version,
            headers=headers,
            body=body,
            query_params=query_params,
            keep_alive=keep_alive,
            transfer_encoding=transfer_encoding,
        )


def _is_keep_alive(http_version: str, connection_header: str) -> bool:
    token = connection_header.lower()
    if http_version == "HTTP/1.1":
        return "close" not in token
    if http_version == "HTTP/1.0":
        return "keep-alive" in token
    return False


def _decode_chunked_body(encoded_body: bytes) -> bytes:
    position = 0
    decoded = bytearray()

    while True:
        line_end = encoded_body.find(b"\r\n", position)
        if line_end == -1:
            raise HTTPRequestParseError("Incomplete chunk size line")

        raw_size = encoded_body[position:line_end]
        size_token = raw_size.split(b";", 1)[0].strip()
        if not size_token:
            raise HTTPRequestParseError("Missing chunk size")
        try:
            chunk_size = int(size_token, 16)
        except ValueError as exc:
            raise HTTPRequestParseError("Malformed chunk size") from exc
        position = line_end + 2

        if chunk_size == 0:
            while True:
                trailer_end = encoded_body.find(b"\r\n", position)
                if trailer_end == -1:
                    raise HTTPRequestParseError("Incomplete chunked trailer section")
                if trailer_end == position:
                    if trailer_end + 2 != len(encoded_body):
                        raise HTTPRequestParseError("Unexpected bytes after chunked terminator")
                    return bytes(decoded)

                trailer_line = encoded_body[position:trailer_end]
                if b":" not in trailer_line:
                    raise HTTPRequestParseError("Malformed trailer header")
                position = trailer_end + 2

        chunk_end = position + chunk_size
        if chunk_end + 2 > len(encoded_body):
            raise HTTPRequestParseError("Incomplete chunk data")
        decoded.extend(encoded_body[position:chunk_end])
        if len(decoded) > MAX_BODY_BYTES:
            raise HTTPRequestParseError("Decoded body exceeded MAX_BODY_BYTES", status_code=413)
        if encoded_body[chunk_end : chunk_end + 2] != b"\r\n":
            raise HTTPRequestParseError("Chunk missing CRLF terminator")
        position = chunk_end + 2
