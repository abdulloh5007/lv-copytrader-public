"""The ingest path driven end to end: revisions, skips, the replay guard, albums, serialisation.

These tests run the real `app.telegram.ingest.Ingestor` against a real (throwaway) SQLite file.
Nothing about ingest is mocked — only the two things outside it are stood in for: Telethon's
media download (`_StubTelegram`, which writes a real 2x2 PNG so the stored dimensions and byte
counts are genuine) and, in the serialisation test, the agent entrypoint the worker calls.

Message stand-ins follow `tests/test_classify.py`: the *media* objects are real
`telethon.tl.types` instances because `classify()` branches on `isinstance` of the media, while
the message itself is a plain object carrying exactly the attributes ingest reads. `TgMessage`
adds the envelope fields (`id`, `chat_id`, `date`, `edit_date`, `grouped_id`) that the
classifier never looks at but the ingestor does.

What is defended here, by acceptance criterion:

  crit 8   a voice note / video note / sticker is recorded, badged, and NEVER enqueued, while a
           chart "sent as file" is enqueued -- the mechanical form of "zero provider.request"
  crit 9   an edit is a REVISION of the same post, sharing its `signal_ref`, and an edit of a
           skipped row is re-classified from scratch and starts its own chain at 1 (§3.3)
  crit 10  two messages 200 ms apart may run together; a fourth waits behind three workers
  §3.4     every image of an album is stored; the MAX_IMAGES_PER_RUN cap is not applied here
  §3.5     a replayed message inserts exactly one row, guarded by the unique index and an
           IntegrityError rollback -- and `edit_date` is 0, never NULL
"""

from __future__ import annotations

import asyncio
import io
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import select

from app.config import settings as app_settings
from app.db import init_db, session_scope, set_setting
from app.operator_settings import KEY_TAG_FILTER_ENABLED, KEY_TAG_FILTER_TAG, SKIP_REASON_TAG
from app.events import EventBus
from app.models import Message, MessageImage, Source
from app.runtime import Runtime
from app.telegram.ingest import Ingestor

from tests.test_classify import FakeDocument, FakeMessage, document_media, photo_media

#: The active source's chat id, in Telegram's `-100`-marked channel form.
CHAT_ID = -1001234567890


def _png_bytes() -> bytes:
    """A real 2x2 PNG, so `image_dimensions` and `getsize` read genuine values."""
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), (255, 255, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


PNG = _png_bytes()


# ---------------------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------------------


class TgMessage(FakeMessage):
    """`FakeMessage` plus the envelope fields the ingestor reads off a Telethon message."""

    def __init__(
        self,
        *,
        id: int,
        chat_id: int = CHAT_ID,
        date: datetime | None = None,
        edit_date: datetime | None = None,
        grouped_id: int | None = None,
        reply_to_msg_id: int | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self.id = int(id)
        self.chat_id = chat_id
        self.date = date if date is not None else datetime.now(UTC)
        self.edit_date = edit_date
        self.grouped_id = grouped_id
        self.reply_to_msg_id = reply_to_msg_id


class _StubTelegram:
    """Stands in for `TelegramService`; ingest only ever asks it for a downloaded file."""

    def __init__(self) -> None:
        self.downloads: list[int] = []
        self._n = 0

    async def download_image(self, message: Any, dest_dir: str) -> str | None:
        """Write a real PNG under `dest_dir` and report which message it came from."""
        self._n += 1
        self.downloads.append(int(getattr(message, "id", 0) or 0))
        path = Path(dest_dir) / f"stub-{self._n:03d}.png"
        path.write_bytes(PNG)
        return str(path)


def text_message(message_id: int, text: str, **kw: Any) -> TgMessage:
    """A plain text post -- the ordinary signal."""
    return TgMessage(id=message_id, message=text, **kw)


def photo_message(message_id: int, caption: str | None = None, **kw: Any) -> TgMessage:
    """A chart posted as a Telegram photo. `photo` is set truthy on purpose (§3.2)."""
    return TgMessage(id=message_id, media=photo_media(), message=caption, photo=object(), **kw)


def voice_message(message_id: int) -> TgMessage:
    """A voice note: an audio document flagged `voice`."""
    return TgMessage(
        id=message_id, media=document_media(), document=FakeDocument("audio/ogg"), voice=True
    )


def video_note_message(message_id: int) -> TgMessage:
    """A round video note. `video` is truthy for it too, which is the §3.2 trap."""
    return TgMessage(
        id=message_id,
        media=document_media(),
        document=FakeDocument("video/mp4"),
        video_note=True,
        video=True,
    )


def sticker_message(message_id: int) -> TgMessage:
    """A webp sticker -- an image mime type that is emphatically not a chart."""
    return TgMessage(
        id=message_id,
        media=document_media(),
        document=FakeDocument("image/webp"),
        sticker=object(),
    )


def uncompressed_chart(message_id: int, caption: str | None = None) -> TgMessage:
    """A chart "sent as file" to preserve quality: a document with an image mime type."""
    return TgMessage(
        id=message_id,
        media=document_media(),
        document=FakeDocument("image/png"),
        message=caption,
    )


class _SerialRecorder:
    """Stands in for `app.ai.loop.run_agent`, recording when each run held the single worker."""

    def __init__(self, duration: float, release: asyncio.Event | None = None) -> None:
        self.release = release
        self.started = asyncio.Event()
        self.concurrent = asyncio.Event()
        self.three_started = asyncio.Event()
        self.duration = duration
        self.live = 0
        self.max_live = 0
        self.intervals: list[tuple[int, float, float]] = []

    async def __call__(
        self,
        runtime: Any,
        *,
        message_row_id: int | None,
        kind: str = "signal",
        instruction: str | None = None,
    ) -> dict[str, Any]:
        """Occupy the worker for `duration` seconds and record the (enter, exit) interval."""
        self.live += 1
        self.max_live = max(self.max_live, self.live)
        enter = time.monotonic()
        self.started.set()
        if self.live >= 2:
            self.concurrent.set()
        if self.live >= 3:
            self.three_started.set()
        if self.release is not None:
            await self.release.wait()
        await asyncio.sleep(self.duration)
        exit_at = time.monotonic()
        self.live -= 1
        self.intervals.append((int(message_row_id or 0), enter, exit_at))
        return {
            "run_id": "0" * 32,
            "status": "finished",
            "decision": "acted",
            "summary": "recorded",
            "error": None,
        }


# ---------------------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------------------


async def _build(data_dir: Path) -> tuple[Ingestor, Runtime, int]:
    """Assemble a real Ingestor over an isolated database with one active source."""
    await init_db()
    runtime = Runtime(settings=app_settings, rpc=None, resolver=None, bus=EventBus())
    runtime.telegram = _StubTelegram()  # type: ignore[assignment]
    ingestor = Ingestor(runtime)
    runtime.ingestor = ingestor
    async with session_scope() as session:
        source = Source(
            kind="telegram",
            chat_id=CHAT_ID,
            title="Gold Signals",
            username="goldsignals",
            entity_type="channel",
            is_active=True,
            last_message_id=0,
        )
        session.add(source)
        await session.flush()
        source_id = int(source.id)
    return ingestor, runtime, source_id


async def _messages() -> list[Message]:
    """Every ingested message row, oldest first."""
    async with session_scope() as session:
        rows = await session.execute(select(Message).order_by(Message.id))
        return list(rows.scalars().all())


async def _images() -> list[MessageImage]:
    """Every stored image row, in album order."""
    async with session_scope() as session:
        rows = await session.execute(
            select(MessageImage).order_by(MessageImage.message_row_id, MessageImage.order_index)
        )
        return list(rows.scalars().all())


def _received(runtime: Runtime) -> list[dict[str, Any]]:
    """The `signal.received` envelopes emitted so far, in order."""
    return [e["data"] for e in runtime.bus.recent() if e["type"] == "signal.received"]


# ---------------------------------------------------------------------------------------
# Criterion 9 -- an edit is a revision, not a new signal
# ---------------------------------------------------------------------------------------


def _dt(stamp: int):
    """A timezone-aware datetime for an edit_date, the shape Telethon hands over."""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(stamp, tz=timezone.utc)


async def test_an_edit_stores_a_second_revision_of_the_same_post(data_dir: Path) -> None:
    """Criterion 9 / §3.3: an edit is a revision row, not a duplicate and not an overwrite.

    Overwriting would destroy the previous revision the agent needs in context; treating the
    edit as a fresh post is how one channel call becomes two positions.
    """
    ingestor, runtime, source_id = await _build(data_dir)
    posted = datetime.now(UTC) - timedelta(seconds=30)

    original = text_message(4242, "buy gold now, sl 2340", date=posted)
    first_id = await ingestor.handle_message(original)
    assert first_id is not None

    edited = text_message(
        4242, "buy gold now, sl 2335 (corrected)", date=posted, edit_date=datetime.now(UTC)
    )
    second_id = await ingestor.handle_message(edited, edited=True)
    assert second_id is not None
    assert second_id != first_id, "the edit is a NEW row, not the old one rewritten"

    rows = await _messages()
    assert len(rows) == 2, "one edit of one post leaves exactly two revision rows"
    assert [r.revision for r in rows] == [1, 2]
    assert {(r.source_id, r.message_id) for r in rows} == {(source_id, 4242)}
    assert rows[0].text == "buy gold now, sl 2340", "revision 1 is preserved for the run context"
    assert rows[1].text.endswith("(corrected)")
    assert rows[0].edit_date == 0
    assert rows[1].edit_date > 0, "the edit's own edit_date is what the unique index keys on"
    # The edit gets its own agent run; both rows are queued.
    assert ingestor._queue.qsize() == 2


async def test_both_revisions_share_one_signal_ref(data_dir: Path) -> None:
    """Criterion 9 + §5.5: the duplicate guard keys on `signal_ref`, so an edit must not change it.

    `SIG:<source>:<message_id>` is revision-independent by construction. If the edit produced a
    different ref, the pre-write guard would not recognise the position already opened from
    revision 1 and would open a second one for the same call.
    """
    ingestor, runtime, source_id = await _build(data_dir)
    posted = datetime.now(UTC)

    await ingestor.handle_message(text_message(77, "gold long 2350", date=posted))
    await ingestor.handle_message(
        text_message(77, "gold long 2352", date=posted, edit_date=datetime.now(UTC)),
        edited=True,
    )

    rows = await _messages()
    assert len(rows) == 2
    expected = f"SIG:{source_id}:77"
    assert rows[0].signal_ref == expected
    assert rows[1].signal_ref == expected, "an edit must not mint a new signal_ref"
    assert len({r.signal_ref for r in rows}) == 1

    # The same identity travels on the stream, which is where the cockpit reads it from.
    assert [e["signal_ref"] for e in _received(runtime)] == [expected, expected]


async def test_an_edit_of_a_skipped_row_is_reclassified_and_starts_its_own_chain(
    data_dir: Path,
) -> None:
    """§3.3: a voice note replaced by text becomes a real signal, at revision 1.

    Continuing the skipped row's chain would mark the first real signal from this post as
    revision 2 and send the agent looking for a previous revision that was never processed.
    """
    ingestor, runtime, source_id = await _build(data_dir)

    voice_row = await ingestor.handle_message(voice_message(909))
    assert voice_row is not None
    rows = await _messages()
    assert rows[0].kind == "voice"
    assert rows[0].status == "skipped"
    assert ingestor._queue.qsize() == 0, "the voice note itself cost nothing"

    replaced = text_message(909, "scrap the voice note -- buy gold 2350 sl 2340",
                            edit_date=datetime.now(UTC))
    edited_row = await ingestor.handle_message(replaced, edited=True)
    assert edited_row is not None

    rows = await _messages()
    assert len(rows) == 2
    assert rows[1].kind == "text", "the edit is classified from scratch, not inherited"
    assert rows[1].status == "pending"
    assert rows[1].skip_reason is None
    assert rows[1].revision == 1, "an edit of a skipped row starts its OWN revision chain at 1"
    assert ingestor._queue.qsize() == 1, "and only the re-classified revision is queued"


# ---------------------------------------------------------------------------------------
# Criterion 8 -- skipped messages cost nothing
# ---------------------------------------------------------------------------------------


async def test_voice_video_note_and_sticker_are_recorded_but_never_enqueued(
    data_dir: Path,
) -> None:
    """Criterion 8: three skipped rows and nothing on the queue -- the mechanical form of
    "zero provider.request events".

    A provider call can only originate from the worker, and the worker only ever sees what was
    enqueued. An empty queue after all three is therefore the guarantee, not a claim about logs.
    """
    ingestor, runtime, source_id = await _build(data_dir)

    assert await ingestor.handle_message(voice_message(1)) is not None
    assert await ingestor.handle_message(video_note_message(2)) is not None
    assert await ingestor.handle_message(sticker_message(3)) is not None

    rows = await _messages()
    assert [r.kind for r in rows] == ["voice", "video_note", "sticker"]
    assert [r.status for r in rows] == ["skipped"] * 3, "recorded, so the operator sees nothing was lost"
    assert all(r.skip_reason for r in rows), "each skipped row carries its badge phrase"
    assert all(r.image_count == 0 for r in rows)

    assert ingestor._queue.qsize() == 0, "not one of them reaches the agent worker"
    assert runtime.telegram.downloads == [], "and no media is downloaded for a skipped kind"

    emitted = _received(runtime)
    assert len(emitted) == 3, "they still show in the feed"
    assert {e["status"] for e in emitted} == {"skipped"}


async def test_a_chart_sent_as_an_uncompressed_document_is_enqueued(data_dir: Path) -> None:
    """Criterion 8, the other half: "send as file" preserves chart quality and is a real signal.

    This is the message that a naive `if m.photo` handler drops on the floor -- the channel is
    trying to give the model a *better* chart and the system would ignore it.
    """
    ingestor, runtime, source_id = await _build(data_dir)

    row_id = await ingestor.handle_message(uncompressed_chart(42, caption="XAUUSD, entry on retest"))
    assert row_id is not None

    rows = await _messages()
    assert len(rows) == 1
    assert rows[0].kind == "photo_caption"
    assert rows[0].status == "pending", "it goes to the model like any other chart"
    assert rows[0].skip_reason is None
    assert rows[0].image_count == 1, "the uncompressed file is downloaded and stored"
    assert ingestor._queue.qsize() == 1

    images = await _images()
    assert len(images) == 1
    assert images[0].mime == "image/png"
    assert (images[0].width, images[0].height) == (2, 2)
    assert images[0].bytes > 0


# ---------------------------------------------------------------------------------------
# Worker pool -- up to three agent runs, a fourth waits
# ---------------------------------------------------------------------------------------

#: Longer than the 200 ms gap on purpose: the second message must arrive while the first run is
#: still in flight, so overlap is observable rather than accidental.
RUN_SECONDS = 0.4
GAP_SECONDS = 0.2


async def test_two_messages_200ms_apart_run_on_two_workers(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second post must not wait out the first run. Broker writes stay serial elsewhere.

    Hold the first worker until the second enters. The 200 ms gap remains, but
    SQLite or CPU scheduling under load cannot accidentally erase the overlap.
    A single-worker regression times out waiting for the second entry.
    """
    ingestor, runtime, source_id = await _build(data_dir)
    release = asyncio.Event()
    recorder = _SerialRecorder(RUN_SECONDS, release)
    # ingest.py imports run_agent lazily from app.ai.loop inside _process, so the module
    # attribute is what the worker resolves at call time -- that is the thing to patch.
    monkeypatch.setattr("app.ai.loop.run_agent", recorder)

    await ingestor.start()
    try:
        first = await ingestor.handle_message(text_message(101, "buy gold now"))
        await asyncio.wait_for(recorder.started.wait(), timeout=10)
        await asyncio.sleep(GAP_SECONDS)
        assert recorder.live == 1, (
            "precondition: the first run is still in flight when the second message arrives"
        )
        second = await ingestor.handle_message(text_message(102, "and add a second entry"))
        assert first is not None and second is not None
        await asyncio.wait_for(recorder.concurrent.wait(), timeout=10)
        release.set()
        deadline = time.monotonic() + 10.0
        while len(recorder.intervals) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        await asyncio.wait_for(ingestor._queue.join(), timeout=10)
    finally:
        release.set()
        await ingestor.stop()

    assert recorder.max_live == 2, "two free workers should both be in a run"
    assert len(recorder.intervals) == 2

    ordered = sorted(recorder.intervals, key=lambda item: item[1])
    assert [item[0] for item in ordered] == [first, second], "both rows were processed"
    assert ordered[1][1] < ordered[0][2], (
        f"the second run started at {ordered[1][1]:.4f} only after the first ended at "
        f"{ordered[0][2]:.4f} -- the pool did not overlap"
    )

    rows = await _messages()
    assert [r.status for r in rows] == ["processed", "processed"]


async def test_a_fourth_message_waits_for_one_of_three_workers(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pool is three, not unbounded. The fourth post starts only after one run ends."""
    ingestor, runtime, source_id = await _build(data_dir)
    release = asyncio.Event()
    recorder = _SerialRecorder(RUN_SECONDS, release)
    monkeypatch.setattr("app.ai.loop.run_agent", recorder)

    await ingestor.start()
    try:
        ids = []
        for offset, text in enumerate(
            ("one", "two", "three", "four"), start=201
        ):
            row_id = await ingestor.handle_message(text_message(offset, text))
            assert row_id is not None
            ids.append(row_id)
        await asyncio.wait_for(recorder.three_started.wait(), timeout=10)
        assert recorder.live == 3
        assert ingestor._queue.qsize() == 1, "the fourth message must wait for a free worker"
        release.set()
        await asyncio.wait_for(ingestor._queue.join(), timeout=10)
    finally:
        release.set()
        await ingestor.stop()

    assert recorder.max_live == 3, f"peak concurrency was {recorder.max_live}, want 3"
    assert len(recorder.intervals) == 4
    assert {item[0] for item in recorder.intervals} == set(ids)


# ---------------------------------------------------------------------------------------
# §3.5 -- the dedup / replay guard
# ---------------------------------------------------------------------------------------


async def test_the_same_message_ingested_twice_inserts_exactly_one_row(data_dir: Path) -> None:
    """§3.5: a Telethon reconnect replays messages; a second row would open the same trade twice.

    The second call must be refused by the unique index, and `handle_message` reports that with
    None rather than a second row id.
    """
    ingestor, runtime, source_id = await _build(data_dir)
    posted = datetime.now(UTC)

    first = await ingestor.handle_message(text_message(555, "sell gold 2360 sl 2372", date=posted))
    replay = await ingestor.handle_message(text_message(555, "sell gold 2360 sl 2372", date=posted))

    assert first is not None
    assert replay is None, "a replayed message is deduped, not re-ingested"

    rows = await _messages()
    assert len(rows) == 1, "exactly one row survives the replay"
    assert rows[0].id == first
    assert ingestor._queue.qsize() == 1, "and the replay queues no second agent run"


async def test_a_concurrent_replay_is_closed_by_the_index_not_a_select(data_dir: Path) -> None:
    """§3.5: dedup is the insert itself, never "select then insert".

    A live update and a reconnect replay can be in flight at the same instant; a select-then-
    insert check passes in both coroutines and both then insert. Driving the two ingests
    concurrently exercises the IntegrityError rollback path that actually closes the race.
    """
    ingestor, runtime, source_id = await _build(data_dir)
    posted = datetime.now(UTC)

    results = await asyncio.gather(
        ingestor.handle_message(text_message(888, "buy gold 2350", date=posted)),
        ingestor.handle_message(text_message(888, "buy gold 2350", date=posted)),
    )

    assert sorted(r is None for r in results) == [False, True], "exactly one insert wins"
    rows = await _messages()
    assert len(rows) == 1
    assert rows[0].message_id == 888


async def test_edit_date_is_zero_and_not_null_on_an_unedited_row(data_dir: Path) -> None:
    """§3.5: SQLite treats every NULL as distinct, so a NULL edit_date silently voids the guard.

    `(source_id, message_id, NULL)` never collides with itself, so a replay would insert again
    and no test of the happy path would ever notice. The column is NOT NULL and the stored value
    for an un-edited post is the integer 0.
    """
    ingestor, runtime, source_id = await _build(data_dir)

    await ingestor.handle_message(text_message(1234, "gold long"))

    rows = await _messages()
    assert len(rows) == 1
    assert rows[0].edit_date is not None, "a NULL here would make every replay look like a new post"
    assert rows[0].edit_date == 0
    assert isinstance(rows[0].edit_date, int)
    assert Message.__table__.c.edit_date.nullable is False, "the column itself forbids NULL"
    assert {c.name for c in Message.__table__.c} >= {"source_id", "message_id", "edit_date"}
    constraint = next(
        c for c in Message.__table__.constraints if getattr(c, "name", "") == "uq_message_dedup"
    )
    assert [c.name for c in constraint.columns] == ["source_id", "message_id", "edit_date"]


# ---------------------------------------------------------------------------------------
# §3.4 -- albums
# ---------------------------------------------------------------------------------------


async def test_an_album_of_three_images_is_one_message_and_three_image_rows(
    data_dir: Path,
) -> None:
    """§3.4: every image of an album is stored, and the album is ONE signal, not three.

    Three message rows would run the agent three times on one call; storing one image would
    hand the model a single frame of a multi-chart setup.
    """
    ingestor, runtime, source_id = await _build(data_dir)
    album = [photo_message(700 + i, grouped_id=99887766) for i in range(3)]
    # A Telegram album carries its caption on exactly one member, which is not always the anchor.
    album[1].message = "GOLD -- 3 timeframes, long above 2350"
    album[1].raw_text = album[1].message

    row_id = await ingestor.handle_message(album[0], album=album)
    assert row_id is not None

    rows = await _messages()
    assert len(rows) == 1, "an album is one Message row"
    assert rows[0].kind == "photo_caption", "the caption counts wherever in the album it sits"
    assert rows[0].text == "GOLD -- 3 timeframes, long above 2350"
    assert rows[0].grouped_id == 99887766
    assert rows[0].status == "pending"
    assert rows[0].image_count == 3

    images = await _images()
    assert len(images) == 3, "THREE MessageImage rows -- nothing is dropped at ingest"
    assert [i.order_index for i in images] == [0, 1, 2]
    assert len({i.path for i in images}) == 3, "each album item is its own file"
    assert all(i.width == 2 and i.height == 2 and i.bytes > 0 for i in images)
    assert runtime.telegram.downloads == [700, 701, 702]

    assert ingestor._queue.qsize() == 1, "one album, one agent run"
    emitted = _received(runtime)
    assert len(emitted) == 1
    assert emitted[0]["image_count"] == 3
    assert len(emitted[0]["images"]) == 3


async def test_ingest_does_not_apply_the_context_time_image_cap(data_dir: Path) -> None:
    """§3.4: MAX_IMAGES_PER_RUN belongs at context-build time, never at ingest.

    Capping here would throw away images that cannot be fetched again -- the message is gone
    from the wire and only the run context is allowed to decide it can afford four of them.
    """
    ingestor, runtime, source_id = await _build(data_dir)
    cap = int(app_settings.MAX_IMAGES_PER_RUN)
    total = cap + 3
    album = [photo_message(300 + i, grouped_id=5150) for i in range(total)]

    row_id = await ingestor.handle_message(album[0], album=album)
    assert row_id is not None

    images = await _images()
    assert len(images) == total, (
        f"ingest stored {len(images)} of {total} images; the cap of {cap} was applied too early"
    )
    rows = await _messages()
    assert rows[0].image_count == total


async def test_a_grouped_live_delivery_defers_to_the_album_event(data_dir: Path) -> None:
    """§3.4: Telethon delivers an album twice -- as NewMessage updates AND as one Album event.

    Handling both writes one row per album item plus one for the album. The grouped NewMessage
    delivery therefore stands down and records nothing.
    """
    ingestor, runtime, source_id = await _build(data_dir)

    deferred = await ingestor.handle_message(photo_message(801, grouped_id=424242))
    assert deferred is None, "the grouped NewMessage delivery defers to the Album event"
    assert await _messages() == []
    assert ingestor._queue.qsize() == 0
    assert runtime.telegram.downloads == []

    # A one-item "album" never fires the Album event, so an ungrouped photo is ingested normally.
    solo = await ingestor.handle_message(photo_message(802))
    assert solo is not None
    rows = await _messages()
    assert len(rows) == 1
    assert rows[0].message_id == 802


async def test_a_post_and_its_edit_arriving_together_number_the_chain_correctly(
    data_dir: Path,
) -> None:
    """Observed live: message 19189 was stored as revisions 1, 1, 2 — the middle one wrong.

    Telethon calls `handle_message` straight from its handlers, so a post and an edit seconds
    apart ran concurrently and both read the revision chain before either had committed. The
    unique index still stopped a true duplicate and `signal_ref` is revision-independent, so no
    trade was ever at risk — what broke was the audit trail and the "previous revision" the
    model is shown when it reasons about an edit (§3.3).
    """
    ingestor, runtime, source_id = await _build(data_dir)

    original = text_message(9001, "buy gold now")
    first_edit = text_message(9001, "buy gold now, sl 20 pips below")
    first_edit.edit_date = _dt(1_787_000_100)
    second_edit = text_message(9001, "buy gold now, sl 20 pips below, tp 3400")
    second_edit.edit_date = _dt(1_787_000_200)

    # Fired together, exactly as Telethon delivers a rapid post-then-edit.
    await asyncio.gather(
        ingestor.handle_message(original),
        ingestor.handle_message(first_edit, edited=True),
        ingestor.handle_message(second_edit, edited=True),
    )

    rows = [r for r in await _messages() if r.message_id == 9001]
    rows.sort(key=lambda r: r.edit_date)
    revisions = [r.revision for r in rows]

    assert len(rows) == 3, f"three distinct edit_dates must give three rows, got {len(rows)}"
    assert revisions == [1, 2, 3], (
        f"the chain must number 1, 2, 3 — got {revisions}. A repeated number means two "
        f"ingests read the chain before either committed."
    )
    assert len({r.edit_date for r in rows}) == 3, "each row keeps its own edit_date"


async def test_tag_filter_off_lets_untagged_text_through(data_dir: Path) -> None:
    """The filter ships off, so an ordinary post still becomes a pending signal."""
    ingestor, _, _ = await _build(data_dir)
    await ingestor.handle_message(text_message(41, "XAUUSD BUY 2380 SL 2370 TP 2390"))
    rows = await _messages()
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].skip_reason is None


async def test_tag_filter_drops_a_post_that_lacks_the_tag(data_dir: Path) -> None:
    """On, with a tag stored: no tag means skipped, recorded, and not queued."""
    ingestor, runtime, _ = await _build(data_dir)
    async with session_scope() as session:
        await set_setting(session, KEY_TAG_FILTER_ENABLED, True)
        await set_setting(session, KEY_TAG_FILTER_TAG, "#soft")
    await ingestor.handle_message(text_message(42, "XAUUSD BUY 2380 SL 2370 TP 2390"))
    rows = await _messages()
    assert rows[0].status == "skipped"
    assert rows[0].skip_reason == SKIP_REASON_TAG
    assert rows[0].text.startswith("XAUUSD")
    assert runtime.ingestor is ingestor


async def test_tag_filter_matches_the_tag_without_caring_about_case(data_dir: Path) -> None:
    """#SOFT and #SoFt are the same tag as #soft."""
    ingestor, _, _ = await _build(data_dir)
    async with session_scope() as session:
        await set_setting(session, KEY_TAG_FILTER_ENABLED, True)
        await set_setting(session, KEY_TAG_FILTER_TAG, "#soft")
    await ingestor.handle_message(text_message(43, "XAUUSD BUY 2380 #SOFT SL 2370"))
    await ingestor.handle_message(text_message(44, "noise without the marker"))
    await ingestor.handle_message(
        text_message(45, "FULL CLOSE TRADE", reply_to_msg_id=43)
    )
    rows = {row.message_id: row for row in await _messages()}
    assert rows[43].status == "pending"
    assert rows[44].status == "skipped"
    assert rows[44].skip_reason == SKIP_REASON_TAG
    assert rows[45].status == "skipped"
    assert rows[45].skip_reason == SKIP_REASON_TAG
