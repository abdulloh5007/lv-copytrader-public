"""Public data boundary, accounting, bounded presence and cache invariants."""

from __future__ import annotations

import asyncio
import gzip
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.config import settings
from app.db import init_db, session_scope
from app.main import create_app
from app.models import Trade
from app.public_demo.limits import Buckets, PublicLimits, Viewers
from app.public_demo.service import account_projection


class FakeAccount:
    def __init__(self):
        self.calls = 0
        self.fail = False

    async def account_info(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("PRIVATE-SECRET must never reach a public response")
        return SimpleNamespace(
            balance=2500.5,
            equity=2510.5,
            profit=10.0,
            currency="USD",
            trade_mode=0,
            login=123456789,
            name="PRIVATE-ACCOUNT-OWNER",
            server="PRIVATE-BROKER",
            password="PRIVATE-SECRET",
            email="owner@example.com",
        )


@pytest.fixture
async def demo(data_dir, monkeypatch):
    monkeypatch.setattr(settings, "SP_AUTH_USER", "operator")
    monkeypatch.setattr(settings, "SP_AUTH_PASSWORD", "private-password")
    await init_db()
    app = create_app()
    rpc = FakeAccount()
    app.state.runtime = SimpleNamespace(
        rpc=rpc,
        status={
            "armed": True,
            "telegram": {"ok": True, "error": "PRIVATE-ERROR"},
            "provider": {"ok": False, "error": "PRIVATE-PROVIDER-KEY"},
        },
    )
    await app.state.public_demo.refresh()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        yield app, client, rpc
    await app.state.public_demo.stop()


async def seed(
    profit, *, ticket, external=False, magic=None, status="closed", kind="position", day=0
):
    closed_at = datetime(2026, 10, 21, 12) - timedelta(days=day)
    async with session_scope() as session:
        session.add(
            Trade(
                ticket=ticket,
                kind=kind,
                symbol="XAUUSDr",
                side="buy",
                order_type=0,
                volume=0.2,
                entry_price=2400,
                close_price=2410,
                sl=2390,
                tp=2420,
                status=status,
                magic=settings.MAGIC_NUMBER if magic is None else magic,
                external=external,
                profit=profit,
                opened_at=closed_at - timedelta(hours=1),
                closed_at=closed_at if status == "closed" else None,
                comment="PRIVATE-TRADE-COMMENT",
                open_reason="PRIVATE-CHANNEL-TEXT",
            )
        )


async def test_public_snapshot_is_an_allowlist_and_uses_real_values(demo):
    app, client, rpc = demo
    response = await client.get("/api/demo/snapshot")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "account",
        "account_updated_at",
        "history_updated_at",
        "copytrader",
        "status",
        "generated_at",
    }
    assert body["account"] == {
        "balance": 2500.5,
        "equity": 2510.5,
        "floating_profit": 10.0,
        "currency": "USD",
        "mode": "demo",
    }
    assert body["status"] == {"broker": True, "telegram": True, "running": False}
    assert not any(
        v in response.text
        for v in ["PRIVATE", "123456789", "owner@example.com", "private-password"]
    )
    assert "set-cookie" not in response.headers
    assert "public" in response.headers["cache-control"]


async def test_only_copytrader_closed_positions_contribute_and_null_is_not_a_loss(demo):
    app, client, _ = demo
    await seed(12.5, ticket=1001, day=2)
    await seed(-4.25, ticket=1002, day=1)
    await seed(0, ticket=1003, day=0)
    await seed(None, ticket=1004, day=0)
    await seed(9999, ticket=1005, external=True)
    await seed(9999, ticket=1006, magic=settings.MAGIC_NUMBER + 1)
    await seed(9999, ticket=1007, kind="order")
    await seed(100, ticket=1008, status="open")
    app.state.public_demo.history_at = None
    await app.state.public_demo.refresh()
    body = (await client.get("/api/demo/snapshot")).json()["copytrader"]
    assert body["closed"] == 4
    assert body["settled"] == 3
    assert body["profit"] == 8.25
    assert body["wins"] == 1 and body["losses"] == 1
    assert body["win_rate"] == 33.3
    assert body["open"] == 1
    assert body["curve"][-1]["profit"] == 8.25
    assert len(body["trades"]) == 4
    assert set(body["trades"][0]) == {
        "symbol",
        "side",
        "volume",
        "entry",
        "exit",
        "profit",
        "opened_at",
        "closed_at",
    }
    assert "PRIVATE" not in json.dumps(body)


async def test_history_is_bounded_and_cumulative_curve_retains_prior_profit(demo):
    app, client, _ = demo
    for i in range(95):
        await seed(1, ticket=2000 + i, day=i)
    app.state.public_demo.history_at = None
    await app.state.public_demo.refresh()
    body = (await client.get("/api/demo/snapshot")).json()["copytrader"]
    assert body["closed"] == 95 and body["profit"] == 95
    assert len(body["trades"]) == 30 and len(body["curve"]) == 90
    assert body["curve"][0]["profit"] == 6
    assert body["curve"][-1]["profit"] == 95


async def test_concurrent_visitors_never_trigger_broker_or_database_work(demo, monkeypatch):
    app, _, rpc = demo
    original = rpc.calls

    async def forbid():
        raise AssertionError("A request called the broker")

    monkeypatch.setattr(rpc, "account_info", forbid)

    async def visit(index):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app, client=(f"10.0.{index // 255}.{index % 255}", 1)
            ),
            base_url="https://test",
        ) as client:
            return await client.get("/api/demo/snapshot")

    responses = await asyncio.gather(*[visit(i) for i in range(300)])
    assert all(r.status_code == 200 for r in responses)
    assert all(r.content == responses[0].content for r in responses)
    assert rpc.calls == original


async def test_etag_head_gzip_and_cookies_do_not_change_the_snapshot(demo):
    _, client, _ = demo
    first = await client.get("/api/demo/snapshot")
    response = await client.get(
        "/api/demo/snapshot", headers={"If-None-Match": first.headers["etag"]}
    )
    assert response.status_code == 304 and not response.content
    head = await client.head("/api/demo/snapshot")
    assert head.status_code == 200 and not head.content
    identity = await client.get(
        "/api/demo/snapshot", headers={"Accept-Encoding": "identity", "Cookie": "sp_auth=invalid"}
    )
    assert identity.json() == first.json()
    assert identity.headers["etag"] != first.headers["etag"]
    assert "content-encoding" not in identity.headers
    assert "Set-Cookie" not in identity.headers


async def test_presence_deduplicates_tabs_and_cannot_authenticate_operator(demo):
    _, client, _ = demo
    first = await client.post("/api/demo/presence")
    assert first.json() == {"viewers": 1, "lease_seconds": 50}
    cookie = first.headers["set-cookie"]
    assert (
        "HttpOnly" in cookie
        and "Secure" in cookie
        and "SameSite=strict" in cookie
        and "Path=/api/demo" in cookie
    )
    assert (await client.post("/api/demo/presence")).json()["viewers"] == 1
    assert "no-store" in first.headers["cache-control"]
    token = client.cookies.get(Viewers.COOKIE)
    client.cookies.set("sp_auth", token, domain="test.local", path="/")
    assert (await client.get("/api/settings")).status_code == 401
    assert (await client.get("/api/waitlist")).status_code == 401


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/health"),
        ("GET", "/api/settings"),
        ("GET", "/api/history/trades"),
        ("GET", "/api/trading/account"),
        ("POST", "/api/trading/arm"),
        ("POST", "/api/trading/close"),
        ("GET", "/api/docs"),
        ("GET", "/api/demo/presence"),
        ("POST", "/api/demo/snapshot"),
        ("GET", "/api/demo/settings"),
        ("GET", "/api/demo/snapshot/private"),
    ],
)
async def test_demo_does_not_open_other_endpoints(demo, method, path):
    _, client, _ = demo
    assert (await client.request(method, path)).status_code == 401


async def test_snapshot_cannot_select_an_account_or_query_private_data(demo):
    _, client, _ = demo
    for query in [
        "?account=123",
        "?include=secrets",
        "?limit=999999999",
        "?sql=SELECT+*+FROM+app_settings",
    ]:
        assert (await client.get("/api/demo/snapshot" + query)).status_code == 400
    for path in ["/api/demo/%2e%2e/settings", "/api/demo/snapshot%2f..%2f..%2fsettings"]:
        assert (await client.get(path)).status_code == 401


async def test_presence_rejects_cross_origin_bodies_and_large_chunked_input(demo):
    _, client, _ = demo
    assert (
        await client.post("/api/demo/presence", headers={"Origin": "https://evil.test"})
    ).status_code == 403
    assert (
        await client.post("/api/demo/presence", json={"email": "someone@example.com"})
    ).status_code == 400

    async def chunks():
        yield b"a" * 200
        yield b"a" * 200

    assert (await client.post("/api/demo/presence", content=chunks())).status_code == 413


async def test_failed_broker_keeps_original_timestamp_and_never_exposes_error(demo):
    app, client, rpc = demo
    previous = app.state.public_demo.account_at
    rpc.fail = True
    await app.state.public_demo.refresh()
    body = (await client.get("/api/demo/snapshot")).json()
    assert body["account_updated_at"] == previous
    assert body["account"]["balance"] == 2500.5
    assert body["status"]["broker"] is False
    assert "PRIVATE" not in json.dumps(body)


async def test_public_rate_limit_and_new_cookie_issuance_are_bounded(demo):
    app, client, _ = demo
    app.state.public_demo.limits.peers = Buckets(capacity=2, refill=0.001)
    assert (await client.get("/api/demo/snapshot")).status_code == 200
    assert (await client.get("/api/demo/snapshot")).status_code == 200
    response = await client.get("/api/demo/snapshot")
    assert response.status_code == 429 and response.headers.get("retry-after")
    assert (await client.get("/api/settings")).status_code == 401


def test_presence_expiry_forgery_future_and_capacity():
    now = [1000.0]
    viewers = Viewers(lambda: "secret", clock=lambda: now[0], wall=lambda: now[0], maximum=2)
    token = viewers.mint()
    assert viewers.valid(token)
    assert not viewers.valid(token[:-1] + "x")
    assert not viewers.valid("١٢٣.abc.def")
    assert not viewers.valid("9" * 100000)
    viewers.touch(token)
    viewers.touch(token)
    assert viewers.count == 1
    other = viewers.mint()
    assert viewers.touch(other)
    assert not viewers.touch(viewers.mint())
    now[0] += 51
    assert viewers.count == 0
    now[0] = 999
    assert not viewers.valid(token)


def test_rate_limiter_does_not_grow_unbounded_or_evict_active_keys():
    now = [1.0]
    buckets = Buckets(2, 1, max_keys=2, clock=lambda: now[0])
    assert buckets.admit("a") == 0 and buckets.admit("b") == 0
    assert buckets.admit("c") > 0 and len(buckets.entries) == 2
    assert buckets.admit("a") == 0 and buckets.admit("a") > 0
    now[0] += 601
    assert buckets.admit("c") == 0 and len(buckets.entries) == 1


def test_projection_rejects_nonfinite_values():
    for value in [float("nan"), float("inf"), "bad", 1e100]:
        assert account_projection({"balance": value, "equity": 10}) is None


async def test_background_cache_stops_and_does_not_poll_without_viewers(demo):
    app, _, rpc = demo
    service = app.state.public_demo
    previous = rpc.calls
    service.REFRESH = 0.01
    service.ACTIVE_FOR = 0.025
    service.start()
    await asyncio.sleep(0.02)
    assert rpc.calls == previous
    service.demand()
    await asyncio.sleep(0.08)
    await service.stop()
    assert rpc.calls > previous
    assert service.task is None


async def test_encoding_negotiation_and_head_representation_length(demo):
    _, client, _ = demo
    for accept in ["gzip;q=0", "gzip;q=0, *;q=1", "identity"]:
        response = await client.get("/api/demo/snapshot", headers={"Accept-Encoding": accept})
        assert "content-encoding" not in response.headers
    get = await client.get("/api/demo/snapshot", headers={"Accept-Encoding": "gzip"})
    head = await client.head("/api/demo/snapshot", headers={"Accept-Encoding": "gzip"})
    assert get.headers["content-length"] == head.headers["content-length"]
    assert get.headers["etag"] == head.headers["etag"]


async def test_error_responses_are_never_shared_cached(demo):
    _, client, _ = demo
    response = await client.get("/api/demo/snapshot?account=123")
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"


async def test_cookie_minting_rate_cannot_be_reset_by_clearing_browser_cookies(demo):
    app, client, _ = demo
    app.state.public_demo.limits.issuance = Buckets(2, 0.001, clock=lambda: 100)
    for _ in range(2):
        client.cookies.clear()
        assert (await client.post("/api/demo/presence")).status_code == 200
    client.cookies.clear()
    response = await client.post("/api/demo/presence")
    assert response.status_code == 429
    assert app.state.public_demo.viewers.count == 2


async def test_authenticated_snapshot_cannot_poison_public_cache_or_mint_auth_cookie(demo):
    _, client, _ = demo
    public = await client.get("/api/demo/snapshot")
    authenticated = await client.get("/api/demo/snapshot", auth=("operator", "private-password"))
    assert public.content == authenticated.content
    assert public.headers["etag"] == authenticated.headers["etag"]
    assert "set-cookie" not in authenticated.headers


async def test_public_http_exceptions_do_not_open_any_websocket(demo):
    app, _, _ = demo
    for path in ["/api/demo/snapshot", "/api/demo/presence", "/api/stream"]:
        messages = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            messages.append(message)

        await app(
            {
                "type": "websocket",
                "path": path,
                "scheme": "wss",
                "headers": [(b"host", b"test"), (b"origin", b"https://test")],
                "query_string": b"",
                "client": ("127.0.0.1", 1),
                "server": ("test", 443),
                "subprotocols": [],
            },
            receive,
            send,
        )
        assert messages == [{"type": "websocket.close", "code": 1008}]
