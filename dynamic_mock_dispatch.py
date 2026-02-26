"""Dynamic dispatch for API playground mock routes."""

from __future__ import annotations

from request import HTTPRequest
from response import HTTPResponse

RESERVED_PREFIXES = (
    "/static/",
    "/api/mocks",
    "/api/history",
    "/api/replay",
    "/api/playground",
)
RESERVED_EXACT_PATHS = {
    "/",
    "/submit",
    "/stream",
    "/_metrics",
    "/playground",
}


ALLOWED_MOCK_METHODS = {"GET", "POST", "PUT", "DELETE"}


def is_reserved_path(path: str) -> bool:
    if path in RESERVED_EXACT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in RESERVED_PREFIXES)


def validate_mock_payload(
    payload: dict[str, object],
    *,
    max_mock_body_bytes: int,
) -> tuple[bool, str]:
    method = str(payload.get("method", "")).upper()
    if method not in ALLOWED_MOCK_METHODS:
        return False, "method must be one of GET, POST, PUT, DELETE"

    path_pattern = str(payload.get("path_pattern", ""))
    if not path_pattern.startswith("/"):
        return False, "path_pattern must start with /"

    if is_reserved_path(path_pattern):
        return False, "path_pattern is reserved"

    status = payload.get("status", 200)
    try:
        status_code = int(status)
    except (TypeError, ValueError):
        return False, "status must be an integer"

    if status_code < 100 or status_code > 599:
        return False, "status must be between 100 and 599"

    headers = payload.get("headers", {})
    if headers is None:
        headers = {}
    if not isinstance(headers, dict):
        return False, "headers must be an object"

    body = str(payload.get("body", ""))
    if len(body.encode("utf-8")) > max_mock_body_bytes:
        return False, "body exceeds max mock body size"

    content_type = payload.get("content_type", "application/json")
    if not isinstance(content_type, str) or not content_type.strip():
        return False, "content_type must be a non-empty string"

    return True, ""


def response_from_mock(request: HTTPRequest, mock: dict[str, object]) -> HTTPResponse:
    _ = request
    headers = dict(mock.get("headers", {}))
    headers.setdefault("Content-Type", str(mock.get("content_type", "application/json")))
    return HTTPResponse(
        status_code=int(mock.get("status", 200)),
        headers=headers,
        body=str(mock.get("body", "")),
    )
