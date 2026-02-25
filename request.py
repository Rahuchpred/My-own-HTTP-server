"""HTTP request model and parser."""

from dataclasses import dataclass, field


@dataclass(slots=True)
class HTTPRequest:
    method: str
    path: str
    http_version: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    query_params: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "HTTPRequest":
        raise NotImplementedError("Implemented in phase P02")
