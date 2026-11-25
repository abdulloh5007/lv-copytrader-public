"""Research-only parser for abbreviated Uzbek gold signals.

No broker actions are permitted by this module. A recent gold quote is required
for shortened prices. Prices that are ambiguous, far from the quote, or
contradict the trade direction are refused; channel typos are never corrected.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

_SIDE = re.compile(r'(?<![A-Z])(BUY|SELL|SEL)\b', re.I)
_RANGE = re.compile(r'(?<!\d)(\d{1,5})\s*[-–—]\s*(\d{1,5})(?!\d)')
_TARGET = re.compile(r'\bTP\s*[:=]?\s*(\d{1,6}(?:\.\d+)?)\b', re.I)
_SL_PIPS = re.compile(r'\bSL\s*[:=]?\s*(\d+(?:\.\d+)?)\s*PIPS?\b', re.I)
_SL = re.compile(r'\bSL\s*[:=]?\s*(\d{3,6}(?:\.\d+)?)\b', re.I)
_REENTRY_ANNOUNCEMENT = re.compile(
    r'^\s*stop\s+urdi\s*[,!.!‼️❗️✅]*\s*re\s*-?\s*entry\s+beraman\s*[,!.!‼️❗️✅]*\s*$',
    re.I,
)
_OTHER_SYMBOL = re.compile(r'\b(?:BTC|BTCUSD|ETH|ETHUSD|XAG|XAGUSD|EURUSD|GBPUSD)\b', re.I)
_BE_MENTION = re.compile(r'\bb\s*/\s*u\b|bezubit|bezub|безубыт', re.I)
_BE_CLOSE = re.compile(
    r'^\s*(?:(?:b\s*/\s*u|bezubit\w*|bezub\w*|безубыт\w*)\s*(?:da|ga)?\s+)?'
    r'(?:yoping|yopinglar|закройте|закрывайте|close|exit)\b', re.I,
)
_BE_NONCOMMAND = re.compile(r'\?|\b(?:not|never|dont|don.t|if|не|если|deb)\b', re.I)
_BE_MOVE = re.compile(r'\b(?:obqoying|obqo[yʻ’\x27]ing|qoying|qo[yʻ’\x27]ing|'
                      r'перенесите|поставьте|move|set)\b', re.I)
_PREVIOUS_TP_REFERENCE = re.compile(
    r'\b(?:(?:berilgan|oldingi|avvalgi)\s+)?tp\s*gacha\b|'
    r'\bдо\s+(?:(?:указанного|предыдущего|данного)\s+)?(?:тп|tp)\b', re.I,
)
CORRECTION_RETRY_REASONS = frozenset({
    'signal follow-up changed more than targets',
    'signal follow-up is not consistent with the original',
    'signal entry zone changed',
    'signal changed before entry and could not be verified',
    'gold channel side or split no longer matches the verified signal',
    'gold channel entry refused: targets_changed_before_order',
})
_LEVEL = (r'(?:TP\s*[:=]?\s*\d{1,6}(?:\.\d+)?|'
          r'SL\s*[:=]?\s*(?:\d+(?:\.\d+)?\s*PIPS?|\d{3,6}(?:\.\d+)?))')
_EXPLICIT_REENTRY = re.compile(
    r"^\s*(?P<zone>\d{1,5}\s*[-–—]\s*\d{1,5})\s*"
    r"(?P<side>BUY|SELL|SEL)\s+RE\s*-?\s*ENTRY"
    r"(?P<levels>(?:\s+" + _LEVEL + r"){0,4})\s*[.!‼️✅]*\s*$", re.I
)
_MARKET_NOW = re.compile(
    r'^\s*(?:(?:GOLD|XAUUSD)\s+)?(BUY|SELL|SEL)\s+NOW'
    r'(?P<levels>(?:\s+' + _LEVEL + r'){0,3})\s*[.!‼️]*\s*$', re.I
)
_UZ_PERSONAL_ENTRY = re.compile(
    r'^\s*(?:(?:GOLD|XAUUSD)\s+)?(?:'
    r'(?P<side_first>SELLDAMAN|SELDAMAN|BUYDAMAN)\s*(?P<zone_after>\d{1,5}\s*[-–—]\s*\d{1,5})'
    r'|(?P<zone_before>\d{1,5}\s*[-–—]\s*\d{1,5})\s*(?P<side_last>SELLDAMAN|SELDAMAN|BUYDAMAN)'
    r')(?:\s*kirsangiz\s+bo[\'‘’]?ladi)?'
    r'(?P<levels>(?:\s+' + _LEVEL + r'){0,3})\s*[.!‼️✅]*\s*$', re.I
)


@dataclass(frozen=True)
class GoldZonePost:
    kind: Literal['entry', 'management', 'invalid', 'ignored']
    side: Literal['buy', 'sell'] | None = None
    zone: tuple[float, float] | None = None
    targets: tuple[float, ...] = ()
    sl: float | None = None
    sl_pips: float | None = None
    action: str = ''
    reason: str = ''
    ignored_levels: tuple[str, ...] = ()
    verified_first_target: float | None = None


def _expand_first(raw: str, quote: float) -> int:
    value = int(raw)
    if len(raw) >= 4:
        return value
    modulus = 10 ** len(raw)
    return int(quote // modulus) * modulus + value


def _expand_suffix(raw: str, reference: float) -> int | None:
    if len(raw) >= 4:
        return int(raw)
    modulus = 10 ** len(raw)
    base = int(reference // modulus) * modulus + int(raw)
    candidates = sorted((abs(n - reference), n) for n in (base-modulus, base, base+modulus) if n > 0)
    if not candidates or (len(candidates) > 1 and candidates[0][0] == candidates[1][0]):
        return None
    return candidates[0][1]


def _target(raw: str, zone: tuple[float, float], side: str, previous: float | None) -> float | None:
    explicit = '.' in raw or len(raw) >= 4
    if explicit:
        candidates = [float(raw)]
    else:
        modulus = 10 ** len(raw)
        mid = sum(zone) / 2
        base = int(mid // modulus) * modulus + int(raw)
        candidates = [float(base + i * modulus) for i in (-2, -1, 0, 1, 2)]
    low, high = zone
    # Full absolute prices do not need century expansion. The channel has
    # published a distant 3750 TP for a 3914-3919 entry; retain a bounded
    # sanity check while accepting that observed format.
    max_distance = 250 if explicit else 100
    if side == 'buy':
        viable = [x for x in candidates if x > high and x - high <= max_distance and (previous is None or x > previous)]
    else:
        viable = [x for x in candidates if 0 < x < low and low - x <= max_distance and (previous is None or x < previous)]
    return viable[0] if len(viable) == 1 else None


def target_count(text: str) -> int:
    """Count explicit TP prices without interpreting the price or taking action."""
    return len(_TARGET.findall(text or ""))


def references_previous_targets(text: str) -> bool:
    """Recognize an explicit reference, without inventing a target price."""
    return bool(_PREVIOUS_TP_REFERENCE.search(text or ''))


def channel_stop(text: str) -> tuple[float | None, float | None]:
    """Read an explicit stop price or pip distance, rejecting ambiguous levels."""
    pip_matches = list(_SL_PIPS.finditer(text or ''))
    price_matches = [m for m in _SL.finditer(text or '')
                     if not any(m.start() == p.start() for p in pip_matches)]
    if len(pip_matches) + len(price_matches) > 1:
        raise ValueError('ambiguous_stop')
    pips = float(pip_matches[0].group(1)) if pip_matches else None
    stop = float(price_matches[0].group(1)) if price_matches else None
    if pips is not None and not 0 < pips <= 1000:
        raise ValueError('invalid_stop_distance')
    if stop is not None and (not math.isfinite(stop) or stop <= 0):
        raise ValueError('invalid_stop_price')
    return stop, pips


def stop_only_post(text: str) -> tuple[float | None, float | None] | None:
    """Accept a standalone SL only when it is the whole reply, without extra instructions."""
    value = (text or '').strip()
    if not re.fullmatch(r'SL\s*[:=]?\s*(?:\d+(?:\.\d+)?\s*PIPS?|\d{3,6}(?:\.\d+)?)\s*[.!‼️✅]*', value, re.I):
        return None
    return channel_stop(value)


def entry_identity(text: str) -> tuple[str, str | None, str | None] | None:
    """Extract direction and printed zone for edit-conflict checks without guessing prices."""
    text = text or ""
    reentry = _EXPLICIT_REENTRY.fullmatch(text)
    if reentry:
        zone = _RANGE.search(reentry.group("zone"))
        assert zone is not None
        return ("buy" if reentry.group("side").lower() == "buy" else "sell", zone.group(1), zone.group(2))
    if _BE_MENTION.search(text) or re.search(r'\byop|\bre\s*-?\s*entry\b|\brentery\b', text, re.I):
        return None
    market = _MARKET_NOW.fullmatch(text)
    if market:
        return ("buy" if market.group(1).lower() == "buy" else "sell", None, None)
    personal = _UZ_PERSONAL_ENTRY.fullmatch(text)
    if personal:
        side = personal.group('side_first') or personal.group('side_last')
        zone = _RANGE.search(personal.group('zone_after') or personal.group('zone_before'))
        assert zone is not None
        return ('buy' if side.lower().startswith('buy') else 'sell', zone.group(1), zone.group(2))
    sides, zones = list(_SIDE.finditer(text)), list(_RANGE.finditer(text))
    if len(sides) != 1 or len(zones) != 1 or _OTHER_SYMBOL.search(text):
        return None
    side = "buy" if sides[0].group(1).lower() == "buy" else "sell"
    return side, zones[0].group(1), zones[0].group(2)


def equivalent_entry_revision(original: str, revised: str) -> bool:
    """Prove a zone was expanded without changing its meaning or other instructions.

    An explicit four-digit price anchors the comparison; without one we refuse
    to infer the missing hundreds from a potentially stale market quote.
    TP/SL additions are allowed, but unrelated text changes are not.
    """
    before, after = entry_identity(original), entry_identity(revised)
    if not before or not after or before[0] != after[0]:
        return False
    if before[1] is None or after[1] is None:
        return before == after
    if len(before[1]) < 4 and len(after[1]) < 4:
        return False
    anchor = float(before[1] if len(before[1]) >= 4 else after[1])
    old_post = parse_uz_gold_post(original, quote=anchor, fallback_levels=True)
    new_post = parse_uz_gold_post(revised, quote=anchor, fallback_levels=True)
    if old_post.kind != 'entry' or new_post.kind != 'entry' or old_post.zone is None or old_post.zone != new_post.zone:
        return False
    def body(text: str) -> str:
        text = re.sub(r'\bTP\s*[:=]?\s*\d{1,6}(?:\.\d+)?\b', '', text, flags=re.I)
        text = re.sub(r'\bSL\s*[:=]?\s*\d+(?:\.\d+)?(?:\s*PIPS?)?\b', '', text, flags=re.I)
        return ' '.join(_RANGE.sub('<zone>', text).lower().split())
    return body(original) == body(revised)


def parse_uz_gold_post(text: str, *, quote: float | None, fallback_levels: bool = False) -> GoldZonePost:
    """Interpret a channel post without authorizing a trade or guessing absent SL."""
    text = text or ''
    lower = text.lower()
    if _REENTRY_ANNOUNCEMENT.fullmatch(text):
        # "Stop hit, I will give a re-entry": an announcement, not an order.
        return GoldZonePost('ignored', action='reentry_announced')
    if _BE_MENTION.search(text):
        if _BE_CLOSE.search(text) and not _BE_NONCOMMAND.search(text):
            return GoldZonePost('management', action='close_at_break_even_request')
        if ((_BE_MOVE.search(text) and not _BE_NONCOMMAND.search(text))
                or re.fullmatch(r'\s*b\s*/\s*u\s*[.!‼️❗️✅]*\s*', text, re.I)):
            return GoldZonePost('management', action='break_even_request')
        return GoldZonePost('ignored', action='break_even_mentioned')
    if re.search(r'\byop(?:ing|ish|dik|ildi|amiz|inglar)?\b|\byoping\b', lower):
        return GoldZonePost('management', action='close_request')
    reentry = _EXPLICIT_REENTRY.fullmatch(text)
    if reentry:
        normalized = f"{reentry.group('zone')} {reentry.group('side')} zone{reentry.group('levels')}"
        return parse_uz_gold_post(normalized, quote=quote, fallback_levels=fallback_levels)
    if re.search(r'\bre\s*-?\s*entry\b|\brentery\b', lower):
        return GoldZonePost('management', action='reentry_request')
    personal = _UZ_PERSONAL_ENTRY.fullmatch(text)
    if personal:
        side = personal.group('side_first') or personal.group('side_last')
        zone = personal.group('zone_after') or personal.group('zone_before')
        # Reuse the same price, target and contradiction checks as explicit
        # buy/sell zone posts; no prices are inferred without a fresh quote.
        normalized = f"{zone} {'buy' if side.lower().startswith('buy') else 'sell'}{personal.group('levels')}"
        return parse_uz_gold_post(normalized, quote=quote, fallback_levels=fallback_levels)
    market = _MARKET_NOW.fullmatch(text)
    if market:
        side = 'buy' if market.group(1).lower() == 'buy' else 'sell'
        raw_targets = _TARGET.findall(market.group('levels'))
        ignored_levels: list[str] = []
        try:
            stop, pips = channel_stop(market.group('levels'))
        except ValueError as exc:
            if not fallback_levels:
                return GoldZonePost('invalid', side=side, reason=str(exc))
            stop, pips = None, None
            ignored_levels.append('invalid_stop')
        if not raw_targets and stop is None:
            return GoldZonePost('entry', side=side, sl_pips=pips, action='market_now')
        if isinstance(quote, bool) or not isinstance(quote, (int, float)) or not math.isfinite(quote) or not 100 <= quote <= 10000:
            return GoldZonePost('invalid', side=side, reason='missing_or_invalid_gold_quote')
        if stop is not None and not (stop < quote if side == 'buy' else stop > quote):
            if not fallback_levels:
                return GoldZonePost('invalid', side=side, reason='contradictory_stop')
            stop = None
            ignored_levels.append('invalid_stop')
        reference = (float(quote), float(quote))
        targets: list[float] = []
        verified_first_target = None
        for raw in raw_targets:
            target = _target(raw, reference, side, targets[-1] if targets else None)
            if target is None:
                if not fallback_levels:
                    return GoldZonePost('invalid', side=side, reason='ambiguous_or_contradictory_target')
                verified_first_target = targets[0] if targets else None
                targets.clear()
                ignored_levels.append('invalid_targets')
                break
            targets.append(target)
        return GoldZonePost('entry', side=side, targets=tuple(targets), sl=stop, sl_pips=pips, action='market_now', ignored_levels=tuple(ignored_levels), verified_first_target=verified_first_target)
    sides = list(_SIDE.finditer(text))
    zones = list(_RANGE.finditer(text))
    if not sides or not zones:
        return GoldZonePost('ignored')
    if len(sides) != 1 or len(zones) != 1 or _OTHER_SYMBOL.search(text):
        return GoldZonePost('invalid', reason='ambiguous_direction_or_instrument')
    side = 'buy' if sides[0].group(1).lower() == 'buy' else 'sell'
    if isinstance(quote, bool) or not isinstance(quote, (int, float)) or not math.isfinite(quote) or not 100 <= quote <= 10000:
        return GoldZonePost('invalid', reason='missing_or_invalid_gold_quote')
    first = _expand_first(zones[0].group(1), float(quote))
    second = _expand_suffix(zones[0].group(2), first)
    if second is None or first == second or abs(first-second) > 30:
        return GoldZonePost('invalid', side=side, reason='ambiguous_entry_zone')
    zone = float(min(first, second)), float(max(first, second))
    if min(abs(quote-zone[0]), abs(quote-zone[1])) > 100 and not zone[0] <= quote <= zone[1]:
        return GoldZonePost('invalid', side=side, zone=zone, reason='entry_too_far_from_quote')
    targets: list[float] = []
    ignored_levels: list[str] = []
    verified_first_target = None
    for match in _TARGET.finditer(text):
        target = _target(match.group(1), zone, side, targets[-1] if targets else None)
        if target is None:
            if not fallback_levels:
                return GoldZonePost('invalid', side=side, zone=zone, reason='ambiguous_or_contradictory_target')
            verified_first_target = targets[0] if targets else None
            targets.clear()
            ignored_levels.append('invalid_targets')
            break
        targets.append(target)
    if len(targets) > 4:
        if not fallback_levels:
            return GoldZonePost('invalid', side=side, zone=zone, reason='excess_targets')
        verified_first_target = targets[0] if targets else None
        targets.clear()
        ignored_levels.append('invalid_targets')
    try:
        stop, pips = channel_stop(text)
    except ValueError as exc:
        if not fallback_levels:
            return GoldZonePost('invalid', side=side, zone=zone, reason=str(exc))
        stop, pips = None, None
        ignored_levels.append('invalid_stop')
    if stop is not None and not (stop < zone[0] if side == 'buy' else stop > zone[1]):
        if not fallback_levels:
            return GoldZonePost('invalid', side=side, zone=zone, reason='contradictory_stop')
        stop = None
        ignored_levels.append('invalid_stop')
    return GoldZonePost('entry', side=side, zone=zone, targets=tuple(targets), sl=stop, sl_pips=pips, ignored_levels=tuple(ignored_levels), verified_first_target=verified_first_target)
