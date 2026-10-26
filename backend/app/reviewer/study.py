"""One-shot channel study: collect posts, ask the configured provider, clip the playbook.

The provider is whoever Settings already points at (OpenAI-compatible, including
ZenMux, or Anthropic). This module does not pick a vendor and does not call the
agent loop, so it never takes a copytrader run slot.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.ai.base import Capabilities, DoneDelta, Provider, TextDelta, text_block
from app.db import session_scope
from app.models import Message, Source
from app.telegram.classify import classify
from app.util.logging import get_logger

log = get_logger(__name__)

#: Below this many stored posts, or a span shorter than this, the reviewer asks
#: Telegram for the last few days instead of guessing from a thin table.
DEFAULT_MIN_MESSAGES: int = 12
MIN_SPAN: timedelta = timedelta(days=2)
HISTORY_DAYS_MIN: int = 3
HISTORY_DAYS_MAX: int = 4
MAX_CORPUS_MESSAGES: int = 180
MAX_CORPUS_CHARS: int = 14_000
TEXT_CLIP: int = 400
DEFAULT_REVIEWER_MAX_TOKENS: int = 2048
DEFAULT_MAX_LINES: int = 60
HARD_MAX_LINES: int = 60

STUDY_SYSTEM: str = (
    "You write a channel playbook for a trading desk that copy-trades one Telegram channel.\n"
    "Read the sample posts and describe only this channel's habits. Write Markdown, "
    "40 to 55 lines, and never more than 60. No preamble and no closing remark.\n"
    "Use these headings, in this order:\n"
    "# Channel playbook\n"
    "## Signals (act)\n"
    "## Skip (noise)\n"
    "## Teaser versus ladder\n"
    "## Stop and targets\n"
    "## Quirks\n"
    "Rules:\n"
    "- Signals are new entries, pending orders, stop or take-profit updates, closes, "
    "partials, and replies that change an open trade.\n"
    "- Skip is celebration, greetings, target-hit notes that do not change a position, "
    "and commentary.\n"
    "- Say how a teaser (an entry, sometimes with no stop or targets yet) differs from "
    "the ladder reply that follows it.\n"
    "- Describe the usual stop and target layout: how many targets, where the stop sits, "
    "and the words the channel uses.\n"
    "- Name quirks that would fool a classifier: emojis, result screenshots, VIP upsells, "
    "posts that only celebrate a target.\n"
    "- Do not invent prices that are not in the sample. Do not give trading advice. "
    "Do not call tools."
)


def history_days(raw: Any) -> int:
    """Clamp a configured window onto the 3–4 day fetch the reviewer is allowed."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = HISTORY_DAYS_MAX
    return max(HISTORY_DAYS_MIN, min(HISTORY_DAYS_MAX, value))


def max_brief_lines(raw: Any) -> int:
    """Clamp the playbook length. Sixty lines is the hard ceiling."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_LINES
    return max(1, min(HARD_MAX_LINES, value))


def corpus_is_thin(rows: list[dict[str, Any]], *, min_messages: int) -> bool:
    """True when the database sample is too small or too short to trust alone."""
    if len(rows) < max(1, int(min_messages)):
        return True
    stamps = [row["posted_at"] for row in rows if isinstance(row.get("posted_at"), datetime)]
    if len(stamps) < 2:
        return True
    return (max(stamps) - min(stamps)) < MIN_SPAN


def _naive_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _clip_text(text: Any) -> str:
    body = " ".join(str(text or "").split())
    if len(body) <= TEXT_CLIP:
        return body
    return body[: TEXT_CLIP - 1] + "…"


def _row_view(row: Message) -> dict[str, Any]:
    return {
        "message_id": int(row.message_id),
        "posted_at": row.posted_at,
        "kind": str(row.kind or ""),
        "text": str(row.text or ""),
        "reply_to_message_id": row.reply_to_message_id,
        "status": str(row.status or ""),
        "skip_reason": row.skip_reason,
        "origin": "db",
    }


def telegram_message_view(message: Any) -> dict[str, Any] | None:
    """Turn one Telethon message into a study row. None when it has no id."""
    try:
        message_id = int(getattr(message, "id", 0) or 0)
    except (TypeError, ValueError):
        return None
    if message_id <= 0:
        return None
    text = ""
    for attribute in ("raw_text", "message", "text"):
        value = getattr(message, attribute, None)
        if isinstance(value, str) and value:
            text = value
            break
    try:
        kind = classify(message)
    except Exception:  # noqa: BLE001 - a weird update is still a sample line
        kind = "unsupported"
    reply = getattr(message, "reply_to_msg_id", None)
    try:
        reply_to = int(reply) if reply else None
    except (TypeError, ValueError):
        reply_to = None
    return {
        "message_id": message_id,
        "posted_at": _naive_utc(getattr(message, "date", None)),
        "kind": kind,
        "text": text,
        "reply_to_message_id": reply_to,
        "status": "",
        "skip_reason": None,
        "origin": "telegram",
    }


def merge_corpus(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe by message id. A stored row wins over a freshly fetched copy."""
    chosen: dict[int, dict[str, Any]] = {}
    for group in groups:
        for row in group:
            try:
                message_id = int(row.get("message_id") or 0)
            except (TypeError, ValueError):
                continue
            if message_id <= 0:
                continue
            current = chosen.get(message_id)
            if current is None or (current.get("origin") != "db" and row.get("origin") == "db"):
                chosen[message_id] = row
    rows = list(chosen.values())
    rows.sort(key=lambda row: (row.get("posted_at") or datetime.min, int(row["message_id"])))
    if len(rows) > MAX_CORPUS_MESSAGES:
        rows = rows[-MAX_CORPUS_MESSAGES:]
    return rows


async def load_source(source_id: int) -> tuple[str, int] | None:
    """Return ``(title, chat_id)`` for a source, or None when the row is gone."""
    async with session_scope() as session:
        row = await session.get(Source, int(source_id))
        if row is None:
            return None
        return str(row.title or ""), int(row.chat_id)


async def load_db_messages(source_id: int) -> list[dict[str, Any]]:
    """Recent stored posts for one source, oldest first, capped."""
    async with session_scope() as session:
        result = await session.execute(
            select(Message)
            .where(Message.source_id == int(source_id))
            .order_by(Message.posted_at.desc(), Message.id.desc())
            .limit(MAX_CORPUS_MESSAGES)
        )
        rows = list(result.scalars())
    views = [_row_view(row) for row in rows]
    views.reverse()
    return views


def render_corpus(title: str, chat_id: int, rows: list[dict[str, Any]]) -> str:
    """The user turn the reviewer model sees. Clipped so one noisy channel cannot fill it."""
    lines = [
        f"Channel: {title or '(untitled)'}",
        f"chat_id: {int(chat_id)}",
        f"posts: {len(rows)}",
        "",
    ]
    for row in rows:
        posted = row.get("posted_at")
        stamp = posted.isoformat(timespec="minutes") if isinstance(posted, datetime) else "?"
        kind = str(row.get("kind") or "")
        reply = row.get("reply_to_message_id")
        header = f"[{stamp}] #{row.get('message_id')} {kind}"
        if reply:
            header += f" reply_to=#{reply}"
        status = str(row.get("status") or "")
        reason = str(row.get("skip_reason") or "")
        if status == "skipped" and reason:
            header += f" skipped:{reason}"
        lines.append(header)
        lines.append(_clip_text(row.get("text")) or "(no text)")
        lines.append("")
    blob = "\n".join(lines)
    if len(blob) > MAX_CORPUS_CHARS:
        blob = blob[:MAX_CORPUS_CHARS]
    return blob


def study_artifact(rows: list[dict[str, Any]], *, origin: str) -> dict[str, Any]:
    """JSON-safe copy of the sample. This file is reviewer-only."""
    packed: list[dict[str, Any]] = []
    for row in rows:
        posted = row.get("posted_at")
        packed.append(
            {
                "message_id": row.get("message_id"),
                "posted_at": posted.isoformat() if isinstance(posted, datetime) else None,
                "kind": row.get("kind"),
                "text": _clip_text(row.get("text")),
                "reply_to_message_id": row.get("reply_to_message_id"),
                "status": row.get("status") or None,
                "skip_reason": row.get("skip_reason"),
                "origin": row.get("origin"),
            }
        )
    return {"origin": origin, "messages": packed}


def strip_fences(text: str) -> str:
    """Drop a single surrounding markdown fence if the model added one."""
    body = (text or "").strip()
    if not body.startswith("```"):
        return body
    lines = body.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


async def compose_brief(
    provider: Provider,
    *,
    model: str,
    capabilities: Capabilities,
    corpus: str,
    max_tokens: int,
) -> str:
    """Ask the configured model for the playbook. Tools stay off. Errors propagate."""
    quiet = Capabilities(
        supports_vision=False,
        supports_tools=bool(getattr(capabilities, "supports_tools", False)),
        thinking_form=None,
        source="channel_reviewer",
    )
    chunks: list[str] = []
    stream = provider.run(
        system=STUDY_SYSTEM,
        messages=[{"role": "user", "content": [text_block(corpus or "(no posts)")]}],
        tools=[],
        max_tokens=max(1, int(max_tokens)),
        model=model or "",
        capabilities=quiet,
    )
    async for delta in stream:
        if isinstance(delta, TextDelta) and delta.text:
            chunks.append(delta.text)
        elif isinstance(delta, DoneDelta):
            break
    return strip_fences("".join(chunks))
