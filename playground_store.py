"""Persistent in-memory store for API playground mocks and request history."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class PlaygroundStore:
    def __init__(
        self,
        *,
        state_file: str,
        history_limit: int,
        max_mock_routes: int,
    ) -> None:
        self._state_file = Path(state_file)
        self._history_limit = max(1, history_limit)
        self._max_mock_routes = max(1, max_mock_routes)
        self._lock = threading.Lock()
        self._version = 1
        self._mocks: dict[str, dict[str, Any]] = {}
        self._history: list[dict[str, Any]] = []
        self._load_state()

    def list_mocks(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._mocks.values()]

    def get_mock(self, mock_id: str) -> dict[str, Any] | None:
        with self._lock:
            mock = self._mocks.get(mock_id)
            if mock is None:
                return None
            return dict(mock)

    def find_mock(self, *, method: str, path: str) -> dict[str, Any] | None:
        method_token = method.upper()
        with self._lock:
            for mock in self._mocks.values():
                if mock["method"] == method_token and mock["path_pattern"] == path:
                    return dict(mock)
        return None

    def add_mock(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = _utc_now()
        with self._lock:
            if len(self._mocks) >= self._max_mock_routes:
                raise ValueError("max_mock_routes_exceeded")

            method = str(payload["method"]).upper()
            path_pattern = str(payload["path_pattern"])
            self._ensure_unique_locked(method=method, path_pattern=path_pattern)

            mock_id = str(payload["id"])
            if mock_id in self._mocks:
                raise ValueError("mock_id_exists")

            normalized = {
                "id": mock_id,
                "method": method,
                "path_pattern": path_pattern,
                "status": int(payload["status"]),
                "headers": dict(payload.get("headers", {})),
                "body": str(payload.get("body", "")),
                "content_type": str(payload.get("content_type", "application/json")),
                "created_at": now,
                "updated_at": now,
            }
            self._mocks[mock_id] = normalized
            self._save_state_locked()
            return dict(normalized)

    def update_mock(self, mock_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            existing = self._mocks.get(mock_id)
            if existing is None:
                return None

            method = str(payload.get("method", existing["method"])).upper()
            path_pattern = str(payload.get("path_pattern", existing["path_pattern"]))
            self._ensure_unique_locked(method=method, path_pattern=path_pattern, ignore_id=mock_id)

            existing["method"] = method
            existing["path_pattern"] = path_pattern
            existing["status"] = int(payload.get("status", existing["status"]))
            existing["headers"] = dict(payload.get("headers", existing["headers"]))
            existing["body"] = str(payload.get("body", existing["body"]))
            existing["content_type"] = str(payload.get("content_type", existing["content_type"]))
            existing["updated_at"] = _utc_now()
            self._save_state_locked()
            return dict(existing)

    def delete_mock(self, mock_id: str) -> bool:
        with self._lock:
            deleted = self._mocks.pop(mock_id, None)
            if deleted is None:
                return False
            self._save_state_locked()
            return True

    def add_history(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._history.append(dict(record))
            if len(self._history) > self._history_limit:
                del self._history[: len(self._history) - self._history_limit]
            self._save_state_locked()

    def list_history(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._history]

    def find_history(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            for item in reversed(self._history):
                if item.get("request_id") == request_id:
                    return dict(item)
        return None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": self._version,
                "mocks": [dict(item) for item in self._mocks.values()],
                "history": [dict(item) for item in self._history],
            }

    def _ensure_unique_locked(
        self,
        *,
        method: str,
        path_pattern: str,
        ignore_id: str | None = None,
    ) -> None:
        for item in self._mocks.values():
            if ignore_id is not None and item["id"] == ignore_id:
                continue
            if item["method"] == method and item["path_pattern"] == path_pattern:
                raise ValueError("duplicate_method_path")

    def _load_state(self) -> None:
        if not self._state_file.exists():
            return

        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception:
            self._backup_corrupt_file()
            return

        mocks_raw = data.get("mocks", [])
        history_raw = data.get("history", [])
        if not isinstance(mocks_raw, list) or not isinstance(history_raw, list):
            self._backup_corrupt_file()
            return

        for mock in mocks_raw:
            if not isinstance(mock, dict):
                continue
            mock_id = str(mock.get("id", ""))
            method = str(mock.get("method", "")).upper()
            path_pattern = str(mock.get("path_pattern", ""))
            if not mock_id or not method or not path_pattern:
                continue
            self._mocks[mock_id] = {
                "id": mock_id,
                "method": method,
                "path_pattern": path_pattern,
                "status": int(mock.get("status", 200)),
                "headers": dict(mock.get("headers", {})),
                "body": str(mock.get("body", "")),
                "content_type": str(mock.get("content_type", "application/json")),
                "created_at": str(mock.get("created_at", _utc_now())),
                "updated_at": str(mock.get("updated_at", _utc_now())),
            }

        self._history = [item for item in history_raw if isinstance(item, dict)]
        if len(self._history) > self._history_limit:
            self._history = self._history[-self._history_limit :]

    def _save_state_locked(self) -> None:
        payload = {
            "version": self._version,
            "mocks": list(self._mocks.values()),
            "history": self._history,
        }
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._state_file.with_suffix(self._state_file.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self._state_file)

    def _backup_corrupt_file(self) -> None:
        timestamp = int(time.time())
        backup = self._state_file.with_suffix(self._state_file.suffix + f".corrupt.{timestamp}")
        try:
            self._state_file.replace(backup)
        except OSError:
            pass


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
