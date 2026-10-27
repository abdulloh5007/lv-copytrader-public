"""Nothing is reachable without a login once credentials are configured — and the login is ours.

Two separate properties are under test here.

**The gate holds.** The dashboard carries the ARM switch, the kill switch, manual order entry
and the broker credentials form, so "protected" has to mean the whole surface — not the pages
someone remembered to decorate. In particular `/api/stream` is the live feed of every run,
position and account tick, and it is a WebSocket: Starlette's BaseHTTPMiddleware never sees
websocket scopes, so an app guarded that way still streams to anyone who asks. That is the
specific hole these tests exist to keep shut.

**The browser never sees its own login box.** The gate must not send `WWW-Authenticate`: that
one header is what makes a browser throw up the grey native prompt, which cannot be themed,
translated, or cancelled into anything but a blank error page. Instead an unauthenticated
navigation is answered with the SPA shell (200 HTML) so the app can render its own login
screen, the bundle under `/assets` loads without credentials so that screen can paint, and
`/api/auth/*` carries the actual sign-in. Every one of those is a deliberate hole in the gate,
so each is pinned here alongside the proof that nothing else leaked through with it.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest

from app.util.auth import (
    COOKIE_NAME,
    MAX_FAILED_ATTEMPTS,
    AuthMiddleware,
    LoginRateLimiter,
    _sign,
    valid_cookie,
)

USER = "operator"
PASSWORD = "a-long-enough-passphrase"
HEADER = {"Authorization": "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()}
#: What a browser sends when the person in front of it typed a URL and pressed enter.
NAVIGATION = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
SHELL_MARKER = "<!doctype html><title>LV Copytrader test shell</title><div id=root></div>"
BUNDLE_MARKER = "console.log('lv-copytrader')"


def _basic(user: str, password: str) -> dict[str, str]:
    """Return an Authorization header for one username/password pair."""
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


async def _client_for(app: Any) -> httpx.AsyncClient:
    """Return an HTTP client speaking ASGI straight to the app, cookies and all."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _build_dist(root: Path) -> Path:
    """Write a stand-in for the built SPA, so shell and bundle responses are real files."""
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(SHELL_MARKER, encoding="utf-8")
    (dist / "assets" / "app.js").write_text(BUNDLE_MARKER, encoding="utf-8")
    return dist


@pytest.fixture
async def secured(data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """Boot the real app with authentication switched on and a built bundle on disk."""
    from app.config import settings as app_settings
    from app.main import create_app

    monkeypatch.setattr(app_settings, "SP_AUTH_USER", USER, raising=False)
    monkeypatch.setattr(app_settings, "SP_AUTH_PASSWORD", PASSWORD, raising=False)
    monkeypatch.setattr(app_settings, "FRONTEND_DIST", str(_build_dist(tmp_path)), raising=False)

    app = create_app()
    async with app.router.lifespan_context(app):
        async with await _client_for(app) as client:
            yield app, client


@pytest.fixture
async def unsecured(data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """Boot the same app with no credentials configured — the localhost default."""
    from app.config import settings as app_settings
    from app.main import create_app

    monkeypatch.setattr(app_settings, "SP_AUTH_USER", "", raising=False)
    monkeypatch.setattr(app_settings, "SP_AUTH_PASSWORD", "", raising=False)
    monkeypatch.setattr(app_settings, "FRONTEND_DIST", str(_build_dist(tmp_path)), raising=False)

    app = create_app()
    async with app.router.lifespan_context(app):
        async with await _client_for(app) as client:
            yield app, client


# =======================================================================================
# The gate
# =======================================================================================


async def test_every_surface_refuses_without_credentials(secured) -> None:
    """Routes, health, and the SPA shell asked for as data all answer 401 in one envelope."""
    _, client = secured
    for path in ("/", "/settings", "/api/health", "/api/settings", "/api/trading/state",
                 "/api/trading/positions", "/api/history/trades"):
        response = await client.get(path)
        assert response.status_code == 401, f"{path} answered {response.status_code} unauthenticated"
        assert response.json() == {
            "error": {
                "code": "unauthorized",
                "message": "authentication required: sign in to the dashboard",
                "detail": {},
            }
        }, f"{path} did not answer in the standard error envelope"


async def test_the_native_browser_prompt_is_never_offered(secured) -> None:
    """`WWW-Authenticate` is the entire reason a browser shows its own grey login box."""
    _, client = secured
    for path in ("/", "/api/health", "/api/trading/state"):
        response = await client.get(path)
        assert response.status_code == 401
        assert "www-authenticate" not in response.headers, (
            f"{path} sent a challenge, so the browser will paint its native prompt over the SPA"
        )


async def test_a_write_route_refuses_too(secured) -> None:
    """The arm switch is the one that matters most."""
    _, client = secured
    response = await client.post("/api/trading/arm", json={"armed": True})
    assert response.status_code == 401


async def test_correct_credentials_pass_and_hand_back_a_cookie(secured) -> None:
    """Basic still works for curl and the compose healthcheck, and still mints the cookie."""
    _, client = secured
    response = await client.get("/api/health", headers=HEADER)
    assert response.status_code == 200
    assert COOKIE_NAME in response.cookies, "a successful login must set the session cookie"


async def test_a_wrong_password_is_refused(secured) -> None:
    _, client = secured
    assert (await client.get("/api/health", headers=_basic(USER, "wrong"))).status_code == 401
    assert (await client.get("/api/health", headers=_basic("nobody", PASSWORD))).status_code == 401


# =======================================================================================
# The login page has to be able to load
# =======================================================================================


async def test_a_navigation_gets_the_shell_not_a_401(secured) -> None:
    """A browser asking for a page gets the app, which renders the login screen itself."""
    _, client = secured
    for path in ("/", "/settings", "/positions"):
        response = await client.get(path, headers=NAVIGATION)
        assert response.status_code == 200, f"{path} answered {response.status_code} to a navigation"
        assert response.headers["content-type"].startswith("text/html")
        assert SHELL_MARKER in response.text
        assert "www-authenticate" not in response.headers
        assert COOKIE_NAME not in response.cookies, "the shell must not hand out a session"


async def test_an_api_call_is_still_refused_even_asking_for_html(secured) -> None:
    """The shell fallback is for navigations only; it must not become a way into the API."""
    _, client = secured
    for path in ("/api/health", "/api/settings", "/api/trading/positions"):
        response = await client.get(path, headers=NAVIGATION)
        assert response.status_code == 401, f"{path} leaked to an Accept: text/html request"
        assert response.json()["error"]["code"] == "unauthorized"


async def test_the_bundle_loads_unauthenticated(secured) -> None:
    """Without /assets the login page cannot paint, so it is open by design."""
    _, client = secured
    response = await client.get("/assets/app.js")
    assert response.status_code == 200, "the SPA bundle must load before anyone can log in"
    assert BUNDLE_MARKER in response.text


# =======================================================================================
# POST /api/auth/login
# =======================================================================================


async def test_login_succeeds_and_the_cookie_it_sets_works(secured) -> None:
    """The whole point: username and password in, a session the next request is judged by."""
    _, client = secured
    response = await client.post("/api/auth/login", json={"username": USER, "password": PASSWORD})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "username": USER}

    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Path=/" in cookie
    assert "Secure" not in cookie, "this request came over plain http; Secure would be dropped"

    # The client kept the cookie, so the gate now lets it through with no header at all.
    assert COOKIE_NAME in client.cookies
    assert (await client.get("/api/health")).status_code == 200
    assert (await client.post("/api/trading/arm", json={"armed": False})).status_code != 401


async def test_login_over_https_marks_the_cookie_secure(secured) -> None:
    """Secure is set from the request's own scheme, never guessed."""
    app, _ = secured
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://t"
    ) as client:
        response = await client.post(
            "/api/auth/login", json={"username": USER, "password": PASSWORD}
        )
    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]


async def test_login_failure_is_401_and_sets_no_cookie(secured) -> None:
    """A wrong password must not leave a session behind, and must not say which half was wrong."""
    _, client = secured
    for body in (
        {"username": USER, "password": "wrong"},
        {"username": "nobody", "password": PASSWORD},
        {"username": "", "password": ""},
    ):
        response = await client.post("/api/auth/login", json=body)
        assert response.status_code == 401, f"{body['username']!r} was not refused"
        assert response.json()["error"]["code"] == "invalid_credentials"
        assert "set-cookie" not in response.headers
        assert COOKIE_NAME not in client.cookies

    assert (await client.get("/api/health")).status_code == 401


async def test_login_is_reachable_but_useless_when_no_login_is_configured(unsecured) -> None:
    """No configured pair can match anything, and it says so with the same code."""
    _, client = unsecured
    response = await client.post("/api/auth/login", json={"username": "a", "password": "b"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


# =======================================================================================
# POST /api/auth/logout
# =======================================================================================


async def test_logout_clears_the_cookie(secured) -> None:
    """And the very next request is refused again."""
    _, client = secured
    assert (
        await client.post("/api/auth/login", json={"username": USER, "password": PASSWORD})
    ).status_code == 200
    assert (await client.get("/api/health")).status_code == 200

    response = await client.post("/api/auth/logout")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    cleared = response.headers["set-cookie"]
    assert cleared.startswith(f"{COOKIE_NAME}=;") and "Max-Age=0" in cleared
    assert "Path=/" in cleared, "attributes must match the ones it was set with, or it survives"

    assert COOKIE_NAME not in client.cookies, "the browser was told to drop it and did not"
    assert (await client.get("/api/health")).status_code == 401


async def test_logout_works_when_nobody_was_signed_in(secured) -> None:
    """It is reachable without credentials, so it must be harmless without them too."""
    _, client = secured
    response = await client.post("/api/auth/logout")
    assert response.status_code == 200
    assert response.json()["ok"] is True


# =======================================================================================
# GET /api/auth/status
# =======================================================================================


async def test_status_answers_unauthenticated_and_leaks_nothing_else(secured) -> None:
    """This is how the SPA knows to render the login page — and it is all it may learn."""
    _, client = secured
    response = await client.get("/api/auth/status")
    assert response.status_code == 200
    assert response.json() == {"authenticated": False, "required": True, "username": None}, (
        "status must carry the login question and nothing about the system, settings or account"
    )


async def test_status_reports_the_session_once_there_is_one(secured) -> None:
    _, client = secured
    await client.post("/api/auth/login", json={"username": USER, "password": PASSWORD})
    assert (await client.get("/api/auth/status")).json() == {
        "authenticated": True,
        "required": True,
        "username": USER,
    }


async def test_status_accepts_basic_too(secured) -> None:
    """A script that authenticates the curl way sees itself as signed in."""
    _, client = secured
    assert (await client.get("/api/auth/status", headers=HEADER)).json()["authenticated"] is True


async def test_status_says_no_login_is_required_when_none_is_configured(unsecured) -> None:
    """On a localhost deployment the SPA must go straight to the dashboard."""
    _, client = unsecured
    assert (await client.get("/api/auth/status")).json() == {
        "authenticated": True,
        "required": False,
        "username": None,
    }


# =======================================================================================
# Brute force
# =======================================================================================


async def test_the_throttle_the_app_ships_with_trips_after_eight_failures(secured) -> None:
    """The limiter `create_app` installs, at its real settings, on the real route."""
    app, client = secured
    assert isinstance(app.state.login_rate_limiter, LoginRateLimiter), (
        "create_app must install the throttle; without it every login is unmetered"
    )

    for attempt in range(MAX_FAILED_ATTEMPTS):
        response = await client.post(
            "/api/auth/login", json={"username": USER, "password": "wrong"}
        )
        assert response.status_code == 401, f"attempt {attempt + 1} should still be a plain refusal"

    refused = await client.post("/api/auth/login", json={"username": USER, "password": "wrong"})
    assert refused.status_code == 429
    assert refused.json()["error"]["detail"]["retry_after"] == 60


async def test_the_rate_limit_trips_and_then_recovers(secured) -> None:
    """Eight wrong passphrases from one address buy a minute of silence — and only a minute."""
    app, client = secured
    # A fake clock rather than a real minute of sleep; everything else is the live route.
    now = {"t": 10_000.0}
    app.state.login_rate_limiter = LoginRateLimiter(clock=lambda: now["t"])

    for attempt in range(MAX_FAILED_ATTEMPTS):
        response = await client.post(
            "/api/auth/login", json={"username": USER, "password": "wrong"}
        )
        assert response.status_code == 401, f"attempt {attempt + 1} should still be a plain refusal"

    refused = await client.post("/api/auth/login", json={"username": USER, "password": "wrong"})
    assert refused.status_code == 429
    body = refused.json()["error"]
    assert body["code"] == "too_many_attempts"
    assert 0 < body["detail"]["retry_after"] <= 60
    assert refused.headers["retry-after"] == str(body["detail"]["retry_after"])

    # The correct passphrase is refused too while the address is locked: a throttle that let
    # the right answer through would be an oracle for finding it.
    locked = await client.post("/api/auth/login", json={"username": USER, "password": PASSWORD})
    assert locked.status_code == 429
    assert COOKIE_NAME not in client.cookies

    # ...and the lock lifts on its own, with no successful login needed to clear it.
    now["t"] += 61.0
    recovered = await client.post("/api/auth/login", json={"username": USER, "password": PASSWORD})
    assert recovered.status_code == 200, "a correct password must never be locked out permanently"
    assert COOKIE_NAME in client.cookies
    assert (await client.get("/api/health")).status_code == 200


async def test_a_success_wipes_the_failures_before_it(secured) -> None:
    """Seven typos then the right passphrase must leave nothing behind for the next session."""
    app, client = secured
    for _ in range(MAX_FAILED_ATTEMPTS - 1):
        assert (
            await client.post("/api/auth/login", json={"username": USER, "password": "no"})
        ).status_code == 401

    assert (
        await client.post("/api/auth/login", json={"username": USER, "password": PASSWORD})
    ).status_code == 200

    # The count is back to zero, so the next typo is attempt one, not attempt nine.
    assert (
        await client.post("/api/auth/login", json={"username": USER, "password": "no"})
    ).status_code == 401


def test_the_rate_limiter_counts_only_failures_and_only_per_client() -> None:
    """The unit behind the route: window arithmetic, isolation, and the reset on success."""
    now = {"t": 1000.0}
    limiter = LoginRateLimiter(max_attempts=3, window=60.0, clock=lambda: now["t"])

    assert limiter.retry_after("1.2.3.4") == 0
    for _ in range(3):
        limiter.record_failure("1.2.3.4")

    assert limiter.retry_after("1.2.3.4") == 60
    assert limiter.retry_after("5.6.7.8") == 0, "one attacker must not lock out the operator"

    now["t"] += 30.0
    assert limiter.retry_after("1.2.3.4") == 30, "the window counts down from the last failure"

    now["t"] += 31.0
    assert limiter.retry_after("1.2.3.4") == 0, "and expires without any successful login"

    # Expiry forgets the client outright: the next single typo starts a fresh count.
    limiter.record_failure("1.2.3.4")
    assert limiter.retry_after("1.2.3.4") == 0

    for _ in range(2):
        limiter.record_failure("1.2.3.4")
    assert limiter.retry_after("1.2.3.4") == 60
    limiter.record_success("1.2.3.4")
    assert limiter.retry_after("1.2.3.4") == 0


# =======================================================================================
# The WebSocket — the hole a BaseHTTPMiddleware guard would leave wide open
# =======================================================================================


def _ws_scope(app: Any, headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    """Build a raw `/api/stream` handshake scope, since httpx cannot speak WebSocket."""
    return {
        "type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "scheme": "ws", "path": "/api/stream", "raw_path": b"/api/stream", "query_string": b"",
        "root_path": "", "headers": headers, "client": ("127.0.0.1", 1234),
        "server": ("t", 80), "subprotocols": [], "state": {}, "app": app,
    }


async def test_the_websocket_is_closed_without_credentials(secured) -> None:
    """The live feed of every run, position and account tick is not open to the internet."""
    app, _ = secured
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(_ws_scope(app, [(b"host", b"t")]), receive, send)

    assert messages, "the connection must be answered, not left hanging"
    assert messages[0]["type"] == "websocket.close", (
        f"/api/stream accepted an unauthenticated WebSocket: {messages[0]}"
    )
    assert messages[0].get("code") == 1008
    assert not any(m.get("type") == "websocket.accept" for m in messages)


async def test_the_websocket_is_closed_even_asking_for_html(secured) -> None:
    """The shell fallback is HTTP-only; a handshake can never be talked into a 200."""
    app, _ = secured
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    headers = [(b"host", b"t"), (b"accept", b"text/html")]
    await app(_ws_scope(app, headers), receive, send)
    assert messages[0]["type"] == "websocket.close"
    assert not any(m.get("type") == "websocket.accept" for m in messages)


async def test_the_websocket_opens_with_avalid_cookie(secured) -> None:
    """And the authenticated browser is not locked out of its own live feed."""
    app, _ = secured

    from app.config import settings as app_settings

    cookie = _sign(app.state.auth_sessions.secret, int(time.time()))
    messages: list[dict[str, Any]] = []
    sent = {"done": False}

    async def receive() -> dict[str, Any]:
        if not sent["done"]:
            sent["done"] = True
            return {"type": "websocket.connect"}
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    headers = [(b"host", b"t"), (b"cookie", f"{COOKIE_NAME}={cookie}".encode())]
    await app(_ws_scope(app, headers), receive, send)
    assert any(m.get("type") == "websocket.accept" for m in messages), (
        f"a valid cookie must be accepted, got {[m.get('type') for m in messages]}"
    )


async def test_the_cookie_the_login_route_hands_out_opens_the_websocket(secured) -> None:
    """The end-to-end claim: sign in through the SPA, and the live feed connects."""
    app, client = secured
    await client.post("/api/auth/login", json={"username": USER, "password": PASSWORD})
    cookie = client.cookies[COOKIE_NAME]

    messages: list[dict[str, Any]] = []
    sent = {"done": False}

    async def receive() -> dict[str, Any]:
        if not sent["done"]:
            sent["done"] = True
            return {"type": "websocket.connect"}
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    headers = [(b"host", b"t"), (b"cookie", f"{COOKIE_NAME}={cookie}".encode())]
    await app(_ws_scope(app, headers), receive, send)
    assert any(m.get("type") == "websocket.accept" for m in messages)


# =======================================================================================
# The cookie itself
# =======================================================================================


def test_a_forged_cookie_is_rejected() -> None:
    """The signature is an HMAC over SECRET_KEY, so it cannot be minted client-side."""
    now = int(time.time())
    assert valid_cookie("the-real-secret", _sign("the-real-secret", now)) is True
    assert valid_cookie("the-real-secret", _sign("a-different-secret", now)) is False
    assert valid_cookie("the-real-secret", f"{now}.deadbeef") is False
    assert valid_cookie("the-real-secret", "not-a-cookie") is False
    # And an old one expires rather than lasting forever.
    assert valid_cookie("the-real-secret", _sign("the-real-secret", now - 999_999)) is False


def test_missing_credentials_fail_closed_unless_explicit_dev_opt_in() -> None:
    """Localhost use stays frictionless; the warning at startup is what flags it."""
    middleware = AuthMiddleware(lambda *a: None, username="", password="", secret="x")
    assert middleware.enabled is True
    dev = AuthMiddleware(lambda *a: None, username="", password="", secret="x", allow_unconfigured=True)
    assert dev.enabled is False


def test_the_open_prefix_cannot_be_walked_out_of() -> None:
    """The exemption list is a prefix match, so it has to refuse a traversal outright."""
    from app.util.auth import is_public_path

    assert is_public_path("/assets/index-abc123.js") is True
    assert is_public_path("/api/auth/status") is True
    assert is_public_path("/api/settings") is False
    assert is_public_path("/api/stream") is False
    assert is_public_path("/assets/../api/settings") is False
    assert is_public_path("/assets/..%2fapi/settings".replace("%2f", "/")) is False


def test_a_non_ascii_passphrase_fails_the_login_instead_of_raising() -> None:
    """`hmac.compare_digest` refuses a non-ASCII `str`; comparing bytes is why this is a 401."""
    from app.util.auth import credentials_match

    assert credentials_match("op", "пароль", "op", "пароль") is True
    assert credentials_match("op", "пароль", "op", PASSWORD) is False
    assert credentials_match("op", PASSWORD, "op", "пароль") is False
