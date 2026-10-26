"""Symbol resolution, pip conventions, quotes, and broker-clock arithmetic.

The model reads ``buy gold, sl 20 pips``. It does not know the broker calls that symbol
``XAUUSDm``, it does not know what a "pip" is worth there, and it must never be the thing
that multiplies floats. Everything in that sentence except the intent is resolved here.

Three ideas carry the module:

* **Resolution is a ranked list, never a silent guess.** ``candidates()`` runs four passes
  in a fixed order — exact name, exact after a broker suffix is stripped, alias expansion,
  substring on name or description — and hands the model the union, ranked by pass order.
  The model picks. ``closest()`` exists so that *zero* matches still produce a usable hint.
* **The pip convention is explicit and auditable.** ``pip_convention()`` names the branch
  that fired, so a wrong stop distance is traceable to a rule rather than to a guess, and
  ``PIP_OVERRIDES`` in the config corrects any symbol without touching this code.
* **The broker's clock is not this machine's clock.** Brokers sit on quarter-hour offsets
  from UTC, MT5 reports deal and tick times on that clock, and "today's realized P/L" is
  meaningless without it. ``server_time_offset()`` measures it from a live tick.

Every value read off an RPC result is read as an attribute (``si.digits``), never with
``dict.get()``: a missing field must raise ``AttributeError`` here rather than become a
silent ``None`` three layers down in the money path.
"""

from __future__ import annotations

import asyncio
import difflib
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Final

from app.config import Settings
from app.config import settings as _settings
from app.errors import TradingError
from app.trading.constants import SYMBOL_TRADE_MODE_FULL
from app.util.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - imported for type checkers only
    from app.trading.rpc import Mt5Rpc

log = get_logger(__name__)


# --------------------------------------------------------------------------------------
# Broker suffixes.
#
# The same instrument is XAUUSD, XAUUSDm, XAUUSD.raw, XAUUSDc or XAUUSD#, depending on the
# broker and the account type. The first eight entries are the set named in BUILD_PROMPT
# §2; the rest are the separator-carrying variants brokers ship alongside them. Stripping
# is case-insensitive and longest-suffix-first, so ".pro" is never mistaken for "p" + "ro".
# --------------------------------------------------------------------------------------
BROKER_SUFFIXES: tuple[str, ...] = (
    "m",
    # "r" is Exness's Raw-Spread suffix and it is NOT optional trivia: the live
    # Exness-MT5Trial15 demo this was tested against quotes XAUUSDr / EURUSDr / XAUEURr,
    # with no "m" variants at all. Without this entry resolve_symbol("gold") walks the
    # whole alias table, fails to strip the suffix from the candidate, and reports
    # "no symbol matches 'gold'" for an instrument sitting right there in symbols_get().
    "r",
    "c",
    ".raw",
    "_i",
    "#",
    ".pro",
    "-ECN",
    ".stp",
    ".m",
    "_m",
    "-m",
    ".r",
    "_r",
    "-r",
    ".ecn",
    "z",
    "p",
    "e",
    "s",
    "_pro",
    ".a",
)

# Upper-cased and ordered longest-first: that order is what makes the stripping
# unambiguous, and it is computed once instead of on every comparison.
_SUFFIXES_LONGEST_FIRST: Final[tuple[str, ...]] = tuple(
    sorted({suffix.upper() for suffix in BROKER_SUFFIXES}, key=lambda s: (-len(s), s))
)

# Refuse to strip a suffix that would leave a stump: "USDm" -> "USD" is fine, "Um" is not.
_MIN_STEM_LENGTH: Final[int] = 3


# --------------------------------------------------------------------------------------
# Aliases.
#
# What a signal channel writes ("gold", "nas", "dow", "brent") against what the broker
# calls it. Each key maps to every standard spelling worth trying, because the same index
# is US30 at one broker, DJ30 at the next and WS30 at the third — and the alias targets are
# themselves put through the exact and suffix passes, so "GOLD" reaches "XAUUSDm".
# --------------------------------------------------------------------------------------
ALIASES: dict[str, tuple[str, ...]] = {
    # --- metals ---
    "GOLD": ("XAUUSD",),
    "XAU": ("XAUUSD",),
    "SILVER": ("XAGUSD",),
    "XAG": ("XAGUSD",),
    "PLATINUM": ("XPTUSD",),
    "XPT": ("XPTUSD",),
    "PALLADIUM": ("XPDUSD",),
    "XPD": ("XPDUSD",),
    # --- US indices ---
    "US30": ("US30", "DJ30", "WS30"),
    "DOW": ("US30", "DJ30", "WS30"),
    "DOWJONES": ("US30", "DJ30", "WS30"),
    "DJ30": ("US30", "DJ30", "WS30"),
    "DJI": ("US30", "DJ30", "WS30"),
    "WS30": ("US30", "DJ30", "WS30"),
    "NAS100": ("USTEC", "NAS100", "NDX100"),
    "USTEC": ("USTEC", "NAS100", "NDX100"),
    "NDX": ("USTEC", "NAS100", "NDX100"),
    "NDX100": ("USTEC", "NAS100", "NDX100"),
    "NAS": ("USTEC", "NAS100", "NDX100"),
    "NASDAQ": ("USTEC", "NAS100", "NDX100"),
    "US100": ("USTEC", "NAS100", "NDX100"),
    "SPX": ("US500", "SPX500"),
    "SPX500": ("US500", "SPX500"),
    "SP500": ("US500", "SPX500"),
    "US500": ("US500", "SPX500"),
    # --- European and Asian indices ---
    "GER40": ("GER40", "DE40"),
    "DE40": ("GER40", "DE40"),
    "DAX": ("GER40", "DE40", "GER30", "DE30"),
    "GER30": ("GER40", "DE40", "GER30", "DE30"),
    "UK100": ("UK100", "FTSE100"),
    "FTSE": ("UK100", "FTSE100"),
    "JP225": ("JP225", "JPN225", "NIKKEI225"),
    "JPN225": ("JP225", "JPN225", "NIKKEI225"),
    "NIKKEI": ("JP225", "JPN225", "NIKKEI225"),
    # --- crypto ---
    "BTC": ("BTCUSD",),
    "BITCOIN": ("BTCUSD",),
    "ETH": ("ETHUSD",),
    "ETHEREUM": ("ETHUSD",),
    "XRP": ("XRPUSD",),
    "LTC": ("LTCUSD",),
    # --- energy ---
    "OIL": ("USOIL", "XTIUSD", "WTI"),
    "WTI": ("USOIL", "XTIUSD", "WTI"),
    "CRUDE": ("USOIL", "XTIUSD", "WTI"),
    "USOIL": ("USOIL", "XTIUSD", "WTI"),
    "BRENT": ("UKOIL", "XBRUSD"),
    "UKOIL": ("UKOIL", "XBRUSD"),
    # --- FX slang that shows up in signal channels ---
    "CABLE": ("GBPUSD",),
    "FIBER": ("EURUSD",),
    "EURO": ("EURUSD",),
}


# A symbol is a metal when its name carries one of these, or when the broker's description
# says so. Metals quote with digits=2 and point=0.01, where one pip is 0.10 — ten points.
_METAL_NAME_TOKENS: Final[tuple[str, ...]] = ("XAU", "XAG", "XPT", "XPD", "GOLD", "SILVER")
_METAL_DESCRIPTION_TOKENS: Final[tuple[str, ...]] = ("GOLD", "SILVER")

# A one-character query would substring-match most of the table; the earlier passes are
# exact and stay available for it.
_MIN_SUBSTRING_QUERY: Final[int] = 2

# How far down the ranking to keep looking when a candidate cannot be converted.
_CANDIDATE_SCAN_FACTOR: Final[int] = 3

# difflib cutoff for closest(); below this the fallback ranking takes over so the hint is
# never empty.
_CLOSEST_CUTOFF: Final[float] = 0.4

# An unselected symbol ticks zeros rather than erroring; give the terminal one chance to
# deliver a real tick before calling the market closed.
_ZERO_TICK_RETRY_SECONDS: Final[float] = 0.5

_SECONDS_PER_DAY: Final[float] = 86400.0

_WHITESPACE_OR_SLASH = re.compile(r"[\s/]+")


def _compact(text: str) -> str:
    """Collapse a symbol-ish string to its comparison key: no spaces, no slashes."""
    return _WHITESPACE_OR_SLASH.sub("", text)


def _strip_suffix(key: str) -> str:
    """Strip one broker suffix from an upper-case key, longest suffix first."""
    for suffix in _SUFFIXES_LONGEST_FIRST:
        if key.endswith(suffix):
            stem = key[: -len(suffix)]
            if len(stem) >= _MIN_STEM_LENGTH:
                return stem
    return key


def _group_path(path: str, name: str) -> str:
    """Return the broker's group for a symbol: its ``path`` minus the symbol leaf.

    MT5 reports ``path`` as the full Market Watch node — ``Raw\\Forex\\XAUUSDr`` — where only
    ``Raw\\Forex`` is the folder the operator navigates. The leaf is stripped only when it
    really is this symbol's name, so a broker that already stores the group alone keeps it.
    """
    text = str(path or "").strip()
    if not text:
        return ""
    leaf = str(name or "").strip()
    if leaf:
        for separator in ("\\", "/"):
            tail = f"{separator}{leaf}"
            if text.upper().endswith(tail.upper()):
                return text[: -len(tail)]
    return text


#: Rank bands used by :meth:`SymbolResolver.catalog`, best first. They are the same
#: equivalence the resolution passes use — exact, exact-after-a-suffix, then looser — so a
#: picker search and a signal's symbol resolution agree about what "XAUUSD" means.
_CATALOG_RANK_EXACT: Final[int] = 0
_CATALOG_RANK_STEM: Final[int] = 1
_CATALOG_RANK_ALIAS: Final[int] = 2
_CATALOG_RANK_PREFIX: Final[int] = 3
_CATALOG_RANK_NAME: Final[int] = 4
_CATALOG_RANK_DESCRIPTION: Final[int] = 5
_CATALOG_RANK_GROUP: Final[int] = 6


def _catalog_rank(
    ref: "SymbolRef", q_upper: str, q_key: str, q_stem: str, aliases: frozenset[str]
) -> int | None:
    """Score one symbol against a picker query, or return None when it does not match.

    An empty query matches everything at the top band, which is how the picker lists the
    whole table. Name comparisons run on the compacted upper-case key and on that key with
    one broker suffix removed, so the match survives the suffix in both directions; the
    description and the broker's group are matched on the raw upper-cased text, because a
    query like "us dollar" or "crypto" is prose, not a symbol name.

    ``aliases`` is :attr:`ALIASES` already expanded for this query, and it earns its own band
    directly under the suffix match — the same position the resolution passes give it. It is
    what makes the operator's "gold" put ``XAUUSDr`` at the top rather than ``BTCXAUr``,
    whose description merely mentions gold, so the picker and the model agree on what the
    word means.
    """
    if not q_key and not q_upper:
        return _CATALOG_RANK_EXACT

    name_key = _compact(ref.name.upper())
    name_stem = _strip_suffix(name_key)
    forms = {name_key, name_stem}
    wanted = {q_key, q_stem} - {""}

    if name_key == q_key:
        return _CATALOG_RANK_EXACT
    if forms & wanted:
        return _CATALOG_RANK_STEM
    if aliases and forms & aliases:
        return _CATALOG_RANK_ALIAS
    if any(form.startswith(needle) for form in forms for needle in wanted):
        return _CATALOG_RANK_PREFIX
    if any(needle in form for form in forms for needle in wanted):
        return _CATALOG_RANK_NAME
    if q_upper and q_upper in ref.description.upper():
        return _CATALOG_RANK_DESCRIPTION
    # Last, and last on purpose: the broker's folder is how an operator asks for a whole
    # asset class ("crypto", "indices") when they do not know one name in it. It ranks below
    # every name and description hit so a group query can never displace the symbol someone
    # actually typed.
    if q_upper and q_upper in ref.path.upper():
        return _CATALOG_RANK_GROUP
    return None


def _as_rows(result: Any) -> tuple[Any, ...]:
    """Normalise whatever ``symbols_get()`` returned into a tuple of symbol rows."""
    if result is None:
        return ()
    if isinstance(result, (list, tuple)):
        return tuple(result)
    return (result,)


#: How brokers spell "priced in" between the two sides of a pair.
_PAIR_SEPARATORS: Final[tuple[str, ...]] = (" VS ", " VS. ", " VERSUS ", "/", " / ", " TO ", " AGAINST ")


def _base_side(description: str) -> str:
    """Return the part of a description BEFORE the pair separator — the instrument itself.

    "Gold vs US Dollar" -> "GOLD", "Spot Gold" -> "SPOT GOLD", "Bitcoin vs Gold" -> "BITCOIN".
    Position alone is not enough here: a broker that calls gold "Spot Gold" or "Gold Spot" is
    naming the instrument, while "Bitcoin vs Gold" is naming what it is priced in, and only the
    separator tells the two apart.
    """
    text = description.upper().strip()
    for separator in _PAIR_SEPARATORS:
        index = text.find(separator)
        if index > 0:
            return text[:index].strip()
    return text


def _is_metal(si: Any) -> bool:
    """Report whether a symbol is a precious metal, by name first and description second."""
    # Position, not mere presence: a metal quote has the metal as its BASE. BTCXAUr is
    # Bitcoin priced in gold — treating it as a metal would send it down the metal pip branch
    # and out the other side as "unresolved", when its 5 digits describe it perfectly well.
    name = _strip_suffix(str(si.name).upper())
    if any(name.startswith(token) for token in _METAL_NAME_TOKENS):
        return True
    base = _base_side(str(si.description or ""))
    return any(token in base for token in _METAL_DESCRIPTION_TOKENS)


#: What one pip is worth for each metal, independent of how many decimals the broker quotes.
#: A trader saying "20 pips on gold" means 2.00 in price on every feed; the digit count only
#: changes the tick, never the pip.
_METAL_PIPS: Final[tuple[tuple[tuple[str, ...], float, str], ...]] = (
    (("XAU", "GOLD"), 0.10, "metal_gold_0.10"),
    (("XAG", "SILVER"), 0.01, "metal_silver_0.01"),
    (("XPT", "PLATINUM"), 0.10, "metal_platinum_0.10"),
    (("XPD", "PALLADIUM"), 0.10, "metal_palladium_0.10"),
)


def _metal_pip(si: Any) -> tuple[float, str]:
    """Return the pip for a precious metal, or refuse rather than guess.

    A cross like BTCXAU is not a metal quote in the sense that matters here -- its pip is
    whatever the base instrument's is -- so an unrecognised metal falls through to a
    TradingError naming PIP_OVERRIDES instead of silently inheriting gold's.
    """
    # The metal must be the BASE of the pair, not merely mentioned in it. A substring search
    # gives BTCXAUr — Bitcoin priced in gold, quoted to 5 digits — gold's 0.10 pip, which is
    # wrong by three orders of magnitude and exactly the mislabel this function's docstring
    # already promised to avoid. Position is the discriminator: XAUUSDr starts with the metal,
    # BTCXAUr does not. The description is consulted the same way, so "Gold vs US Dollar"
    # qualifies and "Bitcoin vs Gold" does not.
    name = _strip_suffix(str(si.name).upper())
    base = _base_side(str(si.description or ""))
    for tokens, pip, convention in _METAL_PIPS:
        if any(name.startswith(token) for token in tokens):
            return pip, convention
        if any(token in base for token in tokens):
            return pip, convention
    raise TradingError(
        f"no pip convention for the metal {si.name}: set PIP_OVERRIDES"
        f"[{str(si.name).upper()!r}] to its pip size",
        detail={"symbol": str(si.name), "digits": int(si.digits), "point": float(si.point)},
    )


def _pip_branch(si: Any, cfg: Settings) -> tuple[float, str]:
    """Return ``(pip_size, convention)`` for a symbol, naming the branch that decided it.

    The order is the one in BUILD_PROMPT §2 and it is load-bearing: getting a pip wrong
    scales every stop by ten. ``digits == 3`` is the one ambiguous case — it is a JPY pair
    at almost every broker but a three-decimal metal at a few — so the metal test runs
    first *within* that branch. Both produce ``point * 10``; only the reported convention
    differs, which is exactly what makes the choice auditable afterwards.
    """
    name = str(si.name).upper()
    override = cfg.PIP_OVERRIDES.get(name)
    if override is not None:
        return float(override), "override"

    digits = int(si.digits)
    point = float(si.point)

    if _is_metal(si):
        # Metals are anchored to the metal, NOT to a multiple of `point`, because brokers
        # quote the same metal to different depths. Measured on the live Exness demo:
        # XAUUSDr is digits=3 / point=0.001, while the classic quote is digits=2 / point=0.01.
        # `point * 10` gives 0.10 on the 2-digit feed and 0.01 on the 3-digit one -- the same
        # instrument with a pip differing by a factor of ten, which is precisely the error
        # BUILD_PROMPT §2 says "scales every stop by ten".
        # Gold, platinum and palladium trade in 0.10 pips; silver in 0.01.
        return _metal_pip(si)
    if digits in (3, 5):
        if digits == 5:
            return point * 10.0, "fx_5digit"
        # digits == 3 is a JPY pair at almost every broker, but a 3-decimal oil or index
        # CFD lands here too. The pip size is the same either way; only the label differs,
        # and the label is the whole point of an auditable convention -- calling USOILm a
        # JPY pair would be a lie in the record the operator reads when a stop looks wrong.
        return point * 10.0, "fx_3digit_jpy" if "JPY" in name else "three_digit_x10"
    if digits in (2, 4):
        return point, "point"

    raise TradingError(
        f"no pip convention for {si.name} (digits={digits}) — "
        f"set PIP_OVERRIDES[{name}] to this symbol's pip size to trade it",
        detail={"symbol": str(si.name), "digits": digits, "point": point},
    )


def pip_size(si: Any, *, settings: Settings | None = None) -> float:
    """Return the price distance of one pip for a symbol_info namespace."""
    return _pip_branch(si, settings or _settings)[0]


def pip_convention(si: Any, *, settings: Settings | None = None) -> str:
    """Return the name of the rule that decided this symbol's pip size."""
    return _pip_branch(si, settings or _settings)[1]


@dataclass(frozen=True)
class SymbolRef:
    """Everything the trading layer needs about one symbol, resolved once and frozen.

    Built from a ``symbol_info`` namespace, never from the raw RPC dict, so a broker that
    stops returning a field fails loudly at construction instead of silently sizing a
    trade off ``None`` later.
    """

    name: str
    description: str
    digits: int
    point: float
    pip_size: float
    pip_convention: str
    volume_min: float
    volume_step: float
    volume_max: float
    stops_level_points: int
    freeze_level_points: int
    spread_points: int
    trade_tick_size: float
    trade_tick_value: float
    trade_tick_value_loss: float
    contract_size: float
    currency_profit: str
    currency_margin: str
    trade_mode: int
    trade_exemode: int
    filling_mode: int
    visible: bool
    #: The broker's own Market Watch group -- "Raw\Forex" on the live Exness demo, with the
    #: symbol leaf that MT5 appends to ``symbol_info().path`` already removed. It is what makes
    #: a table of several hundred symbols navigable, and it is the ONE field here read with
    #: ``getattr``: it names a folder, it never reaches an order, and a broker that omits it
    #: must leave a tradable symbol listed without a group rather than drop it from the list.
    path: str = ""

    @classmethod
    def from_info(cls, si: Any, settings: Settings) -> "SymbolRef":
        """Build a reference from one ``symbol_info`` result, deciding the pip convention."""
        try:
            size, convention = _pip_branch(si, settings)
        except TradingError:
            # An undecidable pip convention must NOT delete the symbol. digits not in
            # {2,3,4,5} is ordinary -- indices quoted to one decimal (US30 at 38950.5),
            # JP225 and many BTCUSD feeds at zero -- and §2's alias table mandates exactly
            # those instruments. Dropping the candidate made resolve_symbol answer
            # "no symbol matches 'US30'" for a tradable symbol sitting in the table, with the
            # real reason and its remedy buried in a swallowed exception the model never sees.
            # Carry it instead, with pip_size 0.0: pricing._pip_of refuses to divide by that
            # and says why, so pip-denominated distances fail loudly while absolute prices,
            # quotes and orders keep working.
            size, convention = 0.0, "unresolved"
        return cls(
            name=str(si.name),
            description=str(si.description or ""),
            digits=int(si.digits),
            point=float(si.point),
            pip_size=size,
            pip_convention=convention,
            volume_min=float(si.volume_min),
            volume_step=float(si.volume_step),
            volume_max=float(si.volume_max),
            stops_level_points=int(si.trade_stops_level),
            freeze_level_points=int(si.trade_freeze_level),
            spread_points=int(si.spread),
            trade_tick_size=float(si.trade_tick_size),
            trade_tick_value=float(si.trade_tick_value),
            trade_tick_value_loss=float(si.trade_tick_value_loss),
            contract_size=float(si.trade_contract_size),
            currency_profit=str(si.currency_profit),
            currency_margin=str(si.currency_margin),
            trade_mode=int(si.trade_mode),
            trade_exemode=int(si.trade_exemode),
            filling_mode=int(si.filling_mode),
            visible=bool(si.visible),
            path=_group_path(str(getattr(si, "path", "") or ""), str(si.name)),
        )

    @property
    def tradable(self) -> bool:
        """Report whether the broker allows both directions on this symbol right now."""
        return self.trade_mode == SYMBOL_TRADE_MODE_FULL

    def to_dict(self) -> dict[str, Any]:
        """Return exactly the row shape ``resolve_symbol`` shows the model."""
        row: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "digits": self.digits,
            "point": self.point,
            "pip_size": self.pip_size,
            "pip_convention": self.pip_convention,
            "volume_min": self.volume_min,
            "volume_step": self.volume_step,
            "volume_max": self.volume_max,
            "stops_level_points": self.stops_level_points,
            "freeze_level_points": self.freeze_level_points,
            "spread_points": self.spread_points,
            "tradable": self.tradable,
        }
        if self.pip_convention == "unresolved":
            row["pip_note"] = (
                f"this broker quotes {self.name} to {self.digits} decimals, which matches no "
                f"standard pip convention, so pip-denominated distances are unavailable for it. "
                f"Quote stops and targets as absolute prices or in points "
                f"(1 point = {self.point}), or have the operator set "
                f"PIP_OVERRIDES['{self.name.upper()}'] to this symbol's pip size."
            )
        return row


@dataclass(frozen=True)
class Quote:
    """One live tick, with the spread pre-converted into both units the model reads."""

    symbol: str
    bid: float
    ask: float
    spread_points: int
    spread_pips: float
    time_ts: float
    server_time: str

    def to_dict(self) -> dict[str, Any]:
        """Return the tool-facing shape of ``get_quote``."""
        return {
            "symbol": self.symbol,
            "bid": self.bid,
            "ask": self.ask,
            "spread_points": self.spread_points,
            "spread_pips": round(self.spread_pips, 4),
            "server_time": self.server_time,
            "time_ts": self.time_ts,
        }


def _server_time_iso(ts: float) -> str:
    """Render a broker-clock unix timestamp as a naive ISO string.

    MT5 reports tick and deal times as seconds since the epoch *on the broker's clock*, so
    formatting them as UTC yields the broker's wall clock. The result is deliberately naive
    and carries no ``Z``: it is the server's local reading, not UTC.
    """
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).replace(tzinfo=None).isoformat(
            timespec="seconds"
        )
    except (OverflowError, OSError, ValueError):
        return ""


def _tick_is_live(tick: Any) -> bool:
    """Report whether a tick carries a real two-sided price rather than the zeros MT5 returns."""
    if tick is None:
        return False
    return float(tick.bid) > 0.0 and float(tick.ask) > 0.0


class SymbolResolver:
    """Caches the broker's symbol table and turns a human query into ranked candidates."""

    def __init__(self, rpc: "Mt5Rpc", settings: Settings) -> None:
        self._rpc = rpc
        self._settings = settings
        self._rows: tuple[Any, ...] = ()
        self._by_name: dict[str, Any] = {}
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    # -- cache ---------------------------------------------------------------------

    @property
    def count(self) -> int:
        """Return how many symbols are currently cached."""
        return len(self._rows)

    async def refresh(self, force: bool = False) -> int:
        """Re-pull ``symbols_get()`` when the cache is older than SYMBOL_CACHE_SECONDS."""
        async with self._lock:
            age = time.monotonic() - self._fetched_at
            if not force and self._rows and age < float(self._settings.SYMBOL_CACHE_SECONDS):
                return len(self._rows)
            rows = _as_rows(await self._rpc.symbols_get())
            by_name: dict[str, Any] = {}
            for row in rows:
                by_name.setdefault(str(row.name).upper(), row)
            self._rows = rows
            self._by_name = by_name
            self._fetched_at = time.monotonic()
            log.info("symbol table refreshed: %d symbols", len(rows))
            return len(rows)

    async def _ensure(self) -> None:
        """Make sure the cache is warm and fresh before reading it."""
        await self.refresh(False)

    # -- resolution ----------------------------------------------------------------

    def _alias_targets(self, *keys: str) -> list[str]:
        """Expand every alias hit for the given query keys, in table order, deduplicated."""
        out: list[str] = []
        seen: set[str] = set()
        for key in keys:
            for target in ALIASES.get(key, ()):
                upper = target.upper()
                if upper not in seen:
                    seen.add(upper)
                    out.append(upper)
        return out

    def _rank(self, text: str) -> list[str]:
        """Run the four passes over the cached table and return real names, best first.

        A symbol keeps the *lowest* pass index it matched under, and the final order is
        (pass index, name length, name): an exact hit always outranks a substring one, and
        among equals the shortest name wins, which is what puts ``XAUUSDm`` above
        ``XAUUSDm.pro`` for the query "gold".
        """
        q_upper = text.upper()
        q_key = _compact(q_upper)
        q_stem = _strip_suffix(q_key)
        best: dict[str, int] = {}

        def record(real: str, pass_index: int) -> None:
            current = best.get(real)
            if current is None or pass_index < current:
                best[real] = pass_index

        # Passes 1 and 2 — exact, then exact once a broker suffix is stripped. The suffix
        # comes off the candidate ("XAUUSD" finds "XAUUSDm") and, symmetrically, off the
        # query ("XAUUSDm" finds "XAUUSD.pro"), which is the same equivalence read backwards.
        for name_upper, row in self._by_name.items():
            real = str(row.name)
            key = _compact(name_upper)
            if key == q_key:
                record(real, 1)
                continue
            stem = _strip_suffix(key)
            if stem == q_key or key == q_stem or (stem and stem == q_stem):
                record(real, 2)

        # Pass 3 — alias expansion, then passes 1 and 2 again on each alias target.
        targets = self._alias_targets(q_key, q_stem)
        if targets:
            for name_upper, row in self._by_name.items():
                real = str(row.name)
                key = _compact(name_upper)
                stem = _strip_suffix(key)
                for target in targets:
                    if key == target or stem == target:
                        record(real, 3)
                        break

        # Pass 4 — substring on the description or the name. This is the pass that finds
        # "XAUUSDm" from "gold" at a broker whose description reads "Gold vs US Dollar".
        if len(q_upper) >= _MIN_SUBSTRING_QUERY:
            for name_upper, row in self._by_name.items():
                real = str(row.name)
                if q_upper in name_upper or q_key in name_upper:
                    record(real, 4)
                    continue
                if q_upper in str(row.description or "").upper():
                    record(real, 4)

        return sorted(best, key=lambda name: (best[name], len(name), name.upper()))

    async def _build_ref(self, name: str) -> SymbolRef | None:
        """Convert one survivor into a SymbolRef, or return None if it cannot be converted.

        A fresh ``symbol_info`` call is what makes the spread current; the cached row is the
        fallback when that call fails. A symbol whose pip convention cannot be decided is
        skipped with a warning rather than raised, so one exotic instrument in the table
        cannot break the resolution of an unrelated query.
        """
        info: Any = None
        try:
            info = await self._rpc.symbol_info(name)
        except TradingError as exc:
            log.warning("symbol_info(%s) failed, using the cached row: %s", name, exc)
        if info is None:
            info = self._by_name.get(name.upper())
        if info is None:
            return None
        try:
            return SymbolRef.from_info(info, self._settings)
        except (TradingError, AttributeError, TypeError, ValueError) as exc:
            log.warning("skipping symbol %s: %s", name, exc)
            return None

    async def candidates(self, query: str) -> list[SymbolRef]:
        """Return up to MAX_SYMBOL_CANDIDATES ranked matches for a free-form query."""
        text = (query or "").strip()
        if not text:
            return []
        await self._ensure()
        ranked = self._rank(text)
        if not ranked:
            return []
        cap = max(1, int(self._settings.MAX_SYMBOL_CANDIDATES))
        out: list[SymbolRef] = []
        # Only the survivors are converted: the table holds hundreds of symbols and
        # building a SymbolRef for each would cost one RPC round-trip per symbol.
        for name in ranked[: cap * _CANDIDATE_SCAN_FACTOR]:
            ref = await self._build_ref(name)
            if ref is not None:
                out.append(ref)
                if len(out) >= cap:
                    break
        return out

    async def catalog(
        self,
        *,
        query: str = "",
        only_tradable: bool = False,
        limit: int | None = None,
    ) -> tuple[list[SymbolRef], int]:
        """Return the account's symbol table as a filtered, ranked page plus the match total.

        This is the operator's picker, not a trading decision, and the two want different
        things from this module:

        * :meth:`candidates` re-reads ``symbol_info`` for each survivor because a spread
          quoted to the model has to be current. A real Exness account carries several
          hundred symbols and the picker shows all of them, so this builds every row from
          the cached ``symbols_get`` snapshot instead — one RPC round trip, not one per row.
        * :meth:`candidates` answers "which symbol did the signal mean" and stops at eight.
          This answers "what can this account trade", so the caller sets its own page size
          and gets ``total`` back beside the page: a picker that silently showed the first
          N would tell the operator a symbol does not exist when it simply did not fit.

        ``query`` matches the name and the description case-insensitively, and it matches
        across a broker suffix in both directions, so ``XAUUSD`` finds ``XAUUSDr`` and
        ``XAUUSDr`` finds ``XAUUSD``. Ranking puts an exact name first, then an exact name
        once the suffix is off, then an :attr:`ALIASES` hit (so the operator's "gold" leads
        with ``XAUUSDr``, exactly as it does for the model), then a prefix, any substring of
        the name, a description hit, and last the broker's group; ties break on that group
        and then the name, which is the order the picker renders. That ranking is what makes
        truncation safe: the page that survives a clip is the relevant end of the list.

        Returns ``(rows, total)`` where ``total`` counts every symbol that matched before
        the page size was applied. The caller reports ``truncated`` from the difference.
        """
        await self._ensure()
        text = str(query or "").strip()
        q_upper = text.upper()
        q_key = _compact(q_upper)
        q_stem = _strip_suffix(q_key)

        aliases = frozenset(self._alias_targets(q_key, q_stem))

        scored: list[tuple[int, str, str, SymbolRef]] = []
        for row in self._rows:
            try:
                ref = SymbolRef.from_info(row, self._settings)
            except (TradingError, AttributeError, TypeError, ValueError) as exc:
                # One unreadable instrument must not empty the picker: it is logged and
                # left out, exactly as _build_ref does for resolution.
                log.warning("skipping symbol %r in catalog: %s", getattr(row, "name", "?"), exc)
                continue
            if only_tradable and not ref.tradable:
                continue
            rank = _catalog_rank(ref, q_upper, q_key, q_stem, aliases)
            if rank is None:
                continue
            scored.append((rank, ref.path.upper(), ref.name.upper(), ref))

        scored.sort(key=lambda item: (item[0], item[1], item[2]))
        total = len(scored)
        page = total if limit is None else max(0, int(limit))
        return [item[3] for item in scored[:page]], total

    async def closest(self, query: str, n: int = 3) -> list[str]:
        """Return the n closest symbol names by edit distance, even when nothing matched.

        This is the ``hint`` on a failed resolution, so it must produce something for a
        query that scored zero in every pass. ``difflib`` decides it, which keeps the whole
        thing pure-stdlib; below the 0.4 cutoff it falls back to a plain ratio ranking so
        the model always gets names to try rather than an empty list.
        """
        limit = max(0, int(n))
        if limit == 0:
            return []
        text = (query or "").strip()
        if not text:
            return []
        await self._ensure()
        upper_to_real: dict[str, str] = {}
        for row in self._rows:
            real = str(row.name)
            upper_to_real.setdefault(real.upper(), real)
        keys = list(upper_to_real)
        if not keys:
            return []
        target = _compact(text.upper())
        matches = difflib.get_close_matches(target, keys, n=limit, cutoff=_CLOSEST_CUTOFF)
        if not matches:
            # Fixed b, varying a: SequenceMatcher caches the b sequence, so set_seq1 is the
            # cheap direction over a table this size.
            matcher = difflib.SequenceMatcher(None, "", target)
            scored: list[tuple[float, str]] = []
            for key in keys:
                matcher.set_seq1(key)
                scored.append((matcher.ratio(), key))
            scored.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
            matches = [key for _, key in scored[:limit]]
        return [upper_to_real[key] for key in matches]

    async def get(self, name: str) -> SymbolRef:
        """Resolve an exact symbol name, selecting it in Market Watch first."""
        text = (name or "").strip()
        if not text:
            raise TradingError("a symbol name is required", detail={"symbol": name})
        try:
            await self._rpc.symbol_select(text, True)
        except TradingError as exc:
            # Selection failing is usually the same "unknown symbol" the symbol_info call
            # below reports with a hint attached, so it is logged rather than raised here.
            log.debug("symbol_select(%s) failed: %s", text, exc)
        info = await self._rpc.symbol_info(text)
        if info is None:
            hint = await self.closest(text, 3)
            raise TradingError(
                f"symbol {text!r} is not available on this account",
                detail={"symbol": text, "hint": hint},
            )
        return SymbolRef.from_info(info, self._settings)

    async def quote(self, name: str) -> Quote:
        """Return a live tick, retrying once when the terminal answers with zeros.

        An unselected symbol answers with a tick of zeros instead of an error, and a
        just-selected one needs a moment before the first real tick arrives — so a single
        zero read is not yet a closed market. One retry after 500 ms decides it.
        """
        ref = await self.get(name)
        tick = await self._rpc.symbol_info_tick(ref.name)
        if not _tick_is_live(tick):
            await asyncio.sleep(_ZERO_TICK_RETRY_SECONDS)
            tick = await self._rpc.symbol_info_tick(ref.name)
        if not _tick_is_live(tick):
            raise TradingError(
                f"no quotes for {ref.name} — market closed or symbol unavailable",
                detail={"symbol": ref.name},
            )
        bid = float(tick.bid)
        ask = float(tick.ask)
        spread = ask - bid
        spread_points = int(round(spread / ref.point)) if ref.point > 0.0 else 0
        spread_pips = spread / ref.pip_size if ref.pip_size > 0.0 else 0.0
        time_ts = float(tick.time)
        return Quote(
            symbol=ref.name,
            bid=bid,
            ask=ask,
            spread_points=spread_points,
            spread_pips=spread_pips,
            time_ts=time_ts,
            server_time=_server_time_iso(time_ts),
        )

    async def _offset_probe_names(self) -> list[str]:
        """Return the symbols to try when measuring the broker clock, likeliest first.

        Crypto first because it quotes at the weekend, then metals and majors, then
        whatever the operator already has in Market Watch, then the rest of the table.
        """
        await self._ensure()
        out: list[str] = []
        seen: set[str] = set()

        def add(real: str) -> None:
            key = real.upper()
            if key not in seen:
                seen.add(key)
                out.append(real)

        for prefix in _PREFERRED_PROBE_PREFIXES:
            for row in self._rows:
                real = str(row.name)
                if _compact(real.upper()).startswith(prefix):
                    add(real)
                    break
        for row in self._rows:
            if bool(row.visible):
                add(str(row.name))
        for row in self._rows:
            add(str(row.name))
        return out[:_OFFSET_PROBE_LIMIT]


# --------------------------------------------------------------------------------------
# The broker clock.
#
# MT5 reports tick, order and deal times as seconds since the epoch measured on the trade
# server's own clock, which sits at a whole or quarter-hour offset from UTC (Exness is
# UTC+2/+3 with DST). "Today's realized P/L" means since midnight on *that* clock, so the
# offset is measured from a live tick rather than assumed, then snapped to the nearest
# 900 s — the network round-trip and the tick's own age are noise next to a quarter hour.
# --------------------------------------------------------------------------------------
_OFFSET_CACHE_SECONDS: Final[float] = 300.0
_OFFSET_QUANTUM: Final[float] = 900.0
_OFFSET_PROBE_LIMIT: Final[int] = 12
_PREFERRED_PROBE_PREFIXES: Final[tuple[str, ...]] = (
    "BTCUSD",
    "ETHUSD",
    "XAUUSD",
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "XAGUSD",
)

_offset_lock = asyncio.Lock()
_offset_value: float | None = None
_offset_at: float = 0.0


async def server_time_offset(rpc: "Mt5Rpc", resolver: SymbolResolver) -> float:
    """Return broker-server seconds minus ``time.time()``, cached for five minutes.

    Falls back to the last measured value, and then to ``0.0``, when nothing quotes — over
    a weekend every symbol can be closed, and refusing to answer would take the account
    panel down with it. The fallback is logged, never silent.
    """
    global _offset_value, _offset_at
    async with _offset_lock:
        now = time.monotonic()
        if _offset_value is not None and (now - _offset_at) < _OFFSET_CACHE_SECONDS:
            return _offset_value

        measured: float | None = None
        probed = 0
        for name in await resolver._offset_probe_names():
            probed += 1
            try:
                await rpc.symbol_select(name, True)
                tick = await rpc.symbol_info_tick(name)
            except TradingError as exc:
                log.debug("broker clock probe failed on %s: %s", name, exc)
                continue
            if not _tick_is_live(tick):
                continue
            raw = float(tick.time) - time.time()
            measured = round(raw / _OFFSET_QUANTUM) * _OFFSET_QUANTUM
            log.info(
                "broker clock offset measured on %s: %+.0fs raw, %+.0fs snapped",
                name,
                raw,
                measured,
            )
            break

        if measured is None:
            if _offset_value is not None:
                log.warning(
                    "no symbol quoted across %d probes; keeping the cached broker offset %+.0fs",
                    probed,
                    _offset_value,
                )
                return _offset_value
            log.warning(
                "no symbol quoted across %d probes; assuming the broker clock is UTC", probed
            )
            return 0.0

        _offset_value = float(measured)
        _offset_at = now
        return _offset_value


async def broker_day_start_ts(rpc: "Mt5Rpc", resolver: SymbolResolver) -> float:
    """Return midnight of the broker's current day as a unix timestamp.

    The value is on the **broker's clock**, the same time base as ``symbol_info_tick().time``
    and every deal time MT5 reports, so it can be passed straight to ``history_deals_get``
    as ``date_from``. Subtract ``server_time_offset()`` from it to get the true UTC instant
    that midnight happened.
    """
    offset = await server_time_offset(rpc, resolver)
    broker_now = time.time() + offset
    return math.floor(broker_now / _SECONDS_PER_DAY) * _SECONDS_PER_DAY
