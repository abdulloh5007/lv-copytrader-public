"""The port must open before the broker does.

BUILD_PROMPT acceptance criterion 1 says `docker compose up --build` brings up both
containers and *serves the dashboard*. On a first run the sidecar spends minutes installing
MT5 under Wine before it answers anything, and reaching an absent sidecar costs
RPC_TIMEOUT_SECONDS per call plus retries. If the lifespan awaited that, uvicorn would
accept no connection for the whole of it -- and the dashboard is the only place the operator
can watch the bring-up or fix the credentials blocking it.

This file asserts the structure that guarantees it, rather than a wall-clock number that
would be flaky on a loaded machine: the broker and Telegram bring-ups are *tasks*, they are
owned by the runtime so shutdown cancels them, and the app answers while they are still
running.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from app.main import BROKER_BRINGUP_TASK, QUOTE_CACHE_TASK, TELEGRAM_BRINGUP_TASK, create_app


async def test_lifespan_does_not_wait_for_the_broker(data_dir: Path) -> None:
    """Startup registers the bring-ups as tasks and returns without awaiting them."""
    app = create_app()
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        assert runtime is not None, "the service container exists even with no broker"

        assert BROKER_BRINGUP_TASK in runtime.tasks, (
            "the MT5 connect + reconcile must be a background task, not an awaited step"
        )
        assert TELEGRAM_BRINGUP_TASK in runtime.tasks
        assert QUOTE_CACHE_TASK in runtime.tasks, (
            "the quote cache writer must be a background task, not an awaited step"
        )

        # The app answers *now*, while those tasks are still flailing at an absent sidecar.
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            health = await client.get("/api/health")
            assert health.status_code == 200

            status = health.json()["status"]
            assert status["mt5"]["ok"] is False, "and it tells the truth about the broker"

            for path in ("/api/trading/state", "/api/trading/signals", "/api/history/trades"):
                assert (await client.get(path)).status_code == 200


async def test_shutdown_cancels_the_bringup_tasks(data_dir: Path) -> None:
    """A bring-up still in flight must not outlive the process it belongs to."""
    app = create_app()
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        tasks = [runtime.tasks.get(BROKER_BRINGUP_TASK), runtime.tasks.get(TELEGRAM_BRINGUP_TASK)]
        assert all(t is not None for t in tasks)

    await asyncio.sleep(0)
    for task in tasks:
        assert task.done() or task.cancelled(), "shutdown left a bring-up task running"


async def test_env_seeds_telegram_credentials_once(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.env` can carry api_id/api_hash/session so a fresh clone starts already signed in.

    The pair from my.telegram.org is issued once per application and a StringSession is what
    survives a rebuild, so both belong in `.env` next to the broker credentials -- otherwise
    cloning the repository still means walking the interactive login by hand.
    """
    from app.config import settings as app_settings
    from app.db import get_setting, init_db, session_scope

    monkeypatch.setattr(app_settings, "TELEGRAM_API_ID", 1234567, raising=False)
    monkeypatch.setattr(app_settings, "TELEGRAM_API_HASH", "0" * 32, raising=False)
    monkeypatch.setattr(app_settings, "TELEGRAM_SESSION", "1ABCStringSession", raising=False)
    monkeypatch.setattr(app_settings, "TELEGRAM_PHONE", "+14155552671", raising=False)

    await init_db()
    async with session_scope() as s:
        assert await get_setting(s, "tg.api_id", 0) == 1234567
        assert await get_setting(s, "tg.api_hash", "") == "0" * 32
        assert await get_setting(s, "tg.session", "") == "1ABCStringSession"
        assert await get_setting(s, "tg.phone", "") == "+14155552671"


async def test_env_seed_never_overwrites_the_connected_account(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signing a different account in through the UI must survive every later restart.

    Seeding fills blanks only. If it overwrote, the account in `.env` would silently come
    back on the next `docker compose up` and the operator would be reading someone else's
    channel -- the exact failure the UI-first design is meant to prevent.
    """
    from app.config import settings as app_settings
    from app.db import get_setting, init_db, session_scope, set_setting

    monkeypatch.setattr(app_settings, "TELEGRAM_SESSION", "seeded-from-env", raising=False)
    await init_db()
    async with session_scope() as s:
        await set_setting(s, "tg.session", "signed-in-through-the-ui", secret=True)

    await init_db()
    async with session_scope() as s:
        assert await get_setting(s, "tg.session", "") == "signed-in-through-the-ui"
