"""Fernet encryption helpers, SECRET_KEY bootstrap and secret masking."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.errors import SignalPilotError

__all__ = [
    "CryptoError",
    "MASK",
    "bootstrap_secret_key",
    "decrypt",
    "encrypt",
    "get_fernet",
    "is_encrypted",
    "mask",
]


class CryptoError(SignalPilotError):
    """Raised when a value cannot be encrypted or decrypted with the current SECRET_KEY."""

    code = "crypto_error"


#: Prefix stamped on every value this module encrypts. Bare (prefix-less) Fernet
#: tokens written by older builds are still accepted by :func:`decrypt`.
_PREFIX = "enc:v1:"

#: Name of the environment variable / .env line that carries the Fernet key.
_ENV_KEY = "SECRET_KEY"

#: A Fernet token is base64url of a payload whose first byte is 0x80, so every
#: token starts with these characters. Used to recognise legacy un-prefixed rows.
_FERNET_TOKEN_PREFIX = "gAAAAA"

_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?" + _ENV_KEY + r"\s*=")

#: Eight U+2022 bullets — the only thing an API endpoint ever returns for a secret.
MASK = "•" * 8

_fernet_cache: tuple[str, Fernet] | None = None


def _logger() -> Any:
    """Return the module logger, imported lazily so app.util.logging cannot cycle back."""
    from app.util.logging import get_logger

    return get_logger("app.crypto")


def _register_secret(value: str | None) -> None:
    """Register a plaintext secret with the log redactor, never failing the caller."""
    if not value:
        return
    try:
        from app.util.logging import register_secret

        register_secret(value)
    except Exception:  # noqa: BLE001 - redaction bookkeeping must never break the money path
        pass


def _current_key() -> str | None:
    """Return the configured SECRET_KEY from settings, falling back to the environment."""
    key = getattr(settings, "SECRET_KEY", None) or os.environ.get(_ENV_KEY)
    if key is None:
        return None
    key = key.strip()
    return key or None


def _reset_cache() -> None:
    """Drop the memoised Fernet instance so the next call rebuilds it."""
    global _fernet_cache
    _fernet_cache = None


def get_fernet() -> Fernet:
    """Return a Fernet built from the current SECRET_KEY, memoised per key value."""
    global _fernet_cache
    key = _current_key()
    if not key:
        raise CryptoError(
            "SECRET_KEY is not configured — cannot encrypt or decrypt stored secrets",
            detail={"env_key": _ENV_KEY},
        )
    if _fernet_cache is not None and _fernet_cache[0] == key:
        return _fernet_cache[1]
    try:
        fernet = Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise CryptoError(
            "SECRET_KEY is not a valid Fernet key (expected 32 url-safe base64-encoded bytes)",
            detail={"env_key": _ENV_KEY},
        ) from exc
    _fernet_cache = (key, fernet)
    return fernet


def is_encrypted(value: str | None) -> bool:
    """Report whether a stored value is an LV Copytrader or bare Fernet ciphertext."""
    if not isinstance(value, str) or not value:
        return False
    if value.startswith(_PREFIX):
        return True
    return value.startswith(_FERNET_TOKEN_PREFIX) and len(value) >= 60


def encrypt(value: str) -> str:
    """Encrypt a string and return it prefixed with the ``enc:v1:`` marker."""
    if value is None:
        raise CryptoError("cannot encrypt None")
    if not isinstance(value, str):
        raise CryptoError(
            "cannot encrypt a non-string value",
            detail={"type": type(value).__name__},
        )
    _register_secret(value)
    token = get_fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return f"{_PREFIX}{token}"


def decrypt(value: str) -> str:
    """Decrypt a prefixed or bare Fernet token, raising CryptoError when it fails."""
    if not isinstance(value, str) or not value:
        raise CryptoError("cannot decrypt an empty value")
    token = value[len(_PREFIX) :] if value.startswith(_PREFIX) else value
    fernet = get_fernet()
    try:
        plain = fernet.decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise CryptoError(
            "stored value cannot be decrypted with the current SECRET_KEY",
            detail={"reason": "invalid_token"},
        ) from exc
    except (TypeError, ValueError) as exc:
        raise CryptoError(
            "stored value is not a readable Fernet token",
            detail={"reason": "malformed_token"},
        ) from exc
    _register_secret(plain)
    return plain


def mask(value: str | None) -> str | None:
    """Return the bullet mask for any present value, or None when there is nothing stored."""
    if value is None or value == "":
        return None
    return MASK


def _write_env_key(env_path: str, key: str) -> bool:
    """Write SECRET_KEY into the .env file at env_path, replacing any existing line."""
    path = Path(env_path)
    try:
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        existing_lines: list[str] = []
        if path.exists():
            existing_lines = path.read_text(encoding="utf-8").splitlines()
        out: list[str] = []
        replaced = False
        for line in existing_lines:
            if _ENV_LINE_RE.match(line):
                if replaced:
                    continue
                out.append(f"{_ENV_KEY}={key}")
                replaced = True
                continue
            out.append(line)
        if not replaced:
            out.append(f"{_ENV_KEY}={key}")
        text = "\n".join(out).rstrip("\n") + "\n"
        tmp = path.with_name(path.name + ".sp-tmp")
        tmp.write_text(text, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        return True
    except OSError as exc:
        _logger().error(
            "secret_key_write_failed env_path=%s error=%s",
            env_path,
            exc.__class__.__name__,
        )
        return False


def bootstrap_secret_key(env_path: str) -> tuple[str, bool]:
    """Return the SECRET_KEY, generating and persisting one to .env when it is absent."""
    log = _logger()
    existing = _current_key()
    if existing:
        try:
            Fernet(existing.encode("utf-8"))
        except (ValueError, TypeError):
            log.warning(
                "secret_key_invalid env_key=%s message=%s",
                _ENV_KEY,
                "present but not a valid Fernet key; encrypted settings will need re-entry",
            )
        settings.SECRET_KEY = existing
        os.environ[_ENV_KEY] = existing
        _register_secret(existing)
        _reset_cache()
        return existing, False

    key = Fernet.generate_key().decode("ascii")
    written = _write_env_key(env_path, key)
    settings.SECRET_KEY = key
    os.environ[_ENV_KEY] = key
    _register_secret(key)
    _reset_cache()
    log.warning(
        "secret_key_generated env_path=%s written=%s message=%s",
        env_path,
        written,
        "a new SECRET_KEY was generated; keep this .env file — losing it makes stored secrets unreadable",
    )
    return key, True
