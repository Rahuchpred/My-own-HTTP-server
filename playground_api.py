"""HTTP handlers for API playground endpoints."""

from __future__ import annotations

import json
import uuid
from typing import Callable

from dynamic_mock_dispatch import validate_mock_payload
from playground_store import PlaygroundStore
from request import HTTPRequest
from response import HTTPResponse

DispatchFn = Callable[[HTTPRequest], HTTPResponse]


class PlaygroundAPI:
    def __init__(
        self,
        *,
        store: PlaygroundStore,
        max_mock_body_bytes: int,
        dispatch_request: DispatchFn,
    ) -> None:
        self._store = store
        self._max_mock_body_bytes = max_mock_body_bytes
        self._dispatch_request = dispatch_request

    def handle(self, request: HTTPRequest) -> HTTPResponse:
        if request.path == "/api/playground/state" and request.method == "GET":
            return self._ok(self._store.snapshot())

        if request.path == "/api/mocks":
            if request.method == "GET":
                return self._ok({"mocks": self._store.list_mocks()})
            if request.method == "POST":
                return self._create_mock(request)
            return self._method_not_allowed("GET, POST")

        if request.path.startswith("/api/mocks/"):
            mock_id = request.path.removeprefix("/api/mocks/")
            if not mock_id:
                return self._error(404, "not_found", "mock not found")
            if request.method == "PUT":
                return self._update_mock(mock_id, request)
            if request.method == "DELETE":
                deleted = self._store.delete_mock(mock_id)
                if not deleted:
                    return self._error(404, "not_found", "mock not found")
                return self._ok({"deleted": True, "id": mock_id})
            return self._method_not_allowed("PUT, DELETE")

        if request.path == "/api/history":
            if request.method != "GET":
                return self._method_not_allowed("GET")
            return self._history(request)

        if request.path.startswith("/api/replay/"):
            if request.method != "POST":
                return self._method_not_allowed("POST")
            request_id = request.path.removeprefix("/api/replay/")
            return self._replay(request_id)

        if request.path == "/api/playground/request":
            if request.method != "POST":
                return self._method_not_allowed("POST")
            return self._execute_request(request)

        return self._error(404, "not_found", "endpoint not found")

    def _create_mock(self, request: HTTPRequest) -> HTTPResponse:
        payload, error = self._read_json_body(request)
        if error is not None:
            return error

        payload["id"] = str(payload.get("id") or uuid.uuid4().hex[:10])
        ok, message = validate_mock_payload(payload, max_mock_body_bytes=self._max_mock_body_bytes)
        if not ok:
            status = 403 if "reserved" in message else 400
            return self._error(status, "invalid_mock", message)

        try:
            created = self._store.add_mock(payload)
        except ValueError as exc:
            reason = str(exc)
            if reason == "duplicate_method_path":
                return self._error(409, "duplicate_mock", "method/path already exists")
            if reason == "max_mock_routes_exceeded":
                return self._error(413, "mock_limit", "max mock routes reached")
            if reason == "mock_id_exists":
                return self._error(409, "duplicate_mock", "mock id already exists")
            return self._error(400, "invalid_mock", "unable to create mock")

        return self._json(201, {"mock": created})

    def _update_mock(self, mock_id: str, request: HTTPRequest) -> HTTPResponse:
        payload, error = self._read_json_body(request)
        if error is not None:
            return error

        if "method" in payload or "path_pattern" in payload or "status" in payload:
            to_validate = dict(payload)
            existing = self._store.get_mock(mock_id)
            if existing is None:
                return self._error(404, "not_found", "mock not found")
            merged = dict(existing)
            merged.update(to_validate)
            ok, message = validate_mock_payload(
                merged,
                max_mock_body_bytes=self._max_mock_body_bytes,
            )
            if not ok:
                status = 403 if "reserved" in message else 400
                return self._error(status, "invalid_mock", message)

        try:
            updated = self._store.update_mock(mock_id, payload)
        except ValueError as exc:
            if str(exc) == "duplicate_method_path":
                return self._error(409, "duplicate_mock", "method/path already exists")
            return self._error(400, "invalid_mock", "unable to update mock")

        if updated is None:
            return self._error(404, "not_found", "mock not found")

        return self._ok({"mock": updated})

    def _history(self, request: HTTPRequest) -> HTTPResponse:
        records = self._store.list_history()
        filters = {
            "method": _first_query_value(request, "method"),
            "path": _first_query_value(request, "path"),
            "status": _first_query_value(request, "status"),
            "from_ts": _first_query_value(request, "from_ts"),
            "to_ts": _first_query_value(request, "to_ts"),
        }

        filtered = []
        for item in records:
            if (
                filters["method"]
                and str(item.get("method", "")).upper() != filters["method"].upper()
            ):
                continue
            if filters["path"] and filters["path"] not in str(item.get("path", "")):
                continue
            if filters["status"] and str(item.get("status", "")) != filters["status"]:
                continue
            ts = str(item.get("ts", ""))
            if filters["from_ts"] and ts < filters["from_ts"]:
                continue
            if filters["to_ts"] and ts > filters["to_ts"]:
                continue
            filtered.append(item)

        return self._ok({"history": filtered})

    def _replay(self, request_id: str) -> HTTPResponse:
        item = self._store.find_history(request_id)
        if item is None:
            return self._error(404, "not_found", "request id not found")

        replay_input = dict(item.get("replay_input", {}))
        if not replay_input:
            return self._error(400, "not_replayable", "request has no replay input")

        replay_request = HTTPRequest(
            method=str(replay_input.get("method", "GET")),
            path=str(replay_input.get("path", "/")),
            raw_target=str(replay_input.get("raw_target", replay_input.get("path", "/"))),
            http_version=str(replay_input.get("http_version", "HTTP/1.1")),
            headers=dict(replay_input.get("headers", {"host": "localhost"})),
            body=str(replay_input.get("body", "")).encode("utf-8"),
            query_params={},
            keep_alive=False,
            transfer_encoding=None,
        )
        response = self._dispatch_request(replay_request)
        body_preview = _decode_preview(response.body)
        return self._ok(
            {
                "replayed_request_id": request_id,
                "result": {
                    "status": response.status_code,
                    "headers": dict(response.headers),
                    "body_preview": body_preview,
                },
            }
        )

    def _execute_request(self, request: HTTPRequest) -> HTTPResponse:
        payload, error = self._read_json_body(request)
        if error is not None:
            return error

        method = str(payload.get("method", "GET")).upper()
        path = str(payload.get("path", "/"))
        if not path.startswith("/"):
            return self._error(400, "invalid_request", "path must start with /")

        headers = payload.get("headers", {}) or {}
        if not isinstance(headers, dict):
            return self._error(400, "invalid_request", "headers must be an object")

        body_text = str(payload.get("body", ""))
        body_bytes = body_text.encode("utf-8")
        if len(body_bytes) > self._max_mock_body_bytes:
            return self._error(413, "invalid_request", "request body too large")

        normalized_headers = {str(k).lower(): str(v) for k, v in headers.items()}
        normalized_headers.setdefault("host", "localhost")
        replay_request = HTTPRequest(
            method=method,
            path=path,
            raw_target=path,
            http_version="HTTP/1.1",
            headers=normalized_headers,
            body=body_bytes,
            query_params={},
            keep_alive=False,
            transfer_encoding=None,
        )
        response = self._dispatch_request(replay_request)
        body_preview = _decode_preview(response.body)
        return self._ok(
            {
                "request": {
                    "method": method,
                    "path": path,
                },
                "response": {
                    "status": response.status_code,
                    "headers": dict(response.headers),
                    "body": body_preview,
                },
            }
        )

    def _read_json_body(
        self,
        request: HTTPRequest,
    ) -> tuple[dict[str, object], HTTPResponse | None]:
        try:
            decoded = request.body.decode("utf-8") if request.body else "{}"
            payload = json.loads(decoded)
        except UnicodeDecodeError:
            return {}, self._error(400, "invalid_json", "body must be utf-8")
        except json.JSONDecodeError:
            return {}, self._error(400, "invalid_json", "body must be valid json")

        if not isinstance(payload, dict):
            return {}, self._error(400, "invalid_json", "top-level json must be an object")
        return payload, None

    def _ok(self, payload: dict[str, object]) -> HTTPResponse:
        return self._json(200, payload)

    def _json(self, status: int, payload: dict[str, object]) -> HTTPResponse:
        return HTTPResponse(
            status_code=status,
            headers={"Content-Type": "application/json"},
            body=json.dumps(payload, sort_keys=True),
        )

    def _error(self, status: int, code: str, message: str) -> HTTPResponse:
        return self._json(status, {"error": {"code": code, "message": message}})

    def _method_not_allowed(self, allowed: str) -> HTTPResponse:
        body = {
            "error": {
                "code": "method_not_allowed",
                "message": "method not allowed",
            }
        }
        return HTTPResponse(
            status_code=405,
            headers={"Allow": allowed, "Content-Type": "application/json"},
            body=json.dumps(body),
        )


def _first_query_value(request: HTTPRequest, key: str) -> str:
    values = request.query_params.get(key, [])
    if not values:
        return ""
    return values[0]


def _decode_preview(body: bytes | str) -> str:
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return body
