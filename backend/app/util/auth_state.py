"""Owner-only persistent auth state. No passwords, raw sessions or raw IPs are stored."""
from __future__ import annotations
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from typing import Callable


def load_state(path: Path) -> dict:
    if path.is_symlink():
        raise RuntimeError("Auth state symlinks are refused")
    if not path.exists():
        return {}
    if path.stat().st_size > 2 * 1024 * 1024:
        raise RuntimeError("Auth state is unexpectedly large")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("version") != 1:
        raise RuntimeError("Invalid auth state; refusing to reset security state")
    return value


def save_state(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError("Auth state symlinks are refused")
    fd, name = tempfile.mkstemp(prefix=".auth-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"version": 1, **value}, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class SessionGuard:
    def __init__(self, path: Path, secret: Callable[[], str], credentials: Callable[[], tuple[str, str]]):
        self.path, self._secret, self.credentials = path, secret, credentials
        state = load_state(path)
        self.epoch = state.get("epoch") or secrets.token_hex(32)
        self.revoked = {k: float(v) for k, v in state.get("revoked", {}).items() if float(v) > time.time()}
        if not state:
            self._save()

    @property
    def secret(self) -> str:
        return self.secret_for(self.credentials())

    def secret_for(self, credentials: tuple[str, str]) -> str:
        """Bind a new cookie to the credentials actually verified, even during rotation."""
        from app.util.auth import resolve_secret
        # A password change, signing-key change, or missing state invalidates old sessions.
        body = json.dumps(["lvct-session-v2", self.epoch, *credentials]).encode()
        return hmac.new(resolve_secret(self._secret()).encode(), body, hashlib.sha256).hexdigest()

    def valid(self, token: str | None) -> bool:
        from app.util.auth import valid_cookie
        return bool(token and valid_cookie(self.secret, token) and
                    self.revoked.get(hashlib.sha256(token.encode()).hexdigest(), 0) <= time.time())

    def revoke(self, token: str | None) -> None:
        from app.util.auth import COOKIE_MAX_AGE
        if not self.valid(token):
            return
        now = time.time()
        self.revoked = {k: v for k, v in self.revoked.items() if v > now}
        if len(self.revoked) >= 8192:
            self.epoch = secrets.token_hex(32)
            self.revoked.clear()
        else:
            self.revoked[hashlib.sha256(token.encode()).hexdigest()] = int(token.split(".", 1)[0]) + COOKIE_MAX_AGE
        self._save()

    def _save(self) -> None:
        save_state(self.path, {"epoch": self.epoch, "revoked": self.revoked})
