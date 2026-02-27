"""In-memory bearer session store with TTL-based expiration."""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any


class SessionStore:
    def __init__(self, *, ttl_minutes: int) -> None:
        self._ttl_secs = max(60, ttl_minutes * 60)
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}

    def create(self, *, username: str, role: str) -> dict[str, Any]:
        now = int(time.time())
        expires_at = now + self._ttl_secs
        token = secrets.token_urlsafe(32)
        session = {
            "token": token,
            "username": username,
            "role": role,
            "created_at": now,
            "expires_at": expires_at,
        }
        with self._lock:
            self._sessions[token] = session
        return dict(session)

    def get(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        now = int(time.time())
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if int(session.get("expires_at", 0)) <= now:
                self._sessions.pop(token, None)
                return None
            return dict(session)

    def revoke(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            return self._sessions.pop(token, None) is not None

    def prune_expired(self) -> int:
        now = int(time.time())
        removed = 0
        with self._lock:
            for token in list(self._sessions.keys()):
                session = self._sessions[token]
                if int(session.get("expires_at", 0)) <= now:
                    self._sessions.pop(token, None)
                    removed += 1
        return removed
