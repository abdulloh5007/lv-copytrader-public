"""Recognition and price checks for a channel's explicit add-on instruction.

This module never sends an order. Linking to the original broker position,
deduplication, protection and all broker checks are the executor's job.
"""
from __future__ import annotations

import math
import re

_ADDON = re.compile(
    r'^\s*(?P<price>\d{2,5}(?:\.\d+)?)\s+dalivka'
    r'(?:\s+(?:minimal|min)\s+risk)?\s*[.!‼️✅]*\s*$', re.I
)


def is_addon_post(text: str) -> bool:
    return _ADDON.fullmatch(text or '') is not None


def addon_price(text: str, *, reference: float) -> float | None:
    """Resolve an explicit add-on price against the original entry, without guessing sides."""
    match = _ADDON.fullmatch(text or '')
    if not match or not math.isfinite(reference) or reference <= 0:
        return None
    raw = match.group('price')
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
