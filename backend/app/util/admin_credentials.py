"""Local operator password overrides; changing a password never restarts trading."""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import secrets
from typing import Callable

from app.util.auth_state import load_state, save_state

SCHEME = "pbkdf2_sha256"
ITERATIONS = 600_000


def hash_password(password: str) -> str:
    if not 14 <= len(password) <= 1024 or any(ord(c) < 32 for c in password):
        raise ValueError("Use 14–1024 characters without control characters")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
    return f"{SCHEME}${ITERATIONS}${salt}${digest.hex()}"


def is_password_hash(value: str) -> bool:
    parts = value.split("$")
    if len(parts) != 4 or parts[0] != SCHEME or parts[1] != str(ITERATIONS):
        return False
    try:
        return len(parts[2]) == 32 and len(parts[3]) == 64 and len(bytes.fromhex(parts[2])) == 16 and len(bytes.fromhex(parts[3])) == 32
    except ValueError:
        return False


def password_matches(given: str, stored: str) -> bool:
    if not is_password_hash(stored):
        return hmac.compare_digest(given.encode(), stored.encode())
    if len(given) > 1024:
        return False
    _, rounds, salt, expected = stored.split("$")
    actual = hashlib.pbkdf2_hmac("sha256", given.encode(), bytes.fromhex(salt), int(rounds))
    return hmac.compare_digest(actual.hex(), expected)


class AdminCredentials:
    def __init__(self, path: Path, fallback: Callable[[], tuple[str, str]]):
        self.path, self.fallback = path, fallback

    def current(self) -> tuple[str, str]:
        # A broken or symlinked override must fail closed, never restore the old password.
        if not self.path.exists() and not self.path.is_symlink():
            return self.fallback()
        state = load_state(self.path)
        username, password = state.get("username"), state.get("password_hash")
        if not isinstance(username, str) or not username or ":" in username or len(username) > 128 or any(ord(c) < 32 for c in username):
            raise RuntimeError("Invalid admin credential override")
        if not isinstance(password, str) or not is_password_hash(password):
            raise RuntimeError("Invalid admin password hash")
        return username, password

    def change(self, password: str, username: str | None = None) -> None:
        user = self.current()[0] if username is None else username
        if not user or len(user) > 128 or ":" in user or any(ord(c) < 32 for c in user):
            raise ValueError("Use a nonempty username without colons or control characters")
        save_state(self.path, {"username": user, "password_hash": hash_password(password)})
