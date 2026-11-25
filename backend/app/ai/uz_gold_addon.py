"""Recognition and price checks for a channel's explicit add-on instruction.

This module never sends an order. Linking to the original broker position,
deduplication, protection and all broker checks are the executor's job.
"""
from __future__ import annotations

import math
import re

_ADDON = re.compile(
    r'^\s*(?P<price>\d{2,5}(?:\.\d+)?)\s*dalivka'
    r'(?:\s+(?:minimal|min)\s+risk)?'
    r'(?:\s+(?:(?P<stop_before>\d{2,5}(?:\.\d+)?)\s*sl|'
    r'sl\s*[:=]?\s*(?P<stop_after>\d{2,5}(?:\.\d+)?)))?'
    r'\s*[.!‼️✅]*\s*$', re.I
)


def is_addon_post(text: str) -> bool:
    return _ADDON.fullmatch(text or '') is not None


def _resolve_price(raw: str, reference: float) -> float | None:
    if not math.isfinite(reference) or reference <= 0:
        return None
    if '.' in raw or len(raw) >= 4:
        result = float(raw)
    else:
        modulus = 10 ** len(raw)
        base = int(reference // modulus) * modulus + int(raw)
        candidates = sorted((abs(value - reference), value)
                            for value in (base - modulus, base, base + modulus) if value > 0)
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            return None
        result = float(candidates[0][1])
    return result if 0 < result and abs(result - reference) <= 30 else None


def addon_price(text: str, *, reference: float) -> float | None:
    """Resolve an explicit add-on price without guessing its direction."""
    match = _ADDON.fullmatch(text or '')
    return _resolve_price(match.group('price'), reference) if match else None


def addon_channel_stop(text: str, *, reference: float) -> float | None:
    """Resolve an explicitly printed stop against the add-on entry zone."""
    match = _ADDON.fullmatch(text or '')
    if not match:
        raise ValueError('invalid add-on post')
    raw = match.group('stop_before') or match.group('stop_after')
    if raw is None:
        return None
    stop = _resolve_price(raw, reference)
    if stop is None:
        raise ValueError('ambiguous add-on stop')
    return stop
