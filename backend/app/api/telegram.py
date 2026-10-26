"""Telegram account API: the headless login flow, the channel picker, and image bytes.

Everything here delegates to :class:`app.telegram.client.TelegramService`. The login flow of
BUILD_PROMPT §3.1 lives in that service — `connect` → `send_code_request` → `sign_in` → the
`SessionPasswordNeededError` branch — and this module's only job is to turn its outcomes into
HTTP the login screen can render.

That job is not trivial, and §3.1 is explicit about why: *"a 400 with `error` is not good enough
for a login screen"*. An operator who mistypes a digit, an operator whose code expired while they
were looking for their phone, and an operator whose account is flood-limited for six hours need
three different sentences and three different behaviours from the UI. So every Telethon failure
gets its own machine-readable ``code`` and its own HTTP status:

============================  ======  ==================  =========================================
Telethon exception            status  code                what the UI does with it
============================  ======  ==================  =========================================
PhoneCodeInvalidError            400  ``code_invalid``    keep the form, clear the code field
PhoneCodeExpiredError            400  ``code_expired``    offer "send a new code"
PhoneCodeEmptyError              400  ``code_empty``      focus the code field
PhoneNumberInvalidError          400  ``phone_invalid``   mark the phone field
PhoneNumberUnoccupiedError       400  ``phone_unoccupied``mark the phone field
PhoneNumberBannedError           403  ``phone_banned``    terminal: nothing to retry
PasswordHashInvalidError         400  ``password_invalid``keep the 2FA step, clear the password
SessionPasswordNeededError       200  ``—``               **not an error**: ``needs_password: true``
ApiIdInvalidError                401  ``api_id_invalid``  send the operator back to my.telegram.org
FloodWaitError                   429  ``flood_wait``      show the countdown from ``detail.seconds``
AuthKeyUnregisteredError         401  ``session_invalid`` restart the login from the phone number
SessionRevokedError              401  ``session_invalid`` restart the login from the phone number
============================  ======  ==================  =========================================

The sentences themselves are never written here: they come from
``app.telegram.client.TELEGRAM_ERROR_MESSAGES`` through the service, so the wording an operator
reads on the login screen and the wording in the logs are the same string.

Errors are rendered as the ``{"error": {...}}`` envelope of CONTRACTS §18 by returning a
``JSONResponse`` directly rather than raising ``HTTPException``, because FastAPI's built-in
handler wraps its ``detail`` in ``{"detail": ...}`` and would break that envelope.
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Any, Awaitable, Iterable

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select

from app.db import get_setting, has_value, session_scope, set_setting
from app.errors import SignalPilotError, to_http
from app.models import MessageImage, Source
from app.runtime import Runtime, get_runtime
from app.schemas import (
    DialogsOut,
    OkResponse,
    SendCodeOut,
    SignInOut,
    SourceOut,
    SourceSelect,
    SourceSelectOut,
    TelegramCredentials,
    TelegramPassword,
    TelegramSignIn,
    TelegramStatusOut,
)
from app.telegram.client import telegram_error_message
from app.util.logging import get_logger

router = APIRouter(prefix="/api/telegram", tags=["telegram"])

log = get_logger("app.api.telegram")


# --------------------------------------------------------------------------------------
# Error rendering
# --------------------------------------------------------------------------------------

#: Telethon exception class name -> (HTTP status, machine-readable error code).
#:
#: Keyed by class *name* rather than by class object because the service has already turned the
#: exception into an ``IngestError`` by the time it reaches us and only the name survives, in
#: ``detail["telegram_error"]``. Raw Telethon exceptions (anything that escapes the service) are
#: matched against their whole MRO, so a subclass still lands on its parent's row.
TELEGRAM_ERROR_HTTP: dict[str, tuple[int, str]] = {
    "PhoneCodeInvalidError": (400, "code_invalid"),
    "PhoneCodeExpiredError": (400, "code_expired"),
    "PhoneCodeEmptyError": (400, "code_empty"),
    # The hash is half of the (code, hash) pair; if Telegram forgot it the code is unusable and
    # the operator has to request a new one — which is exactly the "expired" experience.
    "PhoneCodeHashEmptyError": (400, "code_expired"),
    "PhoneNumberInvalidError": (400, "phone_invalid"),
    "PhoneNumberUnoccupiedError": (400, "phone_unoccupied"),
    "PhoneNumberBannedError": (403, "phone_banned"),
    "PasswordHashInvalidError": (400, "password_invalid"),
    # Reached only outside sign_in(), which reports 2FA as a 200 with needs_password.
    "SessionPasswordNeededError": (401, "password_needed"),
    "ApiIdInvalidError": (401, "api_id_invalid"),
    "ApiIdPublishedFloodError": (429, "flood_wait"),
    "FloodWaitError": (429, "flood_wait"),
    "AuthKeyUnregisteredError": (401, "session_invalid"),
    "SessionRevokedError": (401, "session_invalid"),
    "AuthRestartError": (409, "auth_restart"),
}

#: Status and code used when a Telethon error has no row of its own: the request reached Telegram
#: and Telegram refused it, which is an upstream failure, not a malformed request.
_TELEGRAM_FALLBACK: tuple[int, str] = (502, "telegram_error")


def _error(
    status: int,
    code: str,
    message: str,
    detail: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Return the CONTRACTS §18 error envelope with an explicit status."""
    body = {"error": {"code": code, "message": message, "detail": dict(detail or {})}}
    return JSONResponse(status_code=status, content=body, headers=headers)


def _unavailable(component: str) -> JSONResponse:
    """Return the 503 used while a service has not finished wiring itself up."""
    return _error(
        503,
        "config_error",
        f"The {component} service is not ready yet — retry once the backend has finished starting.",
        {"component": component},
    )


def _is_telethon_error(exc: BaseException) -> bool:
    """Report whether an exception came out of Telethon rather than out of our own code."""
    return type(exc).__module__.split(".", 1)[0] == "telethon"


def _error_names(exc: BaseException) -> Iterable[str]:
    """Yield the Telethon class names an exception could match, most specific first."""
    if isinstance(exc, SignalPilotError):
        name = exc.detail.get("telegram_error")
        return (str(name),) if name else ()
    return tuple(klass.__name__ for klass in type(exc).__mro__)


def _telegram_failure(exc: BaseException) -> JSONResponse:
    """Map any login-path failure onto its own status, code and operator-facing sentence."""
    if isinstance(exc, SignalPilotError):
        message = exc.message
        detail: dict[str, Any] = dict(exc.detail)
        fallback_status, _body = to_http(exc)
        fallback = (fallback_status, exc.code)
    else:
        # A raw Telethon exception escaped the service. Its sentence still comes from
        # TELEGRAM_ERROR_MESSAGES, so the operator never sees a bare class name.
        message = telegram_error_message(exc)
        detail = {"telegram_error": type(exc).__name__}
        seconds = getattr(exc, "seconds", None)
        if isinstance(seconds, int) and not isinstance(seconds, bool):
            detail["seconds"] = seconds
        fallback = _TELEGRAM_FALLBACK

    resolved: tuple[int, str] | None = None
    for name in _error_names(exc):
        resolved = TELEGRAM_ERROR_HTTP.get(name)
        if resolved is not None:
            break
    if resolved is None:
        # Every flood error carries `.seconds`, including subclasses this table has never heard
        # of. A wait is a 429 whatever Telegram called it.
        seconds = detail.get("seconds")
        if isinstance(seconds, int) and not isinstance(seconds, bool) and seconds > 0:
            resolved = (429, "flood_wait")
    status, code = resolved if resolved is not None else fallback

    headers: dict[str, str] | None = None
    seconds = detail.get("seconds")
    if status == 429 and isinstance(seconds, int) and not isinstance(seconds, bool) and seconds > 0:
        headers = {"Retry-After": str(seconds)}

    log.info("telegram_api_failure status=%d code=%s detail=%s", status, code, detail)
    return _error(status, code, message, detail, headers)


async def _guarded(awaitable: Awaitable[Any]) -> Any:
    """Await a service call, returning the error envelope instead of raising on a known failure.

    Anything that is neither one of our own errors nor a Telethon error is re-raised: a bug in
    this backend must not be dressed up as a Telegram problem the operator is asked to fix.
    """
    try:
        return await awaitable
    except SignalPilotError as exc:
        return _telegram_failure(exc)
    except Exception as exc:  # noqa: BLE001 - narrowed immediately below
        if _is_telethon_error(exc):
            return _telegram_failure(exc)
        raise


# --------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------


def _service(runtime: Runtime) -> Any | None:
    """Return the TelegramService, or None while the runtime is still being assembled."""
    return getattr(runtime, "telegram", None)


def _connected(service: Any) -> bool:
    """Report whether the Telethon client object exists and holds a live connection."""
    client = getattr(service, "client", None)
    if client is None:
        return False
    try:
        return bool(client.is_connected())
    except Exception as exc:  # noqa: BLE001 - a status probe never raises
        log.warning("telegram_connected_check_failed error=%s", exc)
        return False


async def _active_source() -> SourceOut | None:
    """Return the one active signal source, or None before the operator has picked one."""
    async with session_scope() as session:
        result = await session.execute(select(Source).where(Source.is_active.is_(True)))
        row = result.scalars().first()
        return SourceOut.model_validate(row) if row is not None else None


async def _stored_credentials() -> tuple[int, str, str]:
    """Read (api_id, api_hash, phone) from the encrypted settings store, blanks included."""
    async with session_scope() as session:
        raw_api_id = await get_setting(session, "tg.api_id", 0)
        api_hash = await get_setting(session, "tg.api_hash", "")
        phone = await get_setting(session, "tg.phone", "")
    try:
        api_id = int(raw_api_id or 0)
    except (TypeError, ValueError):
        api_id = 0
    return api_id, str(api_hash or "").strip(), str(phone or "").strip()


def _missing_credential(api_id: int, api_hash: str, phone: str) -> JSONResponse | None:
    """Return the field-anchored 400 for the first missing login credential, if any."""
    if api_id <= 0:
        return _error(
            400,
            "config_error",
            "api_id is missing — copy it from my.telegram.org and save the credentials first.",
            {"field": "api_id"},
        )
    if not api_hash:
        return _error(
            400,
            "config_error",
            "api_hash is missing — copy it from my.telegram.org and save the credentials first.",
            {"field": "api_hash"},
        )
    if not phone:
        return _error(
            400,
            "config_error",
            # api_id/api_hash are the APPLICATION's and are already saved by the time this
            # fires; only the account is missing. Telling the operator to "save the credentials"
            # sends them back to a form they have already filled in correctly.
            "Enter the phone number of the Telegram account you want to connect, "
            "in international format like +14155552671.",
            {"field": "phone"},
        )
    return None


# --------------------------------------------------------------------------------------
# POST /api/telegram/credentials
# --------------------------------------------------------------------------------------


@router.post("/credentials", response_model=OkResponse)
async def set_credentials(payload: TelegramCredentials) -> Any:
    """Persist the my.telegram.org application credentials and the account phone number.

    The api_hash is stored encrypted. Sending the mask (or omitting the field) keeps whatever is
    already stored, so the operator can correct a phone number without retyping a 32-character
    hash. Any pending ``phone_code_hash`` is dropped: a login code issued against the previous
    credentials cannot complete a sign-in against these.
    """
    api_hash = payload.api_hash
    async with session_scope() as session:
        if api_hash is None:
            if not await has_value(session, "tg.api_hash"):
                return _error(
                    400,
                    "config_error",
                    "api_hash is missing — copy it from my.telegram.org together with api_id.",
                    {"field": "api_hash"},
                )
        else:
            cleaned = api_hash.strip()
            if not cleaned:
                return _error(
                    400,
                    "config_error",
                    "api_hash is missing — copy it from my.telegram.org together with api_id.",
                    {"field": "api_hash"},
                )
            await set_setting(session, "tg.api_hash", cleaned, secret=True)
        await set_setting(session, "tg.api_id", int(payload.api_id))
        # Only when supplied: api_id/api_hash identify the APPLICATION and are entered once,
        # the phone identifies the ACCOUNT and changes whenever a different one is connected.
        # Overwriting a stored phone with None here would silently disconnect the operator's
        # account the next time they merely re-saved their keys.
        if payload.phone:
            await set_setting(session, "tg.phone", payload.phone)
        # A code requested with the old credentials can never be signed in with the new ones.
        await set_setting(session, "tg.phone_code_hash", "")

    log.info("telegram_credentials_saved api_id=%s", int(payload.api_id))
    return OkResponse(
        ok=True,
        message="Telegram credentials saved. Request a login code to continue.",
    )


# --------------------------------------------------------------------------------------
# POST /api/telegram/send-code
# --------------------------------------------------------------------------------------


@router.post("/send-code", response_model=SendCodeOut)
async def send_code(runtime: Runtime = Depends(get_runtime)) -> Any:
    """Ask Telegram to send a login code to the stored phone number.

    The ``phone_code_hash`` Telegram answers with is persisted by the service, because the code
    arrives in a *second* HTTP request that Telethon's per-process cache may no longer serve
    (§3.1). An account that is already signed in answers ``code_sent: false`` rather than an
    error — there is nothing to fix.
    """
    service = _service(runtime)
    if service is None:
        return _unavailable("Telegram")

    api_id, api_hash, phone = await _stored_credentials()
    missing = _missing_credential(api_id, api_hash, phone)
    if missing is not None:
        return missing

    result = await _guarded(service.send_code(api_id, api_hash, phone))
    if isinstance(result, JSONResponse):
        return result

    already = bool(result.get("already_authorized", False))
    code_sent = bool(result.get("code_sent", False))
    if already:
        message = "This Telegram account is already signed in — pick a channel to continue."
    elif code_sent:
        message = "Telegram sent a login code. Enter it within a few minutes."
    else:
        message = "Telegram did not send a login code."
    return SendCodeOut(ok=bool(result.get("ok", True)), code_sent=code_sent, message=message)


# --------------------------------------------------------------------------------------
# POST /api/telegram/sign-in
# --------------------------------------------------------------------------------------


@router.post("/sign-in", response_model=SignInOut)
async def sign_in(payload: TelegramSignIn, runtime: Runtime = Depends(get_runtime)) -> Any:
    """Complete the login with the code Telegram sent.

    Two-factor authentication is **not** a failure: ``SessionPasswordNeededError`` comes back as a
    200 carrying ``needs_password: true`` so the UI simply reveals the password field. A wrong
    code is a 400 ``code_invalid``; an expired one is a 400 ``code_expired``, which is a different
    remedy (request a new code) and therefore a different code.
    """
    service = _service(runtime)
    if service is None:
        return _unavailable("Telegram")

    result = await _guarded(service.sign_in(payload.code))
    if isinstance(result, JSONResponse):
        return result

    needs_password = bool(result.get("needs_password", False))
    message = (
        "This account has two-factor authentication enabled. Enter your Telegram cloud password."
        if needs_password
        else "Signed in to Telegram."
    )
    return SignInOut(
        ok=bool(result.get("ok", True)), needs_password=needs_password, message=message
    )


# --------------------------------------------------------------------------------------
# POST /api/telegram/password
# --------------------------------------------------------------------------------------


@router.post("/password", response_model=OkResponse)
async def sign_in_password(
    payload: TelegramPassword, runtime: Runtime = Depends(get_runtime)
) -> Any:
    """Finish a two-factor login with the account's cloud password."""
    service = _service(runtime)
    if service is None:
        return _unavailable("Telegram")

    result = await _guarded(service.sign_in_password(payload.password))
    if isinstance(result, JSONResponse):
        return result
    return OkResponse(ok=bool(result.get("ok", True)), message="Signed in to Telegram.")


# --------------------------------------------------------------------------------------
# POST /api/telegram/logout
# --------------------------------------------------------------------------------------


@router.post("/logout", response_model=OkResponse)
async def logout(runtime: Runtime = Depends(get_runtime)) -> Any:
    """Sign out of Telegram, revoke the session and detach the update handlers.

    The ingest worker is deliberately left running. It is idle without handlers, and stopping it
    here would mean a later sign-in that reuses the same source never restarts it — messages would
    queue and never be processed.
    """
    service = _service(runtime)
    if service is None:
        return _unavailable("Telegram")

    result = await _guarded(service.logout())
    if isinstance(result, JSONResponse):
        return result
    revoked = bool(result.get("revoked", False))
    message = (
        "Signed out of Telegram and revoked the session."
        if revoked
        else "Signed out of Telegram."
    )
    return OkResponse(ok=bool(result.get("ok", True)), message=message)


# --------------------------------------------------------------------------------------
# GET /api/telegram/status
# --------------------------------------------------------------------------------------


@router.get("/status", response_model=TelegramStatusOut)
async def status(runtime: Runtime = Depends(get_runtime)) -> Any:
    """Report the connection, the signed-in account and the active source.

    Read-only and never an error: the settings page polls this while the operator works through
    the login, and a disconnected client is a state to render, not a failure to raise.
    """
    service = _service(runtime)
    if service is None:
        return TelegramStatusOut(
            connected=False,
            authorized=False,
            user=None,
            source=await _active_source(),
            error="The Telegram service is not ready yet.",
        )

    connected = _connected(service)
    authorized = False
    user: dict[str, Any] | None = None
    error: str | None = None
    try:
        authorized = bool(await service.is_authorized())
        fetched = await service.me()
        user = fetched if isinstance(fetched, dict) else None
    except SignalPilotError as exc:
        error = exc.message
    except Exception as exc:  # noqa: BLE001 - narrowed immediately below
        if not _is_telethon_error(exc):
            raise
        error = telegram_error_message(exc)

    if error is None:
        entry = runtime.status.get("telegram")
        if isinstance(entry, dict) and not entry.get("ok", False):
            error = entry.get("error")

    return TelegramStatusOut(
        connected=connected,
        authorized=authorized,
        user=user,
        source=await _active_source(),
        error=error,
    )


# --------------------------------------------------------------------------------------
# GET /api/telegram/dialogs
# --------------------------------------------------------------------------------------


@router.get("/dialogs", response_model=DialogsOut)
async def dialogs(runtime: Runtime = Depends(get_runtime)) -> Any:
    """List the channels, supergroups and groups this account can read."""
    service = _service(runtime)
    if service is None:
        return _unavailable("Telegram")

    result = await _guarded(service.dialogs())
    if isinstance(result, JSONResponse):
        return result
    return DialogsOut(dialogs=list(result))


# --------------------------------------------------------------------------------------
# POST /api/telegram/source
# --------------------------------------------------------------------------------------


@router.post("/source", response_model=SourceSelectOut)
async def select_source(
    payload: SourceSelect, runtime: Runtime = Depends(get_runtime)
) -> Any:
    """Make one chat the active signal source and start listening to it.

    Three things happen, in this order and for the reason BUILD_PROMPT §8.4 gives:

    1. **The baseline.** On the *first* selection of a channel the service records its current
       latest message id as ``last_message_id`` and ingests nothing. Picking a channel must not
       replay a month of history as live signals; everything already posted stays history.
    2. **The handlers.** ``select_source`` re-registers NewMessage / MessageEdited / Album /
       ChatAction against the new chat and drops the previous chat's handlers, so exactly one
       source is ever live.
    3. **The worker.** The single ingest worker is started here so the first message that arrives
       has something consuming its queue. ``Ingestor.start()`` is idempotent, so re-selecting the
       same channel — or selecting one after startup already began the worker — is a no-op.
    """
    service = _service(runtime)
    if service is None:
        return _unavailable("Telegram")

    result = await _guarded(service.select_source(payload.chat_id))
    if isinstance(result, JSONResponse):
        return result

    ingestor = getattr(runtime, "ingestor", None)
    if ingestor is not None:
        try:
            await ingestor.start()
        except Exception as exc:  # noqa: BLE001 - the source is selected either way
            log.warning("ingest_start_failed chat_id=%s error=%s", payload.chat_id, exc)

    source = result.get("source")
    return SourceSelectOut(
        ok=bool(result.get("ok", True)),
        source=SourceOut.model_validate(source) if source else None,
        message="This channel is now the active signal source.",
    )


# --------------------------------------------------------------------------------------
# GET /api/telegram/image/{image_id}
# --------------------------------------------------------------------------------------


def _media_type(row: MessageImage, path: Path) -> str:
    """Return the media type to serve: the stored mime when it is one, else the extension's."""
    stored = str(row.mime or "").strip().lower()
    if stored.startswith("image/"):
        return stored
    guessed, _encoding = mimetypes.guess_type(path.name)
    if guessed and guessed.startswith("image/"):
        return guessed
    return "application/octet-stream"


#: A plausible file extension: a dot and up to five alphanumerics, nothing else.
_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,5}$")


def _suffix(path: Path, media_type: str) -> str:
    """Return the download-name extension: the file's own when it is sane, else the media type's."""
    suffix = path.suffix
    if _SUFFIX_RE.match(suffix):
        return suffix.lower()
    guessed = mimetypes.guess_extension(media_type) or ""
    return guessed if _SUFFIX_RE.match(guessed) else ""


def _inside(candidate: Path, root: Path) -> bool:
    """Report whether a fully resolved path really lives under a fully resolved directory."""
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


@router.get("/image/{image_id}")
async def image(image_id: int, runtime: Runtime = Depends(get_runtime)) -> Any:
    """Stream one downloaded signal image by its MessageImage id.

    ``MessageImage.path`` is an absolute path written by the ingestor, but it is still data in a
    database and this endpoint still serves whatever it points at. So the path is resolved — with
    symlinks followed — and rejected unless it lands **inside** ``settings.image_dir``. A row
    doctored to read ``../../../etc/passwd``, or a symlink planted in the image directory, gets a
    404 and a logged warning rather than a file.
    """
    async with session_scope() as session:
        row = await session.get(MessageImage, int(image_id))
        if row is None:
            return _error(
                404,
                "not_found",
                f"No image {image_id} exists.",
                {"image_id": int(image_id)},
            )
        stored_path = str(row.path or "")
        media_type = _media_type(row, Path(stored_path))

    try:
        root = Path(runtime.settings.image_dir).resolve(strict=False)
        resolved = Path(stored_path).resolve(strict=False)
    except OSError as exc:
        log.warning("image_path_unresolvable image_id=%s error=%s", image_id, exc)
        return _error(
            404,
            "not_found",
            f"Image {image_id} is no longer available.",
            {"image_id": int(image_id)},
        )

    if not stored_path or not _inside(resolved, root):
        # Never echo the rejected path back to the client; it is logged and nothing else.
        log.warning(
            "image_path_outside_image_dir image_id=%s image_dir=%s", image_id, str(root)
        )
        return _error(
            404,
            "not_found",
            f"No image {image_id} exists.",
            {"image_id": int(image_id)},
        )

    if not resolved.is_file():
        log.warning("image_file_missing image_id=%s", image_id)
        return _error(
            404,
            "not_found",
            f"Image {image_id} is no longer available.",
            {"image_id": int(image_id)},
        )

    return FileResponse(
        path=str(resolved),
        media_type=media_type,
        # Served inline (this is a thumbnail in the feed), under a name derived from the row id
        # rather than the on-disk name, so "save image as" is sensible and nothing internal leaks.
        content_disposition_type="inline",
        filename=f"signal-image-{int(image_id)}{_suffix(resolved, media_type)}",
        # An image row never changes once written, so the browser may keep it for a day.
        headers={"Cache-Control": "private, max-age=86400"},
    )
