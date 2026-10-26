"""The notification bot answering to the operator and to nobody else.

``detect_chat`` used to bind whoever messaged the bot first. From then on every OPENED /
CLOSED / REJECTED notification — symbol, volume, entry, stop, realised P/L — went to that
chat, and nothing ever re-checked who it belonged to. No attacker is required for that to
go wrong: a bot username that is shared, guessed or simply typed by the wrong person during
the sixty-second detection window is enough.

``notifier.admin_ids`` closes it, and the *empty* case is as much a requirement as the
populated one:

* **empty** — nothing is enforced, exactly as this project shipped. An operator who never
  opened the field must not discover it by their bot going silent. What changes is that the
  binding is reported: who was bound, by name, id and @username.
* **populated** — detection ignores everyone else and keeps waiting; ``send`` refuses any
  chat that is not an admin id, and refuses it *quietly*, because the caller on that path is
  the executor and §7 says a notification never aborts or delays a trade.

The Bot API is a :class:`httpx.MockTransport` rather than a patched method, so these run
through the real ``_call``: the real URL construction, the real JSON handling, the real
``getUpdates`` offset bookkeeping. That last one matters — the non-destructive property of
``recent_senders`` is a claim about the exact query string it sends.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app import events
from app.config import settings as app_settings
from app.db import get_setting, init_db, session_scope, set_setting
from app.events import EventBus
from app.models import Notification
from app.runtime import Runtime
from app.telegram import notifier as notifier_module
from app.telegram.notifier import (
    ADMIN_GROUP_CHAT_MESSAGE,
    ALLOWED_BOT_COMMANDS,
    KEYBOARD_REMOVE,
    LEGACY_BOT_COMMANDS,
    NON_ADMIN_ONLY_MESSAGE,
    NO_MESSAGE_RECEIVED_MESSAGE,
    NOT_ADMIN_CHAT_MESSAGE,
    Notifier,
    bot_command_menu,
    command_sender_allowed,
    parse_bot_command,
)

BOT_TOKEN = "424242:notifier-admin-test-token"

#: The operator, and the stranger who messages the same bot.
ADMIN_ID = 777001
STRANGER_ID = 555002


# ---------------------------------------------------------------------------
# A Bot API that answers getMe, getUpdates and sendMessage
# ---------------------------------------------------------------------------


class FakeBotApi:
    """The Bot API methods this module calls, over an in-process transport."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        #: Updates that appear only once the Nth ``getUpdates`` has been served, so a test
        #: can make the admin write *after* the stranger already has.
        self.arrivals: dict[int, list[dict[str, Any]]] = {}
        self.sent: list[dict[str, Any]] = []
        #: Every ``offset`` seen, ``None`` when the call sent none. The whoami flow must
        #: never appear in here as anything but ``None``.
        self.offsets: list[int | None] = []
        self.polls = 0
        self.methods: list[str] = []
        self.command_menus: list[dict[str, Any]] = []
        self.deleted_commands: list[dict[str, Any]] = []

    def deliver_at(self, poll_index: int, update: dict[str, Any]) -> None:
        """Queue `update` to become visible just before poll number `poll_index` answers."""
        self.arrivals.setdefault(poll_index, []).append(update)

    def _json_body(self, request: httpx.Request) -> dict[str, Any]:
        if not request.content:
            return {}
        try:
            body = json.loads(request.content.decode("utf-8"))
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.methods.append(method)
        if method == "getMe":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {"id": 42, "username": "sp_test_bot", "first_name": "LV Copytrader"},
                },
            )
        if method == "getUpdates":
            for update in self.arrivals.pop(self.polls, []):
                self.updates.append(update)
            self.polls += 1
            raw_offset = request.url.params.get("offset")
            offset = None if raw_offset is None else int(raw_offset)
            self.offsets.append(offset)
            batch = [
                update
                for update in self.updates
                if offset is None or int(update["update_id"]) >= offset
            ]
            return httpx.Response(200, json={"ok": True, "result": batch})
        if method == "sendMessage":
            self.sent.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        if method == "deleteMyCommands":
            self.deleted_commands.append(self._json_body(request))
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "setMyCommands":
            body = self._json_body(request)
            self.command_menus.append(body)
            return httpx.Response(200, json={"ok": True, "result": True})
        return httpx.Response(
            404, json={"ok": False, "error_code": 404, "description": "unknown method"}
        )


def message_update(
    update_id: int,
    user_id: int,
    *,
    chat_id: int | None = None,
    username: str | None = None,
    first_name: str = "Someone",
    text: str = "/start",
) -> dict[str, Any]:
    """One private-message update, shaped the way the Bot API shapes them."""
    sender: dict[str, Any] = {"id": user_id, "is_bot": False, "first_name": first_name}
    if username is not None:
        sender["username"] = username
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": sender,
            "chat": {
                "id": user_id if chat_id is None else chat_id,
                "type": "private",
                "first_name": first_name,
                **({"username": username} if username else {}),
            },
            "date": 1_700_000_000,
            "text": text,
        },
    }


@pytest.fixture
def fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the detection *behaviour* and drop its wall-clock cost.

    What is under test is which sender is bound and which message comes back, never how
    many seconds were spent waiting, so the sixty-second window becomes a fraction of one.
    """
    monkeypatch.setattr(app_settings, "NOTIFY_DETECT_SECONDS", 1, raising=False)
    monkeypatch.setattr(app_settings, "NOTIFY_POLL_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(app_settings, "NOTIFY_RETRY_DELAYS", (0.0, 0.0), raising=False)
    monkeypatch.setattr(notifier_module, "WHOAMI_WINDOW_SECONDS", 0.3, raising=False)


async def build(
    data_dir: Path,
    *,
    admin_ids: list[int] | None = None,
    chat_id: int | None = None,
    enabled: bool = True,
    token: str = BOT_TOKEN,
) -> tuple[Notifier, FakeBotApi, EventBus]:
    """A real Notifier with the settings rows written and a fake Bot API behind it."""
    await init_db()
    async with session_scope() as session:
        if token:
            await set_setting(session, "notifier.bot_token", token, secret=True)
        await set_setting(session, "notifier.chat_id", chat_id)
        await set_setting(session, "notifier.enabled", enabled)
        await set_setting(session, "notifier.admin_ids", list(admin_ids or []))

    bus = EventBus()
    runtime = Runtime(settings=app_settings, rpc=None, resolver=None, bus=bus)
    notifier = Notifier(runtime)
    api = FakeBotApi()
    # Injected rather than opened: `_http()` reuses a live client, so every call in this
    # module goes through the real `_call` and never touches a socket.
    notifier._client = httpx.AsyncClient(
        transport=httpx.MockTransport(api.handler), base_url="https://api.telegram.org"
    )
    runtime.notifier = notifier
    return notifier, api, bus


def drain(bus: EventBus, queue: asyncio.Queue[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every envelope waiting on a subscriber queue, oldest first."""
    seen: list[dict[str, Any]] = []
    while True:
        try:
            seen.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return seen


async def stored(key: str) -> Any:
    """Read one settings row back the way the rest of the application would."""
    async with session_scope() as session:
        return await get_setting(session, key, None)


async def notifications() -> list[Notification]:
    """Every audit row `send` recorded, in insertion order."""
    async with session_scope() as session:
        rows = await session.execute(select(Notification).order_by(Notification.id))
        return list(rows.scalars().all())


def assert_no_button_keyboard(payload: dict[str, Any]) -> None:
    """A notifier message may only clear a leftover keyboard, never send one of its own."""
    markup = payload.get("reply_markup")
    assert markup == dict(KEYBOARD_REMOVE), payload
    assert "keyboard" not in markup
    assert "inline_keyboard" not in markup


def sender_of(update: dict[str, Any]):
    """Reduce a fixture update to the UpdateSender the notifier uses."""
    from app.telegram.notifier import _sender_from_update

    sender = _sender_from_update(update)
    assert sender is not None
    return sender


# ---------------------------------------------------------------------------
# detect_chat — the empty allowlist keeps working, and now says who it bound
# ---------------------------------------------------------------------------


async def test_an_empty_allowlist_binds_the_first_sender_and_reports_who_it_was(
    data_dir: Path, fast_polls: None
) -> None:
    """No allowlist is today's behaviour, and it must stay today's behaviour.

    The one thing that changes is that the answer names the account: with nothing enforcing
    who may bind, the operator reading that name is the only check there is.
    """
    notifier, api, _bus = await build(data_dir, admin_ids=[])
    api.updates.append(
        message_update(1, STRANGER_ID, username="first_arrival", first_name="Alex")
    )

    result = await notifier.detect_chat("")

    assert result["ok"] is True
    assert result["chat_id"] == STRANGER_ID
    assert result["user_id"] == STRANGER_ID
    assert result["username"] == "first_arrival"
    assert result["first_name"] == "Alex"
    assert result["enforced"] is False, "an empty allowlist enforces nothing"
    assert result["is_admin"] is False, "membership is not claimed where there is no list"

    # And the binding is persisted with its identity, so GET /api/settings can answer
    # "who is this bot talking to" without another round trip to Telegram.
    assert await stored("notifier.chat_id") == STRANGER_ID
    assert await stored("notifier.bound_to") == {
        "user_id": STRANGER_ID,
        "username": "first_arrival",
        "first_name": "Alex",
    }


# ---------------------------------------------------------------------------
# detect_chat — a populated allowlist waits for the right person
# ---------------------------------------------------------------------------


async def test_a_populated_allowlist_ignores_a_stranger_and_binds_the_admin(
    data_dir: Path, fast_polls: None
) -> None:
    """The stranger is already waiting in the queue; the admin writes several polls later.

    Binding the stranger and returning would be the old bug. Giving up because a stranger
    arrived first would be a new one — the poll has to keep waiting for someone allowed.
    """
    notifier, api, _bus = await build(data_dir, admin_ids=[ADMIN_ID])
    api.updates.append(message_update(1, STRANGER_ID, username="not_the_operator"))
    api.deliver_at(3, message_update(2, ADMIN_ID, username="operator", first_name="Op"))

    result = await notifier.detect_chat("")

    assert result["ok"] is True
    assert result["chat_id"] == ADMIN_ID
    assert result["user_id"] == ADMIN_ID
    assert result["username"] == "operator"
    assert result["is_admin"] is True
    assert result["enforced"] is True

    # The stranger's chat id must never have been written, not even transiently.
    assert await stored("notifier.chat_id") == ADMIN_ID
    assert (await stored("notifier.bound_to"))["user_id"] == ADMIN_ID


async def test_a_window_of_only_strangers_is_a_different_message_from_silence(
    data_dir: Path, fast_polls: None
) -> None:
    """"Nobody wrote" is fixed in Telegram; "they are not an admin" is fixed in this form.

    Answering the second with the first sends the operator back to press Start again — which
    they already did — while the field they actually have to edit sits untouched.
    """
    notifier, api, _bus = await build(data_dir, admin_ids=[ADMIN_ID])
    api.updates.append(
        message_update(1, STRANGER_ID, username="not_the_operator", first_name="Sam")
    )

    result = await notifier.detect_chat("")

    assert result["ok"] is False
    assert result["message"] == NON_ADMIN_ONLY_MESSAGE
    assert result["message"] != NO_MESSAGE_RECEIVED_MESSAGE
    # Naming the id is what makes the message actionable: it is the value to paste in.
    assert result["seen_user_id"] == STRANGER_ID
    assert str(STRANGER_ID) in result["hint"]
    assert "not_the_operator" in result["hint"]
    assert await stored("notifier.chat_id") is None, "a refused sender must not be bound"


async def test_an_admin_writing_from_a_group_is_not_bound_to_a_chat_send_would_refuse(
    data_dir: Path, fast_polls: None
) -> None:
    """Detection and delivery are held to one rule, or the bot looks configured and is mute.

    The allowlist holds user ids; a group chat id is negative and can never be one, so
    `send` would refuse every notification to it. Binding it anyway would hand the operator
    a green setup screen and total silence, which is the worst of the three outcomes.
    """
    notifier, api, _bus = await build(data_dir, admin_ids=[ADMIN_ID])
    api.updates.append(
        message_update(1, ADMIN_ID, chat_id=-1009876543210, username="operator", text="hi")
    )

    result = await notifier.detect_chat("")

    assert result["ok"] is False
    assert result["message"] == ADMIN_GROUP_CHAT_MESSAGE
    assert result["message"] != NON_ADMIN_ONLY_MESSAGE, "they ARE an admin; say the real reason"
    assert result["seen_chat_id"] == -1009876543210
    assert await stored("notifier.chat_id") is None


async def test_silence_still_reports_silence(data_dir: Path, fast_polls: None) -> None:
    """With an allowlist set and nothing arriving at all, the old message is still right."""
    notifier, _api, _bus = await build(data_dir, admin_ids=[ADMIN_ID])

    result = await notifier.detect_chat("")

    assert result["ok"] is False
    assert result["message"] == NO_MESSAGE_RECEIVED_MESSAGE
    assert "seen_user_id" not in result


# ---------------------------------------------------------------------------
# send — the delivery gate
# ---------------------------------------------------------------------------


async def test_send_refuses_a_chat_that_is_not_an_admin_and_never_raises(
    data_dir: Path, fast_polls: None
) -> None:
    """The leak, closed at the last possible moment before the token hits the wire.

    Three things are asserted together on purpose, because dropping any one of them turns a
    refusal into a different bug: nothing is sent, the operator is told, and the caller —
    which on this path is the executor mid-trade — gets a falsy return rather than an
    exception (§7).
    """
    notifier, api, bus = await build(data_dir, admin_ids=[ADMIN_ID], chat_id=STRANGER_ID)
    queue = bus.subscribe()

    delivered = await notifier.send("opened", "OPENED — BUY XAUUSDm")

    assert delivered is False
    assert not delivered, "the return must be falsy, not merely not-True"
    assert api.sent == [], "a refused notification must never reach Telegram"

    warnings = [
        envelope
        for envelope in drain(bus, queue)
        if envelope["type"] == events.SYSTEM_WARNING
        and envelope["data"].get("reason") == "notify_not_admin"
    ]
    assert len(warnings) == 1, "the operator has to be told the notification was dropped"
    assert warnings[0]["data"]["notification_type"] == "opened"

    # The audit row still exists: what was meant is recoverable even though it was refused.
    rows = await notifications()
    assert len(rows) == 1
    assert rows[0].ok is False
    assert rows[0].sent_at is None
    assert rows[0].error == NOT_ADMIN_CHAT_MESSAGE


async def test_a_refusal_cannot_propagate_into_the_caller(
    data_dir: Path, fast_polls: None
) -> None:
    """`fire` is what the executor actually calls, and it must survive a refusal silently."""
    notifier, api, _bus = await build(data_dir, admin_ids=[ADMIN_ID], chat_id=STRANGER_ID)

    notifier.fire("closed", "CLOSED — XAUUSDm")
    await asyncio.gather(*tuple(notifier._tasks), return_exceptions=False)

    assert api.sent == []


async def test_markdown_tables_are_sent_as_preformatted_html(
    data_dir: Path, fast_polls: None
) -> None:
    """AI notes with GFM tables have to stay readable: Telegram has no native tables."""
    notifier, api, _bus = await build(data_dir, admin_ids=[ADMIN_ID], chat_id=ADMIN_ID)
    body = (
        "# Setup\n\n"
        "| Symbol | Side | Entry |\n"
        "| --- | --- | --- |\n"
        "| XAUUSDr | BUY | 2380.50 |\n"
        "\nUse **0.10** lots."
    )

    assert await notifier.send("attention", body) is True
    assert len(api.sent) == 1
    text = api.sent[0]["text"]
    assert api.sent[0]["parse_mode"] == "HTML"
    assert "<pre>" in text
    assert "XAUUSDr" in text
    assert "<b>Setup</b>" in text
    assert "<b>0.10</b>" in text
    assert_no_button_keyboard(api.sent[0])


async def test_html_injection_in_ai_text_is_escaped(
    data_dir: Path, fast_polls: None
) -> None:
    notifier, api, _bus = await build(data_dir, admin_ids=[ADMIN_ID], chat_id=ADMIN_ID)

    assert await notifier.send("attention", "see <script>alert(1)</script> **now**") is True
    text = api.sent[0]["text"]
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "<b>now</b>" in text
    assert_no_button_keyboard(api.sent[0])


async def test_a_long_markdown_note_is_split_not_truncated(
    data_dir: Path, fast_polls: None
) -> None:
    notifier, api, _bus = await build(data_dir, admin_ids=[ADMIN_ID], chat_id=ADMIN_ID)
    body = "\n\n".join(f"## Block {i}\n" + ("alpha " * 120) for i in range(12))

    assert await notifier.send("attention", body) is True
    assert len(api.sent) >= 2
    joined = "\n".join(row["text"] for row in api.sent)
    assert "Block 0" in joined
    assert "Block 11" in joined
    for row in api.sent:
        assert len(row["text"]) <= 4096
        assert row["parse_mode"] == "HTML"
        assert_no_button_keyboard(row)


async def test_an_admin_chat_still_receives_normally(data_dir: Path, fast_polls: None) -> None:
    """The allowlist must not be a switch that quietly turns notifications off."""
    notifier, api, bus = await build(data_dir, admin_ids=[ADMIN_ID], chat_id=ADMIN_ID)
    queue = bus.subscribe()

    delivered = await notifier.send("opened", "OPENED — BUY XAUUSDm")

    assert delivered is True
    assert len(api.sent) == 1
    assert api.sent[0]["chat_id"] == ADMIN_ID
    assert "XAUUSDm" in api.sent[0]["text"]
    assert api.sent[0]["parse_mode"] == "HTML"
    assert_no_button_keyboard(api.sent[0])

    assert [
        envelope for envelope in drain(bus, queue) if envelope["type"] == events.SYSTEM_WARNING
    ] == []
    rows = await notifications()
    assert len(rows) == 1 and rows[0].ok is True


async def test_an_empty_allowlist_delivers_to_any_stored_chat(
    data_dir: Path, fast_polls: None
) -> None:
    """Regression guard for the requirement that nothing starts refusing on its own.

    A group chat id is negative and can never equal a Telegram user id, so an operator who
    points the bot at a group would be silenced the moment enforcement leaked into the empty
    case.
    """
    notifier, api, _bus = await build(data_dir, admin_ids=[], chat_id=-1001234567890)

    assert await notifier.send("closed", "CLOSED — XAUUSDm") is True
    assert api.sent[0]["chat_id"] == -1001234567890


async def test_a_group_chat_is_refused_once_an_allowlist_exists(
    data_dir: Path, fast_polls: None
) -> None:
    """A negative chat id is not a user id, so a populated allowlist cannot match it."""
    notifier, api, _bus = await build(data_dir, admin_ids=[ADMIN_ID], chat_id=-1001234567890)

    assert await notifier.send("closed", "CLOSED — XAUUSDm") is False
    assert api.sent == []


# ---------------------------------------------------------------------------
# recent_senders — how the operator finds the id to put on the list
# ---------------------------------------------------------------------------


async def test_recent_senders_lists_everyone_flagged_against_the_allowlist(
    data_dir: Path, fast_polls: None
) -> None:
    """One row per sender, each marked with whether it is already allowed."""
    notifier, api, _bus = await build(data_dir, admin_ids=[ADMIN_ID])
    api.updates.append(message_update(1, STRANGER_ID, username="curious", first_name="Sam"))
    api.updates.append(message_update(2, ADMIN_ID, username="operator", first_name="Op"))
    api.updates.append(message_update(3, ADMIN_ID, username="operator", text="hello again"))

    result = await notifier.recent_senders("")

    assert result["ok"] is True
    rows = result["updates"]
    assert [row["user_id"] for row in rows] == [STRANGER_ID, ADMIN_ID], "one row per sender"
    assert rows[0]["is_admin"] is False
    assert rows[1]["is_admin"] is True
    assert rows[1]["text"] == "hello again", "the latest message identifies the sender"
    assert rows[0]["chat_id"] == STRANGER_ID


async def test_recent_senders_does_not_consume_the_offsets_detection_needs(
    data_dir: Path, fast_polls: None
) -> None:
    """The whoami poll must confirm nothing, or it eats the update detect is waiting for.

    ``getUpdates`` treats an ``offset`` as an acknowledgement: everything below it is gone
    for good. If the lookup sent one, pressing "who has messaged me" and then "detect" would
    time out on a message the operator can see in their own Telegram history.
    """
    notifier, api, _bus = await build(data_dir, admin_ids=[])
    api.updates.append(message_update(1, ADMIN_ID, username="operator"))

    listed = await notifier.recent_senders("")
    assert listed["ok"] is True and len(listed["updates"]) == 1
    assert api.offsets == [None], "whoami must never send an offset"

    # The same update is still pending, so detection can still bind from it.
    detected = await notifier.detect_chat("")
    assert detected["ok"] is True
    assert detected["chat_id"] == ADMIN_ID


async def test_recent_senders_says_so_when_nobody_has_written(
    data_dir: Path, fast_polls: None
) -> None:
    """An empty list is an answer, not a failure: the call worked and nobody was there."""
    notifier, _api, _bus = await build(data_dir, admin_ids=[])

    result = await notifier.recent_senders("")

    assert result["ok"] is True
    assert result["updates"] == []
    assert result["message"], "an empty result still has to say what to do next"


# ---------------------------------------------------------------------------
# The REST surface: the operator cannot act on an allowlist they cannot edit
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(data_dir: Path):
    """Boot the real app through its real lifespan and drive it over ASGI, no socket."""
    from app.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            http.app = app
            yield http


def notifier_body(**overrides: Any) -> dict[str, Any]:
    """A complete `PUT /api/settings/notifier` body — the route is a whole-section write."""
    body: dict[str, Any] = {"chat_id": ADMIN_ID, "enabled": True, "admin_ids": [ADMIN_ID]}
    body.update(overrides)
    return body


async def test_admin_ids_round_trip_through_the_settings_tree(client: httpx.AsyncClient) -> None:
    """Stored de-duplicated with the order kept, and read back by GET /api/settings."""
    saved = await client.put(
        "/api/settings/notifier",
        json=notifier_body(admin_ids=[ADMIN_ID, STRANGER_ID, ADMIN_ID]),
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["notifier"]["admin_ids"] == [ADMIN_ID, STRANGER_ID]

    tree = await client.get("/api/settings")
    assert tree.status_code == 200, tree.text
    assert tree.json()["notifier"]["admin_ids"] == [ADMIN_ID, STRANGER_ID]


async def test_an_empty_allowlist_is_the_shipped_default(client: httpx.AsyncClient) -> None:
    """It has to ship off, and turning it back off has to be possible."""
    tree = await client.get("/api/settings")
    assert tree.json()["notifier"]["admin_ids"] == []
    assert tree.json()["notifier"]["bound_to"] is None

    await client.put("/api/settings/notifier", json=notifier_body())
    cleared = await client.put("/api/settings/notifier", json=notifier_body(admin_ids=[]))
    assert cleared.json()["notifier"]["admin_ids"] == []


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param([0], id="zero"),
        pytest.param([-1], id="negative"),
        pytest.param(["@operator"], id="username"),
        pytest.param([1.5], id="fraction"),
        pytest.param([None], id="null"),
        pytest.param("777001", id="not-a-list"),
    ],
)
async def test_a_bad_admin_id_is_refused_with_the_field_named(
    client: httpx.AsyncClient, bad: Any
) -> None:
    """Rejected, not silently dropped — and the sentence names the field it belongs under.

    Dropping the entry would leave the operator looking at a list missing the id they just
    typed, with nothing anywhere saying why; the settings page anchors an error under the
    field it names, so the name is what makes the message land in the right place.
    """
    refused = await client.put("/api/settings/notifier", json=notifier_body(admin_ids=bad))
    assert refused.status_code == 422, refused.text
    body = refused.json()["error"]
    assert body["code"] == "validation_error"
    assert body["message"].startswith("admin_ids"), body["message"]
    assert any("admin_ids" in (error.get("loc") or []) for error in body["detail"]["errors"])


async def test_changing_the_chat_id_by_hand_clears_the_recorded_identity(
    client: httpx.AsyncClient,
) -> None:
    """A name read from one chat must not be shown beside a different chat's id."""
    async with session_scope() as session:
        await set_setting(session, "notifier.chat_id", ADMIN_ID)
        await set_setting(
            session,
            "notifier.bound_to",
            {"user_id": ADMIN_ID, "username": "operator", "first_name": "Op"},
        )
    tree = await client.get("/api/settings")
    assert tree.json()["notifier"]["bound_to"]["username"] == "operator"

    retyped = await client.put("/api/settings/notifier", json=notifier_body(chat_id=STRANGER_ID))
    assert retyped.status_code == 200, retyped.text
    assert retyped.json()["notifier"]["bound_to"] is None
    assert retyped.json()["notifier"]["chat_id"] == STRANGER_ID

    # Saving the same chat id again leaves a later detection's identity alone.
    async with session_scope() as session:
        await set_setting(
            session, "notifier.bound_to", {"user_id": STRANGER_ID, "username": "sam"}
        )
    resaved = await client.put("/api/settings/notifier", json=notifier_body(chat_id=STRANGER_ID))
    assert resaved.json()["notifier"]["bound_to"]["username"] == "sam"


class StubNotifier:
    """A notifier that answers `recent_senders` from a canned payload."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def recent_senders(self, token: str) -> dict[str, Any]:
        return self.payload


async def test_whoami_returns_every_recent_sender_flagged(client: httpx.AsyncClient) -> None:
    """The route's own job: coerce the poll's rows into the documented shape."""
    client.app.state.runtime.notifier = StubNotifier(
        {
            "ok": True,
            "enforced": True,
            "message": "",
            "updates": [
                {
                    "user_id": ADMIN_ID,
                    "username": "operator",
                    "first_name": "Op",
                    "chat_id": ADMIN_ID,
                    "text": "/start",
                    "is_admin": True,
                },
                {
                    "user_id": STRANGER_ID,
                    "username": None,
                    "first_name": "Sam",
                    "chat_id": STRANGER_ID,
                    "text": "hello?",
                    "is_admin": False,
                },
                {"username": "no_id_at_all", "text": "unusable"},
            ],
        }
    )

    response = await client.post("/api/settings/notifier/whoami")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["enforced"] is True
    # The row with no numeric user id is dropped: it is not something anyone could allow.
    assert [row["user_id"] for row in body["updates"]] == [ADMIN_ID, STRANGER_ID]
    assert body["updates"][0]["is_admin"] is True
    assert body["updates"][1]["is_admin"] is False
    # Absent names arrive as empty strings, never null: the settings page trims them.
    assert body["updates"][1]["username"] == ""
    assert body["updates"][1]["text"] == "hello?"


async def test_whoami_reports_an_unconfigured_bot_as_a_result_not_an_error(
    client: httpx.AsyncClient,
) -> None:
    """No token is something the settings page renders next to the button, not a 500."""
    response = await client.post("/api/settings/notifier/whoami")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["updates"] == []
    assert body["message"], "a refusal has to say what is missing"


async def test_the_bound_account_is_reported_as_strings_never_nulls(
    client: httpx.AsyncClient,
) -> None:
    """The settings page trims these names, so a null in either would be a TypeError.

    `bound_to` is present only with a real numeric id, and its two name fields are always
    strings — empty when Telegram published none. A row that somehow lost its id reports as
    no binding at all rather than as a binding to nobody.
    """
    async with session_scope() as session:
        await set_setting(session, "notifier.bound_to", {"user_id": ADMIN_ID})
    bound = (await client.get("/api/settings")).json()["notifier"]["bound_to"]
    assert bound == {"user_id": ADMIN_ID, "username": "", "first_name": ""}

    async with session_scope() as session:
        await set_setting(session, "notifier.bound_to", {"username": "no_id_here"})
    assert (await client.get("/api/settings")).json()["notifier"]["bound_to"] is None


# ---------------------------------------------------------------------------
# Slash commands — the operator menu, not leftover reply-keyboard buttons
# ---------------------------------------------------------------------------


def test_bot_command_menu_is_signalpilot_not_the_legacy_advisor_set() -> None:
    """The registered set is start/status/help in both locales; leftovers stay gone."""
    for locale in ("en", "ru"):
        names = [item["command"] for item in bot_command_menu(locale)]
        assert names == ["start", "status", "help"]
        assert LEGACY_BOT_COMMANDS.isdisjoint(names)
        for item in bot_command_menu(locale):
            assert item["description"].strip()
            assert len(item["description"]) <= 256


def test_parse_bot_command_accepts_menu_and_at_mentions() -> None:
    """Telegram's command menu sends `/start`; a mention form is the same command."""
    assert parse_bot_command("/start") == "start"
    assert parse_bot_command("/start@lv_tradebot") == "start"
    assert parse_bot_command("/status extra") == "status"
    assert parse_bot_command("/HELP") == "help"
    assert parse_bot_command("/plan") == "plan"
    assert parse_bot_command("please /start") is None
    assert parse_bot_command("hello") is None


async def test_admin_slash_commands_are_answered_without_a_button_keyboard(
    data_dir: Path, fast_polls: None
) -> None:
    """The bound admin gets /start /help /status; none of those replies grow buttons."""
    notifier, api, _bus = await build(data_dir, admin_ids=[ADMIN_ID], chat_id=ADMIN_ID)
    runtime = notifier._runtime
    runtime.set_status("mt5", True, None)
    runtime.set_status("telegram", False, "the user account is not signed in")
    async with session_scope() as session:
        await set_setting(session, "trading.armed", True)

    for text, fragment in (
        ("/start", "Bound chat"),
        ("/start@sp_test_bot", "Bound chat"),
        ("/help", "/status"),
        ("/status", "ARM"),
    ):
        api.sent.clear()
        sender = sender_of(message_update(1, ADMIN_ID, text=text))
        assert command_sender_allowed(sender, await notifier._load_config()) is True
        assert await notifier.handle_sender_command(sender) is True
        assert len(api.sent) == 1
        assert api.sent[0]["chat_id"] == ADMIN_ID
        assert fragment in api.sent[0]["text"]
        assert_no_button_keyboard(api.sent[0])

    assert "the user account is not signed in" in api.sent[0]["text"]
    assert "disarmed" not in api.sent[0]["text"]
    assert "armed" in api.sent[0]["text"]
    assert "deleteWebhook" not in api.methods


async def test_stranger_slash_commands_are_ignored(
    data_dir: Path, fast_polls: None
) -> None:
    """A guessed bot username must not be enough to read ARM/MT5 state."""
    notifier, api, _bus = await build(data_dir, admin_ids=[ADMIN_ID], chat_id=ADMIN_ID)
    sender = sender_of(message_update(1, STRANGER_ID, text="/status"))
    config = await notifier._load_config()
    assert command_sender_allowed(sender, config) is False
    assert await notifier.handle_sender_command(sender) is False
    assert api.sent == []


async def test_empty_allowlist_answers_only_the_bound_chat(
    data_dir: Path, fast_polls: None
) -> None:
    """Unenforced still means 'this chat', not 'whoever DMs the bot'."""
    notifier, api, _bus = await build(data_dir, admin_ids=[], chat_id=ADMIN_ID)
    config = await notifier._load_config()
    admin = sender_of(message_update(1, ADMIN_ID, text="/help"))
    stranger = sender_of(message_update(2, STRANGER_ID, text="/help"))
    assert command_sender_allowed(admin, config) is True
    assert command_sender_allowed(stranger, config) is False
    assert await notifier.handle_sender_command(admin) is True
    assert await notifier.handle_sender_command(stranger) is False
    assert [row["chat_id"] for row in api.sent] == [ADMIN_ID]
    assert_no_button_keyboard(api.sent[0])


async def test_unbound_empty_allowlist_does_not_answer_whoever_writes_first(
    data_dir: Path, fast_polls: None
) -> None:
    """Setup is detect_chat; inbound commands stay quiet until a chat is bound."""
    notifier, api, _bus = await build(data_dir, admin_ids=[], chat_id=None)
    sender = sender_of(message_update(1, ADMIN_ID, text="/start"))
    config = await notifier._load_config()
    assert command_sender_allowed(sender, config) is False
    assert await notifier.handle_sender_command(sender) is False
    assert api.sent == []


async def test_legacy_advisor_commands_are_not_implemented(
    data_dir: Path, fast_polls: None
) -> None:
    """`/plan` and friends must not grow a handler; the admin is pointed at /help."""
    notifier, api, _bus = await build(data_dir, admin_ids=[ADMIN_ID], chat_id=ADMIN_ID)
    sender = sender_of(message_update(1, ADMIN_ID, text="/plan"))
    assert await notifier.handle_sender_command(sender) is True
    assert len(api.sent) == 1
    assert "Unknown command" in api.sent[0]["text"]
    assert "/help" in api.sent[0]["text"]
    assert_no_button_keyboard(api.sent[0])


async def test_command_poller_answers_admin_and_advances_the_shared_offset(
    data_dir: Path, fast_polls: None
) -> None:
    """The inbound poller is the path Telegram's command menu actually hits."""
    notifier, api, _bus = await build(data_dir, admin_ids=[ADMIN_ID], chat_id=ADMIN_ID)
    api.updates.append(message_update(4, ADMIN_ID, text="/help"))
    api.updates.append(message_update(5, STRANGER_ID, text="/status"))

    await notifier._poll_commands_once()

    assert notifier._updates_offset == 6
    assert len(api.sent) == 1
    assert api.sent[0]["chat_id"] == ADMIN_ID
    assert "/start" in api.sent[0]["text"]
    assert_no_button_keyboard(api.sent[0])


async def test_command_poller_waits_while_detect_holds_the_updates_lock(
    data_dir: Path, fast_polls: None
) -> None:
    """Detect owns getUpdates for its window; the poller must not sneak a consuming poll in."""
    notifier, api, _bus = await build(data_dir, admin_ids=[ADMIN_ID])
    await notifier._updates_lock.acquire()
    task = asyncio.create_task(notifier._poll_commands_once())
    await asyncio.sleep(0.05)
    assert not task.done()
    assert api.polls == 0
    notifier._updates_lock.release()
    await asyncio.wait_for(task, 1.0)
    assert api.polls >= 1


async def test_detect_still_binds_start_and_answers_it_once(
    data_dir: Path, fast_polls: None
) -> None:
    """Pressing Start during detect both binds the chat and answers /start.

    Detect consumes the update (shared offset), so the command poller would never
    see it; answering here is how `/start` still works on the binding tap.
    """
    notifier, api, _bus = await build(data_dir, admin_ids=[ADMIN_ID])
    api.updates.append(message_update(1, ADMIN_ID, username="operator", first_name="Op"))

    result = await notifier.detect_chat("")

    assert result["ok"] is True
    assert result["chat_id"] == ADMIN_ID
    assert notifier._updates_offset == 2
    assert len(api.sent) == 1
    assert "Bound chat" in api.sent[0]["text"]
    assert_no_button_keyboard(api.sent[0])


async def test_start_registers_slash_commands_and_not_legacy_ones(
    data_dir: Path, fast_polls: None
) -> None:
    """Boot must own BotFather's menu: delete leftovers, then write start/status/help."""
    notifier, api, _bus = await build(data_dir, admin_ids=[ADMIN_ID], chat_id=ADMIN_ID)
    try:
        await notifier.start()
        pending = [task for task in tuple(notifier._tasks) if not task.done()]
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending), 1.0)
        assert api.deleted_commands, "deleteMyCommands has to run so leftover names die"
        assert api.command_menus, "setMyCommands has to run on start"
        names: set[str] = set()
        for menu in api.command_menus:
            for item in menu.get("commands") or []:
                names.add(item["command"])
        assert names == {"start", "status", "help"}
        assert LEGACY_BOT_COMMANDS.isdisjoint(names)
        assert "deleteWebhook" not in api.methods
    finally:
        await notifier.stop()


async def test_token_rotation_re_registers_commands(
    data_dir: Path, fast_polls: None
) -> None:
    """Saving a new token has to refresh the menu; the old product's names must not return."""
    notifier, api, _bus = await build(data_dir, admin_ids=[ADMIN_ID], chat_id=ADMIN_ID)
    notifier._started = True
    await notifier._load_config()
    pending = [task for task in tuple(notifier._tasks) if not task.done()]
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending), 1.0)
    api.command_menus.clear()
    api.deleted_commands.clear()
    notifier._updates_offset = 9

    async with session_scope() as session:
        await set_setting(session, "notifier.bot_token", "999999:rotated-token", secret=True)
    await notifier._load_config()
    assert notifier._updates_offset == 0
    pending = [task for task in tuple(notifier._tasks) if not task.done()]
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending), 1.0)
    assert api.command_menus
    names = {
        item["command"]
        for menu in api.command_menus
        for item in menu.get("commands") or []
    }
    assert names == {"start", "status", "help"}
    assert LEGACY_BOT_COMMANDS.isdisjoint(names)


async def test_help_follows_the_notifier_locale(
    data_dir: Path, fast_polls: None
) -> None:
    """Russian UI locale produces Russian command copy, still without buttons."""
    notifier, api, _bus = await build(data_dir, admin_ids=[ADMIN_ID], chat_id=ADMIN_ID)
    async with session_scope() as session:
        await set_setting(session, "ui.locale", "ru")
    sender = sender_of(message_update(1, ADMIN_ID, text="/help"))
    assert await notifier.handle_sender_command(sender) is True
    assert "Команды" in api.sent[0]["text"]
    assert_no_button_keyboard(api.sent[0])

