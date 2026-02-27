"""Local user store with PBKDF2 password verification and role metadata."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

ROLES = {"viewer", "operator", "admin"}
_HASH_PREFIX = "pbkdf2_sha256"
_DEFAULT_ITERATIONS = 180_000


def hash_password(
    password: str,
    *,
    iterations: int = _DEFAULT_ITERATIONS,
    salt_bytes: bytes | None = None,
) -> str:
    if salt_bytes is None:
        salt_bytes = os.urandom(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt_bytes,
        max(10_000, iterations),
    )
    return f"{_HASH_PREFIX}${max(10_000, iterations)}${salt_bytes.hex()}${derived.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations_raw, salt_hex, digest_hex = password_hash.split("$", 3)
    except ValueError:
        return False
    if algorithm != _HASH_PREFIX:
        return False
    try:
        iterations = int(iterations_raw)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (TypeError, ValueError):
        return False

    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, max(10_000, iterations))
    return hmac.compare_digest(actual, expected)


class AuthStore:
    def __init__(self, *, users_file: str) -> None:
        self._users_file = Path(users_file)
        self._users: dict[str, dict[str, Any]] = {}
        self._load()

    @property
    def users_file(self) -> Path:
        return self._users_file

    @property
    def has_users(self) -> bool:
        return bool(self._users)

    def authenticate(self, username: str, password: str) -> tuple[bool, str]:
        user = self._users.get(username.strip())
        if user is None:
            return False, ""
        if not user.get("enabled", True):
            return False, ""
        password_hash = str(user.get("password_hash", ""))
        if not verify_password(password, password_hash):
            return False, ""
        role = str(user.get("role", "viewer")).strip().lower()
        if role not in ROLES:
            role = "viewer"
        return True, role

    def user_role(self, username: str) -> str:
        user = self._users.get(username.strip())
        if user is None:
            return ""
        role = str(user.get("role", "viewer")).strip().lower()
        return role if role in ROLES else "viewer"

    def _load(self) -> None:
        self._users = {}
        if not self._users_file.exists():
            return
        try:
            payload = json.loads(self._users_file.read_text(encoding="utf-8"))
        except Exception:
            return
        users = payload.get("users", [])
        if not isinstance(users, list):
            return
        for raw in users:
            if not isinstance(raw, dict):
                continue
            username = str(raw.get("username", "")).strip()
            password_hash = str(raw.get("password_hash", "")).strip()
            role = str(raw.get("role", "viewer")).strip().lower()
            if not username or not password_hash or role not in ROLES:
                continue
            self._users[username] = {
                "username": username,
                "password_hash": password_hash,
                "role": role,
                "enabled": bool(raw.get("enabled", True)),
            }
