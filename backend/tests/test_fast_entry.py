"""Fast entry: triage yes sends the order before the tool loop.

The channel writes ``XAUUSD BUY 4316=44`` and does not say "now". Close to that price
is a market open (within FAST_ENTRY_METAL_MARKET_DISTANCE, default $7 on gold). Farther
is a miss: no pending order. buy now / sell now with no printed price is a market open.
A printed price outside the band is a miss even when the post says now. A
stale catch-up is not chased. Noise that triage calls "no" never opens. A ladder reply
modifies the naked legs and does not add a second pair. The full-agent token and
iteration caps stay where they are.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.ai import loop as loop_mod
from app.ai.base import Capabilities, DoneDelta, TextDelta
from app.ai.fast_entry import (
    ALREADY_SENT_TITLE,
    decide_route,
    parse_price_token,
    read_post,
)
from app.ai.prompts import SYSTEM_PROMPT
from app.config import (
    FAST_ENTRY_DEFAULT_MARKET_PIPS,
    FAST_ENTRY_METAL_MARKET_DISTANCE,
    MAX_TOKENS,
    MAX_TOOL_ITERATIONS,
    TRIAGE_MAX_TOKENS,
    Settings,
)
from app.db import init_db, session_scope
from app.models import Message, RunStep, Source, Trade
from app.trading.executor import RunContext

from tests.test_executor import _build as build_executor
from tests.test_ingest import CHAT_ID


def test_caps_are_unchanged_by_the_fast_path() -> None:
    assert Settings.model_fields["MAX_TOKENS"].default == 32768
    assert Settings.model_fields["MAX_TOOL_ITERATIONS"].default == 32
    assert MAX_TOKENS >= 32768
    assert MAX_TOOL_ITERATIONS >= 32
    assert Settings.model_fields["FAST_ENTRY_METAL_MARKET_DISTANCE"].default == 7.0
    assert Settings.model_fields["FAST_ENTRY_DEFAULT_MARKET_PIPS"].default == 5.0
    assert Settings.model_fields["STALE_SIGNAL_SECONDS"].default == 600
    assert FAST_ENTRY_METAL_MARKET_DISTANCE == 7.0
    assert FAST_ENTRY_DEFAULT_MARKET_PIPS == 5.0


def test_equals_sign_is_a_decimal_and_now_is_not_required() -> None:
    """4316=44 is 4316.44. The post does not have to say now."""
    assert parse_price_token("4316=44") == 4316.44
    assert parse_price_token("4316.44") == 4316.44
    assert parse_price_token("4316,44") == 4316.44
    assert parse_price_token("4316 = 44") == 4316.44
    assert parse_price_token("4310-4320") == (4310.0, 4320.0)
    assert parse_price_token("4316=4320") == (4316.0, 4320.0)

    post = read_post("✌️XAUUSD BUY 4316=44✌️")
    assert post.fresh is True
    assert post.side == "buy"
    assert post.symbol == "XAUUSD"
    assert post.entry == 4316.44
    assert post.now_hint is False
    assert post.sl is None
    assert post.tps == ()

    labeled = read_post("XAUUSD BUY 4316 SL 4308 TP1 4324 TP2 4330")
    assert labeled.entry == 4316.0
    assert labeled.sl == 4308.0
    assert labeled.tps == (4324.0, 4330.0)

    unlabeled = read_post("XAUUSD BUY 4316 4320")
    assert unlabeled.entry == 4316.0
    assert unlabeled.tps == ()
    assert unlabeled.sl is None


def test_now_without_a_price_opens_and_a_far_printed_price_does_not() -> None:
    """buy now with no price is a market open. A far printed price is a miss, not a pending."""
    far = read_post("XAUUSD BUY NOW 4200")
    assert far.fresh is True
    assert far.now_hint is True
    assert far.entry == 4200.0
    route = decide_route(
        far,
        bid=4316.20,
        ask=4316.45,
        pip_size=0.10,
        symbol_name="XAUUSDm",
        pip_convention="metal_gold_0.10",
    )
    assert route.action == "skip"
    assert route.reason == "missed"
    assert route.order_type is None
    assert route.pending_price is None
    assert route.limit == 7.0

    bare = read_post("buy now")
    assert bare.fresh is False
    assert bare.symbol is None

    immediate = read_post("XAUUSD BUY NOW")
    assert immediate.fresh is True
    assert immediate.entry is None
    market = decide_route(
        immediate,
        bid=2380.55,
        ask=2380.80,
        pip_size=0.10,
        symbol_name="XAUUSDm",
    )
    assert market.action == "open_market"
    assert market.reason == "now"

    sell_now = read_post("XAUUSD SELL NOW 4500")
    forced = decide_route(
        sell_now, bid=4316.20, ask=4316.45, pip_size=0.10, symbol_name="XAUUSDm"
    )
    assert forced.action == "skip"
    assert forced.reason == "missed"


def test_distance_band_opens_when_close_and_skips_when_far() -> None:
    near = read_post("XAUUSD BUY 4316.44")
    at_band = decide_route(
        near,
        bid=4323.19,
        ask=4323.44,
        pip_size=0.10,
        symbol_name="XAUUSDm",
    )
    assert at_band.action == "open_market"
    assert at_band.reason == "near"
    assert at_band.distance == 7.0

    just_past = decide_route(
        near,
        bid=4323.20,
        ask=4323.45,
        pip_size=0.10,
        symbol_name="XAUUSDm",
    )
    assert just_past.action == "skip"
    assert just_past.reason == "missed"
    assert just_past.pending_price is None

    zone = read_post("XAUUSD BUY 4310-4320")
    inside = decide_route(
        zone, bid=4314.0, ask=4314.25, pip_size=0.10, symbol_name="XAUUSDm"
    )
    assert inside.action == "open_market"
    assert inside.distance == 0.0
    above = decide_route(
        zone, bid=4330.0, ask=4330.25, pip_size=0.10, symbol_name="XAUUSDm"
    )
    assert above.action == "skip"
    assert above.reason == "missed"

    sell_high = read_post("XAUUSD SELL 4400")
    routed = decide_route(
        sell_high, bid=4316.20, ask=4316.45, pip_size=0.10, symbol_name="XAUUSDm"
    )
    assert routed.action == "skip"
    assert routed.reason == "missed"
    sell_low = read_post("XAUUSD SELL 4200")
    breakdown = decide_route(
        sell_low, bid=4316.20, ask=4316.45, pip_size=0.10, symbol_name="XAUUSDm"
    )
    assert breakdown.action == "skip"
    assert breakdown.reason == "missed"

    stale = decide_route(
        near,
        bid=4316.20,
        ask=4316.45,
        pip_size=0.10,
        symbol_name="XAUUSDm",
        stale=True,
    )
    assert stale.action == "skip"
    assert stale.reason == "stale"

    euro = read_post("EURUSD BUY 1.0850")
    assert euro.symbol == "EURUSD"
    assert euro.entry == 1.085
    close = decide_route(
        euro,
        bid=1.08490,
        ask=1.08520,
        pip_size=0.0001,
        symbol_name="EURUSDm",
        pip_convention="fx_5digit",
    )
    assert close.action == "open_market"
    assert close.limit == 5 * 0.0001

    no_price = read_post("XAUUSD BUY")
    assert no_price.fresh is False
    assert no_price.entry is None
    assert (
        decide_route(
            no_price, bid=4316.20, ask=4316.45, pip_size=0.10, symbol_name="XAUUSDm"
        ).action
        == "skip"
    )


def test_a_ladder_reply_is_levels_not_a_fresh_entry() -> None:
    reply = read_post(
        "TP1 4324 TP2 4330 TP3 4336 TP4 4344 TP5 4352 TP6 4360 LONG 4450 #SL4308"
    )
    assert reply.fresh is False
    assert reply.side is None
    assert reply.sl == 4308.0
    assert reply.tps[0] == 4324.0
    assert reply.tps[-1] == 4450.0
    assert len(reply.tps) == 7
    assert read_post("XAUUSD BUY 4316 and also SELL 4300").ambiguous is True
    assert read_post("tp1 hit, congrats").fresh is False


def test_metal_sl_drops_the_extra_digit_and_tp_keeps_the_thousands() -> None:
    """SIG:4:20033 style: ``SL 42672`` is 4267.2, and ``TP 4288`` is not 88.

    The quote is about $1.16 from the printed entry, inside the $7 band, so the
    fast path opens. A $10 chase is still a miss. A stop that is not a clear
    dropped decimal, and a pip count, are left as printed.
    """
    post = read_post(
        "XAUUSD BUY 4285\n"
        "SL 42672\n"
        "TP 4288\n"
        "TP 4291\n"
        "TP 4294\n"
        "TP 4297\n"
        "TP 4303\n"
        "TP 4306"
    )
    assert post.entry == 4285.0
    assert post.sl == 4267.2
    assert post.tps == (4288.0, 4291.0, 4294.0, 4297.0, 4303.0, 4306.0)
    assert 88.0 not in post.tps
    assert read_post("XAUUSD BUY 4316 TP 4332").tps == (4332.0,)

    route = decide_route(
        post,
        bid=4285.91,
        ask=4286.16,
        pip_size=0.10,
        symbol_name="XAUUSDm",
        pip_convention="metal_gold_0.10",
    )
    assert route.action == "open_market"
    assert round(route.distance, 2) == 1.16
    assert route.limit == 7.0
    assert route.sl == 4267.2
    assert route.tps[0] == 4288.0

    chased = decide_route(
        read_post("XAUUSD BUY 4285"),
        bid=4294.75,
        ask=4295.0,
        pip_size=0.10,
        symbol_name="XAUUSDm",
    )
    assert chased.action == "skip"
    assert chased.reason == "missed"
    assert chased.distance == 10.0

    assert read_post("XAUUSD BUY 4316 SL 4308").sl == 4308.0
    assert read_post("XAUUSD BUY 4285 SL 4267.2").sl == 4267.2
    assert read_post("XAUUSD BUY 4285 SL 20").sl == 20.0
    assert read_post("XAUUSD BUY 4285 SL 99999").sl == 99999.0
    assert read_post("EURUSD BUY 1.0850 SL 10850").sl == 10850.0


def test_the_prompt_tells_the_model_the_order_may_already_be_sent() -> None:
    assert ALREADY_SENT_TITLE in SYSTEM_PROMPT
    assert "4316=44" in SYSTEM_PROMPT
    assert "7.0" in SYSTEM_PROMPT
    assert "600" in SYSTEM_PROMPT
    assert "buy now" in SYSTEM_PROMPT
    assert "hanging limit" in SYSTEM_PROMPT
    assert "but they are one" in SYSTEM_PROMPT


class _ScriptedProvider:
    """One scripted triage answer. Records the request the classifier actually sent."""

    kind = "openai"

    def __init__(self, text: str, *, boom: bool = False) -> None:
        self.text = text
        self.boom = boom
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[Any],
        max_tokens: int,
        model: str,
        capabilities: Capabilities,
    ) -> Any:
        self.calls.append(
            {
                "system": system,
                "tools": list(tools),
                "max_tokens": max_tokens,
                "model": model,
                "thinking_form": capabilities.thinking_form,
            }
        )

        async def _stream() -> Any:
            if self.boom:
                raise RuntimeError("gateway down")
            yield TextDelta(text=self.text)
            yield DoneDelta(stop_reason="end_turn", assistant_blocks=[])

        return _stream()


def _install_provider(runtime: Any, provider: _ScriptedProvider) -> None:
    caps = Capabilities(
        supports_vision=True,
        supports_tools=True,
        thinking_form="adaptive",
        source="probed",
    )

    async def _provider() -> tuple[Any, str, Capabilities]:
        return provider, "operator-picked-model", caps

    runtime.provider = _provider  # type: ignore[method-assign]


async def _seed(
    text: str,
    *,
    message_id: int,
    reply_to: int | None = None,
    catchup: bool = False,
    posted_at: datetime | None = None,
) -> tuple[int, int]:
    async with session_scope() as session:
        found = await session.execute(select(Source).where(Source.is_active.is_(True)).limit(1))
        source = found.scalars().first()
        if source is None:
            source = Source(
                kind="telegram",
                chat_id=CHAT_ID,
                title="Gold",
                username="gold",
                entity_type="channel",
                is_active=True,
            )
            session.add(source)
            await session.flush()
        row = Message(
            source_id=int(source.id),
            message_id=message_id,
            kind="text",
            status="pending",
            text=text,
            reply_to_message_id=reply_to,
            catchup=catchup,
            posted_at=posted_at or datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(row)
        await session.flush()
        return int(source.id), int(row.id)


async def _drive(
    runtime: Any,
    provider: _ScriptedProvider,
    row_id: int,
    *,
    on_iterate: Any = None,
) -> dict[str, Any]:
    async def _iterate(self: loop_mod._AgentRun) -> None:
        if on_iterate is not None:
            await on_iterate(self)
        await self._finalize("finished", decision="ignored", summary="full loop")

    original = loop_mod._AgentRun._iterate
    loop_mod._AgentRun._iterate = _iterate  # type: ignore[method-assign]
    try:
        return await loop_mod.run_agent(
            runtime,  # type: ignore[arg-type]
            message_row_id=row_id,
            kind="signal",
        )
    finally:
        loop_mod._AgentRun._iterate = original


async def test_near_market_opens_before_the_tool_loop(data_dir: Path) -> None:
    """Triage yes + entry within the classic drift opens at market before the tool loop."""
    ex, broker, runtime = await build_executor(data_dir)
    broker.move("XAUUSDm", 4316.20)
    candles = {"n": 0}
    original_candles = ex.get_candles

    async def _candles(*args: Any, **kwargs: Any) -> Any:
        candles["n"] += 1
        return await original_candles(*args, **kwargs)

    ex.get_candles = _candles  # type: ignore[method-assign]
    provider = _ScriptedProvider("yes")
    _install_provider(runtime, provider)
    _source, row_id = await _seed("✌️XAUUSD BUY 4316=44✌️", message_id=100)

    seen: dict[str, Any] = {}

    async def _on_iterate(run: loop_mod._AgentRun) -> None:
        seen["positions"] = len(broker.positions)
        seen["orders"] = len(broker.orders)
        seen["provider_calls"] = len(provider.calls)
        seen["candles"] = candles["n"]
        seen["rates"] = [name for name, _params in broker.calls if name == "copy_rates_from_pos"]
        seen["fast"] = dict(run.fast_entry or {})

    result = await _drive(runtime, provider, row_id, on_iterate=_on_iterate)

    assert seen["positions"] == 2
    assert seen["orders"] == 0
    assert seen["provider_calls"] == 1
    assert seen["candles"] == 0
    assert seen["rates"] == []
    assert provider.calls[0]["max_tokens"] == TRIAGE_MAX_TOKENS
    assert provider.calls[0]["tools"] == []
    assert provider.calls[0]["model"] == "operator-picked-model"
    assert "atria" not in provider.calls[0]["model"].lower()
    assert seen["fast"]["ok"] is True
    assert seen["fast"]["action"] == "open_market"
    assert seen["fast"]["kind"] == "incomplete"
    assert seen["fast"]["volume_source"] == "ladder_volume"
    assert seen["fast"]["limit"] == 5.0
    volumes = sorted(row["volume"] for row in broker.positions.values())
    assert volumes == [0.05, 0.05]
    assert all(row["sl"] == 0.0 for row in broker.positions.values())
    assert result["decision"] == "acted"
    assert result["iterations"] == 0
    assert result["write_calls"] >= 1
    assert "Fast entry already sent" in (result["summary"] or "")

    async with session_scope() as session:
        steps = list(
            (await session.execute(select(RunStep).where(RunStep.name == "triage"))).scalars()
        )
    assert steps[0].payload["max_tokens"] == MAX_TOKENS
    assert steps[0].payload["max_tokens"] >= 32768


async def test_in_band_ladder_with_a_dropped_decimal_stop_opens(data_dir: Path) -> None:
    """The fast path accepts the in-band ladder once SL/TP parse as full gold prices."""
    _ex, broker, runtime = await build_executor(data_dir)
    broker.move("XAUUSDm", 4285.91)
    provider = _ScriptedProvider("yes")
    _install_provider(runtime, provider)
    _source, row_id = await _seed(
        "XAUUSD BUY 4285\nSL 42672\nTP 4288\nTP 4291\nTP 4294\nTP 4297\nTP 4303\nTP 4306",
        message_id=160,
    )

    async def _on_iterate(run: loop_mod._AgentRun) -> None:
        assert run.fast_entry is not None
        assert run.fast_entry["ok"] is True
        assert run.fast_entry["action"] == "open_market"
        assert broker.positions
        assert {row["sl"] for row in broker.positions.values()} == {4267.2}

    await _drive(runtime, provider, row_id, on_iterate=_on_iterate)
    assert broker.positions
    assert broker.orders == {}
    assert {row["sl"] for row in broker.positions.values()} == {4267.2}


async def test_far_entry_is_a_miss_and_does_not_place_a_pending_order(data_dir: Path) -> None:
    ex, broker, runtime = await build_executor(data_dir)
    assert broker.symbols["XAUUSDm"]["bid"] < 3000
    provider = _ScriptedProvider("yes")
    _install_provider(runtime, provider)
    _source, row_id = await _seed("✌️XAUUSD BUY 4316=44✌️", message_id=110)
    opened = {"market": 0}
    pending = {"n": 0}

    original_open = ex.open_market
    original_pending = ex.place_pending

    async def _open(*args: Any, **kwargs: Any) -> Any:
        opened["market"] += 1
        return await original_open(*args, **kwargs)

    async def _pending(*args: Any, **kwargs: Any) -> Any:
        pending["n"] += 1
        return await original_pending(*args, **kwargs)

    ex.open_market = _open  # type: ignore[method-assign]
    ex.place_pending = _pending  # type: ignore[method-assign]

    async def _on_iterate(run: loop_mod._AgentRun) -> None:
        assert len(broker.positions) == 0
        assert len(broker.orders) == 0
        assert run.fast_entry is not None
        assert run.fast_entry["action"] == "skip"
        assert run.fast_entry["reason"] == "missed"
        assert run.fast_entry["ok"] is False

    await _drive(runtime, provider, row_id, on_iterate=_on_iterate)
    assert opened["market"] == 0
    assert pending["n"] == 0
    assert len(provider.calls) == 1
    assert broker.positions == {}
    assert broker.orders == {}


async def test_buy_now_far_from_the_market_does_not_open(data_dir: Path) -> None:
    _ex, broker, runtime = await build_executor(data_dir)
    broker.move("XAUUSDm", 4316.20)
    provider = _ScriptedProvider("yes")
    _install_provider(runtime, provider)
    _source, row_id = await _seed("XAUUSD BUY NOW 4200", message_id=120)

    async def _on_iterate(run: loop_mod._AgentRun) -> None:
        assert len(broker.orders) == 0
        assert len(broker.positions) == 0
        assert run.fast_entry is not None
        assert run.fast_entry["action"] == "skip"
        assert run.fast_entry["reason"] == "missed"
        assert run.fast_entry["ok"] is False

    await _drive(runtime, provider, row_id, on_iterate=_on_iterate)
    assert broker.orders == {}
    assert broker.positions == {}


async def test_a_side_without_an_entry_does_not_invent_a_price(data_dir: Path) -> None:
    _ex, broker, runtime = await build_executor(data_dir)
    broker.move("XAUUSDm", 4316.20)
    provider = _ScriptedProvider("yes")
    _install_provider(runtime, provider)
    _source, row_id = await _seed("XAUUSD BUY", message_id=140)

    async def _on_iterate(run: loop_mod._AgentRun) -> None:
        assert run.fast_entry is not None
        assert run.fast_entry["action"] == "skip"
        assert run.fast_entry["reason"] == "no_entry"
        assert broker.positions == {}
        assert broker.orders == {}

    await _drive(runtime, provider, row_id, on_iterate=_on_iterate)
    assert broker.positions == {}
    assert broker.orders == {}


async def test_an_old_catchup_does_not_market_chase(data_dir: Path) -> None:
    _ex, broker, runtime = await build_executor(data_dir)
    broker.move("XAUUSDm", 4316.20)
    provider = _ScriptedProvider("yes")
    _install_provider(runtime, provider)
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=3)
    _source, row_id = await _seed(
        "✌️XAUUSD BUY 4316=44✌️",
        message_id=150,
        catchup=True,
        posted_at=old,
    )

    async def _on_iterate(run: loop_mod._AgentRun) -> None:
        assert run.fast_entry is not None
        assert run.fast_entry["action"] == "skip"
        assert run.fast_entry["reason"] == "stale"
        assert broker.positions == {}
        assert broker.orders == {}

    await _drive(runtime, provider, row_id, on_iterate=_on_iterate)
    assert broker.positions == {}
    assert broker.orders == {}


async def test_triage_no_and_unparsed_noise_do_not_open(data_dir: Path) -> None:
    _ex, broker, runtime = await build_executor(data_dir)
    broker.move("XAUUSDm", 4316.20)
    provider = _ScriptedProvider("no")
    _install_provider(runtime, provider)
    _source, row_id = await _seed("✌️XAUUSD BUY 4316=44✌️", message_id=130)
    result = await _drive(runtime, provider, row_id)
    assert result["decision"] == "ignored"
    assert result["fast_entry"] is None
    assert broker.positions == {}
    assert broker.orders == {}
    assert len(provider.calls) == 1

    provider_yes = _ScriptedProvider("yes")
    _install_provider(runtime, provider_yes)
    _source, noise_id = await _seed("buy now, great morning", message_id=131)
    noise = await _drive(runtime, provider_yes, noise_id)
    assert noise["fast_entry"] is None
    assert broker.positions == {}
    assert broker.orders == {}

    uncertain = _ScriptedProvider("no", boom=True)
    _install_provider(runtime, uncertain)
    _source, row_uncertain = await _seed("✌️XAUUSD BUY 4316=44✌️", message_id=132)
    ran: list[str] = []

    async def _on_iterate(run: loop_mod._AgentRun) -> None:
        ran.append(run.triage_verdict or "")
        assert run.fast_entry is None

    await _drive(runtime, uncertain, row_uncertain, on_iterate=_on_iterate)
    assert ran == ["uncertain"]
    assert broker.positions == {}
    assert broker.orders == {}


async def test_ladder_reply_recovers_sl_42672_without_reprinting_the_entry(
    data_dir: Path,
) -> None:
    """A reply that only prints ``SL 42672`` still attaches 4267.2 to the open legs."""
    _ex, broker, runtime = await build_executor(data_dir)
    broker.move("XAUUSDm", 4285.91)
    provider = _ScriptedProvider("yes")
    _install_provider(runtime, provider)
    _source, teaser_id = await _seed("XAUUSD BUY 4285", message_id=170)
    _source, reply_id = await _seed(
        "SL 42672\nTP 4288\nTP 4291\nTP 4294\nTP 4297\nTP 4303\nTP 4306",
        message_id=171,
        reply_to=170,
    )
    teaser = await _drive(runtime, provider, teaser_id)
    assert teaser["fast_entry"]["action"] == "open_market"
    assert all(row["sl"] == 0.0 for row in broker.positions.values())
    opened = set(broker.positions)

    async def _on_reply(run: loop_mod._AgentRun) -> None:
        assert run.fast_entry is not None
        assert run.fast_entry["ok"] is True
        assert run.fast_entry["action"] == "modify_existing"
        assert set(broker.positions) == opened
        assert {row["sl"] for row in broker.positions.values()} == {4267.2}

    reply = await _drive(runtime, provider, reply_id, on_iterate=_on_reply)
    assert reply["fast_entry"]["action"] == "modify_existing"
    assert {row["sl"] for row in broker.positions.values()} == {4267.2}
    assert len(broker.positions) == 2


async def test_ladder_reply_modifies_the_open_legs_and_does_not_add_another_pair(
    data_dir: Path,
) -> None:
    ex, broker, runtime = await build_executor(data_dir)
    broker.move("XAUUSDm", 4316.20)
    provider = _ScriptedProvider("yes")
    _install_provider(runtime, provider)
    _source, teaser_id = await _seed("✌️XAUUSD BUY 4316=44✌️", message_id=200)
    _source, reply_id = await _seed(
        "TP1 4324 TP2 4330 TP3 4336 TP4 4344 TP5 4352 TP6 4360 LONG 4450 #SL4308",
        message_id=201,
        reply_to=200,
    )

    teaser = await _drive(runtime, provider, teaser_id)
    assert teaser["fast_entry"]["action"] == "open_market"
    assert len(broker.positions) == 2
    opened = set(broker.positions)

    attempts: list[dict[str, Any]] = []

    async def _on_reply(run: loop_mod._AgentRun) -> None:
        assert run.fast_entry is not None
        assert run.fast_entry["ok"] is True
        assert run.fast_entry["action"] == "modify_existing"
        assert set(broker.positions) == opened
        assert broker.orders == {}
        assert all(row["sl"] == 4308.0 for row in broker.positions.values())
        again = await ex.place_pending(
            RunContext(
                run_id=run.run_id,
                signal_ref=run.signal_ref,
                source_id=run.source_id,
                message_row_id=run.message_row_id,
                origin="agent",
            ),
            symbol="XAUUSDm",
            side="buy",
            order_type="limit",
            price=4310.0,
            sl=4308.0,
            tp=[4324.0, 4330.0, 4336.0, 4344.0, 4352.0, 4360.0, 4450.0],
            reason="agent tries a second pair",
        )
        attempts.append(again)

    reply = await _drive(runtime, provider, reply_id, on_iterate=_on_reply)
    assert reply["fast_entry"]["action"] == "modify_existing"
    assert attempts and attempts[0]["ok"] is False
    assert len(broker.positions) == 2
    assert set(broker.positions) == opened
    assert broker.orders == {}
    assert all(row["sl"] == 4308.0 for row in broker.positions.values())
    assert all(row["tp"] > row["price_open"] for row in broker.positions.values())

    async with session_scope() as session:
        rows = list((await session.execute(select(Trade))).scalars())
    assert len(rows) == 2
    assert {row.ladder_role for row in rows} == {"cover", "runner"}
    assert {row.sl for row in rows} == {4308.0}
    cover = next(row.tp for row in rows if row.ladder_role == "cover")
    runner = next(row.tp for row in rows if row.ladder_role == "runner")
    assert runner > cover
