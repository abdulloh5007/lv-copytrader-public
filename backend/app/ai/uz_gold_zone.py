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
_OTHER_SYMBOL = re.compile(r'\b(?:BTC|BTCUSD|ETH|ETHUSD|XAG|XAGUSD|EURUSD|GBPUSD)\b', re.I)


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
    if '.' in raw or len(raw) >= 4:
        candidates = [float(raw)]
    else:
        modulus = 10 ** len(raw)
        mid = sum(zone) / 2
        base = int(mid // modulus) * modulus + int(raw)
        candidates = [float(base + i * modulus) for i in (-2, -1, 0, 1, 2)]
    low, high = zone
    if side == 'buy':
        viable = [x for x in candidates if x > high and x - high <= 100 and (previous is None or x > previous)]
    else:
        viable = [x for x in candidates if 0 < x < low and low - x <= 100 and (previous is None or x < previous)]
    return viable[0] if len(viable) == 1 else None


def target_count(text: str) -> int:
    """Count explicit TP prices without interpreting the price or taking action."""
    return len(_TARGET.findall(text or ""))


def entry_identity(text: str) -> tuple[str, str, str] | None:
    """Extract direction and printed zone for edit-conflict checks without guessing prices."""
    text = text or ""
    if re.search(r'\bb\s*/\s*u\b|bezubit|bezub|\byop|\bre\s*-?\s*entry\b|\brentery\b', text, re.I):
        return None
    sides, zones = list(_SIDE.finditer(text)), list(_RANGE.finditer(text))
    if len(sides) != 1 or len(zones) != 1 or _OTHER_SYMBOL.search(text):
        return None
    side = "buy" if sides[0].group(1).lower() == "buy" else "sell"
    return side, zones[0].group(1), zones[0].group(2)


def parse_uz_gold_post(text: str, *, quote: float | None) -> GoldZonePost:
    """Interpret a channel post without authorizing a trade or guessing absent SL."""
    text = text or ''
    lower = text.lower()
    if re.search(r'\bb\s*/\s*u\b|bezubit|bezub', lower):
        return GoldZonePost('management', action='break_even_request')
    if re.search(r'\byop(?:ing|ish|dik|ildi|amiz|inglar)?\b|\byoping\b', lower):
        return GoldZonePost('management', action='close_request')
    if re.search(r'\bre\s*-?\s*entry\b|\brentery\b', lower):
        return GoldZonePost('management', action='reentry_request')
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
    for match in _TARGET.finditer(text):
        target = _target(match.group(1), zone, side, targets[-1] if targets else None)
        if target is None:
            return GoldZonePost('invalid', side=side, zone=zone, reason='ambiguous_or_contradictory_target')
        targets.append(target)
    if len(targets) > 4:
        return GoldZonePost('invalid', side=side, zone=zone, reason='excess_targets')
    pip_matches = list(_SL_PIPS.finditer(text))
    stop_matches = [m for m in _SL.finditer(text) if not any(m.start() == p.start() for p in pip_matches)]
    if len(pip_matches) + len(stop_matches) > 1:
        return GoldZonePost('invalid', side=side, zone=zone, reason='ambiguous_stop')
    pips = float(pip_matches[0].group(1)) if pip_matches else None
    stop = float(stop_matches[0].group(1)) if stop_matches else None
    if pips is not None and not 0 < pips <= 1000:
        return GoldZonePost('invalid', side=side, zone=zone, reason='invalid_stop_distance')
    if stop is not None and not (stop < zone[0] if side == 'buy' else stop > zone[1]):
        return GoldZonePost('invalid', side=side, zone=zone, reason='contradictory_stop')
    return GoldZonePost('entry', side=side, zone=zone, targets=tuple(targets), sl=stop, sl_pips=pips)
