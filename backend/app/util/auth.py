"""Dashboard authentication: the gate, the signed session cookie, and the login throttle.

The dashboard carries the ARM switch, the kill switch, manual order entry and the broker
credentials form. Behind localhost that is fine; the moment it is published — a cloudflared
tunnel, a reverse proxy, a port forward — an unauthenticated copy of that page is a stranger's
control panel for somebody else's money. So this is deliberately applied to *everything*
rather than to a handful of "sensitive" routes.

Four details this gets right that a naive implementation does not:

* **It is pure ASGI, not BaseHTTPMiddleware.** Starlette's BaseHTTPMiddleware never sees
  `websocket` scopes, so an app "protected" that way still serves `/api/stream` — the live
  feed of every run, position and account tick — to anyone who asks.
* **Browsers cannot send an Authorization header on a WebSocket handshake.** Requiring Basic
  there would break the dashboard for the very user who just logged in, so a successful login
  also sets a signed cookie, and the WebSocket accepts that cookie. The signature is an HMAC
  over SECRET_KEY, so the cookie cannot be forged and needs no server-side session store.
* **It never sends `WWW-Authenticate`.** That one header is the entire reason a browser throws
  up its grey native prompt — a box that cannot be themed, cannot be translated, cannot say
  what LV Copytrader is and cannot be cancelled into anything but a blank error page. Without it
  the browser stays quiet and the SPA renders its own login screen. Basic is still *accepted*,
  because curl, the compose healthcheck and any script the operator writes rely on it; it is
  simply never *demanded*.
* **An unauthenticated navigation is answered with the app shell, not a 401.** The login page
  is part of the SPA, so the shell (and the `/assets` bundle it pulls) must load before there
  is anything to log in with. Only API calls and WebSockets are refused outright.

Brute force is handled by :class:`LoginRateLimiter`: a plain dict of failures per client IP,
in-process because this app runs on exactly one worker by design (see the note at the bottom of
``app/main.py``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import math
from pathlib import Path
import secrets
import time
from typing import Any, Awaitable, Callable, Final

__all__ = [
    "ASSET_PREFIX",
    "AuthMiddleware",
    "COOKIE_MAX_AGE",
    "COOKIE_NAME",
    "FALLBACK_SECRET",
    "LOCKOUT_SECONDS",
    "LoginRateLimiter",
    "MAX_FAILED_ATTEMPTS",
    "PUBLIC_PATHS",
    "basic_credentials_ok",
    "clear_cookie_header",
    "credentials_match",
    "is_public_path",
    "resolve_secret",
    "session_cookie_header",
    "valid_cookie",
]

#: Name of the signed session cookie. The WebSocket handshake carries it too.
COOKIE_NAME: Final[str] = "sp_auth"

#: How long a login stays valid before the operator is asked again.
COOKIE_MAX_AGE: Final[int] = 12 * 3600

#: Signing key used when the deployment has no SECRET_KEY yet. A cookie signed with it is not
#: a security boundary — it only keeps the flow working during first-boot bootstrap, before
#: ``crypto.bootstrap_secret_key`` has written a real key.
FALLBACK_SECRET: Final[str] = secrets.token_urlsafe(48)

#: The three routes that must answer without credentials, or there is no way to *get* any.
PUBLIC_PATHS: Final[frozenset[str]] = frozenset(
    {"/api/auth/login", "/api/auth/logout", "/api/auth/status"}
)

#: The built bundle. The login page cannot paint without it, so it is served unauthenticated —
#: hashed JS and CSS of a public open-source dashboard, carrying no operator data.
ASSET_PREFIX: Final[str] = "/assets"

#: Failed logins from one IP before that IP is refused outright.
MAX_FAILED_ATTEMPTS: Final[int] = 8

#: How long that refusal lasts. Short on purpose: this is here to make an online password
#: guess hopeless, not to lock the operator out of their own cockpit for the evening.
LOCKOUT_SECONDS: Final[int] = 60

#: Size at which the failure table is swept for expired entries.
_PRUNE_AT: Final[int] = 256

#: 1008 = policy violation (RFC 6455). Sent *before* accept, so the stream never opens.
WS_POLICY_VIOLATION: Final[int] = 1008


# =======================================================================================
# The signed cookie
# =======================================================================================


def resolve_secret(secret: str | None) -> str:
    """Return the key the cookie is signed with, falling back before SECRET_KEY exists."""
    return secret or FALLBACK_SECRET


def _sign(secret: str, issued_at: int, nonce: str | None = None) -> str:
    """Sign a unique, bounded, domain-separated session token."""
    nonce = secrets.token_hex(16) if nonce is None else nonce
    payload = f"{int(issued_at)}.{nonce}"
    digest = hmac.new(secret.encode(), ("lvct-cookie-v2:" + payload).encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{digest}"


def valid_cookie(secret: str, value: str | None, max_age: int = COOKIE_MAX_AGE) -> bool:
    if not value or len(value) > 128 or not value.isascii():
        return False
    parts = value.split(".")
    if len(parts) != 3:
        return False
    issued, nonce, digest = parts
    if not issued.isdigit() or len(issued) > 12 or len(nonce) != 32 or len(digest) != 64:
        return False
    try:
        int(nonce, 16)
    except ValueError:
        return False
    expected = _sign(secret, int(issued), nonce).rsplit(".", 1)[1]
    return hmac.compare_digest(digest, expected) and 0 <= time.time() - int(issued) <= max_age


def _cookie_attributes(secure: bool) -> list[str]:
    """Return the attributes every ``sp_auth`` cookie carries, set or cleared.

    ``HttpOnly`` keeps it out of reach of any script on the page, ``SameSite=Lax`` keeps a
    third-party page from driving the API with it, and ``Secure`` is added only when the
    request actually arrived over TLS — marking it Secure on a plain-http deployment would
    make the browser drop the cookie and the operator could never log in.
    """
    attributes = ["Path=/", "HttpOnly", "SameSite=Lax"]
    if secure:
        attributes.append("Secure")
    return attributes


def session_cookie_header(secret: str, *, secure: bool, issued_at: float | None = None) -> str:
    """Return the ``Set-Cookie`` value that logs a browser in."""
    now = time.time() if issued_at is None else issued_at
    value = _sign(resolve_secret(secret), int(now))
    return "; ".join(
        [f"{COOKIE_NAME}={value}", *_cookie_attributes(secure), f"Max-Age={COOKIE_MAX_AGE}"]
    )


def clear_cookie_header(*, secure: bool) -> str:
    """Return the ``Set-Cookie`` value that logs a browser out.

    The attributes have to match the ones the cookie was set with, or the browser treats it
    as a different cookie and quietly keeps the old one.
    """
    return "; ".join([f"{COOKIE_NAME}=", *_cookie_attributes(secure), "Max-Age=0"])


# =======================================================================================
# Credentials
# =======================================================================================


def credentials_match(
    given_user: str, given_password: str, username: str, password: str
) -> bool:
    """Compare a username/password pair against the configured one, constant time on both.

    Both halves are always compared: bailing out early on a wrong username leaks which half
    was wrong. Comparison is on UTF-8 bytes because ``hmac.compare_digest`` refuses a ``str``
    containing anything outside ASCII, and a passphrase with a non-ASCII character in it must
    fail the login rather than raise a 500.
    """
    user_ok = hmac.compare_digest(given_user.encode("utf-8"), username.encode("utf-8"))
    password_ok = hmac.compare_digest(given_password.encode("utf-8"), password.encode("utf-8"))
    return user_ok and password_ok


def basic_credentials_ok(header: str | None, username: str, password: str) -> bool:
    """Check an HTTP Basic header against the configured pair.

    Still supported, and deliberately so: the compose healthcheck, ``curl`` and every script
    an operator writes authenticate this way. It is only the *challenge* that is gone.
    """
    if not username or not password or not header or len(header) > 8192 or not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header.split(" ", 1)[1].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, IndexError):
        return False
    given_user, _, given_password = raw.partition(":")
    return credentials_match(given_user, given_password, username, password)


# =======================================================================================
# Brute-force throttle
# =======================================================================================


class LoginRateLimiter:
    """Refuse a client IP that has failed too many logins, briefly.

    In-process state — a dict of ``ip -> (failures, last failure)``. That is enough because
    LV Copytrader runs on exactly one uvicorn worker by design; a second worker would be a
    second Telethon client and a second ingest queue long before it was a second copy of this
    dict.

    Three properties matter and each is a line below:

    * **Only failures count.** A logged-in operator hammering the dashboard never trips it.
    * **A success wipes the slate**, so seven typos followed by the right passphrase leave
      nothing behind.
    * **The window is measured from the last failure and expires on its own**, so a correct
      password is never locked out permanently — waiting out ``window`` seconds is always
      enough, and no successful login is required to clear it.
    """

    def __init__(
        self,
        *,
        max_attempts: int = MAX_FAILED_ATTEMPTS,
        window: float = LOCKOUT_SECONDS,
        clock: Callable[[], float] = time.time,
        storage_path: Path | None = None,
        secret: Callable[[], str] = lambda: "",
        global_max_attempts: int = 120,
    ) -> None:
        self.max_attempts = int(max_attempts)
        self.window = float(window)
        self._clock = clock
        self._failures: dict[str, tuple[int, float]] = {}
        self.storage_path, self._secret = storage_path, secret
        self.global_max_attempts = global_max_attempts
        self._global: tuple[int, float] = (0, 0.0)
        if storage_path:
            from app.util.auth_state import load_state
            state = load_state(storage_path)
            self._failures = {k: (int(v[0]), float(v[1])) for k, v in state.get("failures", {}).items()}
            self._global = tuple(state.get("global", [0, 0.0]))
            self._prune(self._clock())

    def _key(self, client: str) -> str:
        if not self.storage_path:
            return client
        return hmac.new(resolve_secret(self._secret()).encode(), ("login-peer:" + client).encode(), hashlib.sha256).hexdigest()

    def _persist(self) -> None:
        if self.storage_path:
            from app.util.auth_state import save_state
            save_state(self.storage_path, {"failures": self._failures, "global": self._global})

    def retry_after(self, client: str, *, include_global: bool = True) -> int:
        """Return the seconds this client must wait, or 0 when it may try right now."""
        now = self._clock()
        count, started = self._global
        if include_global and count >= self.global_max_attempts and now - started < self.window:
            return max(1, int(math.ceil(self.window - (now - started))))
        client = self._key(client)
        entry = self._failures.get(client)
        if entry is None:
            return 0
        failures, last = entry
        remaining = self.window - (now - last)
        if remaining <= 0:
            # The window has passed: forget the client entirely rather than leaving a count
            # behind that would trip again on the next single typo.
            self._failures.pop(client, None)
            return 0
        if failures < self.max_attempts:
            return 0
        return max(1, int(math.ceil(remaining)))

    def record_failure(self, client: str) -> int:
        """Count one failed attempt and return the client's new failure total."""
        now = self._clock()
        client = self._key(client)
        count, started = self._global
        if now - started >= self.window:
            count, started = 0, now
        self._global = (count + 1, started)
        failures, last = self._failures.get(client, (0, 0.0))
        if now - last > self.window:
            failures = 0
        failures += 1
        self._failures[client] = (failures, now)
        self._prune(now)
        self._persist()
        return failures

    def record_success(self, client: str) -> None:
        """Forget every failure recorded for this client."""
        if self._failures.pop(self._key(client), None) is not None:
            self._persist()

    def _prune(self, now: float) -> None:
        """Drop entries whose window has passed, so a scan cannot grow the table forever."""
        if len(self._failures) < _PRUNE_AT:
            return
        stale = [key for key, (_, last) in self._failures.items() if now - last > self.window]
        for key in stale:
            del self._failures[key]
        if len(self._failures) > 8192:
            oldest = sorted(self._failures, key=lambda key: self._failures[key][1])
            for key in oldest[:len(self._failures) - 8192]:
                del self._failures[key]


# =======================================================================================
# Scope helpers
# =======================================================================================


def _header(scope: dict[str, Any], name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _cookie(scope: dict[str, Any], name: str) -> str | None:
    raw = _header(scope, b"cookie")
    if not raw:
        return None
    for part in raw.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def _path(scope: dict[str, Any]) -> str:
    """Return the request path without a trailing slash, so `/api/auth/status/` matches too."""
    raw = str(scope.get("path") or "/")
    trimmed = raw.rstrip("/")
    return trimmed or "/"


def is_public_path(path: str) -> bool:
    """Return whether a path is reachable with no credentials at all.

    A path carrying ``..`` is never public. Starlette matches routes as literal strings and
    its StaticFiles refuses traversal, so `/assets/../api/settings` could not have reached the
    settings route anyway — but the exemption list is the one place where being obviously right
    beats being provably right.
    """
    if ".." in path:
        return False
    return path in PUBLIC_PATHS or path == "/favicon.svg" or path == ASSET_PREFIX or path.startswith(ASSET_PREFIX + "/")


def _is_api_path(path: str) -> bool:
    """Return whether a path belongs to the JSON API rather than to the SPA."""
    return path == "/api" or path.startswith("/api/")


def _wants_document(scope: dict[str, Any]) -> bool:
    """Return whether this is a browser navigation, which must be answered with the shell.

    A navigation is a GET (or HEAD) whose ``Accept`` names ``text/html``. Every call the SPA
    makes with ``fetch`` asks for JSON, so this cannot swallow an API request by accident —
    and API paths are excluded by the caller anyway.
    """
    if str(scope.get("method") or "GET").upper() not in ("GET", "HEAD"):
        return False
    return "text/html" in (_header(scope, b"accept") or "").lower()


# =======================================================================================
# The middleware
# =======================================================================================


class AuthMiddleware:
    """Gate every HTTP request and every WebSocket on a signed cookie or an HTTP Basic header.

    ``shell`` is the SPA-shell factory from ``app.main``; it is injected rather than imported
    so this module stays a leaf, and so an unauthenticated navigation is answered by the exact
    same handler that serves the dashboard to a logged-in operator.
    """

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        username: str,
        password: str,
        secret: str | Callable[[], str],
        shell: Callable[[], Any] | None = None,
        limiter: LoginRateLimiter | None = None,
        sessions: Any = None,
        allow_unconfigured: bool = False,
    ) -> None:
        self.app = app
        self.username = username
        self.password = password
        self._secret = secret
        self.shell = shell
        self.limiter = limiter or LoginRateLimiter()
        self.sessions = sessions
        self.allow_unconfigured = allow_unconfigured

    @property
    def secret(self) -> str:
        value = self._secret() if callable(self._secret) else self._secret
        return resolve_secret(value)

    @property
    def enabled(self) -> bool:
        """Missing credentials lock private routes unless dev mode was explicitly enabled."""
        return bool(self.username and self.password) or not self.allow_unconfigured

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if not self.enabled or scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # The login/logout/status routes and the bundle are open. Only for HTTP: there is no
        # WebSocket on any of those paths, and an exemption a WebSocket could reach would be
        # a hole in the one place this whole module exists to close.
        if scope["type"] == "http" and (is_public_path(_path(scope)) or (
            _path(scope) in {"/api/waitlist", "/api/demo/presence"} and scope.get("method") == "POST"
        ) or (
            _path(scope) == "/api/demo/snapshot" and scope.get("method") in {"GET", "HEAD"}
        )):
            await self.app(scope, receive, send)
            return

        if not self.username or not self.password:
            await self._reject(scope, receive, send)
            return
        cookie = _cookie(scope, COOKIE_NAME)
        by_cookie = self.sessions.valid(cookie) if self.sessions else valid_cookie(self.secret, cookie)
        authorization = _header(scope, b"authorization")
        peer = str((scope.get("client") or ("unknown", 0))[0])
        by_basic = basic_credentials_ok(authorization, self.username, self.password)
        if authorization and not by_cookie and self.limiter.retry_after(peer, include_global=not by_basic):
            if scope["type"] == "websocket":
                await self._reject(scope, receive, send)
                return
            from starlette.responses import JSONResponse
            wait = self.limiter.retry_after(peer, include_global=not by_basic)
            response = JSONResponse({"error": {"code": "too_many_attempts", "message": "Try again later", "detail": {"retry_after": wait}}}, status_code=429, headers={"Retry-After": str(wait)})
            await response(scope, receive, send)
            return
        if authorization and not by_cookie:
            if by_basic:
                self.limiter.record_success(peer)
            else:
                self.limiter.record_failure(peer)

        if not (by_cookie or by_basic):
            await self._reject(scope, receive, send)
            return

        if scope["type"] == "websocket" and by_cookie:
            from starlette.websockets import WebSocketDisconnect
            closed = False
            async def guarded_send(message):
                nonlocal closed
                valid = self.sessions.valid(cookie) if self.sessions else valid_cookie(self.secret, cookie)
                if message.get("type") in {"websocket.accept", "websocket.send"} and (closed or not valid):
                    if not closed:
                        closed = True
                        await send({"type": "websocket.close", "code": WS_POLICY_VIOLATION})
                    raise WebSocketDisconnect(WS_POLICY_VIOLATION)
                await send(message)
            try:
                await self.app(scope, receive, guarded_send)
            except WebSocketDisconnect:
                pass
            return
        if scope["type"] == "websocket" or by_cookie:
            await self.app(scope, receive, send)
            return

        # Authenticated by Basic over HTTP: hand back a cookie so the WebSocket can connect.
        header = session_cookie_header(
            self.secret, secure=scope.get("scheme") in ("https", "wss")
        ).encode("latin-1")

        async def send_with_cookie(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers") or ()) + [(b"set-cookie", header)]
            await send(message)

        await self.app(scope, receive, send_with_cookie)

    async def _reject(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """Refuse the connection in whichever protocol asked — or hand back the login page."""
        if scope["type"] == "websocket":
            # Closing before accept keeps the stream unopened: an accept-then-close would put
            # one frame of the live feed on the wire first.
            await send({"type": "websocket.close", "code": WS_POLICY_VIOLATION})
            return

        path = _path(scope)
        if self.shell is not None and not _is_api_path(path) and _wants_document(scope):
            # A browser asking for a page gets the app, which renders the login screen and
            # posts to /api/auth/login. Answering 401 here would show a blank error page
            # instead — and with no WWW-Authenticate, not even the native prompt.
            response = self.shell()
            await response(scope, receive, send)
            return

        body = (
            b'{"error":{"code":"unauthorized","message":'
            b'"authentication required: sign in to the dashboard","detail":{}}}'
        )
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    # Deliberately NO www-authenticate: see this module's docstring.
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
