"""HTTP response model and serializer."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from email.utils import formatdate

from config import SERVER_NAME

REASON_PHRASES: dict[int, str] = {
    200: "OK",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    503: "Service Unavailable",
    413: "Payload Too Large",
    500: "Internal Server Error",
}


@dataclass(slots=True)
class HTTPResponse:
    status_code: int
    reason_phrase: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | str = b""
    stream: Iterable[bytes] | None = None
    should_close: bool = False
    content_length_override: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.body, str):
            self.body = self.body.encode("utf-8")

    def to_bytes(self) -> bytes:
        """Serialize the response into HTTP/1.1 wire format bytes."""
        reason = self.reason_phrase or REASON_PHRASES.get(self.status_code, "Unknown")
        normalized_headers = dict(self.headers)

        normalized_headers.setdefault(
            "Date",
            formatdate(timeval=None, localtime=False, usegmt=True),
        )
        normalized_headers.setdefault("Server", SERVER_NAME)
        normalized_headers.setdefault("Content-Type", "text/plain; charset=utf-8")
        payload: bytes
        if self.stream is not None:
            if self.body:
                raise ValueError("Response cannot set both body and stream")
            payload = _encode_chunked_stream(self.stream)
            normalized_headers.pop("Content-Length", None)
            normalized_headers["Transfer-Encoding"] = "chunked"
        else:
            payload = self.body
            content_length = self.content_length_override
            if content_length is None:
                content_length = len(payload)
            normalized_headers["Content-Length"] = str(content_length)

        header_lines = [f"HTTP/1.1 {self.status_code} {reason}"]
        header_lines.extend(f"{key}: {value}" for key, value in normalized_headers.items())

        head = "\r\n".join(header_lines).encode("iso-8859-1")
        return head + b"\r\n\r\n" + payload


def _encode_chunked_stream(chunks: Iterable[bytes]) -> bytes:
    payload = bytearray()
    for chunk in chunks:
        payload.extend(f"{len(chunk):X}\r\n".encode("ascii"))
        payload.extend(chunk)
        payload.extend(b"\r\n")
    payload.extend(b"0\r\n\r\n")
    return bytes(payload)
