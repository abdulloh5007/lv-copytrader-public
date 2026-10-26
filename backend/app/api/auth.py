"""`/api/auth/*` — the dashboard login, the logout, and the one status probe the SPA may ask
for before it has any credentials at all.

These three routes are the only ones exempt from the gate in ``app.util.auth`` (they are
listed there, in ``PUBLIC_PATHS``, so the exemption is stated once). That makes them the whole
attack surface of an unauthenticated visitor, and three decisions follow from it:

* **``GET /api/auth/status`` answers exactly three fields** — ``authenticated``, ``required``
  and ``username`` — and nothing else. It is tempting to fold the health payload into it so
  the SPA can boot in one round trip; that would publish the MT5 server, the account state and
  the armed flag to anyone who can reach the port. It stays a login-screen question:
  *do I need to log in, and am I already in?*
* **A failed login is counted, a successful one forgets the count** (:class:`LoginRateLimiter`).
  Eight wrong passphrases from one address and that address is refused for a minute, which
  turns an online guessing attack into an unaffordable one while leaving the operator's own
  typo-then-correct-password sequence completely unaffected.
* **The response never says which half was wrong.** ``invalid_credentials`` covers a wrong
  username, a wrong password, and a deployment with no login configured at all.

The cookie itself is minted by ``app.util.auth.session_cookie_header`` — the same function the
middleware uses when a script authenticates with Basic — so the flags on it (``HttpOnly``,
``SameSite=Lax``, ``Secure`` over TLS) can never drift between the two paths that issue it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.schemas import AuthLoginOut, AuthLoginRequest, AuthStatusOut, OkResponse
from app.util.auth import (
    COOKIE_NAME,
    LoginRateLimiter,
    basic_credentials_ok,
    clear_cookie_header,
    credentials_match,
    resolve_secret,
    session_cookie_header,
    valid_cookie,
)
from app.util.logging import get_logger

router = APIRouter(prefix="/api/auth", tags=["auth"])

log = get_logger(__name__)

#: Where the per-IP failure table lives. One per application instance, created in
#: ``create_app``; the lazy branch below covers an app assembled some other way (a test that
#: mounts the router alone) rather than letting a missing attribute 500 the login.
STATE_ATTRIBUTE = "login_rate_limiter"


def _limiter(request: Request) -> LoginRateLimiter:
    """Return the application's login throttle, creating it if nothing did."""
    limiter = getattr(request.app.state, STATE_ATTRIBUTE, None)
    if not isinstance(limiter, LoginRateLimiter):
        limiter = LoginRateLimiter()
        setattr(request.app.state, STATE_ATTRIBUTE, limiter)
    return limiter


def _client(request: Request) -> str:
    """Return the client address the throttle counts against.

    Deliberately the real peer and not ``X-Forwarded-For``: that header is attacker-supplied
    unless a proxy is known to overwrite it, and trusting it here would let one attacker
    present a fresh address on every attempt and never be throttled at all. Behind a reverse
    proxy this collapses every visitor onto the proxy's address, which throttles the login
    globally — the safe direction to be wrong in for a single-operator dashboard.
    """
    client = request.client
    return client.host if client is not None else "unknown"


def _secure(request: Request) -> bool:
    """Return whether this request arrived over TLS, which decides the cookie's Secure flag."""
    return request.url.scheme in ("https", "wss")


def _configured() -> tuple[str, str]:
    """Return the configured (username, password) pair, empty when login is switched off."""
    return settings.SP_AUTH_USER or "", settings.SP_AUTH_PASSWORD or ""


def _error(
    status: int, code: str, message: str, detail: dict[str, Any] | None = None
) -> JSONResponse:
    """Build the ``{"error": {code, message, detail}}`` envelope of CONTRACTS §18."""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "detail": detail or {}}},
    )


@router.post(
    "/login",
    response_model=AuthLoginOut,
    summary="Exchange a username and password for the session cookie",
)
async def login(request: Request, payload: AuthLoginRequest) -> JSONResponse:
    """Sign the operator in and hand back the cookie every later request is judged by."""
    limiter = _limiter(request)
    client = _client(request)

    retry_after = limiter.retry_after(client)
    if retry_after:
        # Refused before the password is even looked at, so hammering a locked address cannot
        # extend the lock and cannot be used as an oracle either.
        log.warning("auth_login_throttled client=%s retry_after=%d", client, retry_after)
        response = _error(
            429,
            "too_many_attempts",
            f"too many failed sign-in attempts — try again in {retry_after} seconds",
            {"retry_after": retry_after},
        )
        response.headers["Retry-After"] = str(retry_after)
        return response

    username, password = _configured()
    # An unconfigured deployment has no pair that could match, and says so with the same code
    # a wrong passphrase gets. The SPA never shows this screen there: /api/auth/status has
    # already told it required=false.
    ok = bool(username and password) and credentials_match(
        payload.username, payload.password, username, password
    )

    if not ok:
        failures = limiter.record_failure(client)
        # The attempted username is *not* logged: operators type the passphrase into the
        # username field often enough that it would end up in the log file in clear text.
        log.warning("auth_login_failed client=%s failures=%d", client, failures)
        return _error(
            401, "invalid_credentials", "that username and password do not match"
        )

    limiter.record_success(client)
    log.info("auth_login_ok client=%s user=%s", client, username)
    response = JSONResponse(status_code=200, content={"ok": True, "username": username})
    response.headers.append(
        "set-cookie",
        session_cookie_header(resolve_secret(settings.SECRET_KEY), secure=_secure(request)),
    )
    return response


@router.post("/logout", response_model=OkResponse, summary="Clear the session cookie")
async def logout(request: Request) -> JSONResponse:
    """Sign the operator out. Always succeeds, whether or not anyone was signed in."""
    response = JSONResponse(status_code=200, content={"ok": True})
    response.headers.append("set-cookie", clear_cookie_header(secure=_secure(request)))
    return response


@router.get(
    "/status",
    response_model=AuthStatusOut,
    summary="Whether a login is required, and whether this browser already has one",
)
async def status(request: Request) -> AuthStatusOut | JSONResponse:
    """Answer the one question the SPA has before it can render anything.

    Reachable with no credentials by design — a login page that cannot ask whether a login is
    needed has to guess, and guessing means either a flash of the dashboard on a protected
    deployment or a login form nobody can satisfy on an open one.
    """
    username, password = _configured()
    if not (username and password):
        # Nothing to log in to: every request is already allowed through.
        return AuthStatusOut(authenticated=True, required=False, username=None)

    authenticated = valid_cookie(resolve_secret(settings.SECRET_KEY), request.cookies.get(COOKIE_NAME))
    authorization = request.headers.get("authorization")
    if authorization and not authenticated:
        limiter = _limiter(request)
        peer = _client(request)
        wait = limiter.retry_after(peer)
        if wait:
            response = _error(429, "too_many_attempts", "Try again later", {"retry_after": wait})
            response.headers["Retry-After"] = str(wait)
            return response
        authenticated = basic_credentials_ok(authorization, username, password)
        if authenticated:
            limiter.record_success(peer)
        else:
            limiter.record_failure(peer)
    return AuthStatusOut(
        authenticated=authenticated,
        required=True,
        username=username if authenticated else None,
    )
