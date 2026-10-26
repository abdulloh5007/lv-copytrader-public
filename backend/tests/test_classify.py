"""Pure unit tests for message classification and permalink construction.

No Telethon connection, no event loop, no database: `classify()` reads a handful of attributes
off a message object, so the tests hand it stand-ins that carry exactly those attributes. The
*media* objects are real `telethon.tl.types` instances, because `classify()` branches on
`isinstance` of the media — that is the whole point of the design (BUILD_PROMPT §3.2), and a
duck-typed stand-in for the media would test nothing.

What is being defended here, in the order the branches run:

* a link preview is a **text** post, not a photo — `message.photo` is truthy for it;
* a chart "sent as file" (an uncompressed document with an image mime type) is a **real signal**
  and must be processed, which is half of acceptance criterion 8;
* a voice note, video note, sticker, GIF and audio file each get their own kind and none of them
  reach the model — the other half of acceptance criterion 8;
* `classify()` is total: whatever it is handed, the answer is a member of the closed `KINDS` set,
  so the dashboard feed can never render a raw Telethon class name.
"""

from __future__ import annotations

import pytest
from telethon.tl import types

from app.telegram.classify import (
    KINDS,
    PROCESSABLE,
    classify,
    kind_is_processable,
    skip_reason,
)
from app.telegram.links import channel_short_id, permalink

# The exact vocabulary of BUILD_PROMPT §3.2, spelled out again here on purpose: if someone adds a
# kind to the module, this test makes them add it to the contract too.
EXPECTED_KINDS = {
    "text",
    "photo",
    "photo_caption",
    "voice",
    "video",
    "video_note",
    "audio",
    "sticker",
    "gif",
    "document",
    "service",
    "empty",
    "unsupported",
}


# ---------------------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------------------


class FakeDocument:
    """The `.document` a Telethon message exposes — only its mime type is ever read."""

    def __init__(self, mime_type: str | None) -> None:
        self.mime_type = mime_type


class FakeMessage:
    """A `telethon.tl.custom.Message` reduced to the attributes `classify()` reads.

    `photo` and `video` are carried deliberately even though nothing should look at them: they
    are the two lying convenience flags, and setting them truthy on a link preview or a GIF is
    how these tests prove the classifier does not consult them.
    """

    def __init__(
        self,
        *,
        media=None,
        message: str | None = None,
        document: FakeDocument | None = None,
        sticker=None,
        voice=None,
        video_note=None,
        gif=None,
        video=None,
        audio=None,
        photo=None,
    ) -> None:
        self.media = media
        self.message = message
        self.raw_text = message
        self.document = document
        self.sticker = sticker
        self.voice = voice
        self.video_note = video_note
        self.gif = gif
        self.video = video
        self.audio = audio
        self.photo = photo


def photo_media() -> types.MessageMediaPhoto:
    """A genuine posted photo (compressed by Telegram)."""
    return types.MessageMediaPhoto(photo=None)


def webpage_media() -> types.MessageMediaWebPage:
    """A link preview. Telethon makes `message.photo` truthy for this when the page has an image."""
    return types.MessageMediaWebPage(webpage=None)


def document_media() -> types.MessageMediaDocument:
    """Any "send as file" attachment: charts, stickers, voice notes, GIFs, PDFs."""
    return types.MessageMediaDocument(document=None)


def service_message() -> types.MessageService:
    """A real service message — "channel photo changed", a join, a pin."""
    return types.MessageService(
        id=4242,
        peer_id=types.PeerChannel(channel_id=1234567890),
        action=types.MessageActionChatEditPhoto(photo=None),
    )


def document_message(mime: str | None, *, caption: str | None = None, **flags) -> FakeMessage:
    """A message whose media is a document with the given mime type."""
    return FakeMessage(
        media=document_media(),
        message=caption,
        document=FakeDocument(mime),
        **flags,
    )


# ---------------------------------------------------------------------------------------
# The closed set
# ---------------------------------------------------------------------------------------


def test_kinds_is_exactly_the_contract_set() -> None:
    assert set(KINDS) == EXPECTED_KINDS


def test_processable_is_exactly_text_and_photos() -> None:
    assert set(PROCESSABLE) == {"text", "photo", "photo_caption"}
    assert PROCESSABLE <= KINDS


# ---------------------------------------------------------------------------------------
# Text, empty, and the link preview that pretends to be a photo
# ---------------------------------------------------------------------------------------


def test_plain_text_message() -> None:
    assert classify(FakeMessage(message="BUY XAUUSD @ 2350 SL 2340 TP 2370")) == "text"


def test_message_with_no_media_and_no_text_is_empty() -> None:
    assert classify(FakeMessage(message="")) == "empty"
    assert classify(FakeMessage(message=None)) == "empty"


def test_link_preview_is_text_not_photo() -> None:
    """The headline case: `message.photo` is truthy, and the classifier must ignore it."""
    m = FakeMessage(
        media=webpage_media(),
        message="chart analysis here https://example.com/gold",
        photo=object(),  # exactly the lie BUILD_PROMPT §3.2 warns about
    )
    kind = classify(m)
    assert kind == "text"
    assert kind != "photo"
    assert kind != "photo_caption"
    assert kind_is_processable(kind)


def test_link_preview_with_no_text_is_still_text_not_photo() -> None:
    # A bare URL post: Telethon still fills `message` with the URL, but even when it does not,
    # the preview must never be mistaken for a posted chart.
    assert classify(FakeMessage(media=webpage_media(), message=None, photo=object())) == "text"


# ---------------------------------------------------------------------------------------
# Photos
# ---------------------------------------------------------------------------------------


def test_photo_without_caption() -> None:
    m = FakeMessage(media=photo_media(), message=None, photo=object())
    assert classify(m) == "photo"
    assert kind_is_processable("photo")


def test_photo_with_caption() -> None:
    m = FakeMessage(media=photo_media(), message="GOLD long, SL below 2340", photo=object())
    assert classify(m) == "photo_caption"
    assert kind_is_processable("photo_caption")


def test_whitespace_only_caption_is_not_a_caption() -> None:
    assert classify(FakeMessage(media=photo_media(), message="   \n\t ")) == "photo"


# ---------------------------------------------------------------------------------------
# Acceptance criterion 8: a chart sent as an uncompressed document is PROCESSED
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("mime", ["image/png", "image/jpeg", "image/webp", "IMAGE/PNG"])
def test_uncompressed_image_document_is_processed(mime: str) -> None:
    """"Send as file" preserves chart quality; the signal is still a signal."""
    kind = classify(document_message(mime))
    assert kind == "photo", f"{mime} sent as a document must classify as a photo, got {kind!r}"
    assert kind_is_processable(kind) is True
    assert skip_reason(kind) == ""


def test_uncompressed_image_document_with_caption() -> None:
    kind = classify(document_message("image/png", caption="EURUSD setup, entry on retest"))
    assert kind == "photo_caption"
    assert kind_is_processable(kind) is True


def test_non_image_document_is_skipped() -> None:
    for mime in ("application/pdf", "application/zip", "text/csv", ""):
        kind = classify(document_message(mime))
        assert kind == "document", f"{mime!r} should be a document, got {kind!r}"
        assert kind_is_processable(kind) is False
        assert skip_reason(kind)


# ---------------------------------------------------------------------------------------
# Every media kind that must NOT reach the model
# ---------------------------------------------------------------------------------------

# Each entry is (kind, flags set on the message). The mime types are the ones Telegram actually
# uses, so a classifier that fell through to the mime check would land somewhere visibly wrong
# (audio/ogg is not an image, but webp stickers would be read as charts — hence 'sticker' first).
SKIPPED_MEDIA_CASES = [
    ("voice", "audio/ogg", {"voice": True}),
    ("video_note", "video/mp4", {"video_note": True, "video": True}),
    ("sticker", "image/webp", {"sticker": object()}),
    ("gif", "video/mp4", {"gif": True, "video": True}),
    ("audio", "audio/mpeg", {"audio": True}),
    ("video", "video/mp4", {"video": True}),
]


@pytest.mark.parametrize("expected,mime,flags", SKIPPED_MEDIA_CASES)
def test_skipped_media_kinds(expected: str, mime: str, flags: dict) -> None:
    kind = classify(document_message(mime, **flags))
    assert kind == expected, f"expected {expected!r}, got {kind!r}"
    assert kind in KINDS
    assert kind_is_processable(kind) is False, f"{expected} must never reach the model"
    assert skip_reason(kind), f"{expected} needs a badge phrase"


@pytest.mark.parametrize("expected,mime,flags", SKIPPED_MEDIA_CASES)
def test_skipped_media_kinds_ignore_a_caption(expected: str, mime: str, flags: dict) -> None:
    # A captioned voice note is still a voice note; the caption must not promote it.
    assert classify(document_message(mime, caption="listen to this", **flags)) == expected


def test_sticker_wins_over_its_image_mime() -> None:
    """A webp sticker would otherwise be read as an uncompressed chart."""
    assert classify(document_message("image/webp", sticker=object())) == "sticker"


def test_video_note_wins_over_the_video_flag() -> None:
    assert classify(document_message("video/mp4", video_note=True, video=True)) == "video_note"


def test_gif_wins_over_the_video_flag() -> None:
    assert classify(document_message("video/mp4", gif=True, video=True)) == "gif"


# ---------------------------------------------------------------------------------------
# Service and unsupported
# ---------------------------------------------------------------------------------------


def test_service_message() -> None:
    kind = classify(service_message())
    assert kind == "service"
    assert kind_is_processable(kind) is False
    assert skip_reason(kind)


def test_unsupported_media() -> None:
    poll = types.MessageMediaPoll(poll=None, results=None)
    assert classify(FakeMessage(media=poll, message="vote")) == "unsupported"
    geo = types.MessageMediaGeo(geo=None)
    assert classify(FakeMessage(media=geo)) == "unsupported"


# ---------------------------------------------------------------------------------------
# Totality: whatever goes in, a member of KINDS comes out
# ---------------------------------------------------------------------------------------


class ExplodingMessage:
    """A message whose properties raise — a Telethon-side failure must degrade, not abort ingest."""

    @property
    def media(self):
        raise RuntimeError("telethon blew up")

    @property
    def message(self):
        raise RuntimeError("telethon blew up")


class ExplodingFlags:
    """An image document whose `.sticker` property raises — the flag loop must not crash."""

    def __init__(self) -> None:
        self.media = document_media()
        self.message = "x"
        self.document = FakeDocument("image/png")
        self.voice = None
        self.video_note = None
        self.gif = None
        self.video = None
        self.audio = None

    @property
    def sticker(self):
        raise RuntimeError("telethon blew up")


def _every_case() -> list:
    cases: list = [
        None,
        object(),
        ExplodingMessage(),
        ExplodingFlags(),
        FakeMessage(),
        FakeMessage(message="text"),
        FakeMessage(media=webpage_media(), message="link"),
        FakeMessage(media=photo_media()),
        FakeMessage(media=photo_media(), message="cap"),
        FakeMessage(media=types.MessageMediaEmpty()),
        FakeMessage(media=types.MessageMediaUnsupported()),
        FakeMessage(media=types.MessageMediaPoll(poll=None, results=None)),
        FakeMessage(media=types.MessageMediaGeo(geo=None)),
        FakeMessage(media=types.MessageMediaContact(
            phone_number="1", first_name="a", last_name="b", vcard="", user_id=1)),
        FakeMessage(media=types.MessageMediaDice(value=1, emoticon="\N{GAME DIE}")),
        service_message(),
        # A document with no `.document` at all, and one whose mime type is not a string.
        FakeMessage(media=document_media(), document=None),
        FakeMessage(media=document_media(), document=FakeDocument(None)),
        FakeMessage(media=document_media(), document=FakeDocument(12345)),  # type: ignore[arg-type]
    ]
    for expected, mime, flags in SKIPPED_MEDIA_CASES:
        cases.append(document_message(mime, **flags))
        cases.append(document_message(mime, caption="c", **flags))
    for mime in ("image/png", "application/pdf", ""):
        cases.append(document_message(mime))
        cases.append(document_message(mime, caption="c"))
    return cases


def test_classify_never_escapes_the_closed_set() -> None:
    for case in _every_case():
        kind = classify(case)
        assert isinstance(kind, str)
        assert kind in KINDS, f"{type(case).__name__} classified as {kind!r}, outside KINDS"


def test_skip_reason_is_empty_for_processable_and_a_phrase_otherwise() -> None:
    for kind in sorted(KINDS):
        reason = skip_reason(kind)
        if kind in PROCESSABLE:
            assert reason == "", f"{kind} is processed and has no skip reason"
        else:
            assert reason and len(reason) <= 64, f"{kind} needs a short badge phrase"
    # An invented kind string reports the catch-all rather than leaking itself into the UI.
    assert skip_reason("wormhole") == skip_reason("unsupported")
    assert kind_is_processable("wormhole") is False


# ---------------------------------------------------------------------------------------
# Permalinks (BUILD_PROMPT §3.5)
# ---------------------------------------------------------------------------------------


def test_channel_short_id_is_the_resolve_id_inverse() -> None:
    assert channel_short_id(-1001234567890) == 1234567890
    assert channel_short_id(-1000000000001) == 1
    assert channel_short_id(-1002097152000) == 2097152000


def test_permalink_public_channel_uses_the_username() -> None:
    assert permalink("channel", "goldsignals", -1001234567890, 55) == (
        "https://t.me/goldsignals/55"
    )
    # A pasted @ prefix must not end up doubled in the URL.
    assert permalink("channel", "@goldsignals", -1001234567890, 55) == (
        "https://t.me/goldsignals/55"
    )


def test_permalink_private_channel_uses_the_short_id() -> None:
    assert permalink("channel", None, -1001234567890, 55) == "https://t.me/c/1234567890/55"
    assert permalink("supergroup", None, -1001234567890, 7) == "https://t.me/c/1234567890/7"
    # The short id in the path is exactly channel_short_id.
    assert str(channel_short_id(-1001234567890)) in permalink(
        "channel", None, -1001234567890, 55
    )


def test_permalink_basic_group_and_dm_have_none() -> None:
    assert permalink("group", None, -4123456789, 3) is None
    assert permalink("user", "someone", 123456789, 3) is None
    # An unknown entity kind with no username gets no guessed URL either.
    assert permalink("", None, -4123456789, 3) is None


def test_permalink_rejects_a_nonsense_message_id() -> None:
    assert permalink("channel", "goldsignals", -1001234567890, 0) is None
    assert permalink("channel", None, -1001234567890, -1) is None
