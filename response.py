"""HTTP response model and serializer."""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from email.utils import formatdate
from pathlib import Path

from config import SERVER_NAME

REASON_PHRASES: dict[int, str] = {
    100: "Continue",
    200: "OK",
    304: "Not Modified",
    408: "Request Timeout",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Payload Too Large",
    414: "URI Too Long",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    501: "Not Implemented",
    503: "Service Unavailable",
    505: "HTTP Version Not Supported",
}


@dataclass(slots=True)
class PreparedResponse:
    head: bytes
    body: bytes | None = None
    stream: Iterator[bytes] | None = None
    file_path: Path | None = None


@dataclass(slots=True)
class HTTPResponse:
    status_code: int
    reason_phrase: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | str = b""
    stream: Iterable[bytes] | None = None
    file_path: Path | None = None
    should_close: bool = False
    content_length_override: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.body, str):
            self.body = self.body.encode("utf-8")
        if self.stream is not None and self.file_path is not None:
            raise ValueError("Response cannot set both stream and file_path")
        if self.file_path is not None and self.body:
            raise ValueError("Response cannot set both body and file_path")

    def to_bytes(self) -> bytes:
        """Serialize the response into HTTP/1.1 wire format bytes."""
        prepared = prepare_response(self)
        payload = bytearray(prepared.head)
        if prepared.body is not None:
            payload.extend(prepared.body)
            return bytes(payload)

        if prepared.stream is not None:
            for encoded_chunk in iter_chunked_encoded(prepared.stream):
                payload.extend(encoded_chunk)
            return bytes(payload)

        if prepared.file_path is not None:
            payload.extend(prepared.file_path.read_bytes())
            return bytes(payload)

        return bytes(payload)


def prepare_response(response: HTTPResponse) -> PreparedResponse:
    reason = response.reason_phrase or REASON_PHRASES.get(response.status_code, "Unknown")
    normalized_headers = dict(response.headers)
    normalized_headers.setdefault(
        "Date",
        formatdate(timeval=None, localtime=False, usegmt=True),
    )
    normalized_headers.setdefault("Server", SERVER_NAME)
    normalized_headers.setdefault("Content-Type", "text/plain; charset=utf-8")

    body: bytes | None = None
    stream: Iterator[bytes] | None = None
    file_path: Path | None = None
    if response.stream is not None:
        normalized_headers.pop("Content-Length", None)
        normalized_headers["Transfer-Encoding"] = "chunked"
        stream = iter(response.stream)
    elif response.file_path is not None:
        file_path = response.file_path
        file_size = file_path.stat().st_size
        content_length = response.content_length_override
        if content_length is None:
            content_length = file_size
        normalized_headers["Content-Length"] = str(content_length)
    else:
        body = response.body
        content_length = response.content_length_override
        if content_length is None:
            content_length = len(body)
        normalized_headers["Content-Length"] = str(content_length)

    header_lines = [f"HTTP/1.1 {response.status_code} {reason}"]
    header_lines.extend(f"{key}: {value}" for key, value in normalized_headers.items())
    head = "\r\n".join(header_lines).encode("iso-8859-1") + b"\r\n\r\n"
    return PreparedResponse(head=head, body=body, stream=stream, file_path=file_path)


def iter_chunked_encoded(chunks: Iterable[bytes]) -> Iterator[bytes]:
    for chunk in chunks:
        yield f"{len(chunk):X}\r\n".encode("ascii")
        yield chunk
        yield b"\r\n"
    yield b"0\r\n\r\n"
