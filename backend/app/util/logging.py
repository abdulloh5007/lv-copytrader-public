"""Process-wide logging with secret redaction applied to every formatted line."""

from __future__ import annotations

import logging
import re
import sys
import threading
import time

# Key names whose value is a secret wherever it appears in a log line, in any casing.
SECRET_KEYS: tuple[str, ...] = (
    "password",
    "api_key",
    "api_hash",
    "token",
    "session",
    "secret",
    "authorization",
    "x-api-key",
)

_REDACTED = "***"

# Literals shorter than this are too generic to blank out safely.
_MIN_SECRET_LENGTH = 4

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Third-party loggers that are chatty at INFO and say nothing an operator needs.
_NOISY_LOGGERS: tuple[tuple[str, int], ...] = (
    ("telethon.network", logging.WARNING),
    ("httpx", logging.WARNING),
    ("httpcore", logging.WARNING),
    ("asyncio", logging.WARNING),
)

_HANDLER_MARK = "_signalpilot_redacting_handler"

_lock = threading.Lock()
_secrets: set[str] = set()
# Longest first, so a secret that contains another is masked before its substring.
_secret_literals: tuple[str, ...] = ()


def _key_alternation() -> str:
    """Build the regex alternation matching every SECRET_KEYS spelling."""
    parts: list[str] = []
    for key in sorted(SECRET_KEYS, key=len, reverse=True):
        parts.append("".join(r"[_\-]?" if ch in "_-" else re.escape(ch) for ch in key))
    return "|".join(parts)


# A key is any dotted/underscored identifier one of SECRET_KEYS is a whole segment of:
# `mt5.password`, `SECRET_KEY`, `notifier.bot_token`, `X-Api-Key`. `input_tokens` is not one,
# because `token` there is followed by more word characters rather than a separator.
_KEY_PATTERN = (
    r"(?:[A-Za-z0-9]+[._\-])*(?:" + _key_alternation() + r")(?:[._\-][A-Za-z0-9]+)*"
)

# `key = value`, `"key": "value"`, `key:value` — quoted, bare, or an auth scheme plus its token.
_KV_RE = re.compile(
    r"(?P<key>" + _KEY_PATTERN + r")"
    r"(?P<sep>[\"']?\s*[=:]\s*)"
    r"(?P<value>"
    r"\"[^\"]*\""
    r"|'[^']*'"
    r"|(?:bearer|basic)\s+[^\s,;)\]}]+"
    r"|(?!(?:true|false|null|none|nan)(?![A-Za-z0-9]))[^\s,;)\]}\"']+"
    r")",
    re.IGNORECASE,
)

# A bearer token standing on its own, without a key in front of it.
_BEARER_RE = re.compile(r"\b(?P<scheme>bearer|basic)\s+(?P<token>[A-Za-z0-9\-._~+/=]{6,})", re.IGNORECASE)

# A Telegram Bot API token, which travels inside the request URL: /bot<id>:<secret>/sendMessage
_BOT_TOKEN_RE = re.compile(r"\bbot\d{5,}:[A-Za-z0-9_\-]{20,}", re.IGNORECASE)


_CLOUD_TOKEN_RE = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|"
    r"gd_pat_[A-Za-z0-9_]{30,}|AIza[A-Za-z0-9_-]{35}|(?:sk|rk)_live_[A-Za-z0-9]{20,})"
)


def register_secret(value: str | None) -> None:
    """Register a secret literal so every later log line masks it."""
    if not value:
        return
    literal = str(value).strip()
    if len(literal) < _MIN_SECRET_LENGTH:
        return
    global _secret_literals
    with _lock:
        if literal in _secrets:
            return
        _secrets.add(literal)
        _secret_literals = tuple(sorted(_secrets, key=len, reverse=True))


def _kv_replacement(match: re.Match[str]) -> str:
    """Rebuild one key/value pair with its value masked, keeping the original quoting."""
    value = match.group("value")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        masked = f"{value[0]}{_REDACTED}{value[0]}"
    elif value[:6].lower() in ("bearer", "basic "):
        masked = f"{value.split(None, 1)[0]} {_REDACTED}"
    else:
        masked = _REDACTED
    return f"{match.group('key')}{match.group('sep')}{masked}"


def _bearer_replacement(match: re.Match[str]) -> str:
    """Mask the credential after an Authorization scheme, keeping the scheme visible."""
    return f"{match.group('scheme')} {_REDACTED}"


def redact(text: str) -> str:
    """Mask registered secret literals and every secret-looking key/value in one piece of text."""
    if not text:
        return text
    out = text if isinstance(text, str) else str(text)
    for literal in _secret_literals:
        if literal in out:
            out = out.replace(literal, _REDACTED)
    out = _BOT_TOKEN_RE.sub("bot" + _REDACTED, out)
    out = _CLOUD_TOKEN_RE.sub(_REDACTED, out)
    out = _KV_RE.sub(_kv_replacement, out)
    out = _BEARER_RE.sub(_bearer_replacement, out)
    return out


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts the finished line — message, args, exception text and all."""

    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        """Format the record normally, then scrub secrets out of the whole line."""
        return redact(super().format(record))


def _resolve_level(level: str | int | None) -> int:
    """Turn a level name, a numeric string or an int into a logging level number."""
    if isinstance(level, int):
        return level
    if not level:
        return logging.INFO
    name = str(level).strip()
    named = logging.getLevelName(name.upper())
    if isinstance(named, int):
        return named
    try:
        return int(name)
    except ValueError:
        return logging.INFO


def setup_logging(level: str) -> None:
    """Install the single redacting stream handler on the root logger; safe to call twice."""
    resolved = _resolve_level(level)
    root = logging.getLogger()
    handler: logging.Handler | None = None
    for existing in root.handlers:
        if getattr(existing, _HANDLER_MARK, False):
            handler = existing
            break
    if handler is None:
        handler = logging.StreamHandler(stream=sys.stderr)
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)
    handler.setFormatter(RedactingFormatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT))
    handler.setLevel(resolved)
    root.setLevel(resolved)
    for name, noisy_level in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(noisy_level)


def get_logger(name: str) -> logging.Logger:
    """Return the logger for a module."""
    return logging.getLogger(name)
