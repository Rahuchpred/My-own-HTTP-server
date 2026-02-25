"""HTTP response model and serializer."""

from dataclasses import dataclass, field


@dataclass(slots=True)
class HTTPResponse:
    status_code: int
    reason_phrase: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def to_bytes(self) -> bytes:
        raise NotImplementedError("Implemented in phase P03")
