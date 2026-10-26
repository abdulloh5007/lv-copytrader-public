"""The ladder policy has to live in the system prompt, not only in executor comments."""

from app.ai.prompts import SYSTEM_PROMPT, build_system_prompt
from app.ai.tools import spec_by_name


def test_system_prompt_forbids_risk_percent_and_tp1_parking_on_ladders() -> None:
    text = SYSTEM_PROMPT
    assert "MULTI-TP LADDERS" in text
    assert "Do NOT pass risk_percent" in text
    assert "channel TP1" in text
    assert "TARGET N COMPLETE" in text
    assert "break-even" in text
    assert "0.01" in text
    assert "runner_mode" in text
    assert "runner_tp_index" in text
    assert "hold_channel_sl" in text
    assert "be_after_cover" in text
    assert "re-scan channel history" in text
    assert "get_channel_ladder_stats" in text
    assert "CHANNEL LADDER STATS" in text
    assert "stop still 0" in text
    assert "modify_position" in text
    assert "but they are one" in text


def test_account_prompt_names_the_fixed_ladder_lots() -> None:
    prompt = build_system_prompt(
        margin_mode="hedging",
        armed=True,
        source_title="XAUUADTRADINGMASTER",
        limits={},
        currency="USD",
        ladder_volume=0.10,
    )
    assert "0.1" in prompt or "0.10" in prompt
    assert "risk-percent" in prompt or "risk_percent" in prompt
    assert "TP4" in prompt or "TP4–TP6" in prompt or "TP4-TP6" in prompt


def test_open_market_schema_accepts_a_full_ladder() -> None:
    spec = spec_by_name("open_market")
    assert spec is not None
    tp = spec.input_schema["properties"]["tp"]
    assert tp["maxItems"] >= 8
    assert "ladder" in spec.description.lower() or "1R" in spec.description
    risk = spec.input_schema["properties"]["risk_percent"]["description"].lower()
    assert "ladder" in risk
    volume = spec.input_schema["properties"]["volume"]["description"].lower()
    assert "ladder" in volume
    runner = spec.input_schema["properties"]["runner_mode"]
    assert set(runner["enum"]) == {"be_after_cover", "hold_channel_sl"}
    index = spec.input_schema["properties"]["runner_tp_index"]
    assert index["type"] == "integer"
    pending = spec_by_name("place_pending")
    assert pending is not None
    assert "runner_tp_index" in pending.input_schema["properties"]


def test_system_prompt_forbids_inventing_sl_or_tp() -> None:
    text = SYSTEM_PROMPT
    assert "DO NOT INVENT SL OR TP" in text
    assert "without sl" in text
    assert "Never invent a price" in spec_by_name("open_market").input_schema["properties"]["sl"]["description"]
    assert "sl" not in spec_by_name("open_market").input_schema["required"]
    assert "sl" not in spec_by_name("place_pending").input_schema["required"]
    evaluate = spec_by_name("evaluate_entry")
    assert evaluate is not None
    assert "sl" in evaluate.input_schema["required"]


def test_get_channel_ladder_stats_is_a_read_tool() -> None:
    spec = spec_by_name("get_channel_ladder_stats")
    assert spec is not None
    assert "do not recount" in spec.description.lower()
    from app.ai.tools import READ_TOOL_NAMES, TOOL_SPECS

    assert "get_channel_ladder_stats" in READ_TOOL_NAMES
    names = [item.name for item in TOOL_SPECS]
    assert names.index("get_recent_signals") < names.index("get_channel_ladder_stats")
    assert names.index("get_channel_ladder_stats") < names.index("open_market")


def test_system_prompt_requires_an_open_inside_five_dollars_and_forbids_lot_fear() -> None:
    text = SYSTEM_PROMPT
    assert "max_entry_drift_usd" in text
    assert "5.0" in text
    assert "must_open" in text
    assert "price_moved" in text
    assert "use_fixed_order_lots" in text
    assert "reward:risk to TP1" in text
    evaluate = spec_by_name("evaluate_entry")
    assert evaluate is not None
    assert "must_open" in evaluate.description
    assert "max_entry_drift_usd" in evaluate.description
    finish = spec_by_name("finish")
    assert finish is not None
    assert "must_open" in finish.description


def test_account_prompt_states_the_live_drift_and_fixed_lot_law() -> None:
    prompt = build_system_prompt(
        margin_mode="hedging",
        armed=True,
        source_title="LV",
        limits={},
        currency="USD",
        orders_per_signal=2,
        order_lot_size=0.2,
        use_fixed_order_lots=True,
        max_entry_drift_usd=5,
    )
    assert "OPERATOR ENTRY LAW" in prompt
    assert "trading.max_entry_drift_usd is 5" in prompt
    assert "2 orders of 0.2 lots" in prompt
    assert "use_fixed_order_lots is ON" in prompt
    assert "Do not defer or skip because those lots feel too" in prompt


def test_custom_standing_prompt_keeps_the_live_order_sizing() -> None:
    prompt = build_system_prompt(
        margin_mode="hedging",
        armed=True,
        source_title="LV",
        limits={},
        currency="USD",
        standing_prompt="Follow the channel exactly.",
        orders_per_signal=2,
        order_lot_size=0.2,
        be_after_tp1=True,
    )
    assert prompt.startswith("Follow the channel exactly.")
    assert "OPERATOR ORDER SIZING" in prompt
    assert "2 orders of 0.2 lots" in prompt
    assert "Break-even after TP1 is on" in prompt
    assert "MULTI-TP LADDERS" not in prompt.split("OPERATOR ORDER SIZING", 1)[0]
