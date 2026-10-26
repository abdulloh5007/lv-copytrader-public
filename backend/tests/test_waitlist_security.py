"""Public signup and browser security regressions, with an isolated SQLite DB."""
import asyncio
import time

import httpx
import pytest
from sqlalchemy import func, select

from app.config import settings
from app.crypto import decrypt
from app.db import init_db, session_scope
from app.main import create_app
from app.models import WaitlistEntry
from app.util.auth import COOKIE_NAME, _sign, valid_cookie


@pytest.fixture
async def client(data_dir, monkeypatch):
    monkeypatch.setattr(settings, "SP_AUTH_USER", "operator")
    monkeypatch.setattr(settings, "SP_AUTH_PASSWORD", "long-test-password")
    await init_db()
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as c:
        yield c


async def test_public_signup_is_durable_encrypted_and_deduplicated(client):
    for email in ["Tester@Example.com", "tester@example.com"]:
        response = await client.post("/api/waitlist", json={"email": email, "consent": True})
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert "tester" not in response.text.lower()
    async with session_scope() as session:
        rows = (await session.scalars(select(WaitlistEntry))).all()
        assert len(rows) == 1
        assert rows[0].email_encrypted.startswith("enc:v1:")
        assert "tester" not in rows[0].email_encrypted
        assert decrypt(rows[0].email_encrypted) == "tester@example.com"
        assert rows[0].created_at is not None
        assert rows[0].consent_version == "2026-10-21"


@pytest.mark.parametrize("email", ["bad", "a@localhost", "a@-bad.com", "a..b@example.com", ".a@example.com", "a.@example.com", "a\n@example.com", "x" * 65 + "@example.com"])
async def test_invalid_email_is_rejected_without_echoing_input(client, email):
    response = await client.post("/api/waitlist", json={"email": email, "consent": True})
    assert response.status_code == 422
    assert '"input"' not in response.text


async def test_consent_is_required_and_honeypot_is_silent(client):
    for payload in [{"email": "a@example.com"}, {"email": "a@example.com", "consent": False}]:
        assert (await client.post("/api/waitlist", json=payload)).status_code == 422
    assert (await client.post("/api/waitlist", json={"email": "bot@example.com", "consent": True, "website": "spam"})).json() == {"ok": True}
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(WaitlistEntry)) == 0


async def test_only_signup_is_public_and_no_operator_data_leaks(client):
    for path in ["/api/waitlist", "/api/settings", "/api/health", "/api/trading/positions", "/api/docs"]:
        assert (await client.get(path)).status_code == 401
    response = await client.get("/api/waitlist", auth=("operator", "long-test-password"))
    assert response.json() == {"count": 0}


async def test_signup_throttle_survives_application_recreation(client):
    for i in range(20):
        assert (await client.post("/api/waitlist", json={"email": f"person{i}@example.com", "consent": True})).status_code == 200
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="https://test") as new_client:
        response = await new_client.post("/api/waitlist", json={"email": "next@example.com", "consent": True})
    assert response.status_code == 429
    assert 1 <= int(response.headers["retry-after"]) <= 60


async def test_concurrent_duplicate_signup_stores_one_row(client):
    replies = await asyncio.gather(*[client.post("/api/waitlist", json={"email": "same@example.com", "consent": True}) for _ in range(6)])
    assert all(r.status_code == 200 for r in replies)
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(WaitlistEntry)) == 1


@pytest.mark.parametrize("path", ["/api/waitlist", "/api/auth/login", "/api/auth/logout", "/api/trading/arm"])
async def test_cross_origin_writes_are_refused(client, path):
    response = await client.post(path, json={}, headers={"Origin": "https://attacker.example"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "cross_origin_request"


async def test_same_origin_login_works_and_invalid_input_is_not_echoed(client):
    response = await client.post("/api/auth/login", json={"username": "operator", "password": "long-test-password"}, headers={"Origin": "https://test"})
    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]
    response = await client.post("/api/auth/login", json={"username": "operator", "password": {"secret": "never-echo-this"}})
    assert response.status_code == 422
    assert "never-echo-this" not in response.text


async def test_security_headers_on_unauthorized_and_public_responses(client):
    for path in ["/api/settings", "/api/auth/status"]:
        response = await client.get(path)
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["cache-control"] == "no-store"
        assert "script-src 'self'" in response.headers["content-security-policy"]
        assert "object-src 'none'" in response.headers["content-security-policy"]


async def test_large_public_json_is_refused_even_without_content_length(client):
    async def body():
        yield b'{"email":"'
        yield b"a" * 9000
        yield b'"}'
    response = await client.post("/api/waitlist", content=body(), headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert (await client.post("/api/auth/login", content=b"a" * 9000)).status_code == 413


@pytest.mark.parametrize("value", ["١٢٣.abc", "9" * 200 + ".abc", ".", "0." + "а" * 64])
def test_malformed_cookie_never_crashes(value):
    assert not valid_cookie("key", value)


def test_future_cookie_is_not_a_valid_session():
    assert not valid_cookie("key", _sign("key", int(time.time()) + 3600))


async def test_session_key_follows_bootstrap_instead_of_capturing_old_key(client, monkeypatch):
    first = await client.get("/api/auth/status")  # instantiate middleware
    assert first.status_code == 200
    monkeypatch.setattr(settings, "SECRET_KEY", "rotated-session-key")
    response = await client.post("/api/auth/login", json={"username": "operator", "password": "long-test-password"})
    assert response.status_code == 200
    assert COOKIE_NAME in client.cookies
    assert (await client.get("/api/health")).status_code == 200


@pytest.mark.parametrize("path", ["/api/health", "/api/auth/status"])
async def test_basic_auth_cannot_bypass_login_throttle(client, path):
    from app.util.auth import MAX_FAILED_ATTEMPTS
    for _ in range(MAX_FAILED_ATTEMPTS):
        await client.get(path, auth=("operator", "incorrect"))
    response = await client.get(path, auth=("operator", "long-test-password"))
    assert response.status_code == 429
    assert "retry-after" in response.headers


async def test_owner_cli_export_and_deletion(client, capsys):
    from app.cli.waitlist import manage
    assert (await client.post("/api/waitlist", json={"email": "=person@example.com", "consent": True})).status_code == 200
    await manage("count")
    assert capsys.readouterr().out.strip() == "1"
    await manage("export")
    exported = capsys.readouterr().out
    assert "email,consented_at_utc,consent_version" in exported
    assert "'=person@example.com" in exported
    await manage("delete", "=PERSON@example.com")
    assert capsys.readouterr().out.strip() == "Deleted: 1"
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(WaitlistEntry)) == 0


async def test_cross_origin_websocket_is_closed_before_accept(client):
    messages = []
    async def receive():
        return {"type": "websocket.connect"}
    async def send(message):
        messages.append(message)
    await create_app()({"type": "websocket", "path": "/api/stream", "scheme": "wss", "headers": [(b"host", b"test"), (b"origin", b"https://attacker.example")], "query_string": b"", "client": ("127.0.0.1", 1), "server": ("test", 443), "subprotocols": []}, receive, send)
    assert messages == [{"type": "websocket.close", "code": 1008}]
