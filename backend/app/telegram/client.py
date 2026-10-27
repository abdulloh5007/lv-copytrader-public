"""Telethon lifecycle: login, dialog listing, source selection and live update handlers.

Every structural choice in this module comes straight from BUILD_PROMPT §3.6. Telethon binds
itself to the event loop that ran ``connect()`` and re-checks that loop on **every** request,
raising ``RuntimeError: The asyncio event loop must not change after connection`` when it moved.
The consequences are mandatory, and each is annotated where it applies below:

* :class:`TelegramService` is created and connected inside FastAPI's ``lifespan`` — never at
  import time and never inside a separate ``asyncio.run()``.
* The client is built as ``TelegramClient(StringSession(saved or None), api_id, api_hash)``.
  ``client.start()`` is never called: it prompts for the login code on stdin.
* ``await client.get_me()`` follows every ``connect()`` on an authorised session, because
  updates do not start flowing until a high-level request has been made. Skipping it produces
  a client that connects fine and receives nothing, which is a miserable bug to find.
* ``run_until_disconnected()`` is never called — under a running loop it returns a coroutine,
  and awaiting it inside ``lifespan`` blocks startup forever. Uvicorn's loop already keeps us
  alive.
* ``telethon.sync`` is never imported.
* ``flood_sleep_threshold`` is ``settings.TELEGRAM_FLOOD_SLEEP_THRESHOLD`` (60): Telethon
  sleeps through short flood waits and *raises* anything longer, so a multi-hour
  ``FloodWaitError`` reaches the dashboard instead of parking a worker for the afternoon.

The login flow (§3.1) persists ``sent.phone_code_hash`` **in the database** rather than relying
on Telethon's per-process cache, because the operator types the code into a *second* HTTP
request that may well be served after a restart.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Final
from uuid import uuid4

from sqlalchemy import select, update as sql_update
from telethon import TelegramClient
from telethon import events as tg_events
from telethon import utils as tg_utils
from telethon.errors import (
    ApiIdInvalidError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeEmptyError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    PhoneNumberUnoccupiedError,
    RPCError,
    SessionRevokedError,
)
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl import types as tl_types

from app import events
from app.config import Settings
from app.crypto import MASK
from app.db import get_setting, session_scope, set_setting
from app.errors import ConfigError, IngestError
from app.models import Source
from app.util.logging import get_logger, register_secret

if TYPE_CHECKING:  # pragma: no cover - type checkers only, never imported at runtime
    from app.runtime import Runtime

log = get_logger(__name__)

_CHANNEL_MARKER: int = 1_000_000_000_000


def _short_chat_id(value: Any) -> int:
    """Normalise a chat id so ``-100…`` and the short channel id compare equal."""
    number = abs(int(value))
    if number > _CHANNEL_MARKER:
        number -= _CHANNEL_MARKER
    return number

__all__ = [
    "ALBUM_GRACE_SECONDS",
    "TELEGRAM_ERROR_MESSAGES",
    "TelegramService",
    "telegram_error_message",
]


# --------------------------------------------------------------------------------------
# Operator-facing error sentences.
#
# The login screen renders these next to the field that caused them, so every entry is a
# distinct, actionable sentence. A 400 carrying the word "error" tells the operator nothing
# and is exactly what §3.1 forbids. All of these import from ``telethon.errors``.
# --------------------------------------------------------------------------------------
TELEGRAM_ERROR_MESSAGES: dict[type, str] = {
    SessionPasswordNeededError: (
        "This account has two-factor authentication enabled. Enter your Telegram cloud "
        "password to finish signing in."
    ),
    PhoneCodeInvalidError: (
        "That login code is not correct. Check the digits Telegram sent you and enter them "
        "again."
    ),
    PhoneCodeExpiredError: (
        "That login code has expired. Request a new code and enter it within a few minutes."
    ),
    PhoneCodeEmptyError: (
        "No login code was entered. Type the digits Telegram sent to your account."
    ),
    PhoneNumberInvalidError: (
        "That is not a valid Telegram phone number. Use the full international form, "
        "for example +447700900123."
    ),
    PhoneNumberUnoccupiedError: (
        "No Telegram account exists for that phone number. Register it in a Telegram app "
        "first, then sign in here."
    ),
    PhoneNumberBannedError: (
        "This phone number is banned from Telegram. The ban has to be lifted by Telegram "
        "support before the number can be used here."
    ),
    PasswordHashInvalidError: (
        "That two-factor password is not correct. Try again, or reset it from a Telegram app "
        "you are already signed in to."
    ),
    ApiIdInvalidError: (
        "The api_id and api_hash do not match. Copy both again from my.telegram.org — they "
        "are issued as a pair."
    ),
    # FloodWaitError carries .seconds; the sentence has to state it or the operator cannot
    # tell a 30-second pause from a six-hour one.
    FloodWaitError: (
        "Telegram is rate-limiting this account. Wait {seconds} seconds before trying again."
    ),
    AuthKeyUnregisteredError: (
        "This Telegram session is no longer valid. Sign in again to create a new one."
    ),
    SessionRevokedError: (
        "This Telegram session was terminated from another device. Sign in again to create a "
        "new one."
    ),
}

#: Which login field each failure belongs next to, so the UI can anchor the message.
_ERROR_FIELDS: dict[type, str] = {
    SessionPasswordNeededError: "password",
    PasswordHashInvalidError: "password",
    PhoneCodeInvalidError: "code",
    PhoneCodeExpiredError: "code",
    PhoneCodeEmptyError: "code",
    PhoneNumberInvalidError: "phone",
    PhoneNumberUnoccupiedError: "phone",
    PhoneNumberBannedError: "phone",
    FloodWaitError: "phone",
    ApiIdInvalidError: "api_id",
    AuthKeyUnregisteredError: "session",
    SessionRevokedError: "session",
}

_UNKNOWN_RPC_MESSAGE: Final[str] = "Telegram rejected the request: {detail}"

#: Telethon reassembles albums behind a 0.5 s delay (``telethon.events.album._HACK_DELAY``);
#: this is how long we hold buffered album members before giving up on the Album event.
ALBUM_GRACE_SECONDS: Final[float] = 1.5

#: How many delivered album ids are remembered, so a late straggler cannot re-deliver a group.
_ALBUM_MEMORY: Final[int] = 256

#: Entity kinds the Source table stores (CONTRACTS.md §7).
ENTITY_TYPES: Final[frozenset[str]] = frozenset({"channel", "supergroup", "group", "user"})


def telegram_error_message(exc: BaseException) -> str:
    """Return the operator-facing sentence for a Telethon exception."""
    for klass in type(exc).__mro__:
        template = TELEGRAM_ERROR_MESSAGES.get(klass)
        if template is None:
            continue
        if "{seconds}" in template:
            seconds = int(getattr(exc, "seconds", 0) or 0)
            return template.format(seconds=seconds)
        return template
    detail = str(exc).strip() or type(exc).__name__
    return _UNKNOWN_RPC_MESSAGE.format(detail=detail)


def _error_field(exc: BaseException) -> str | None:
    """Return the login field a Telethon exception belongs to, if it maps to one."""
    for klass in type(exc).__mro__:
        field = _ERROR_FIELDS.get(klass)
        if field is not None:
            return field
    return None


def _login_failure(exc: BaseException, *, field: str | None = None) -> IngestError:
    """Wrap a Telethon exception as the 400 the login screen renders next to a field."""
    detail: dict[str, Any] = {"telegram_error": type(exc).__name__}
    resolved = field or _error_field(exc)
    if resolved:
        detail["field"] = resolved
    seconds = getattr(exc, "seconds", None)
    if isinstance(seconds, int):
        detail["seconds"] = seconds
    return IngestError(telegram_error_message(exc), detail=detail)


def _entity_type(entity: Any) -> str:
    """Classify a Telegram entity as channel / supergroup / group / user."""
    kind = "group"
    if isinstance(entity, tl_types.User):
        kind = "user"
    elif isinstance(entity, (tl_types.Channel, tl_types.ChannelForbidden)):
        # A megagroup (and a gigagroup, its broadcast-only variant) is still a Channel object;
        # only `broadcast` channels are what a signal provider normally publishes to.
        if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False):
            kind = "supergroup"
        else:
            kind = "channel"
    # The Source table stores a closed set, so an entity Telegram adds later degrades to
    # "group" rather than writing a value nothing downstream knows how to render.
    return kind if kind in ENTITY_TYPES else "group"


def _entity_title(entity: Any) -> str:
    """Return a human title for an entity, falling back to its id."""
    title = tg_utils.get_display_name(entity)
    if title:
        return str(title)
    return f"chat {getattr(entity, 'id', '?')}"


def _entity_username(entity: Any) -> str | None:
    """Return the primary public username of an entity, or None for a private one."""
    username = getattr(entity, "username", None)
    if username:
        return str(username)
    # Telegram now allows several usernames per entity; the active one is what a link needs.
    for extra in getattr(entity, "usernames", None) or ():
        name = getattr(extra, "username", None)
        if name and getattr(extra, "active", True):
            return str(name)
    return None


def _participants(entity: Any) -> int | None:
    """Return the participant count when Telegram supplied one."""
    count = getattr(entity, "participants_count", None)
    if isinstance(count, int) and count >= 0:
        return count
    return None


def _user_payload(user: Any) -> dict[str, Any]:
    """Normalise a Telethon User into the dict the dashboard and settings tree render."""
    return {
        "id": int(getattr(user, "id", 0) or 0),
        "username": _entity_username(user),
        "first_name": getattr(user, "first_name", None),
        "last_name": getattr(user, "last_name", None),
        "phone": getattr(user, "phone", None),
        "name": _entity_title(user),
        "is_bot": bool(getattr(user, "bot", False)),
    }


def _has_downloadable_image(message: Any) -> bool:
    """Report whether a message carries an image we can and should download.

    Branching on the media type is the §3.2 rule: ``message.photo`` is *also* truthy for a
    link-preview image and for a ``MessageActionChatEditPhoto`` service message, and a signal
    channel that "sends as file" to preserve chart quality delivers a **document** with an
    image mime type that must be processed, not skipped.
    """
    media = getattr(message, "media", None)
    if media is None:
        return False
    if isinstance(media, tl_types.MessageMediaPhoto):
        return True
    if isinstance(media, tl_types.MessageMediaDocument):
        # A sticker, GIF, video, video note, voice note and audio file all arrive as documents.
        for attribute in ("sticker", "voice", "video_note", "gif", "video", "audio"):
            if getattr(message, attribute, None):
                return False
        document = getattr(message, "document", None)
        mime = str(getattr(document, "mime_type", "") or "").lower()
        return mime.startswith("image/")
    # MessageMediaWebPage is a link preview: §3.2 classifies it as plain text, so its image is
    # decoration, not a chart the operator posted.
    return False


class TelegramService:
    """Owns the Telethon client: login, dialogs, source selection and update handlers."""

    def __init__(self, runtime_getter: Callable[[], Any] | Any, settings: Settings) -> None:
        # The runtime is supplied late (a callable resolved on use) because Runtime holds a
        # reference to this service, and importing it eagerly would close the cycle.
        self._runtime_getter = runtime_getter
        self._settings: Settings = settings
        self.client: TelegramClient | None = None

        self._lock: asyncio.Lock = asyncio.Lock()
        # Whether a Telegram session is stored. It separates "not logged in yet" from
        # "logged in, currently reconnecting", which are the same state to the client object
        # and completely different states to the operator.
        self._has_stored_session: bool = False
        self._api_id: int | None = None
        self._api_hash: str | None = None

        # Registered (callback, builder) pairs, kept so unregister_handlers() can remove
        # exactly what it added when the operator changes source.
        self._handlers: list[tuple[Callable[[Any], Any], Any]] = []
        self._active_source_id: int | None = None
        self._active_chat_id: int | None = None

        # Album reassembly state (§3.4). See _queue_album_member for why it exists.
        self._album_pending: dict[int, list[Any]] = {}
        self._album_timers: dict[int, asyncio.Task[None]] = {}
        self._album_done: dict[int, float] = {}

        self._entity_cache: dict[int, Any] = {}

    # ---- wiring -----------------------------------------------------------------------

    def _runtime(self) -> Runtime | None:
        """Resolve the Runtime lazily; it may not exist yet during early startup."""
        getter = self._runtime_getter
        if getter is None:
            return None
        try:
            return getter() if callable(getter) else getter
        except Exception as exc:  # noqa: BLE001 - a missing runtime must not break Telegram
            log.debug("runtime_unavailable error=%s", exc)
            return None

    def _bus(self) -> Any:
        """Return the event bus, preferring the runtime's instance."""
        runtime = self._runtime()
        bus = getattr(runtime, "bus", None)
        return bus if bus is not None else events.bus

    def _ingestor(self) -> Any:
        """Return ``runtime.ingestor``; resolved on every use so no import cycle forms."""
        runtime = self._runtime()
        if runtime is None:
            return None
        return getattr(runtime, "ingestor", None)

    def _set_status(self, ok: bool, error: str | None) -> None:
        """Push the Telegram status dot through the runtime; never raises."""
        runtime = self._runtime()
        if runtime is None:
            return
        try:
            runtime.set_status("telegram", ok, error)
        except Exception as exc:  # noqa: BLE001 - the status dot is never worth a crash
            log.warning("telegram_status_failed error=%s", exc)

    def _warn(self, reason: str, **data: Any) -> None:
        """Emit a system warning onto the dashboard stream; never raises."""
        try:
            self._bus().emit(events.SYSTEM_WARNING, reason=reason, **data)
        except Exception as exc:  # noqa: BLE001 - the bus is best effort here
            log.warning("telegram_warning_emit_failed reason=%s error=%s", reason, exc)

    # ---- settings access --------------------------------------------------------------

    async def _read_credentials(self) -> tuple[int | None, str | None, str | None, str | None]:
        """Read (api_id, api_hash, phone, session) from the encrypted settings store."""
        async with session_scope() as s:
            raw_api_id = await get_setting(s, "tg.api_id")
            api_hash = await get_setting(s, "tg.api_hash")
            phone = await get_setting(s, "tg.phone")
            session_string = await get_setting(s, "tg.session")
            self._has_stored_session = bool(session_string)
        api_id: int | None
        try:
            api_id = int(raw_api_id) if raw_api_id not in (None, "") else None
        except (TypeError, ValueError):
            api_id = None
        api_hash = str(api_hash) if api_hash else None
        phone = str(phone) if phone else None
        session_string = str(session_string) if session_string else None
        # Registering them means every later log line masks them, including Telethon's own.
        register_secret(api_hash)
        register_secret(session_string)
        return api_id, api_hash, phone, session_string

    async def _store_credentials(self, api_id: int, api_hash: str, phone: str) -> None:
        """Persist the credentials the operator just supplied; the hash is a secret at rest."""
        async with session_scope() as s:
            await set_setting(s, "tg.api_id", int(api_id))
            if api_hash and api_hash != MASK:
                await set_setting(s, "tg.api_hash", str(api_hash), secret=True)
            if phone:
                await set_setting(s, "tg.phone", str(phone))

    async def _store_user(self, payload: dict[str, Any] | None) -> None:
        """Cache the signed-in account so the settings tree can render it while offline."""
        async with session_scope() as s:
            await set_setting(s, "tg.user", payload)

    async def _persist_session(self) -> None:
        """Save the Telethon session string encrypted and clear the pending code hash."""
        client = self.client
        if client is None:
            return
        session_string = client.session.save()
        if not session_string:
            return
        # Registered before it is written, so no code path can log it in between.
        register_secret(session_string)
        async with session_scope() as s:
            await set_setting(s, "tg.session", session_string, secret=True)
            # The code has been consumed; leaving the hash behind would let a stale one be
            # replayed against a fresh code request.
            await set_setting(s, "tg.phone_code_hash", "")
        self._has_stored_session = True

    async def _clear_session(self) -> None:
        """Forget the stored session, pending code hash and cached user."""
        async with session_scope() as s:
            # Cleared as a plain empty string, not an encrypted one: db.has_value() inspects
            # the stored bytes, and an encrypted "" is still a non-empty row, which would keep
            # the settings tree reporting has_session: true after a logout.
            await set_setting(s, "tg.session", "")
            await set_setting(s, "tg.phone_code_hash", "")
            await set_setting(s, "tg.user", None)
        self._has_stored_session = False

    # ---- client lifecycle -------------------------------------------------------------

    def _build_client(self, api_id: int, api_hash: str, session_string: str | None) -> None:
        """Construct the Telethon client. §3.6: StringSession, never ``client.start()``."""
        self.client = TelegramClient(
            StringSession(session_string or None),
            int(api_id),
            str(api_hash),
            # §3.6: sleep through short flood waits, raise the long ones so they surface on
            # the dashboard instead of blocking an ingest worker for hours.
            flood_sleep_threshold=int(self._settings.TELEGRAM_FLOOD_SLEEP_THRESHOLD),
            device_model="LV Copytrader",
            system_version="Linux",
            app_version="LV Copytrader 1.1",
            # We run our own §8.4 catch-up against the stored last_message_id, so Telethon's
            # own catch-up would only duplicate work and replay updates we already have.
            catch_up=False,
            receive_updates=True,
        )
        self._api_id = int(api_id)
        self._api_hash = str(api_hash)

    async def _ensure_client(
        self, api_id: int, api_hash: str, session_string: str | None
    ) -> TelegramClient:
        """Return a connected client, rebuilding it when the credentials changed."""
        if self.client is not None and (
            self._api_id != int(api_id) or self._api_hash != str(api_hash)
        ):
            await self._disconnect_client()
        if self.client is None:
            self._build_client(api_id, api_hash, session_string)
        client = self.client
        assert client is not None  # _build_client always assigns
        if not client.is_connected():
            # §3.6: this connect() runs on uvicorn's loop, inside lifespan or inside a request
            # handler — both are the same loop, which is the whole point.
            await client.connect()
        return client

    async def _disconnect_client(self) -> None:
        """Disconnect and drop the client; never raises."""
        client = self.client
        self.client = None
        self._api_id = None
        self._api_hash = None
        self._entity_cache.clear()
        if client is None:
            return
        try:
            if client.is_connected():
                # Under a running loop disconnect() returns an awaitable, so it must be awaited.
                await client.disconnect()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            log.warning("telegram_disconnect_failed error=%s", exc)

    async def start(self) -> None:
        """Connect and, when the session is already authorised, start receiving updates.

        Called from FastAPI's ``lifespan`` (§3.6). It never raises out: a Telegram that will
        not start is a red status dot with a sentence, not a backend that refuses to boot.
        """
        async with self._lock:
            try:
                await self._start_locked()
            except Exception as exc:  # noqa: BLE001 - start() never raises out
                # The client is deliberately left in place. A transient failure here is
                # usually the network, and Telethon reconnects on its own; tearing the client
                # down would turn a five-second blip into "sign in to Telegram again".
                log.exception("telegram_start_failed")
                self._set_status(False, f"Telegram did not start: {exc}")

    async def _start_locked(self) -> None:
        """Body of :meth:`start`, run under the service lock."""
        api_id, api_hash, _phone, session_string = await self._read_credentials()
        if not api_id or not api_hash:
            self._set_status(
                False,
                "Telegram is not configured — enter api_id and api_hash from my.telegram.org.",
            )
            log.info("telegram_idle reason=no_credentials")
            return

        client = await self._ensure_client(api_id, api_hash, session_string)

        if not await client.is_user_authorized():
            self._set_status(
                False, "Telegram is not signed in — request a login code to finish the login."
            )
            log.info("telegram_connected authorized=false")
            return

        # §3.6, and this line is load-bearing: updates do not start flowing until a high-level
        # request has been made. Without it the client connects, reports healthy, and silently
        # receives nothing at all.
        user = await client.get_me()
        payload = _user_payload(user) if user is not None else None
        await self._store_user(payload)
        self._set_status(True, None)
        log.info(
            "telegram_ready user_id=%s username=%s",
            (payload or {}).get("id"),
            (payload or {}).get("username"),
        )
        await self._register_handlers_locked()

    async def stop(self) -> None:
        """Unregister handlers and disconnect; never raises out."""
        async with self._lock:
            try:
                await self._unregister_handlers_locked()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                log.warning("telegram_unregister_failed error=%s", exc)
            try:
                await self._disconnect_client()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                log.warning("telegram_stop_failed error=%s", exc)
            self._set_status(False, None)
            log.info("telegram_stopped")

    async def is_authorized(self) -> bool:
        """Report whether the current session is signed in; never raises."""
        client = self.client
        if client is None or not client.is_connected():
            return False
        try:
            return bool(await client.is_user_authorized())
        except Exception as exc:  # noqa: BLE001 - a status probe never raises
            log.warning("telegram_authorized_check_failed error=%s", exc)
            return False

    # ---- login flow (§3.1) ------------------------------------------------------------

    async def send_code(self, api_id: int, api_hash: str, phone: str) -> dict:
        """Request a login code and persist ``phone_code_hash`` in the database.

        Telethon caches the hash **per process**, and the operator types the code into a second
        HTTP request that may be served after a restart — so the database is the only place the
        hash can safely live (§3.1).
        """
        try:
            api_id_int = int(api_id)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                "api_id must be the positive integer issued by my.telegram.org.",
                detail={"field": "api_id"},
            ) from exc
        if api_id_int <= 0:
            raise ConfigError(
                "api_id must be the positive integer issued by my.telegram.org.",
                detail={"field": "api_id"},
            )
        api_hash_str = str(api_hash or "").strip()
        if not api_hash_str or api_hash_str == MASK:
            raise ConfigError(
                "api_hash is missing — copy it from my.telegram.org together with api_id.",
                detail={"field": "api_hash"},
            )
        phone_str = str(phone or "").strip()
        if not phone_str:
            raise ConfigError(
                "Enter the phone number of the Telegram account in full international form.",
                detail={"field": "phone"},
            )

        async with self._lock:
            register_secret(api_hash_str)
            await self._store_credentials(api_id_int, api_hash_str, phone_str)
            _api_id, _api_hash, _stored_phone, session_string = await self._read_credentials()
            client = await self._ensure_client(api_id_int, api_hash_str, session_string)

            if await client.is_user_authorized():
                user = await client.get_me()
                payload = _user_payload(user) if user is not None else None
                await self._store_user(payload)
                self._set_status(True, None)
                await self._register_handlers_locked()
                await self._kick_reviewer()
                return {
                    "ok": True,
                    "code_sent": False,
                    "already_authorized": True,
                    "user": payload,
                }

            try:
                sent = await client.send_code_request(phone_str)
            except RPCError as exc:
                log.warning("telegram_send_code_failed error=%s", type(exc).__name__)
                raise _login_failure(exc, field=_error_field(exc) or "phone") from exc

            code_hash = str(getattr(sent, "phone_code_hash", "") or "")
            async with session_scope() as s:
                await set_setting(s, "tg.phone_code_hash", code_hash)
            self._set_status(
                False, "A login code was sent — enter it to finish signing in to Telegram."
            )
            log.info("telegram_code_sent has_hash=%s", bool(code_hash))
            return {"ok": True, "code_sent": True}

    async def sign_in(self, code: str) -> dict:
        """Complete the login with the code, using the ``phone_code_hash`` from the database."""
        code_str = str(code or "").strip()
        if not code_str:
            raise IngestError(
                TELEGRAM_ERROR_MESSAGES[PhoneCodeEmptyError], detail={"field": "code"}
            )

        async with self._lock:
            api_id, api_hash, phone, session_string = await self._read_credentials()
            if not api_id or not api_hash:
                raise ConfigError(
                    "Telegram is not configured — enter api_id and api_hash first.",
                    detail={"field": "api_id"},
                )
            if not phone:
                raise ConfigError(
                    "No phone number is stored — request a login code first.",
                    detail={"field": "phone"},
                )
            async with session_scope() as s:
                code_hash = str(await get_setting(s, "tg.phone_code_hash", "") or "")
            if not code_hash:
                raise IngestError(
                    "There is no pending login code for this number. Request a new code and "
                    "enter it within a few minutes.",
                    detail={"field": "code"},
                )

            client = await self._ensure_client(api_id, api_hash, session_string)
            try:
                user = await client.sign_in(
                    phone=phone, code=code_str, phone_code_hash=code_hash
                )
            except SessionPasswordNeededError:
                # Not a failure: the account has 2FA and the password is the next step.
                self._set_status(
                    False, TELEGRAM_ERROR_MESSAGES[SessionPasswordNeededError]
                )
                log.info("telegram_sign_in needs_password=true")
                return {"ok": True, "needs_password": True}
            except PhoneCodeExpiredError as exc:
                async with session_scope() as s:
                    await set_setting(s, "tg.phone_code_hash", "")
                raise _login_failure(exc, field="code") from exc
            except RPCError as exc:
                log.warning("telegram_sign_in_failed error=%s", type(exc).__name__)
                raise _login_failure(exc) from exc

            payload = await self._finish_login(user)
            return {"ok": True, "needs_password": False, "user": payload}

    async def sign_in_password(self, password: str) -> dict:
        """Finish a two-factor login with the account's cloud password."""
        secret = str(password or "")
        if not secret:
            raise IngestError(
                "Enter your Telegram cloud password to finish signing in.",
                detail={"field": "password"},
            )

        async with self._lock:
            api_id, api_hash, _phone, session_string = await self._read_credentials()
            if not api_id or not api_hash:
                raise ConfigError(
                    "Telegram is not configured — enter api_id and api_hash first.",
                    detail={"field": "api_id"},
                )
            client = await self._ensure_client(api_id, api_hash, session_string)
            try:
                user = await client.sign_in(password=secret)
            except RPCError as exc:
                log.warning("telegram_password_failed error=%s", type(exc).__name__)
                raise _login_failure(exc, field=_error_field(exc) or "password") from exc

            payload = await self._finish_login(user)
            return {"ok": True, "user": payload}

    async def _finish_login(self, user: Any) -> dict[str, Any] | None:
        """Persist the session, cache the user, flip the dot green and attach handlers."""
        await self._persist_session()
        payload = _user_payload(user) if user is not None else None
        if payload is None:
            client = self.client
            if client is not None:
                # §3.6 again: the first high-level request is what starts the update stream.
                fetched = await client.get_me()
                payload = _user_payload(fetched) if fetched is not None else None
        await self._store_user(payload)
        self._set_status(True, None)
        log.info("telegram_signed_in user_id=%s", (payload or {}).get("id"))
        await self._register_handlers_locked()
        await self._kick_reviewer()
        return payload

    async def logout(self) -> dict:
        """Sign out of Telegram and revoke the session, KEEPING the application credentials.

        api_id and api_hash come from my.telegram.org, belong to the application rather than to
        any one account, and are entered once. Only the account-scoped values go: the session,
        the pending code hash and the cached user. Signing out to connect a different account
        must not send the operator back to my.telegram.org.
        """
        async with self._lock:
            await self._unregister_handlers_locked()
            client = self.client
            revoked = False
            if client is not None and client.is_connected():
                try:
                    revoked = bool(await client.log_out())
                except Exception as exc:  # noqa: BLE001 - a dead session still has to clear
                    log.warning("telegram_logout_failed error=%s", exc)
            await self._clear_session()
            await self._disconnect_client()
            self._set_status(False, "Signed out of Telegram.")
            log.info("telegram_logged_out revoked=%s", revoked)
            return {"ok": True, "revoked": revoked}

    async def me(self) -> dict | None:
        """Return the signed-in account, live when possible and from cache otherwise."""
        client = self.client
        if client is not None and client.is_connected():
            try:
                if await client.is_user_authorized():
                    user = await client.get_me()
                    if user is not None:
                        payload = _user_payload(user)
                        await self._store_user(payload)
                        return payload
            except Exception as exc:  # noqa: BLE001 - fall back to the cached account
                log.warning("telegram_me_failed error=%s", exc)
        async with session_scope() as s:
            stored = await get_setting(s, "tg.user")
        return stored if isinstance(stored, dict) else None

    # ---- dialogs and source selection -------------------------------------------------

    def _require_client(self) -> TelegramClient:
        """Return the connected client or raise the sentence the UI should show."""
        client = self.client
        if client is None or not client.is_connected():
            if self._has_stored_session:
                # The session is fine; the socket is not up yet. Telling the operator to
                # "finish the login" here sends them to redo a login that already worked and
                # implies their session was lost -- after a restart the connect happens in the
                # background, so this window is normal and short.
                raise ConfigError(
                    "Telegram is reconnecting — the saved session is intact, this usually "
                    "clears within a few seconds of a restart. Try again in a moment.",
                    detail={"field": "phone", "reconnecting": True},
                )
            raise ConfigError(
                "Telegram is not connected — finish the Telegram login on the settings page.",
                detail={"field": "phone"},
            )
        return client

    async def dialogs(self) -> list[dict]:
        """List the channels, supergroups and groups this account can read.

        Bots and one-to-one chats are omitted: a DM is not a signal channel, and a bot cannot
        be one either. ``participants`` is reported whenever Telegram supplied a count.
        """
        client = self._require_client()
        if not await self.is_authorized():
            raise ConfigError(
                "Sign in to Telegram before picking a channel.", detail={"field": "code"}
            )

        async with session_scope() as s:
            result = await s.execute(select(Source.chat_id).where(Source.is_active.is_(True)))
            active_ids = {int(chat_id) for chat_id in result.scalars().all()}

        rows: list[dict[str, Any]] = []
        try:
            async for dialog in client.iter_dialogs():
                entity = getattr(dialog, "entity", None)
                if entity is None:
                    continue
                kind = _entity_type(entity)
                if kind == "user":
                    # Skips both bots and human DMs; neither is a channel to trade from.
                    continue
                chat_id = int(getattr(dialog, "id", 0) or 0)
                if not chat_id:
                    continue
                rows.append(
                    {
                        "chat_id": chat_id,
                        "title": _entity_title(entity),
                        "username": _entity_username(entity),
                        "entity_type": kind,
                        "is_channel": bool(getattr(dialog, "is_channel", False)),
                        "participants": _participants(entity),
                        "is_active": chat_id in active_ids,
                    }
                )
                self._entity_cache[chat_id] = entity
        except RPCError as exc:
            log.warning("telegram_dialogs_failed error=%s", type(exc).__name__)
            raise _login_failure(exc) from exc
        log.info("telegram_dialogs count=%d", len(rows))
        return rows

    async def _resolve_entity(self, chat_id: int) -> Any:
        """Resolve a chat id to an entity, warming the cache from the dialog list if needed."""
        key = int(chat_id)
        cached = self._entity_cache.get(key)
        if cached is not None:
            return cached
        client = self._require_client()
        try:
            entity = await client.get_entity(key)
        except (ValueError, TypeError):
            # A StringSession stores the auth key, not the entity cache, so after a restart
            # Telethon has no access_hash for a private channel. Walking the dialog list is
            # what repopulates it.
            entity = None
            async for dialog in client.iter_dialogs():
                if int(getattr(dialog, "id", 0) or 0) == key:
                    entity = getattr(dialog, "entity", None)
                    break
            if entity is None:
                raise IngestError(
                    f"Chat {key} is not in this account's dialog list — join the channel with "
                    "this account first, then pick it again.",
                    detail={"chat_id": key},
                )
        except RPCError as exc:
            raise _login_failure(exc) from exc
        self._entity_cache[key] = entity
        return entity

    async def _latest_message_id(self, entity: Any) -> int:
        """Return the id of the channel's newest message, or 0 when it has none."""
        client = self._require_client()
        try:
            messages = await client.get_messages(entity, limit=1)
        except RPCError as exc:
            log.warning("telegram_latest_message_failed error=%s", type(exc).__name__)
            return 0
        for message in messages or ():
            message_id = int(getattr(message, "id", 0) or 0)
            if message_id > 0:
                return message_id
        return 0

    async def select_source(self, chat_id: int) -> dict:
        """Make one chat the active signal source and (re)attach the update handlers.

        On the **first** selection the channel's current latest message id is recorded as
        ``last_message_id`` without ingesting anything (§8.4): picking a channel must not
        replay its backlog as live signals.
        """
        try:
            key = int(chat_id)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                "chat_id must be the integer id from the dialog list.",
                detail={"field": "chat_id"},
            ) from exc

        async with self._lock:
            entity = await self._resolve_entity(key)
            kind = _entity_type(entity)
            title = _entity_title(entity)
            username = _entity_username(entity)

            # Phase 1: find out whether this is a first selection, holding no transaction open
            # across the network call that follows.
            async with session_scope() as s:
                result = await s.execute(
                    select(Source).where(Source.kind == "telegram", Source.chat_id == key)
                )
                existing = result.scalars().first()
                previous_last_id = int(existing.last_message_id or 0) if existing else 0
                exists = existing is not None

            baseline = previous_last_id
            if previous_last_id <= 0:
                # Phase 2: the §8.4 baseline. Everything already in the channel stays history.
                baseline = await self._latest_message_id(entity)

            # Phase 3: exactly one active source, and it is this one.
            async with session_scope() as s:
                await s.execute(
                    sql_update(Source)
                    .where(Source.is_active.is_(True))
                    .values(is_active=False)
                )
                result = await s.execute(
                    select(Source).where(Source.kind == "telegram", Source.chat_id == key)
                )
                row = result.scalars().first()
                if row is None:
                    row = Source(
                        kind="telegram",
                        chat_id=key,
                        title=title,
                        username=username,
                        entity_type=kind,
                        is_active=True,
                        last_message_id=baseline,
                    )
                    s.add(row)
                else:
                    row.title = title
                    row.username = username
                    row.entity_type = kind
                    row.is_active = True
                    if int(row.last_message_id or 0) <= 0:
                        row.last_message_id = baseline
                await s.flush()
                payload = {
                    "id": int(row.id),
                    "kind": str(row.kind),
                    "chat_id": int(row.chat_id),
                    "title": str(row.title),
                    "username": row.username,
                    "entity_type": str(row.entity_type),
                    "is_active": True,
                    "last_message_id": int(row.last_message_id or 0),
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }

            log.info(
                "telegram_source_selected chat_id=%s type=%s first=%s baseline=%s",
                key,
                kind,
                not exists or previous_last_id <= 0,
                payload["last_message_id"],
            )
            try:
                self._bus().emit(
                    events.SETTINGS_CHANGED, key="telegram.source", source=payload
                )
            except Exception as exc:  # noqa: BLE001 - the bus is best effort
                log.warning("telegram_source_event_failed error=%s", exc)

            await self._register_handlers_locked()
            await self._notify_reviewer(int(payload["id"]), int(payload["chat_id"]))
            return {"ok": True, "source": payload}

    async def _notify_reviewer(self, source_id: int, chat_id: int) -> None:
        """Tell the channel reviewer the binding changed. Selection still succeeds if it fails."""
        runtime = self._runtime()
        reviewer = getattr(runtime, "reviewer", None) if runtime is not None else None
        if reviewer is None:
            return
        try:
            await reviewer.on_source_selected(source_id=source_id, chat_id=chat_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - picking a channel must not depend on the study
            log.warning("channel_reviewer_notify_failed chat_id=%s error=%s", chat_id, exc)

    async def _kick_reviewer(self) -> None:
        """Study the active source once the client is signed in, if it has no brief yet."""
        runtime = self._runtime()
        reviewer = getattr(runtime, "reviewer", None) if runtime is not None else None
        if reviewer is None:
            return
        try:
            await reviewer.kick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - sign-in must not depend on the study
            log.warning("channel_reviewer_kick_failed error=%s", exc)

    # ---- update handlers --------------------------------------------------------------

    async def register_handlers(self) -> None:
        """(Re)attach the update handlers for the active source."""
        async with self._lock:
            await self._register_handlers_locked()

    async def _register_handlers_locked(self) -> None:
        """Body of :meth:`register_handlers`, run under the service lock."""
        await self._unregister_handlers_locked()

        client = self.client
        if client is None or not client.is_connected():
            return
        try:
            if not await client.is_user_authorized():
                return
        except Exception as exc:  # noqa: BLE001 - never raise out of handler wiring
            log.warning("telegram_handler_auth_check_failed error=%s", exc)
            return

        async with session_scope() as s:
            result = await s.execute(select(Source).where(Source.is_active.is_(True)))
            row = result.scalars().first()
            active = (int(row.id), int(row.chat_id), str(row.title)) if row else None

        if active is None:
            # §3.5: before the operator has selected a source, no handler is registered at all
            # and nothing is ingested.
            log.info("telegram_handlers_idle reason=no_active_source")
            return

        self._active_source_id, self._active_chat_id, title = active

        # No `chats=` filter on purpose. Telethon resolves that filter lazily on the first
        # update, inside a dispatch path that does not log resolution failures — a private
        # channel missing from the entity cache would then silently stop every update. The
        # chat is checked in _is_active_chat instead, which cannot fail.
        pairs: tuple[tuple[Callable[[Any], Any], Any], ...] = (
            (self._on_new_message, tg_events.NewMessage()),
            (self._on_message_edited, tg_events.MessageEdited()),
            (self._on_album, tg_events.Album()),
            # Service messages (pins, title changes, joins) arrive through ChatAction, NOT
            # NewMessage — telethon's NewMessage builder drops MessageService outright. They
            # are recorded rather than lost (§3.2).
            (self._on_chat_action, tg_events.ChatAction()),
            # Channel deletes arrive as UpdateDeleteChannelMessages, not as an edit.
            (self._on_message_deleted, tg_events.MessageDeleted()),
        )
        for callback, builder in pairs:
            client.add_event_handler(callback, builder)
            # Keep the exact (callback, builder) pair so unregister_handlers() can remove it
            # when the operator changes source.
            self._handlers.append((callback, builder))

        log.info(
            "telegram_handlers_registered source_id=%s chat_id=%s title=%s handlers=%d",
            self._active_source_id,
            self._active_chat_id,
            title,
            len(self._handlers),
        )

    async def unregister_handlers(self) -> None:
        """Detach every handler this service registered."""
        async with self._lock:
            await self._unregister_handlers_locked()

    async def _unregister_handlers_locked(self) -> None:
        """Body of :meth:`unregister_handlers`, run under the service lock."""
        client = self.client
        for callback, builder in self._handlers:
            if client is None:
                continue
            try:
                client.remove_event_handler(callback, builder)
            except Exception as exc:  # noqa: BLE001 - removal must not raise
                log.warning("telegram_handler_remove_failed error=%s", exc)
        self._handlers.clear()
        self._cancel_album_timers()
        self._album_pending.clear()
        self._album_done.clear()
        self._active_source_id = None
        self._active_chat_id = None

    def _cancel_album_timers(self) -> None:
        """Cancel every pending album flush task."""
        for task in tuple(self._album_timers.values()):
            task.cancel()
        self._album_timers.clear()

    def _is_active_chat(self, event: Any) -> bool:
        """Report whether an event belongs to the active source's chat."""
        if self._active_chat_id is None:
            return False
        try:
            return int(getattr(event, "chat_id", 0) or 0) == int(self._active_chat_id)
        except (TypeError, ValueError):
            return False

    async def _guard(self, name: str, handler: Callable[[Any], Any], event: Any) -> None:
        """Run one handler so that nothing ever raises back into Telethon's dispatcher.

        Telethon logs an unhandled handler exception and carries on, but a raising handler in
        the ingest path is a message lost with no trace on the dashboard. Every failure is a
        `system.warning` instead.
        """
        try:
            await handler(event)
        except Exception as exc:  # noqa: BLE001 - a handler never raises into Telethon
            log.exception("telegram_handler_failed handler=%s", name)
            self._warn("telegram_handler_failed", handler=name, error=str(exc))

    async def _on_new_message(self, event: Any) -> None:
        """Telethon entry point for a new message."""
        await self._guard("new_message", self._handle_new_message, event)

    async def _on_message_edited(self, event: Any) -> None:
        """Telethon entry point for an edited message."""
        await self._guard("message_edited", self._handle_message_edited, event)

    async def _on_album(self, event: Any) -> None:
        """Telethon entry point for a reassembled album."""
        await self._guard("album", self._handle_album, event)

    async def _on_chat_action(self, event: Any) -> None:
        """Telethon entry point for a service message."""
        await self._guard("chat_action", self._handle_chat_action, event)

    async def _on_message_deleted(self, event: Any) -> None:
        """Telethon entry point for deleted channel messages."""
        await self._guard("message_deleted", self._handle_message_deleted, event)

    async def _handle_new_message(self, event: Any) -> None:
        """Hand a new message to the ingestor, buffering album members first."""
        if not self._is_active_chat(event):
            return
        message = getattr(event, "message", None)
        if message is None:
            return
        if getattr(message, "grouped_id", None):
            # Album members are *also* delivered as individual NewMessage updates, so handing
            # each one over would store one row per photo instead of one message with N images.
            self._queue_album_member(message)
            return
        ingestor = self._ingestor()
        if ingestor is None:
            self._warn("ingestor_unavailable", handler="new_message")
            return
        await ingestor.handle_message(message)

    async def _handle_message_edited(self, event: Any) -> None:
        """Hand an edit to the ingestor; §3.3 stores it as a new revision, not a new signal."""
        if not self._is_active_chat(event):
            return
        message = getattr(event, "message", None)
        if message is None:
            return
        ingestor = self._ingestor()
        if ingestor is None:
            self._warn("ingestor_unavailable", handler="message_edited")
            return
        await ingestor.handle_message(message, edited=True)

    async def _handle_album(self, event: Any) -> None:
        """Hand a whole album to the ingestor as one message with every image.

        §3.4, and both halves are real: ``events.Album`` has **no attribute proxy**, so
        ``event.id``, ``event.media`` and ``event.date`` DO NOT EXIST on it — the id comes from
        ``event.messages[0].id`` and the media from iterating ``event.messages``. And a
        one-item "album" never fires this event at all (Telethon's ``Album.filter`` drops a
        group of one), which is why _queue_album_member also flushes on a timer.
        """
        if not self._is_active_chat(event):
            return
        messages = list(getattr(event, "messages", None) or ())
        if not messages:
            return
        grouped_id = getattr(messages[0], "grouped_id", None)
        merged = self._merge_album(grouped_id, messages)
        # Telethon always sets grouped_id on Album members, but when it is absent the
        # delivered-once guard must not simply be skipped -- fall back to the first
        # message id (negated, so it cannot collide with a real grouped_id). The DB
        # unique index would catch a duplicate anyway; this keeps the two ingest paths
        # mutually exclusive rather than relying on that backstop.
        claim_key = int(grouped_id) if grouped_id is not None else -int(getattr(messages[0], "id", 0) or 0)
        if claim_key and not self._claim_album(claim_key):
            return
        ingestor = self._ingestor()
        if ingestor is None:
            self._warn("ingestor_unavailable", handler="album")
            return
        await ingestor.handle_message(merged[0], album=merged)

    async def _handle_chat_action(self, event: Any) -> None:
        """Record a service message; §3.2 requires them stored, not dropped."""
        if not self._is_active_chat(event):
            return
        ingestor = self._ingestor()
        if ingestor is None:
            self._warn("ingestor_unavailable", handler="chat_action")
            return
        await ingestor.record_service(event)

    async def _handle_message_deleted(self, event: Any) -> None:
        """Flatten exposure when the channel deletes a post in the active source.

        ``MessageDeleted.chat_id`` is set for channels (``UpdateDeleteChannelMessages``).
        A private-chat delete has no chat and is ignored, so one missing peer cannot
        flatten every open chain.
        """
        if not self._same_active_chat(getattr(event, "chat_id", None)):
            return
        raw_ids = getattr(event, "deleted_ids", None)
        if not raw_ids:
            single = getattr(event, "deleted_id", None)
            raw_ids = [single] if single else []
        ids: list[int] = []
        for item in raw_ids:
            try:
                number = int(item)
            except (TypeError, ValueError):
                continue
            if number > 0:
                ids.append(number)
        if not ids:
            return
        ingestor = self._ingestor()
        if ingestor is None:
            self._warn("ingestor_unavailable", handler="message_deleted")
            return
        await ingestor.handle_deleted(ids)

    def _same_active_chat(self, chat_id: Any) -> bool:
        """Match the active source, tolerating Telegram's ``-100`` channel marker."""
        if self._active_chat_id is None or chat_id is None:
            return False
        try:
            return _short_chat_id(self._active_chat_id) == _short_chat_id(chat_id)
        except (TypeError, ValueError):
            return False

    # ---- album reassembly (§3.4) -------------------------------------------------------

    def _merge_album(self, grouped_id: Any, messages: list[Any]) -> list[Any]:
        """Merge the Album event's messages with anything buffered, ordered by message id."""
        collected: dict[int, Any] = {}
        for message in messages:
            collected[int(getattr(message, "id", 0) or 0)] = message
        if grouped_id is not None:
            for message in self._album_pending.pop(int(grouped_id), ()):
                collected.setdefault(int(getattr(message, "id", 0) or 0), message)
            timer = self._album_timers.pop(int(grouped_id), None)
            if timer is not None:
                timer.cancel()
        return [collected[key] for key in sorted(collected)]

    def _claim_album(self, grouped_id: int) -> bool:
        """Mark an album delivered, returning False when it was already handed off."""
        if grouped_id in self._album_done:
            return False
        self._album_done[grouped_id] = time.time()
        while len(self._album_done) > _ALBUM_MEMORY:
            oldest = next(iter(self._album_done))
            self._album_done.pop(oldest, None)
        return True

    def _queue_album_member(self, message: Any) -> None:
        """Buffer one album member and (re)arm the flush timer.

        Telethon reassembles albums behind a 0.5 s delay, and a **one-item album never fires
        `events.Album` at all** — its ``filter`` drops any group with fewer than two messages.
        Buffering here and flushing after ALBUM_GRACE_SECONDS covers that case, and the
        delivered-set makes the two paths mutually exclusive so nothing is ingested twice.
        """
        grouped_id = int(getattr(message, "grouped_id", 0) or 0)
        if not grouped_id or grouped_id in self._album_done:
            return
        self._album_pending.setdefault(grouped_id, []).append(message)
        timer = self._album_timers.pop(grouped_id, None)
        if timer is not None:
            timer.cancel()
        self._album_timers[grouped_id] = asyncio.create_task(self._flush_album(grouped_id))

    async def _flush_album(self, grouped_id: int) -> None:
        """Deliver a buffered album that `events.Album` never fired for."""
        try:
            await asyncio.sleep(ALBUM_GRACE_SECONDS)
        except asyncio.CancelledError:
            return
        self._album_timers.pop(grouped_id, None)
        messages = self._album_pending.pop(grouped_id, [])
        if not messages:
            return
        if not self._claim_album(grouped_id):
            return
        messages.sort(key=lambda m: int(getattr(m, "id", 0) or 0))
        try:
            ingestor = self._ingestor()
            if ingestor is None:
                self._warn("ingestor_unavailable", handler="album_flush")
                return
            # Always as an album, even when only one member arrived: the message still carries
            # a grouped_id, and the ingestor stands a grouped NewMessage down in favour of the
            # Album event that (for a group of one) is never going to come. Passing the list is
            # what tells it this IS the album — the same shape catch-up builds for such a group.
            await ingestor.handle_message(messages[0], album=messages)
        except Exception as exc:  # noqa: BLE001 - a timer task never raises into the loop
            log.exception("telegram_album_flush_failed grouped_id=%s", grouped_id)
            self._warn("telegram_handler_failed", handler="album_flush", error=str(exc))

    # ---- media and catch-up -----------------------------------------------------------

    async def download_image(self, message: Any, dest_dir: str) -> str | None:
        """Download a message's image to a uuid-named file under `dest_dir`.

        Returns ``None`` when the message carries no downloadable image — a link preview, a
        sticker, a video or a non-image document. An image sent as an *uncompressed document*
        is downloaded like any photo, because that is how signal channels preserve chart
        quality (§3.2).
        """
        if message is None or not _has_downloadable_image(message):
            return None
        from app.util.images import MAX_SOURCE_IMAGE_BYTES
        file = getattr(message, "file", None)
        size = getattr(file, "size", 0) or getattr(getattr(message, "document", None), "size", 0) or 0
        if size > MAX_SOURCE_IMAGE_BYTES:
            raise IngestError("signal image exceeds the 20 MiB download limit")
        client = self._require_client()
        directory = Path(dest_dir)
        directory.mkdir(parents=True, exist_ok=True)

        media = getattr(message, "media", None)
        extension = ""
        try:
            extension = tg_utils.get_extension(media) or ""
        except Exception as exc:  # noqa: BLE001 - an unknown mime is not a failure
            log.debug("telegram_extension_unknown error=%s", exc)
        if extension.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif"}:
            extension = ".jpg"
        target = directory / f"{uuid4().hex}{extension}"

        try:
            def check_progress(received, total):
                if received > MAX_SOURCE_IMAGE_BYTES or total and total > MAX_SOURCE_IMAGE_BYTES:
                    raise IngestError("signal image exceeds the 20 MiB download limit")
            async with asyncio.timeout(60):
                saved = await client.download_media(message, file=str(target), progress_callback=check_progress)
            if saved and Path(saved).stat().st_size > MAX_SOURCE_IMAGE_BYTES:
                raise IngestError("signal image exceeds the 20 MiB download limit")
        except (IngestError, TimeoutError):
            target.unlink(missing_ok=True)
            raise IngestError("signal image download exceeded its safety limit") from None
        except RPCError as exc:
            log.warning(
                "telegram_download_failed message_id=%s error=%s",
                getattr(message, "id", None),
                type(exc).__name__,
            )
            raise IngestError(
                telegram_error_message(exc),
                detail={
                    "telegram_error": type(exc).__name__,
                    "message_id": getattr(message, "id", None),
                },
            ) from exc
        except OSError as exc:
            raise IngestError(
                f"the image could not be written to {directory}: {exc}",
                detail={"message_id": getattr(message, "id", None)},
            ) from exc

        if not saved:
            return None
        return str(saved)

    async def fetch_newer(self, source: Any, limit: int) -> list:
        """Fetch up to `limit` messages newer than `source.last_message_id`, oldest first.

        §8.4: on the very first run for a source there is no stored message id, so nothing is
        fetched and the channel's current latest id becomes the baseline instead — selecting a
        channel must never replay its history as live signals.
        """
        client = self._require_client()
        chat_id = int(getattr(source, "chat_id", 0) or 0)
        if not chat_id:
            raise IngestError("the source has no chat_id to catch up from")
        entity = await self._resolve_entity(chat_id)

        min_id = int(getattr(source, "last_message_id", 0) or 0)
        if min_id <= 0:
            baseline = await self._latest_message_id(entity)
            source_id = int(getattr(source, "id", 0) or 0)
            if source_id:
                async with session_scope() as s:
                    row = await s.get(Source, source_id)
                    if row is not None and int(row.last_message_id or 0) <= 0:
                        row.last_message_id = baseline
            try:
                source.last_message_id = baseline
            except Exception as exc:  # noqa: BLE001 - a detached row is still fine
                log.debug("telegram_baseline_write_skipped error=%s", exc)
            log.info("telegram_catchup_baseline chat_id=%s last_message_id=%s", chat_id, baseline)
            return []

        count = max(0, int(limit))
        if count == 0:
            return []

        messages: list[Any] = []
        try:
            async for message in client.iter_messages(
                entity, min_id=min_id, reverse=True, limit=count
            ):
                messages.append(message)
        except RPCError as exc:
            log.warning("telegram_fetch_newer_failed error=%s", type(exc).__name__)
            raise IngestError(
                telegram_error_message(exc),
                detail={"telegram_error": type(exc).__name__, "chat_id": chat_id},
            ) from exc

        log.info(
            "telegram_catchup chat_id=%s min_id=%s fetched=%d", chat_id, min_id, len(messages)
        )
        return messages

    async def fetch_recent_days(self, chat_id: int, *, days: int = 4, limit: int = 400) -> list:
        """Return up to ``limit`` messages from the last ``days`` days, oldest first.

        The channel reviewer uses this when the database sample is thin. Nothing here is
        ingested or enqueued, and ``last_message_id`` is not moved: these posts are study
        notes, not live signals.
        """
        client = self._require_client()
        key = int(chat_id)
        if not key:
            raise IngestError("the source has no chat_id to read history from")
        entity = await self._resolve_entity(key)
        window = max(1, int(days))
        since = datetime.now(UTC) - timedelta(days=window)
        cap = max(1, int(limit))
        found: list[Any] = []
        try:
            async for message in client.iter_messages(entity, limit=cap):
                posted = getattr(message, "date", None)
                if isinstance(posted, datetime):
                    aware = posted if posted.tzinfo is not None else posted.replace(tzinfo=UTC)
                    if aware.astimezone(UTC) < since:
                        break
                found.append(message)
        except RPCError as exc:
            log.warning("telegram_history_window_failed error=%s", type(exc).__name__)
            raise IngestError(
                telegram_error_message(exc),
                detail={"telegram_error": type(exc).__name__, "chat_id": key},
            ) from exc
        found.reverse()
        log.info(
            "telegram_history_window chat_id=%s days=%s fetched=%d", key, window, len(found)
        )
        return found

    async def get_reply_message(self, message: Any) -> Any:
        """Return the message a reply points at, or None when it cannot be fetched.

        §3.3 depends on this: a reply is how a channel says *"this one hit TP1"*, and the
        parent's text is what makes `move sl to be` mean something. A parent that cannot be
        fetched is reported as missing rather than raising the whole ingest.
        """
        if message is None or not getattr(message, "is_reply", False):
            return None
        try:
            return await message.get_reply_message()
        except Exception as exc:  # noqa: BLE001 - a missing parent is data, not a failure
            log.warning(
                "telegram_reply_fetch_failed message_id=%s error=%s",
                getattr(message, "id", None),
                exc,
            )
            return None
