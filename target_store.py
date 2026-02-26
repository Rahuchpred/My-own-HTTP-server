"""Persistent store for named proxy targets."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class TargetStore:
    def __init__(self, *, state_file: str, max_targets: int) -> None:
        self._state_file = Path(state_file)
        self._max_targets = max(1, max_targets)
        self._lock = threading.Lock()
        self._targets: dict[str, dict[str, Any]] = {}
        self._load()

    def list_targets(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._targets.values()]

    def get_target(self, target_id: str) -> dict[str, Any] | None:
        with self._lock:
            target = self._targets.get(target_id)
            return None if target is None else dict(target)

    def create_target(self, target: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if len(self._targets) >= self._max_targets:
                raise ValueError("max_targets_exceeded")

            target_id = str(target["id"])
            if target_id in self._targets:
                raise ValueError("target_id_exists")

            if self._name_exists_locked(str(target["name"])):
                raise ValueError("target_name_exists")

            self._targets[target_id] = dict(target)
            self._save_locked()
            return dict(self._targets[target_id])

    def update_target(self, target_id: str, target: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            if target_id not in self._targets:
                return None

            if self._name_exists_locked(str(target["name"]), ignore_id=target_id):
                raise ValueError("target_name_exists")

            self._targets[target_id] = dict(target)
            self._save_locked()
            return dict(self._targets[target_id])

    def delete_target(self, target_id: str) -> bool:
        with self._lock:
            if target_id not in self._targets:
                return False
            del self._targets[target_id]
            self._save_locked()
            return True

    def _name_exists_locked(self, name: str, *, ignore_id: str | None = None) -> bool:
        normalized = name.strip().lower()
        for target in self._targets.values():
            if ignore_id is not None and target.get("id") == ignore_id:
                continue
            if str(target.get("name", "")).strip().lower() == normalized:
                return True
        return False

    def _load(self) -> None:
        if not self._state_file.exists():
            return
        try:
            payload = json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception:
            self._backup_corrupt()
            return

        targets = payload.get("targets", [])
        if not isinstance(targets, list):
            self._backup_corrupt()
            return

        for target in targets:
            if not isinstance(target, dict):
                continue
            target_id = str(target.get("id", "")).strip()
            if not target_id:
                continue
            self._targets[target_id] = target

    def _save_locked(self) -> None:
        payload = {"version": 1, "targets": list(self._targets.values())}
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._state_file.with_suffix(self._state_file.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self._state_file)

    def _backup_corrupt(self) -> None:
        ts = int(time.time())
        backup = self._state_file.with_suffix(self._state_file.suffix + f".corrupt.{ts}")
        try:
            self._state_file.replace(backup)
        except OSError:
            pass


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
