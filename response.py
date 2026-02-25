"""HTTP response model and serializer."""

from dataclasses import dataclass, field

REASON_PHRASES: dict[int, str] = {
    200: "OK",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Payload Too Large",
    500: "Internal Server Error",
}


@dataclass(slots=True)
class HTTPResponse:
    status_code: int
    reason_phrase: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | str = b""

    def __post_init__(self) -> None:
        if isinstance(self.body, str):
            self.body = self.body.encode("utf-8")

    def to_bytes(self) -> bytes:
        """Serialize the response into HTTP/1.1 wire format bytes."""
        reason = self.reason_phrase or REASON_PHRASES.get(self.status_code, "Unknown")
        normalized_headers = dict(self.headers)

        normalized_headers.setdefault("Content-Type", "text/plain; charset=utf-8")
        normalized_headers["Content-Length"] = str(len(self.body))

        header_lines = [f"HTTP/1.1 {self.status_code} {reason}"]
        header_lines.extend(f"{key}: {value}" for key, value in normalized_headers.items())

        head = "\r\n".join(header_lines).encode("iso-8859-1")
        return head + b"\r\n\r\n" + self.body
