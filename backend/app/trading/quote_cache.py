"""Server-side quote cache for the agent prompt and the entry gate.

A background task on the app server asks the MT5 sidecar for the resolved gold symbol
(the same ``resolve_symbol`` / ``get_quote`` path the tools use) about every two minutes
and appends one sample to ``quote-cache.json`` under ``DATA_DIR``. The file is the
cache: there is no second store.

The ring is one list. When it reaches 20 samples (~40 minutes), the oldest 10 are
deleted and the newest 10 are kept. The next samples grow the list again until it
hits 20, and the trim repeats, so the file stays bounded.

The newest sample is copied into each agent user turn (symbol, bid, ask, mid,
``age_seconds``, timestamp) so the model can decide without a quote round-trip. The
open gate may use a sample only when it is still fresh and the live tick is missing.
A stale or empty cache is not permission to open.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.util.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "QUOTE_CACHE_CAP",
    "QUOTE_CACHE_FILENAME",
    "QUOTE_CACHE_FRESH_SECONDS",
    "QUOTE_CACHE_INTERVAL_SECONDS",
    "QUOTE_CACHE_KEEP",
    "CachedQuote",
    "append_quote",
    "cached_quote_lines",
    "capture_once",
    "fresh_quote",
    "latest_quote",
    "load_samples",
    "quote_cache_path",
    "run_quote_cache",
    "trim_samples",
]

#: How often the background writer asks the sidecar, in seconds.
QUOTE_CACHE_INTERVAL_SECONDS: float = 120.0
#: A cached sample older than this is not evidence for the open gate.
#: One interval plus a minute, so a writer that just slipped still counts.
QUOTE_CACHE_FRESH_SECONDS: float = 180.0
#: When the file reaches this many samples, trim it.
QUOTE_CACHE_CAP: int = 20
#: How many newest samples survive a trim.
QUOTE_CACHE_KEEP: int = 10
QUOTE_CACHE_FILENAME: str = "quote-cache.json"

#: While MT5 is down the writer only watches the status dot. It does not pay an RPC
#: timeout against a sidecar that has not connected yet.
_IDLE_POLL_SECONDS: float = 5.0


@dataclass(frozen=True)
class CachedQuote:
    """One persisted tick. ``cached_at`` is the wall clock when the server stored it."""

    symbol: str
    bid: float
    ask: float
    mid: float
    cached_at: float
    server_time: str


def quote_cache_path(data_dir: str | None = None) -> Path:
    """The cache file. ``data_dir`` overrides ``settings.DATA_DIR`` in tests."""
    root = Path(data_dir) if data_dir else Path(settings.DATA_DIR)
    return root / QUOTE_CACHE_FILENAME


def trim_samples(
    samples: list[dict[str, Any]],
    *,
    cap: int = QUOTE_CACHE_CAP,
    keep: int = QUOTE_CACHE_KEEP,
) -> list[dict[str, Any]]:
    """Keep history bounded.

    Samples are ordered oldest-first. Once the list reaches ``cap`` (20), the oldest
    ``cap - keep`` rows (10) are dropped and the newest ``keep`` rows stay. If a caller
    hands in more than ``cap``, the same drop repeats until the list is under ``cap``.
    """
    ordered = sorted(
        (row for row in samples if isinstance(row, dict)),
        key=lambda row: float(row.get("cached_at") or 0.0),
    )
    if cap <= 0:
        return []
    drop = cap - keep
    if drop <= 0:
        return ordered[-max(keep, 0) :]
    while len(ordered) >= cap:
        ordered = ordered[drop:]
    return ordered


def load_samples(path: Path | None = None) -> list[dict[str, Any]]:
    """Read the cache. A missing or unreadable file is an empty cache, not an error."""
    file = path or quote_cache_path()
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        log.warning("quote_cache_unreadable path=%s error=%s", file, exc)
        return []
    samples = raw.get("samples") if isinstance(raw, dict) else None
    if not isinstance(samples, list):
        return []
    return [row for row in samples if isinstance(row, dict)]


def _write_samples(samples: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"samples": samples}, separators=(",", ":"))
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def append_quote(sample: dict[str, Any], *, path: Path | None = None) -> list[dict[str, Any]]:
    """Append one sample and trim. Returns the samples now on disk, oldest first."""
    file = path or quote_cache_path()
    samples = trim_samples([*load_samples(file), sample])
    _write_samples(samples, file)
    return samples


def _parse_sample(row: dict[str, Any]) -> CachedQuote | None:
    try:
        bid = float(row.get("bid"))
        ask = float(row.get("ask"))
        cached_at = float(row.get("cached_at"))
    except (TypeError, ValueError):
        return None
    symbol = str(row.get("symbol") or "").strip()
    if not symbol or bid <= 0.0 or ask <= 0.0:
        return None
    mid_raw = row.get("mid")
    try:
        mid = float(mid_raw) if mid_raw is not None else (bid + ask) / 2.0
    except (TypeError, ValueError):
        mid = (bid + ask) / 2.0
    return CachedQuote(
        symbol=symbol,
        bid=bid,
        ask=ask,
        mid=mid,
        cached_at=cached_at,
        server_time=str(row.get("server_time") or ""),
    )


def latest_quote(path: Path | None = None) -> CachedQuote | None:
    """The newest sample, however old. The prompt shows its age; the gate does not trust it."""
    best: CachedQuote | None = None
    for row in load_samples(path):
        quote = _parse_sample(row)
        if quote is None:
            continue
        if best is None or quote.cached_at > best.cached_at:
            best = quote
    return best


def fresh_quote(
    symbol: str,
    *,
    now: float | None = None,
    max_age: float = QUOTE_CACHE_FRESH_SECONDS,
    path: Path | None = None,
) -> CachedQuote | None:
    """Newest sample for ``symbol`` that is still young enough to stand in for a live tick."""
    clock = time.time() if now is None else float(now)
    wanted = str(symbol or "").strip().upper()
    if not wanted:
        return None
    best: CachedQuote | None = None
    for row in load_samples(path):
        quote = _parse_sample(row)
        if quote is None or quote.symbol.upper() != wanted:
            continue
        age = clock - quote.cached_at
        if age < 0.0 or age > float(max_age):
            continue
        if best is None or quote.cached_at > best.cached_at:
            best = quote
    return best


def _iso(timestamp: float) -> str:
    try:
        return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def cached_quote_lines(now: float | None = None, *, path: Path | None = None) -> list[str]:
    """The user-turn block. Empty cache still says the open gate will not guess."""
    clock = time.time() if now is None else float(now)
    lines = ["=== CACHED QUOTE ==="]
    quote = latest_quote(path)
    if quote is None:
        lines.append(
            "No cached quote yet. You may call get_quote. The server still refuses an open "
            "it cannot prove is within the entry band of the printed price."
        )
        return lines
    age = max(0, int(round(clock - quote.cached_at)))
    lines.append(
        f"symbol={quote.symbol} bid={quote.bid} ask={quote.ask} mid={quote.mid} "
        f"age_seconds={age} cached_at={_iso(quote.cached_at)} "
        f"server_time={quote.server_time or 'unknown'}"
    )
    lines.append(
        "Newest sample from the server quote cache, refreshed about every 2 minutes. "
        "Decide from this without waiting on a quote tool. You may still call get_quote "
        "later, including after a ladder arrives. Before any open the server re-checks "
        "the entry band and the 15-minute stale gate on a live quote."
    )
    return lines


def _sample_from_quote(quote: dict[str, Any]) -> dict[str, Any] | None:
    try:
        bid = float(quote.get("bid") or 0.0)
        ask = float(quote.get("ask") or 0.0)
    except (TypeError, ValueError):
        return None
    symbol = str(quote.get("symbol") or "").strip()
    if not symbol or bid <= 0.0 or ask <= 0.0:
        return None
    return {
        "symbol": symbol,
        "bid": bid,
        "ask": ask,
        "mid": round((bid + ask) / 2.0, 8),
        "cached_at": time.time(),
        "server_time": str(quote.get("server_time") or ""),
    }


async def capture_once(runtime: Any) -> dict[str, Any] | None:
    """Fetch the resolved XAUUSD quote and append it. None when the broker cannot answer.

    One symbol: whatever ``resolve_symbol("XAUUSD")`` ranks first and marks tradable.
    Active sources do not carry a symbol of their own, and this channel is gold.
    """
    executor = getattr(runtime, "executor", None)
    if executor is None:
        return None
    resolved = await executor.resolve_symbol("XAUUSD")
    if not isinstance(resolved, dict) or not resolved.get("ok"):
        log.info("quote_cache_skip reason=resolve")
        return None
    candidates = resolved.get("candidates")
    name = ""
    if isinstance(candidates, list):
        for row in candidates:
            if isinstance(row, dict) and row.get("tradable", True) and row.get("name"):
                name = str(row["name"])
                break
    if not name:
        log.info("quote_cache_skip reason=no_symbol")
        return None
    quote = await executor.get_quote(name)
    if not isinstance(quote, dict) or not quote.get("ok", True):
        log.info("quote_cache_skip reason=quote symbol=%s", name)
        return None
    sample = _sample_from_quote(quote)
    if sample is None:
        log.info("quote_cache_skip reason=empty symbol=%s", name)
        return None
    append_quote(sample)
    log.info(
        "quote_cache_wrote symbol=%s bid=%s ask=%s mid=%s",
        sample["symbol"],
        sample["bid"],
        sample["ask"],
        sample["mid"],
    )
    return sample


def _mt5_ready(runtime: Any) -> bool:
    status = getattr(runtime, "status", None)
    if not isinstance(status, dict):
        return False
    mt5 = status.get("mt5")
    return bool(isinstance(mt5, dict) and mt5.get("ok"))


async def run_quote_cache(
    runtime: Any,
    *,
    interval: float = QUOTE_CACHE_INTERVAL_SECONDS,
    idle_poll: float = _IDLE_POLL_SECONDS,
) -> None:
    """Write a sample every ``interval`` seconds once MT5 is connected.

    Before the sidecar is up the loop only sleeps. It does not call RPC, so a cold
    boot does not spend the connect timeout on a quote nobody can use yet. The first
    successful status dot captures immediately, then the two-minute cadence starts.
    """
    log.info("quote_cache_started interval_s=%s", interval)
    while True:
        if not _mt5_ready(runtime):
            await asyncio.sleep(idle_poll)
            continue
        try:
            await capture_once(runtime)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the next interval retries
            log.warning("quote_cache_capture_failed error=%s", exc)
        await asyncio.sleep(interval)
