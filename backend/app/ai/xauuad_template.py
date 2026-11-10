"""Offline parser for the XAUUADTRADINGMASTER channel's gold signals.

This parser does not place trades itself. In this channel, ``4014=11`` means a zone, not a decimal price.  Inputs that do
not fit the observed format or contain inconsistent levels fail closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

CHANNEL_CHAT_ID = -1001541676734

_ENTRY = re.compile(r"\b(BUY|SELL)\s+(\d{3,6})\s*=\s*(\d{1,6})(?![\d.])", re.I)
_TARGET = re.compile(r"\bTP\s*[:=]?\s*(\d{3,6}(?:\.\d+)?)\b", re.I)
_STOP = re.compile(r"\bSL\s*[:=]?\s*(\d{3,6}(?:\.\d+)?)\b", re.I)
_MANAGEMENT = re.compile(
    r"\b(?:TARGET\s+\d+\s+COMPLETE|ALL\s+TARGET\s+COMPLETE|TOUCH\s+CHECK|"
    r"HOLD|SL\s+IS\s+PART|(?:BUY|SELL)\s+LIMIT\s+ACTIVE)\b", re.I
)
_SIDE = re.compile(r"\b(?:BUY|SELL)\b", re.I)
_OTHER_SYMBOL = re.compile(r"\b(?:EURUSD|GBPUSD|USDJPY|XAGUSD|BTCUSD)\b", re.I)


@dataclass(frozen=True)
class ChannelPost:
    kind: Literal["entry", "complete", "management", "invalid", "ignored"]
    side: Literal["buy", "sell"] | None = None
    zone: tuple[float, float] | None = None
    sl: float | None = None
    targets: tuple[float, ...] = ()
    reason: str = ""


def _zone(left: str, right: str) -> tuple[float, float] | None:
    first = int(left)
    if len(right) < len(left):
        magnitude = 10 ** len(right)
        base = first // magnitude * magnitude + int(right)
        candidates = [n for n in (base - magnitude, base, base + magnitude) if n > 0]
        distances = sorted((abs(n - first), n) for n in candidates)
        # Do not guess when the abbreviated end has two equally plausible values.
        if len(distances) > 1 and distances[0][0] == distances[1][0]:
            return None
        second = distances[0][1]
    else:
        second = int(right)
    # Observed zones are close together. A wide or degenerate zone is not an
    # instruction to guess an opening price or a dropped digit.
    if first == second or abs(first - second) > 30:
        return None
    return float(min(first, second)), float(max(first, second))


def parse_channel_post(text: str) -> ChannelPost:
    """Interpret an individual post; correlation with earlier posts is separate."""
    text = (text or "").replace("＝", "=")
    if _MANAGEMENT.search(text):
        return ChannelPost("management")
    entries = list(_ENTRY.finditer(text))
    if not entries:
        return ChannelPost("ignored")
    if len(entries) != 1 or len({m.group(0).upper() for m in _SIDE.finditer(text)}) != 1 or _OTHER_SYMBOL.search(text):
        return ChannelPost("invalid", reason="ambiguous_entry_or_symbol")
    match = entries[0]
    side = match.group(1).lower()
    zone = _zone(match.group(2), match.group(3))
    if zone is None:
        return ChannelPost("invalid", reason="ambiguous_entry_zone")
    stops = [float(m.group(1)) for m in _STOP.finditer(text)]
    targets = tuple(float(m.group(1)) for m in _TARGET.finditer(text))
    if not stops and not targets:
        return ChannelPost("entry", side=side, zone=zone)
    if len(stops) != 1 or not targets or len(targets) > 12:
        return ChannelPost("invalid", side=side, zone=zone, reason="incomplete_or_ambiguous_levels")
    low, high = zone
    stop = stops[0]
    if side == "buy":
        valid_stop = stop < low
        valid_targets = all(t > high for t in targets) and all(a < b for a, b in zip(targets, targets[1:]))
    else:
        valid_stop = stop > high
        valid_targets = all(t < low for t in targets) and all(a > b for a, b in zip(targets, targets[1:]))
    if not valid_stop or not valid_targets:
        return ChannelPost("invalid", side=side, zone=zone, sl=stop, targets=targets, reason="levels_conflict_with_zone")
    return ChannelPost("complete", side=side, zone=zone, sl=stop, targets=targets)


@dataclass(frozen=True)
class Teaser:
    """A teaser candidate; timestamps are UTC seconds and source scoping is required."""

    message_id: int
    posted_at: float
    text: str


@dataclass(frozen=True)
class TeaserMatch:
    kind: Literal["matched", "missing", "conflict", "ambiguous"]
    message_id: int | None = None


def match_teaser(
    text: str,
    *,
    reply_to: int | None,
    posted_at: float,
    teasers: list[Teaser],
    max_age_seconds: float = 180.0,
) -> TeaserMatch:
    """Match a complete post to one prior teaser, refusing conflicting or ambiguous matches.

    An explicit reply is authoritative: never substitute a different teaser if
    its side or zone differs. For an unthreaded post, only an exact, unique,
    recent match is accepted. Callers must supply candidates from this source
    only and must serialize order creation and updates separately.
    """
    complete = parse_channel_post(text)
    if complete.kind != "complete":
        return TeaserMatch("conflict")
    if reply_to is not None:
        candidates = [t for t in teasers if t.message_id == reply_to]
        if not candidates:
            return TeaserMatch("missing")
        if len(candidates) != 1:
            return TeaserMatch("ambiguous")
        candidate = candidates[0]
        parsed = parse_channel_post(candidate.text)
        if (parsed.kind != "entry" or parsed.side != complete.side or parsed.zone != complete.zone
                or not 0 <= posted_at - candidate.posted_at <= max_age_seconds):
            return TeaserMatch("conflict")
        return TeaserMatch("matched", candidate.message_id)

    matches = []
    for candidate in teasers:
        if not 0 <= posted_at - candidate.posted_at <= max_age_seconds:
            continue
        parsed = parse_channel_post(candidate.text)
        if parsed.kind == "entry" and parsed.side == complete.side and parsed.zone == complete.zone:
            matches.append(candidate)
    if len(matches) == 1:
        return TeaserMatch("matched", matches[0].message_id)
    return TeaserMatch("ambiguous" if matches else "missing")


@dataclass(frozen=True)
class ChannelPlan:
    """A proposed action only; the live executor must still enforce all guards."""

    action: Literal["open_without_sl", "attach_levels", "ignore", "reject"]
    side: Literal["buy", "sell"] | None = None
    zone: tuple[float, float] | None = None
    volume_per_order: float | None = None
    sl: float | None = None
    targets: tuple[float, ...] = ()
    teaser_id: int | None = None
    move_second_to_be_after_first: bool = False
    reason: str = ""


def plan_channel_post(
    text: str, *, reply_to: int | None = None, posted_at: float = 0,
    teasers: list[Teaser] | None = None,
) -> ChannelPlan:
    """Prepare the owner's initial two-order profile without executing it.

    Opening without a stop exposes both orders until levels are applied. An
    attachment proposal is not proof that matching orders exist; the executor
    must verify their identity and refuse to open a replacement when absent.
    """
    post = parse_channel_post(text)
    if post.kind in ("ignored", "management"):
        return ChannelPlan("ignore")
    if post.kind == "invalid":
        return ChannelPlan("reject", reason=post.reason)
    if post.kind == "entry":
        return ChannelPlan("open_without_sl", side=post.side, zone=post.zone,
                           volume_per_order=0.2, move_second_to_be_after_first=True)
    if len(post.targets) < 4:
        return ChannelPlan("reject", reason="fewer_than_four_targets")
    match = match_teaser(text, reply_to=reply_to, posted_at=posted_at, teasers=teasers or [])
    if match.kind != "matched":
        return ChannelPlan("reject", reason="teaser_" + match.kind)
    return ChannelPlan("attach_levels", side=post.side, zone=post.zone, sl=post.sl,
                       targets=(post.targets[1], post.targets[3]), teaser_id=match.message_id,
                       move_second_to_be_after_first=True)


@dataclass(frozen=True)
class ExistingLeg:
    ticket: int
    side: Literal["buy", "sell"]
    volume: float
    role: Literal["cover", "runner"]
    sl: float


def validate_existing_legs(plan: ChannelPlan, legs: list[ExistingLeg]) -> str | None:
    """Reject unsafe attachment proposals before any broker operation.

    The caller must obtain fresh, source-scoped broker state and hold the broker
    write lock until attachment finishes. A duplicate open is never a fallback.
    """
    if plan.action != "attach_levels" or plan.side is None or plan.sl is None:
        return "not_an_attachment"
    if len(legs) != 2 or {leg.role for leg in legs} != {"cover", "runner"}:
        return "expected_two_distinct_legs"
    if any(leg.ticket <= 0 for leg in legs) or len({leg.ticket for leg in legs}) != 2:
        return "invalid_tickets"
    if any(leg.side != plan.side or abs(leg.volume - 0.2) > 1e-8 for leg in legs):
        return "side_or_volume_mismatch"
    for leg in legs:
        # A watchdog or the operator may already have protected a leg. Do not
        # widen its stop when the channel's later SL arrives.
        if leg.sl > 0 and ((plan.side == "buy" and plan.sl < leg.sl)
                           or (plan.side == "sell" and plan.sl > leg.sl)):
            return "would_widen_existing_stop"
    return None
