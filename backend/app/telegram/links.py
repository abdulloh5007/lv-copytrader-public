"""t.me permalink construction for a stored message (BUILD_PROMPT §3.5).

Every signal card and every notification carries a link back to the post it came from, so the
operator can read the original in one tap. Telegram addresses a post in exactly two ways —
`t.me/<username>/<id>` for a public channel, `t.me/c/<short_id>/<id>` for a private one — and
nothing else has a per-message web link at all. When neither form applies this module returns
`None` and the notifier omits the link, because a link that opens a "post not found" page is
worse than no link.
"""

from __future__ import annotations

import re

_BASE_URL = "https://t.me"

# Telethon marks a channel/supergroup peer id as -(1000000000000 + id) so a channel id can never
# collide with a basic-group or user id. This constant is that marker's magnitude.
_CHANNEL_ID_OFFSET = 1_000_000_000_000

# Entity kinds whose posts are addressable on the web. Source.entity_type is one of
# "channel" | "supergroup" | "group" | "user"; "megagroup" is accepted as Telethon's own wording
# for a supergroup, in case a caller passes it straight through.
_LINKABLE_TYPES: frozenset[str] = frozenset({"channel", "supergroup", "megagroup"})

# Basic groups and one-to-one chats. A basic group is not a channel, so t.me/c/ does not resolve
# for it, and a DM has no public address for an individual message even when the peer has a
# username — @name/<id> would just open the profile and drop the message id on the floor.
_NO_PERMALINK_TYPES: frozenset[str] = frozenset({"group", "chat", "user", "bot"})

# What Telegram will actually resolve as a username: letters, digits and underscores only.
# Anything else (a pasted title, a t.me/ prefix, a space) is not a username and must not be
# spliced into a URL.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")


def channel_short_id(chat_id: int) -> int:
    """Strip Telethon's `-100…` channel marker from a chat id.

    This subtraction is exactly the inverse of Telethon's `resolve_id`, which turns a marked id
    `-(1000000000000 + id)` back into `(id, PeerChannel)`. `t.me/c/<id>/<msg>` wants that bare
    channel id, so `abs(chat_id) - 1000000000000` is the number that belongs in the path.
    """
    return abs(int(chat_id)) - _CHANNEL_ID_OFFSET


def _normalize_username(username: str | None) -> str | None:
    """Return a username Telegram will resolve, or None when there is not one."""
    if not isinstance(username, str):
        return None
    name = username.strip().lstrip("@").strip()
    if not name:
        return None
    if not _USERNAME_RE.match(name):
        # Not something t.me can resolve. Falling through to the private form beats emitting a
        # URL with a space or a slash in the middle of it.
        return None
    return name


def _private_short_id(chat_id: int) -> int | None:
    """The `t.me/c/<here>/…` path segment, or None when chat_id is not a channel id."""
    try:
        raw = int(chat_id)
    except (TypeError, ValueError):
        return None
    short = channel_short_id(raw)
    if short > 0:
        return short
    # Some callers hold the *unmarked* id (Telethon's `entity.id`) rather than the -100… form
    # that `utils.get_peer_id` produces; that value is already the short id and subtracting the
    # marker would ruin it. Anything else — zero, or a marked basic-group id — has no c/ link.
    if 0 < raw < _CHANNEL_ID_OFFSET:
        return raw
    return None


def permalink(
    entity_type: str,
    username: str | None,
    chat_id: int,
    message_id: int,
) -> str | None:
    """Build the t.me link to one message, or None when Telegram has no link for it."""
    try:
        msg_id = int(message_id)
    except (TypeError, ValueError):
        return None
    if msg_id <= 0:
        return None

    kind = entity_type.strip().lower() if isinstance(entity_type, str) else ""

    # Checked before the username: a DM or a basic group has no per-message link in either form,
    # and BUILD_PROMPT §3.5 is explicit that both return None. A public peer's @name would build
    # a URL that resolves to a profile, not to the post — the exact broken link this avoids.
    if kind in _NO_PERMALINK_TYPES:
        return None

    name = _normalize_username(username)
    if name is not None:
        # A public channel or supergroup: the post is addressable by @username.
        return f"{_BASE_URL}/{name}/{msg_id}"

    if kind not in _LINKABLE_TYPES:
        # An unknown entity kind with no username. Guessing a URL shape here is how a dead link
        # reaches a notification; say there is none.
        return None

    short = _private_short_id(chat_id)
    if short is None:
        return None
    return f"{_BASE_URL}/c/{short}/{msg_id}"
