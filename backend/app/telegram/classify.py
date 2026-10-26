"""Message kind detection — the closed classification that decides what the model ever sees.

`message.photo` lies (BUILD_PROMPT §3.2). It is truthy for a link preview whose webpage carries
an image and for a `MessageActionChatEditPhoto` service message, so a handler that trusts it
downloads a favicon and calls it a chart. `message.video` lies in the other direction: GIFs and
video notes both carry `DocumentAttributeVideo`, so it is truthy for all three. And signal
channels routinely "send as file" to keep a chart uncompressed, which arrives as a *document*
with an image mime type and is a real signal that must be processed rather than skipped.

So nothing here branches on those convenience flags first. Every decision branches on the media
*type*, and only inside `MessageMediaDocument` on the flags — in an order where each ambiguous
kind is consumed before the flag that overlaps it.

The returned value is always a member of `KINDS`. The set is closed and `unsupported` is its
catch-all, which is what keeps a raw Telethon class name out of the dashboard feed.
"""

from __future__ import annotations

from typing import Any

from telethon.tl import types

# Telethon's own classes are looked up once, defensively. `isinstance(x, ())` is legal and always
# False, so if a version bump ever renames one of these the affected branch simply stops matching
# and the message degrades to 'unsupported' — it never raises AttributeError at import time.
_MESSAGE_SERVICE: Any = getattr(types, "MessageService", ())
_MEDIA_WEBPAGE: Any = getattr(types, "MessageMediaWebPage", ())
_MEDIA_PHOTO: Any = getattr(types, "MessageMediaPhoto", ())
_MEDIA_DOCUMENT: Any = getattr(types, "MessageMediaDocument", ())

# Exactly the set in BUILD_PROMPT §3.2 — closed, and the only vocabulary Message.kind ever holds.
KINDS: frozenset[str] = frozenset(
    {
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
)

# These three reach the model. Everything else is stored with status='skipped' and a badge.
PROCESSABLE: frozenset[str] = frozenset({"text", "photo", "photo_caption"})

# Short English badge phrases, one per non-processable kind. Message.skip_reason is String(64),
# so these stay phrases, not sentences.
_SKIP_REASONS: dict[str, str] = {
    "voice": "voice message",
    "video": "video",
    "video_note": "video note",
    "audio": "audio file",
    "sticker": "sticker",
    "gif": "GIF/animation",
    "document": "non-image document",
    "service": "service message",
    "empty": "empty message",
    "unsupported": "unsupported message type",
}

# Polls, dice, contacts, locations, invoices and anything Telegram adds after this was written
# all land on 'unsupported', and so does any kind string a caller invents.
_UNKNOWN_REASON = "unsupported message type"

# The document flags, in the one order that is correct. Not alphabetical, not arbitrary:
# a sticker is a document, a voice note is an audio document, and m.video is truthy for both
# GIFs and video notes — each ambiguous kind is consumed before the flag that overlaps it, or
# every animation in the channel would be recorded as a video.
_DOCUMENT_FLAGS: tuple[tuple[str, str], ...] = (
    ("sticker", "sticker"),
    ("voice", "voice"),
    ("video_note", "video_note"),
    ("gif", "gif"),
    ("video", "video"),
    ("audio", "audio"),
)


# Returned by _attr when reading an attribute *raised*, which is different from it being absent.
# An attribute we cannot read means we do not know what this message is, and the honest answer to
# that is 'unsupported' — recorded and badged — never a guess that sends half a signal to the model.
_UNREADABLE = object()


def _attr(m: Any, name: str) -> Any:
    """Read one attribute off a Telethon message: its value, None if absent, _UNREADABLE if it raised.

    `sticker`, `voice`, `gif` and friends are computed properties on `telethon.tl.custom.Message`;
    a raw `types.Message` does not carry them at all and a future Telethon may compute them
    differently. A missing or exploding property must degrade classification, never abort ingest.
    """
    try:
        return getattr(m, name, None)
    except Exception:  # noqa: BLE001 - a Telethon-side failure degrades to 'unsupported'
        return _UNREADABLE


def _has_text(text: Any) -> bool:
    """True when the message carries body or caption text.

    There is no separate caption field in Telegram: the caption *is* `message.message`.
    Whitespace-only text is not a caption — it must not turn a bare chart into 'photo_caption'.
    """
    if not isinstance(text, str):
        return False
    return bool(text.strip())


def _document_mime(m: Any) -> str | None:
    """The document's mime type lowercased, '' when there is none, None when it could not be read."""
    document = _attr(m, "document")
    if document is _UNREADABLE:
        return None
    if document is None:
        return ""
    mime = _attr(document, "mime_type")
    if mime is _UNREADABLE:
        return None
    if not isinstance(mime, str):
        return ""
    # Mime types are case-insensitive, and some clients upload "IMAGE/PNG".
    return mime.strip().lower()


def classify(m: Any) -> str:
    """Return the kind of one Telegram message — always a member of KINDS."""
    if m is None:
        # Defensive: nothing to classify is the catch-all, not an AttributeError up the stack.
        return "unsupported"

    # Service messages first ("channel photo changed", "user joined", pinned). They arrive
    # through events.ChatAction rather than events.NewMessage, carry no .media and no .message,
    # and MessageActionChatEditPhoto is one of the two things that make message.photo truthy
    # with no chart behind it.
    if isinstance(m, _MESSAGE_SERVICE):
        return "service"

    media = _attr(m, "media")
    text = _attr(m, "message")
    if media is _UNREADABLE or text is _UNREADABLE:
        return "unsupported"
    captioned = _has_text(text)

    # No media at all: the body text is the whole signal, or there is nothing to process.
    # A deleted message (types.MessageEmpty) has neither attribute and lands on 'empty' here.
    if media is None:
        return "text" if captioned else "empty"

    # A link preview — the other thing that makes message.photo truthy. The post itself is text
    # (the URL, plus whatever the channel wrote around it), so it is processed as text and the
    # preview thumbnail is deliberately never treated as a posted chart.
    if isinstance(media, _MEDIA_WEBPAGE):
        return "text"

    # A genuine photo. The caption, when there is one, is m.message.
    if isinstance(media, _MEDIA_PHOTO):
        return "photo_caption" if captioned else "photo"

    if isinstance(media, _MEDIA_DOCUMENT):
        for flag, kind in _DOCUMENT_FLAGS:
            value = _attr(m, flag)
            if value is _UNREADABLE:
                return "unsupported"
            if value:
                return kind
        # A chart "sent as file" to preserve quality arrives exactly here: a document whose mime
        # type is an image. That is a REAL SIGNAL and must be read like a photo, not skipped.
        # Stickers reach this line only if the flag above missed them, and webp stickers would
        # otherwise be mistaken for charts — which is why 'sticker' is tested first.
        mime = _document_mime(m)
        if mime is None:
            return "unsupported"
        if mime.startswith("image/"):
            return "photo_caption" if captioned else "photo"
        return "document"

    # Polls, dice, contacts, locations, venues, games, invoices, MessageMediaUnsupported, and
    # whatever Telegram ships next. The closed set ends here so the feed never renders a class name.
    return "unsupported"


def kind_is_processable(kind: str) -> bool:
    """True when this kind is handed to the model; every other kind is recorded as skipped."""
    return kind in PROCESSABLE


def skip_reason(kind: str) -> str:
    """Short English badge phrase saying why a message was skipped.

    Returns '' for a processable kind — that message was not skipped and has no reason to show.
    An unknown kind string reports the catch-all rather than leaking itself into the UI.
    """
    if kind in PROCESSABLE:
        return ""
    return _SKIP_REASONS.get(kind, _UNKNOWN_REASON)
