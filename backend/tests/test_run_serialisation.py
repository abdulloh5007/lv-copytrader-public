"""Up to three agent runs at once, and the watchdog's run is scoped by its schema.

Reads and the cheap triage call may overlap. Broker writes do not: the executor holds one
write lock around open, modify, close and cancel, which is what stops a double entry. The
watchdog is still not shown a tool that opens a position.
"""

from __future__ import annotations

import asyncio

from app.ai import loop as loop_mod
from app.ai.tools import TOOL_SPECS, WATCHDOG_TOOL_NAMES, tools_for_kind


def test_a_watchdog_run_is_never_shown_a_position_opening_tool() -> None:
    """§6.4: its only job is to attach a stop."""
    names = {spec.name for spec in tools_for_kind("sl_watchdog")}

    for forbidden in (
        "open_market",
        "place_pending",
        "close_position",
        "cancel_order",
        "close_signal_exposure",
    ):
        assert forbidden not in names, (
            f"the watchdog run can call {forbidden}, and with signal_ref=None the duplicate "
            f"guard cannot refuse what it opens"
        )
    assert "modify_position" in names, "it must still be able to attach the stop"
    assert "finish" in names, "every run must be able to end with finish"
    assert names == WATCHDOG_TOOL_NAMES


def test_a_signal_run_still_gets_the_whole_toolset() -> None:
    """The restriction is scoped to the watchdog; the trader is not clipped."""
    names = {spec.name for spec in tools_for_kind("signal")}
    assert names == {spec.name for spec in TOOL_SPECS}
    assert len(names) == 19
    assert "close_signal_exposure" in names
    assert "get_channel_ladder_stats" in names


async def test_run_agent_allows_three_concurrent_runs_and_no_fourth() -> None:
    """A busy worker must not block the next two posts. The fourth waits for a free slot."""
    slots = loop_mod.run_slots()
    assert isinstance(slots, asyncio.Semaphore)

    active = 0
    peak = 0
    three_started = asyncio.Event()
    release = asyncio.Event()

    async def fake_execute(self):  # noqa: ANN001 - stands in for _AgentRun.execute
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 3:
            three_started.set()
        await release.wait()
        active -= 1
        return {"run_id": self.run_id, "status": "finished", "decision": "ignored",
                "summary": "", "error": None}

    original = loop_mod._AgentRun.execute
    loop_mod._AgentRun.execute = fake_execute
    try:
        tasks = [
            asyncio.create_task(loop_mod.run_agent(None, message_row_id=i, kind="signal"))
            for i in range(4)
        ]
        await asyncio.wait_for(three_started.wait(), timeout=10)
        assert active == 3, f"expected 3 runs in flight and one waiting, saw {active}"
        release.set()
        await asyncio.gather(*tasks)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        loop_mod._AgentRun.execute = original

    assert peak == 3, f"more than three agent runs overlapped (peak={peak})"
    assert slots._value == 3, "every slot must be free once the runs finish"
