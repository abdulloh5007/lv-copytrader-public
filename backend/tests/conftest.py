"""Test-suite bootstrap: isolate the process from the operator's real data and network.

Two things have to happen *before* ``app.config`` is imported, which is why they happen at
module import time rather than in a fixture:

* ``DATA_DIR`` — ``app.db`` builds its engine from ``settings.db_url`` at import, so pointing
  the data directory at a throwaway path afterwards would already be too late. Left alone it
  defaults to ``/data``, and a test run would either fail on permissions or, worse, succeed
  against the deployment's live database.
* ``SECRET_KEY`` — without one, ``crypto.bootstrap_secret_key`` generates a key and writes it
  into the repository's ``.env``. A test run must not edit the operator's environment file, so
  a throwaway key and a throwaway ``SP_ENV_FILE`` are planted here.

``SIDECAR_URL`` points at a closed port on the loopback interface: every test runs with MT5
unreachable, and "unreachable" must mean an instant connection refusal, not a DNS timeout.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="signalpilot-tests-"))

os.environ["DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ.setdefault("SP_ENV_FILE", str(_TEST_DATA_DIR / "env"))
os.environ.setdefault("SECRET_KEY", Fernet.generate_key().decode("ascii"))
os.environ.setdefault("SIDECAR_URL", "http://127.0.0.1:9")
os.environ.setdefault("SIDECAR_TOKEN", "test-token")
os.environ.setdefault("RPC_TIMEOUT_SECONDS", "1.0")
os.environ.setdefault("RPC_CONNECT_TIMEOUT_SECONDS", "1.0")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("FRONTEND_DIST", str(_TEST_DATA_DIR / "static"))

# Imported only after the environment above is in place.
import pytest  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app import db as app_db  # noqa: E402
from app.config import settings as app_settings  # noqa: E402


def _bind_engine(data_dir: Path) -> None:
    """Point ``app.db``'s module-level engine and sessionmaker at ``data_dir``.

    ``app.db`` holds the engine in a module global because every module in the codebase
    imports ``session_scope`` directly, so there is exactly one place to rebind and this is
    it. The SQLite pragmas are re-registered because the listener is attached to the engine
    instance, not to the dialect.
    """
    app_settings.DATA_DIR = str(data_dir)
    app_settings.ensure_dirs()
    engine = create_async_engine(
        app_settings.db_url,
        echo=False,
        future=True,
        pool_pre_ping=True,
        connect_args={"check_same_thread": False},
    )
    event.listen(engine.sync_engine, "connect", app_db._set_sqlite_pragmas)
    app_db.engine = engine
    app_db.SessionLocal = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )


@pytest.fixture
async def data_dir(tmp_path: Path):
    """Give one test its own ``DATA_DIR`` and its own SQLite file, then put the old one back.

    Async so the throwaway engine is disposed while the test's event loop is still running;
    an ``AsyncEngine`` disposed after the loop closes leaves its aiosqlite threads behind.
    """
    previous_dir = app_settings.DATA_DIR
    previous_engine = app_db.engine
    previous_sessions = app_db.SessionLocal
    target = tmp_path / "data"
    _bind_engine(target)
    try:
        yield target
    finally:
        engine = app_db.engine
        app_settings.DATA_DIR = previous_dir
        app_db.engine = previous_engine
        app_db.SessionLocal = previous_sessions
        await engine.dispose()


@pytest.fixture(autouse=True)
def _instant_rpc_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the sidecar retry *count* but drop its backoff sleeps.

    ``Mt5Rpc`` retries an idempotent method twice, sleeping 0.25 s then 1 s. That is right
    against a real sidecar that is restarting and wrong in a test suite where the sidecar is
    a closed port: the retry policy under test is "how many attempts", not "how long we
    waited", and every boot in this suite pays the schedule several times over.
    """
    from app.trading import rpc as rpc_module

    monkeypatch.setattr(
        rpc_module, "RETRY_BACKOFF_SECONDS", (0.0,) * len(rpc_module.RETRY_BACKOFF_SECONDS)
    )

@pytest.fixture(autouse=True)
def _auth_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test against an unauthenticated app unless it says otherwise.

    The suite reads the operator's real `.env` through SP_ENV_FILE, so the moment a dashboard
    login was configured there, every API test started getting 401 — a green suite turning red
    because of a *deployment* setting is a false alarm, and worse, it would tempt someone to
    weaken the auth to make the tests pass. `tests/test_auth.py` opts back in explicitly.
    """
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "SP_AUTH_USER", "", raising=False)
    monkeypatch.setattr(app_settings, "SP_AUTH_PASSWORD", "", raising=False)


@pytest.fixture(autouse=True)
def _telegram_seed_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from a Telegram-less database, whatever the operator's .env holds.

    Same trap as `_auth_disabled_by_default`, one layer deeper: `init_db` seeds tg.api_id /
    api_hash / session out of `.env`, so an operator who saved a real StringSession there
    would hand every test a signed-in Telegram account. `test_health_is_200_and_reports_
    every_dot_red` caught it immediately -- the telegram dot came up green with nothing
    behind it -- but the quieter damage is a suite that stops testing the disconnected path
    at all. Tests that exercise seeding set these back themselves.
    """
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "TELEGRAM_API_ID", None, raising=False)
    monkeypatch.setattr(app_settings, "TELEGRAM_API_HASH", "", raising=False)
    monkeypatch.setattr(app_settings, "TELEGRAM_SESSION", "", raising=False)
    monkeypatch.setattr(app_settings, "TELEGRAM_PHONE", "", raising=False)


@pytest.fixture(autouse=True)
def _explicit_test_only_open_api(monkeypatch):
    """Legacy non-auth tests opt into local development mode; production defaults stay closed."""
    monkeypatch.setattr(app_settings, "SP_ALLOW_UNAUTHENTICATED", True)
