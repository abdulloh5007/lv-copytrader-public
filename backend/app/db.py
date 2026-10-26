"""Async SQLAlchemy engine, session helpers and the encrypted settings key-value store."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, AsyncIterator

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app import events
from app.config import settings
from app.crypto import CryptoError, decrypt, encrypt, is_encrypted
from app.models import AppSetting, Base, SystemState
from app.operator_settings import (
    BE_AFTER_TP1_DEFAULT,
    KEY_BE_AFTER_TP1,
    KEY_ORDER_LOT_SIZE,
    KEY_ORDERS_PER_SIGNAL,
    KEY_REVIEWER_ENABLED,
    KEY_USE_FIXED_ORDER_LOTS,
    KEY_SYSTEM_PROMPT,
    KEY_TAG_FILTER_ENABLED,
    KEY_TAG_FILTER_TAG,
    ORDER_LOT_SIZE_DEFAULT,
    ORDERS_PER_SIGNAL_DEFAULT,
    REVIEWER_ENABLED_DEFAULT,
    USE_FIXED_ORDER_LOTS_DEFAULT,
    TAG_FILTER_ENABLED_DEFAULT,
    TAG_FILTER_TAG_DEFAULT,
)
from app.util.logging import get_logger

__all__ = [
    "DEFAULT_SETTINGS",
    "SessionLocal",
    "clear_needs_reentry",
    "dispose_db",
    "engine",
    "get_session",
    "get_setting",
    "get_settings_map",
    "has_value",
    "init_db",
    "needs_reentry_keys",
    "session_scope",
    "set_setting",
]

log = get_logger("app.db")

#: Every settings key from CONTRACTS.md §6 that has a documented default. Keys with
#: no default (credentials, tokens, model ids) are absent on purpose: an empty row and
#: a missing row must stay distinguishable so `has_value` reports the truth.
DEFAULT_SETTINGS: dict[str, Any] = {
    "trading.armed": False,
    "trading.default_risk_percent": settings.DEFAULT_RISK_PERCENT,
    "trading.ladder_volume": settings.LADDER_VOLUME,
    "trading.ladder_stats_window": settings.LADDER_STATS_WINDOW,
    KEY_ORDERS_PER_SIGNAL: ORDERS_PER_SIGNAL_DEFAULT,
    KEY_ORDER_LOT_SIZE: ORDER_LOT_SIZE_DEFAULT,
    KEY_USE_FIXED_ORDER_LOTS: USE_FIXED_ORDER_LOTS_DEFAULT,
    KEY_BE_AFTER_TP1: BE_AFTER_TP1_DEFAULT,
    KEY_REVIEWER_ENABLED: REVIEWER_ENABLED_DEFAULT,
    KEY_TAG_FILTER_ENABLED: TAG_FILTER_ENABLED_DEFAULT,
    KEY_TAG_FILTER_TAG: TAG_FILTER_TAG_DEFAULT,
    KEY_SYSTEM_PROMPT: "",
    "ui.locale": "en",
    "notifier.enabled": False,
    "notifier.types": {
        "opened": True,
        "modified": True,
        "closed": True,
        "rejected": True,
        "attention": True,
    },
    "limits.max_volume_per_trade": None,
    "limits.max_open_positions": None,
    "limits.max_daily_loss_percent": None,
    "limits.symbol_allowlist": [],
    "limits.trading_hours": [],
    "provider.kind": "anthropic",
    "provider.max_tokens": settings.MAX_TOKENS,
    "provider.extra_headers": {},
}

engine: AsyncEngine = create_async_engine(
    settings.db_url,
    echo=settings.DB_ECHO,
    future=True,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False},
)

SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


def _utcnow() -> datetime:
    """Return the current UTC time as a timezone-naive datetime, as every column stores it."""
    return datetime.now(UTC).replace(tzinfo=None)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
    """Apply WAL, foreign keys and a busy timeout to every new SQLite connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield a session that commits on a clean exit and rolls back on any exception."""
    session = SessionLocal()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session and committing it when the request ends."""
    session = SessionLocal()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def _encode(value: Any) -> str:
    """JSON-encode a settings value for storage."""
    return json.dumps(value, ensure_ascii=False)


def _decode(raw: str) -> Any:
    """JSON-decode a stored settings value, returning the raw string when it is not JSON."""
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


async def _flag_needs_reentry(s: AsyncSession, row: AppSetting, key: str) -> None:
    """Mark a row unreadable and warn the dashboard; this must never raise."""
    try:
        row.needs_reentry = True
        row.updated_at = _utcnow()
        await s.flush()
    except Exception:  # noqa: BLE001 - a settings read must never fail on bookkeeping
        log.warning("needs_reentry_flag_failed key=%s", key)
    try:
        events.bus.emit(events.SYSTEM_WARNING, reason="needs_reentry", key=key)
    except Exception:  # noqa: BLE001 - the event bus is best effort here
        log.warning("needs_reentry_event_failed key=%s", key)
    log.warning("setting_needs_reentry key=%s", key)


async def _get_row(s: AsyncSession, key: str) -> AppSetting | None:
    """Fetch one settings row by key."""
    return await s.get(AppSetting, key)


async def _read_row(s: AsyncSession, row: AppSetting, default: Any) -> Any:
    """Decrypt and decode one settings row, degrading to the default when the key is wrong."""
    raw = row.value
    if raw is None:
        return default
    if row.is_secret or is_encrypted(raw):
        try:
            raw = decrypt(raw)
        except CryptoError:
            await _flag_needs_reentry(s, row, row.key)
            return default
        if row.needs_reentry:
            row.needs_reentry = False
            try:
                await s.flush()
            except Exception:  # noqa: BLE001 - clearing the flag is best effort
                log.warning("needs_reentry_clear_failed key=%s", row.key)
    return _decode(raw)


async def get_setting(s: AsyncSession, key: str, default: Any = None) -> Any:
    """Read a settings value, decrypted and JSON-decoded; never raises on a bad key."""
    row = await _get_row(s, key)
    if row is None:
        return default
    return await _read_row(s, row, default)


async def set_setting(s: AsyncSession, key: str, value: Any, *, secret: bool = False) -> None:
    """Upsert a settings value, encrypting it at rest when secret=True."""
    encoded = _encode(value)
    if secret:
        encoded = encrypt(encoded)
    row = await _get_row(s, key)
    now = _utcnow()
    if row is None:
        row = AppSetting(
            key=key,
            value=encoded,
            is_secret=secret,
            needs_reentry=False,
            updated_at=now,
        )
        s.add(row)
    else:
        row.value = encoded
        row.is_secret = secret
        row.needs_reentry = False
        row.updated_at = now
    await s.flush()
    events.bus.emit(events.SETTINGS_CHANGED, key=key)


async def get_settings_map(s: AsyncSession, prefix: str = "") -> dict[str, Any]:
    """Return every settings value under a key prefix, decrypted — never return this to a client."""
    result = await s.execute(select(AppSetting))
    out: dict[str, Any] = {}
    for row in result.scalars().all():
        if prefix and not row.key.startswith(prefix):
            continue
        out[row.key] = await _read_row(s, row, None)
    return out


async def has_value(s: AsyncSession, key: str) -> bool:
    """Report whether a settings row exists and carries a non-empty value."""
    row = await _get_row(s, key)
    if row is None or row.value is None:
        return False
    return row.value not in ("", '""')


async def needs_reentry_keys(s: AsyncSession) -> list[str]:
    """List the settings keys whose stored value could not be decrypted."""
    result = await s.execute(
        select(AppSetting.key).where(AppSetting.needs_reentry.is_(True)).order_by(AppSetting.key)
    )
    return [str(key) for key in result.scalars().all()]


async def clear_needs_reentry(s: AsyncSession, key: str) -> None:
    """Clear the needs_reentry flag on one settings row."""
    row = await _get_row(s, key)
    if row is None or not row.needs_reentry:
        return
    row.needs_reentry = False
    row.updated_at = _utcnow()
    await s.flush()


async def _seed_defaults(s: AsyncSession) -> int:
    """Insert the documented default settings rows that do not exist yet."""
    result = await s.execute(select(AppSetting.key))
    existing = {str(key) for key in result.scalars().all()}
    now = _utcnow()
    seeded = 0
    for key, value in DEFAULT_SETTINGS.items():
        if key in existing:
            continue
        s.add(
            AppSetting(
                key=key,
                value=_encode(value),
                is_secret=False,
                needs_reentry=False,
                updated_at=now,
            )
        )
        seeded += 1
    if seeded:
        await s.flush()
    return seeded


async def _seed_system_state(s: AsyncSession) -> bool:
    """Create the single system_state row (id=1) when it is missing."""
    row = await s.get(SystemState, 1)
    if row is not None:
        return False
    s.add(
        SystemState(
            id=1,
            mt5_connected=False,
            telegram_connected=False,
            provider_ok=False,
            updated_at=_utcnow(),
        )
    )
    await s.flush()
    return True


async def _seed_mt5_from_env(s: AsyncSession) -> list[str]:
    """Seed mt5.server/login/password from .env so MT5 can connect without an interactive step."""
    seeded: list[str] = []
    server = (settings.MT5_SERVER or "").strip()
    if server and not await has_value(s, "mt5.server"):
        await set_setting(s, "mt5.server", server)
        seeded.append("mt5.server")
    login = settings.MT5_LOGIN
    if login is not None and not await has_value(s, "mt5.login"):
        await set_setting(s, "mt5.login", int(login))
        seeded.append("mt5.login")
    password = settings.MT5_PASSWORD or ""
    if password and not await has_value(s, "mt5.password"):
        try:
            await set_setting(s, "mt5.password", password, secret=True)
        except CryptoError as exc:
            log.error("mt5_password_seed_failed reason=%s", exc.message)
        else:
            seeded.append("mt5.password")
    return seeded


async def _seed_telegram_from_env(s: AsyncSession) -> list[str]:
    """Seed tg.api_id/api_hash/session/phone from .env so a fresh clone starts signed in.

    api_id and api_hash are issued once per application and outlive any single account, and
    a StringSession is what makes the sign-in survive a rebuild -- without this, cloning the
    repository and running it still means walking the interactive login again, even though
    .env already carries every credential. Seeding is one-way and only fills blanks: once a
    value exists in the database the UI owns it, so signing a different account in is never
    undone by the next restart.
    """
    seeded: list[str] = []
    api_id = settings.TELEGRAM_API_ID
    if api_id is not None and not await has_value(s, "tg.api_id"):
        await set_setting(s, "tg.api_id", int(api_id))
        seeded.append("tg.api_id")
    phone = (settings.TELEGRAM_PHONE or "").strip()
    if phone and not await has_value(s, "tg.phone"):
        await set_setting(s, "tg.phone", phone)
        seeded.append("tg.phone")
    for key, raw in (
        ("tg.api_hash", settings.TELEGRAM_API_HASH),
        ("tg.session", settings.TELEGRAM_SESSION),
    ):
        value = (raw or "").strip()
        if not value or await has_value(s, key):
            continue
        try:
            await set_setting(s, key, value, secret=True)
        except CryptoError as exc:
            log.error("telegram_seed_failed key=%s reason=%s", key, exc.message)
        else:
            seeded.append(key)
    return seeded


async def init_db() -> None:
    """Create the schema, apply SQLite pragmas and seed the default settings rows."""
    settings.ensure_dirs()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_sqlite_columns)
    async with session_scope() as s:
        seeded = await _seed_defaults(s)
        state_created = await _seed_system_state(s)
        mt5_seeded = await _seed_mt5_from_env(s)
        tg_seeded = await _seed_telegram_from_env(s)
    log.info(
        "db_ready path=%s seeded_settings=%d system_state_created=%s mt5_seeded=%s tg_seeded=%s",
        settings.db_path,
        seeded,
        state_created,
        ",".join(mt5_seeded) or "none",
        ",".join(tg_seeded) or "none",
    )


#: Columns added after the first create_all; SQLite will not add them itself.
_TRADE_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("ladder_role", "TEXT"),
    ("runner_mode", "TEXT"),
    ("suggested_runner_mode", "TEXT"),
    ("suggested_runner_tp_index", "INTEGER"),
    ("chosen_runner_tp_index", "INTEGER"),
    ("runner_tp_bias", "INTEGER"),
)

_MESSAGE_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("deleted_at", "DATETIME"),
)

_LADDER_OUTCOME_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("suggested_runner_tp_index", "INTEGER"),
    ("chosen_runner_tp_index", "INTEGER"),
    ("runner_tp_bias", "INTEGER"),
    ("runner_hit_chosen_tp", "INTEGER"),
)


def _ensure_sqlite_columns(sync_conn: Any) -> None:
    """ADD COLUMN for fields that landed after an existing operator database was created.

    ``create_all`` only creates missing *tables*. A production file that already has
    ``trades`` would otherwise keep running without ``runner_mode``, and every ladder
    would look like a pre-mode trade (BE-after-cover by default) with no way to store
    ``hold_channel_sl``.
    """
    _add_missing_columns(sync_conn, "trades", _TRADE_NEW_COLUMNS)
    _add_missing_columns(sync_conn, "ladder_outcomes", _LADDER_OUTCOME_NEW_COLUMNS)
    _add_missing_columns(sync_conn, "messages", _MESSAGE_NEW_COLUMNS)
    sync_conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_trade_public_history ON trades (magic, external, kind, status, closed_at)")


def _add_missing_columns(
    sync_conn: Any, table: str, columns: tuple[tuple[str, str], ...]
) -> None:
    """ALTER TABLE ADD COLUMN for each name not already on ``table``."""
    try:
        rows = sync_conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    except Exception:  # noqa: BLE001 - a missing table is create_all's job
        return
    existing = {str(row[1]) for row in rows}
    if not existing:
        return
    for name, coltype in columns:
        if name in existing:
            continue
        sync_conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")
        log.info("db_column_added table=%s column=%s", table, name)


async def dispose_db() -> None:
    """Dispose the engine and close every pooled connection."""
    await engine.dispose()
