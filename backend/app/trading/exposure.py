"""Close every ticket that belongs to one signal chain, without a model call.

A deleted channel post and a reply that says full close both land here. The
server walks ``SIG:<source>:<message id>`` to the teaser and its ladder reply,
then closes open positions and cancels pending orders. The model is not asked
to list tickets.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import or_, select

from app.db import session_scope
from app.models import Message
from app.trading import guards
from app.util.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "FULL_CLOSE_REASON",
    "chain_exposure_closed",
    "close_signal_exposure",
    "is_full_close_text",
]

#: Stored on a reply that was flattened by the deterministic close, so a later
#: open on the same chain is refused and the feed can show why no model ran.
FULL_CLOSE_REASON: str = "full_close"

_EMOJI: re.Pattern[str] = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0000FE00-\U0000FE0F"
    "\U0001F1E6-\U0001F1FF"
    "]+"
)
_PARTIAL: re.Pattern[str] = re.compile(
    r"\b(?:PARTIAL|HALF|HALVE|SCALE|SOME|PORTION)\b|\d+\s*%"
)
# The whole message, after normalisation, must be a full flatten. A symbol may
# sit in front. Buy/sell variants are the side words, not a different command.
_FULL_CLOSE: re.Pattern[str] = re.compile(
    r"""
    ^
    (?:[A-Z][A-Z0-9.]{1,15}\s+)?
    (?:PLEASE\s+)?
    (?:
        FULLY?\s+CLOSE(?:\s+(?:THE|THIS))?(?:\s+TRADE)?
      | FULLY?\s+CLOSE\s+(?:BUY|SELL|LONG|SHORT)(?:\s+TRADE)?
      | CLOSE\s+(?:THE\s+|THIS\s+)?TRADE
      | CLOSE\s+ALL(?:\s+(?:TRADES|POSITIONS|ORDERS))?
      | CLOSE\s+POSITIONS?
      | CLOSE\s+(?:BUY|SELL|LONG|SHORT)(?:\s+TRADE)?
      | FULL\s+EXIT(?:\s+TRADE)?
      | EXIT\s+(?:THE\s+|THIS\s+)?TRADE
      | EXIT\s+(?:BUY|SELL|LONG|SHORT)(?:\s+TRADE)?
      | EXIT\s+FULL
    )
    (?:\s+(?:BUY|SELL|LONG|SHORT|TRADE|NOW|PLEASE|THANKS|TY|DONE))?
    $
    """,
    re.VERBOSE,
)


def _normalize(text: str) -> str:
    """Uppercase, drop emoji and punctuation, collapse whitespace."""
    cleaned = _EMOJI.sub(" ", text or "")
    cleaned = re.sub(r"[^A-Za-z0-9]+", " ", cleaned)
    return " ".join(cleaned.upper().split())


def is_full_close_text(text: str | None) -> bool:
    """True when the post is a full flatten, not a partial scale-out.

    Matches ``FULL CLOSE TRADE``, ``close trade``, ``full close``, and the buy
    or sell variants (``FULL CLOSE BUY``, ``CLOSE SELL TRADE``). ``close half``
    and a sentence that merely mentions a close do not match.
    """
    normalized = _normalize(text or "")
    if not normalized or _PARTIAL.search(normalized):
        return False
    return _FULL_CLOSE.match(normalized) is not None


async def chain_exposure_closed(signal_ref: str | None) -> bool:
    """True when this chain was deleted or already fully closed by the channel.

    A missing ref is not closed: manual and watchdog calls have none, and they
    must keep working. A lookup error refuses the open, because "I could not
    tell" must not become a new position on a voided signal.
    """
    if not signal_ref:
        return False
    try:
        async with session_scope() as session:
            refs = await guards.chain_signal_refs(session, signal_ref)
            source_id: int | None = None
            ids: list[int] = []
            for ref in refs:
                parsed = guards.parse_signal_ref(ref)
                if parsed is None:
                    continue
                source_id, message_id = parsed
                ids.append(message_id)
            if source_id is None or not ids:
                return False
            result = await session.execute(
                select(Message.id)
                .where(
                    Message.source_id == source_id,
                    Message.message_id.in_(ids),
                    or_(
                        Message.deleted_at.is_not(None),
                        Message.skip_reason == FULL_CLOSE_REASON,
                    ),
                )
                .limit(1)
            )
            return result.scalar_one_or_none() is not None
    except Exception as exc:  # noqa: BLE001 - fail closed on a void check
        log.warning("exposure_closed_lookup_failed signal_ref=%s error=%s", signal_ref, exc)
        return True


async def close_signal_exposure(
    executor: Any,
    signal_ref: str | None,
    *,
    reason: str,
    cause: str = "full_close",
    bypass_arm: bool = False,
    source_id: int | None = None,
    message_row_id: int | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Close positions and cancel pendings for one signal chain.

    ``signal_ref`` may be the teaser or any reply in the chain. The executor
    resolves the MT5 tickets. ``bypass_arm`` is for the channel delete and the
    deterministic full-close reply: those must flatten even while disarmed.
    A model call leaves it false, so the arm switch still blocks that path.
    """
    from app.trading.executor import RunContext

    ref = str(signal_ref or "").strip() or None
    origin = cause if cause in ("channel_delete", "full_close") else "agent"
    ctx = RunContext(
        run_id=run_id or uuid.uuid4().hex,
        signal_ref=ref,
        source_id=source_id,
        message_row_id=message_row_id,
        origin=origin,
        bypass_arm=bool(bypass_arm),
    )
    return await executor.close_signal_exposure(
        ctx, signal_ref=ref, reason=str(reason or ""), cause=cause
    )
