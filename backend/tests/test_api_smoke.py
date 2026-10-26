"""The whole backend, booted for real and driven over HTTP with no MT5 and no Telegram.

This is the regression test for the property BUILD_PROMPT §9 states as an acceptance
criterion: **the process comes up DEGRADED but SERVING**. There is no sidecar on the other
end of ``SIDECAR_URL`` and no Telegram session in the database, so every dot is red — and
every read route still has to answer 200 with the truth in it, because the dashboard is the
only place the operator can fix any of that, and they can only reach it if it renders.

A route that answers 500 when MT5 is down fails this file, and it should: it would put the
operator in front of a blank page with a stack trace in the logs and no way back.

The app is exercised through ``httpx.ASGITransport``, so it runs in this process, in this
event loop, with no socket and no server. The lifespan is entered explicitly — ASGITransport
does not run it — which means this covers the real startup path: SECRET_KEY bootstrap, schema
creation, the service container, the failed MT5 connect, reconciliation, the ingest worker,
the notifier, the SL watchdog and the account-tick task, followed by the ordered shutdown.

The WebSocket is driven over the raw ASGI protocol for the same reason: ``ASGITransport``
speaks HTTP only, and a thread-backed test client would run the app on a second event loop,
which is precisely the mistake main.py's closing comment block exists to prevent.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest

from app.crypto import MASK

# A password, an API key, a bot token and an api_hash that must never appear in a response
# body. Distinct strings so a leak names the field it leaked from.
MT5_PASSWORD = "mt5-plaintext-secret-9f3a"
PROVIDER_KEY = "sk-provider-plaintext-7c21"
BOT_TOKEN = "123456:bot-plaintext-token-4d8e"
API_HASH = "0123456789abcdef0123456789abcdef"  # gitleaks:allow -- sequential fake fixture

PLAINTEXT_SECRETS = (MT5_PASSWORD, PROVIDER_KEY, BOT_TOKEN, API_HASH)


# ---------------------------------------------------------------------------
# Booting the real application
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(data_dir: Path) -> AsyncIterator[httpx.AsyncClient]:
    """Boot the real app through its real lifespan and yield an ASGI-transport client.

    ``create_app()`` is called per test rather than importing the module-level ``app``, so
    each test gets its own ``Runtime``, its own background tasks and its own status map, and
    nothing leaks from one test into the next.
    """
    from app.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            http.app = app  # the websocket driver below needs the ASGI callable
            yield http


class AsgiWebSocket:
    """A minimal ASGI websocket client: enough to connect, read frames and send pings."""

    def __init__(self, app: Any, path: str) -> None:
        self._app = app
        self._path = path
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[Any] | None = None

    async def __aenter__(self) -> "AsgiWebSocket":
        scope: dict[str, Any] = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": self._path,
            "raw_path": self._path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 51234),
            "server": ("testserver", 80),
            "subprotocols": [],
            "state": {},
        }
        await self._inbox.put({"type": "websocket.connect"})
        self._task = asyncio.create_task(
            self._app(scope, self._inbox.get, self._outbox.put), name="sp-test-ws"
        )
        opening = await self.receive()
        assert opening["type"] == "websocket.accept", opening
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self._inbox.put({"type": "websocket.disconnect", "code": 1000})
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (TimeoutError, asyncio.TimeoutError):
                self._task.cancel()
                with_suppressed = asyncio.gather(self._task, return_exceptions=True)
                await with_suppressed

    async def receive(self, timeout: float = 5.0) -> dict[str, Any]:
        """Return the next ASGI message the app sent, failing the test on silence."""
        return await asyncio.wait_for(self._outbox.get(), timeout=timeout)

    async def receive_json(self, timeout: float = 5.0) -> dict[str, Any]:
        """Return the next text frame decoded from JSON."""
        message = await self.receive(timeout=timeout)
        assert message["type"] == "websocket.send", message
        payload = message.get("text")
        if payload is None:
            payload = bytes(message["bytes"]).decode("utf-8")
        return json.loads(payload)

    async def send_json(self, payload: dict[str, Any]) -> None:
        """Send one text frame to the app."""
        await self._inbox.put(
            {"type": "websocket.receive", "text": json.dumps(payload), "bytes": None}
        )


# ---------------------------------------------------------------------------
# Health: the dots report the truth
# ---------------------------------------------------------------------------


async def test_health_is_200_and_reports_every_dot_red(client: httpx.AsyncClient) -> None:
    """/api/health answers 200 while MT5, Telegram and the provider are all unavailable."""
    response = await client.get("/api/health")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["ok"] is True
    assert body["version"] == "0.1.0"
    assert body["uptime_s"] >= 0.0
    assert body["armed"] is False

    status = body["status"]
    assert set(status) == {"mt5", "telegram", "provider"}
    for key, entry in status.items():
        assert entry["ok"] is False, f"{key} claims to be up with nothing behind it"
        # A red dot with no sentence beside it is a dot the operator cannot act on.
        assert isinstance(entry.get("error"), str) and entry["error"].strip(), key

    assert body["needs_reentry"] == []


# ---------------------------------------------------------------------------
# Settings: no secret ever leaves the process
# ---------------------------------------------------------------------------


async def test_settings_masks_every_secret_and_reports_has_flags(
    client: httpx.AsyncClient,
) -> None:
    """Every stored secret comes back as the mask beside ``has_<field>: true``."""
    stored = await client.put(
        "/api/settings/mt5",
        json={"server": "Exness-MT5Trial15", "login": 249292544, "password": MT5_PASSWORD},
    )
    assert stored.status_code == 200, stored.text

    stored = await client.put(
        "/api/settings/provider",
        json={
            "kind": "anthropic",
            "base_url": "https://api.anthropic.com/v1",
            "api_key": PROVIDER_KEY,
            "model": "claude-sonnet-4-5",
            "extra_headers": {},
            "max_tokens": 8192,
        },
    )
    assert stored.status_code == 200, stored.text

    stored = await client.put(
        "/api/settings/notifier",
        json={"bot_token": BOT_TOKEN, "chat_id": -1001234567890, "enabled": True},
    )
    assert stored.status_code == 200, stored.text

    stored = await client.post(
        "/api/telegram/credentials",
        json={"api_id": 1234567, "api_hash": API_HASH, "phone": "+15551234567"},
    )
    assert stored.status_code == 200, stored.text

    response = await client.get("/api/settings")
    assert response.status_code == 200, response.text
    body = response.json()
    raw = response.text

    for section, field in (
        ("mt5", "password"),
        ("provider", "api_key"),
        ("notifier", "bot_token"),
        ("telegram", "api_hash"),
    ):
        assert body[section][field] == MASK, f"{section}.{field} is not masked"
        assert body[section][f"has_{field}"] is True, f"{section}.has_{field} is wrong"

    for secret in PLAINTEXT_SECRETS:
        assert secret not in raw, "a plaintext secret was returned by GET /api/settings"

    # The non-secret neighbours still round-trip, or the mask would be hiding a wipe.
    assert body["mt5"]["server"] == "Exness-MT5Trial15"
    assert body["mt5"]["login"] == 249292544
    assert body["provider"]["model"] == "claude-sonnet-4-5"
    assert body["notifier"]["chat_id"] == -1001234567890
    assert body["telegram"]["api_id"] == 1234567

    # BUILD §6: the optional limits ship off.
    limits = body["limits"]
    assert limits["max_volume_per_trade"] is None
    assert limits["max_open_positions"] is None
    assert limits["max_daily_loss_percent"] is None
    assert limits["symbol_allowlist"] == []
    assert limits["trading_hours"] == []
    stats = body["general"]["ladder_stats"]
    assert stats["window"] == 5
    assert stats["sample_size"] == 0
    assert stats["suggested_mode"] == "be_after_cover"
    assert stats["bootstrap"] is True
    assert stats["suggested_runner_tp_bias"] == 0
    assert "Suggested:" in stats["blurb"]


async def test_a_put_that_echoes_the_mask_keeps_the_stored_secret(
    client: httpx.AsyncClient,
) -> None:
    """Re-saving the form the dashboard rendered must not overwrite the key with bullets."""
    await client.put(
        "/api/settings/provider",
        json={
            "kind": "anthropic",
            "base_url": "https://api.anthropic.com",
            "api_key": PROVIDER_KEY,
            "model": "claude-sonnet-4-5",
        },
    )
    echoed = await client.put(
        "/api/settings/provider",
        json={
            "kind": "anthropic",
            "base_url": "https://api.anthropic.com",
            "api_key": MASK,
            "model": "claude-opus-4-1",
        },
    )
    assert echoed.status_code == 200, echoed.text

    from app.db import get_setting, session_scope

    async with session_scope() as session:
        assert await get_setting(session, "provider.api_key") == PROVIDER_KEY
    body = (await client.get("/api/settings")).json()
    assert body["provider"]["has_api_key"] is True
    assert body["provider"]["model"] == "claude-opus-4-1"


# ---------------------------------------------------------------------------
# Read routes degrade; they do not fall over
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/trading/state",
        "/api/trading/positions",
        "/api/trading/orders",
        "/api/trading/signals",
        "/api/trading/runs",
        "/api/trading/account",
        "/api/trading/symbols",
        "/api/history/trades",
        "/api/history/aggregates",
        "/api/telegram/status",
        "/api/provider/models",
        "/api/settings",
        "/api/health",
    ],
)
async def test_read_routes_answer_200_with_no_mt5_and_no_telegram(
    client: httpx.AsyncClient, path: str
) -> None:
    """Every read route the dashboard calls on first paint answers 200 while degraded."""
    response = await client.get(path)
    assert response.status_code == 200, f"{path} -> {response.status_code} {response.text}"
    assert isinstance(response.json(), dict)


async def test_degraded_read_routes_say_why_rather_than_pretending(
    client: httpx.AsyncClient,
) -> None:
    """Positions and orders come back empty, flagged unavailable, with a readable reason."""
    for path, key in (("/api/trading/positions", "positions"), ("/api/trading/orders", "orders")):
        body = (await client.get(path)).json()
        assert body[key] == []
        assert body["count"] == 0
        assert body["mt5_unavailable"] is True
        assert isinstance(body["message"], str) and body["message"].strip()

    state = (await client.get("/api/trading/state")).json()
    assert state["armed"] is False
    assert state["mt5"]["ok"] is False
    assert state["mt5_unavailable"] is True


async def test_symbol_picker_degrades_to_200_so_settings_still_renders(
    client: httpx.AsyncClient,
) -> None:
    """The symbol list is where an allowlist is chosen, and Settings is where MT5 gets fixed.

    A 500 here would blank the page the operator needs in order to repair the connection that
    is failing, so an unreachable terminal is a *state* in the payload: 200, no rows, the flag
    set, a sentence saying so, and the configured account still named from the stored settings.
    """
    response = await client.get("/api/trading/symbols")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["mt5_unavailable"] is True
    assert body["symbols"] == []
    assert body["total"] == 0
    assert body["truncated"] is False
    assert isinstance(body["message"], str) and body["message"].strip()
    # The account block still names the account being configured, from .env or the database.
    assert "login" in body["account"] and "server" in body["account"]


async def test_degraded_state_reports_unknown_facts_as_null(
    client: httpx.AsyncClient,
) -> None:
    """The rest of /state stays honest while MT5 is down: unknown facts are null, not guesses."""
    state = (await client.get("/api/trading/state")).json()
    # Null, not a guess: the mode is a fact about the account and MT5 has not answered
    # yet. Rendering "netting" here would be inventing the one flag that decides whether
    # a three-TP signal splits into three positions.
    assert state["margin_mode"] is None
    assert state["currency"] is None
    assert state["counts"] == {
        "signals": 0,
        "runs": 0,
        "positions": 0,
        "orders": 0,
        "trades": 0,
    }

    aggregates = (await client.get("/api/history/aggregates")).json()
    assert aggregates["count"] == 0
    assert aggregates["by_symbol"] == []


# ---------------------------------------------------------------------------
# The arm switch
# ---------------------------------------------------------------------------


async def test_arm_switch_round_trips_through_the_database(client: httpx.AsyncClient) -> None:
    """POST /api/trading/arm persists, and /state reads the persisted value back."""
    assert (await client.get("/api/trading/state")).json()["armed"] is False

    armed = await client.post("/api/trading/arm", json={"armed": True})
    assert armed.status_code == 200, armed.text
    assert armed.json() == {"ok": True, "armed": True}

    assert (await client.get("/api/trading/state")).json()["armed"] is True
    assert (await client.get("/api/health")).json()["armed"] is True

    disarmed = await client.post("/api/trading/arm", json={"armed": False})
    assert disarmed.status_code == 200, disarmed.text
    assert disarmed.json() == {"ok": True, "armed": False}

    assert (await client.get("/api/trading/state")).json()["armed"] is False
    assert (await client.get("/api/health")).json()["armed"] is False


# ---------------------------------------------------------------------------
# The kill switch
# ---------------------------------------------------------------------------


async def test_kill_switch_rejects_a_wrong_confirmation(client: httpx.AsyncClient) -> None:
    """Anything but the literal "KILL" is a 422 in the one error envelope, not an action."""
    response = await client.post("/api/trading/kill-switch", json={"confirm": "NOPE"})
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert "confirm" in error["message"]


async def test_kill_switch_works_with_no_broker_and_disarms(client: httpx.AsyncClient) -> None:
    """With nothing to close the switch still answers 200 and still disarms (BUILD §5.5)."""
    await client.post("/api/trading/arm", json={"armed": True})
    assert (await client.get("/api/trading/state")).json()["armed"] is True

    response = await client.post("/api/trading/kill-switch", json={"confirm": "KILL"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["closed"] == 0
    assert body["cancelled"] == 0
    assert isinstance(body["message"], str) and body["message"].strip()

    # The half of an emergency stop that is still possible without a broker must have happened.
    assert (await client.get("/api/trading/state")).json()["armed"] is False


# ---------------------------------------------------------------------------
# The WebSocket
# ---------------------------------------------------------------------------


async def test_stream_hello_carries_status_armed_and_recent(client: httpx.AsyncClient) -> None:
    """The first frame on /api/stream is the hello of CONTRACTS §19."""
    await client.post("/api/trading/arm", json={"armed": True})

    async with AsgiWebSocket(client.app, "/api/stream") as socket:
        hello = await socket.receive_json()
        assert hello["type"] == "hello"
        assert isinstance(hello["ts"], (int, float))

        data = hello["data"]
        assert set(data["status"]) == {"mt5", "telegram", "provider"}
        for entry in data["status"].values():
            assert entry["ok"] is False
        assert data["armed"] is True
        assert isinstance(data["recent"], list)
        assert len(data["recent"]) <= 100
        for envelope in data["recent"]:
            assert {"type", "ts", "data"} <= set(envelope)

        await socket.send_json({"type": "ping"})
        pong = await socket.receive_json()
        assert pong["type"] == "pong"

    await client.post("/api/trading/arm", json={"armed": False})


async def test_stream_delivers_events_emitted_after_the_hello(
    client: httpx.AsyncClient,
) -> None:
    """A subscriber receives envelopes emitted while it is connected, and not the ones before."""
    async with AsgiWebSocket(client.app, "/api/stream") as socket:
        hello = await socket.receive_json()
        assert hello["type"] == "hello"

        client.app.state.runtime.bus.emit("system.warning", reason="smoke_test", message="hi")
        frame = await socket.receive_json()
        assert frame["type"] == "system.warning"
        assert frame["data"]["reason"] == "smoke_test"


# ---------------------------------------------------------------------------
# The error envelope
# ---------------------------------------------------------------------------


async def test_unknown_api_path_uses_the_one_error_envelope(client: httpx.AsyncClient) -> None:
    """A 404 under /api is the same ``{"error": {...}}`` shape as everything else."""
    response = await client.get("/api/does-not-exist")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert isinstance(error["message"], str) and error["message"].strip()
    assert isinstance(error["detail"], dict)


async def test_saving_an_mt5_account_reports_when_it_could_not_be_applied(client) -> None:
    """Storing credentials is not applying them.

    An adversarial audit found `PUT /api/settings/mt5` writing server/login/password to the
    database while the sidecar went on reading MT5_* from the environment it booted with — so
    changing the account in Settings reported success and left the terminal logged into the old
    one. That is worse than an error: nothing looks wrong until trades land in the wrong account.

    There is no sidecar here, so the apply must FAIL — and the response must say so while still
    having saved the credentials and still masking the password.
    """
    response = await client.put(
        "/api/settings/mt5",
        json={"server": "Exness-MT5Trial15", "login": 474436674, "password": MT5_PASSWORD},
    )
    assert response.status_code == 200

    mt5 = response.json()["mt5"]
    assert mt5["apply_error"], "a broker-side failure must be reported, not swallowed"
    assert mt5["login"] == 474436674, "and the credentials are still saved"
    assert mt5["password"] == MASK and mt5["has_password"] is True
    assert MT5_PASSWORD not in response.text


async def test_general_settings_round_trip_orders_filter_and_prompt(
    client: httpx.AsyncClient,
) -> None:
    """Defaults, then the operator recipe, then a custom prompt that stays stored."""
    from app.ai.prompts import SYSTEM_PROMPT
    from app.db import get_setting, session_scope
    from app.operator_settings import (
        KEY_REVIEWER_ENABLED,
        KEY_SYSTEM_PROMPT,
        KEY_TAG_FILTER_ENABLED,
        KEY_TAG_FILTER_TAG,
        KEY_USE_FIXED_ORDER_LOTS,
    )

    fresh = (await client.get("/api/settings")).json()["general"]
    assert fresh["reviewer_enabled"] is False
    assert fresh["tag_filter_enabled"] is False
    assert fresh["tag_filter_tag"] == "#soft"
    assert fresh["orders_per_signal"] == 2
    assert fresh["order_lot_size"] == 0.2
    assert fresh["use_fixed_order_lots"] is True
    assert fresh["max_entry_drift_usd"] == 5
    assert fresh["be_after_tp1"] is True
    assert fresh["system_prompt_custom"] is False
    assert fresh["system_prompt"] == fresh["system_prompt_default"]
    assert fresh["system_prompt_default"] == SYSTEM_PROMPT

    echoed = await client.put(
        "/api/settings/general",
        json={
            "default_risk_percent": fresh["default_risk_percent"],
            "ladder_volume": fresh["ladder_volume"],
            "locale": fresh["locale"],
            "orders_per_signal": 2,
            "order_lot_size": 0.2,
            "use_fixed_order_lots": False,
            "max_entry_drift_usd": 5,
            "be_after_tp1": True,
            "reviewer_enabled": False,
            "tag_filter_enabled": True,
            "tag_filter_tag": "#soft",
            "system_prompt": fresh["system_prompt_default"],
        },
    )
    assert echoed.status_code == 200, echoed.text
    kept = echoed.json()["general"]
    assert kept["tag_filter_enabled"] is True
    assert kept["reviewer_enabled"] is False
    assert kept["system_prompt_custom"] is False
    async with session_scope() as session:
        assert await get_setting(session, KEY_SYSTEM_PROMPT) == ""
        assert await get_setting(session, KEY_TAG_FILTER_ENABLED) is True
        assert await get_setting(session, KEY_TAG_FILTER_TAG) == "#soft"
        assert await get_setting(session, KEY_REVIEWER_ENABLED) is False
        assert await get_setting(session, KEY_USE_FIXED_ORDER_LOTS) is False

    custom = "You trade only posts that the operator already filtered."
    saved = await client.put(
        "/api/settings/general",
        json={
            "default_risk_percent": fresh["default_risk_percent"],
            "ladder_volume": fresh["ladder_volume"],
            "locale": fresh["locale"],
            "orders_per_signal": 2,
            "order_lot_size": 0.2,
            "use_fixed_order_lots": False,
            "max_entry_drift_usd": 5,
            "be_after_tp1": True,
            "reviewer_enabled": False,
            "tag_filter_enabled": True,
            "tag_filter_tag": "#SoFt",
            "system_prompt": custom,
        },
    )
    assert saved.status_code == 200, saved.text
    general = saved.json()["general"]
    assert general["system_prompt"] == custom
    assert general["system_prompt_custom"] is True
    assert general["tag_filter_tag"] == "#SoFt"
    assert general["orders_per_signal"] == 2
    assert general["order_lot_size"] == 0.2
    assert general["use_fixed_order_lots"] is False
    assert general["max_entry_drift_usd"] == 5
    assert general["be_after_tp1"] is True
