"""The agent loop — the one place where the model's turn becomes a persisted, streamed run.

One call to :func:`run_agent` is one row in ``runs``: a Telegram post (or the stop-loss watchdog)
handed to the model, every deliberation and every tool call recorded as a ``run_steps`` row and
mirrored onto the event bus, ending in a ``finish`` decision or in a status the operator can see was
not one.

What this module is responsible for, and why each rule exists:

* **The assistant turn goes back verbatim.** ``DoneDelta.assistant_blocks`` is appended to the
  conversation unmodified and in order — thinking blocks, their signatures, and the ``tool_use``
  blocks alongside them. Anthropic signs those blocks and a mismatch is a hard 400, so this loop
  never edits, reorders or drops one (CONTRACTS §16.1, BUILD_PROMPT §4.1).
* **A bad tool call is data, not a crash.** An unknown tool name comes back as an error
  ``tool_result``; the OpenAI provider's synthetic
  :data:`~app.ai.openai_provider.PARSE_ERROR_TOOL_NAME` call — unparsable ``function.arguments`` —
  is fed back with the message that explains the parse failure, so the model retries instead of the
  run dying (CONTRACTS §16.2, BUILD_PROMPT §4.2).
* **``model_context_window_exceeded`` arrives on an HTTP 200.** The providers raise it as
  ``ProviderError(kind="context_window")``. It is a hard stop, not a transport blip: the oldest
  turns are trimmed once and the call is retried, and a second occurrence ends the run as an error
  with a ``system.warning`` (CONTRACTS §16.3, BUILD_PROMPT §4.1).
* **Every provider call emits ``provider.request``** with the model, the token counts and the
  latency. That event is what makes "zero provider calls for a voice note" checkable rather than a
  claim (acceptance criterion 8).
* **A run that never called ``finish`` is ``incomplete`` and shown in red.** On the iteration cap the
  loop forces one final tool-less turn so there is always a recorded ending, and records
  ``incomplete`` when that ending is not a ``finish`` (BUILD_PROMPT §5.1).
* **Nothing propagates.** Ingest may run up to three workers on one queue. An exception escaping
  this function would take a worker down and stall that slot. Every failure path here ends as a
  recorded ``error`` run and a returned dict. Whole runs are not a single mutex: up to three may
  read and think at once, except posts that share a reply chain (a teaser and the ladder reply
  that quotes it). Those take one chain lock so they cannot both open. Broker writes stay one
  at a time inside the executor.
* **A Soft ``#SOFT`` post never reaches the model.** When the text carries that hashtag,
  a deterministic template parses side / symbol / entry / SL / TP1 / TP2 and opens two
  market legs (or fails closed with a reason). Incomplete Soft parses do not fall through
  to the LLM. Posts without the tag keep the path below.
* **A clear new entry is sent before the tool loop.** After triage says yes, a deterministic
  fast path may call ``open_market`` from the live quote (no candles, no extra model turn).
  ``buy now`` / ``sell now`` with no printed price is a market open. A printed entry is a
  market open only inside the band, even when the post says now. A far quote is a miss:
  no pending order is placed, and the executor refuses a later ``open_market`` or
  ``place_pending`` for the same print. A stale or old catch-up post is not chased. The
  full loop still runs afterwards, with the same ``max_tokens`` and ``MAX_TOOL_ITERATIONS``.
  A ladder reply on naked legs is the executor's ``modify_existing`` path, not a second pair.

The arm gate is *not* enforced here. Write tools are gated inside the executor, which owns the
refusal wording and the ``bypass_arm`` exceptions; this loop reads
:data:`~app.ai.tools.WRITE_TOOL_NAMES` only to label a step as a write for the dashboard, so a
disarmed run still streams complete reasoning and complete tool calls (CONTRACTS §16.4).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import select

from app import events
from app.ai import prompts
from app.ai.base import (
    Block,
    Capabilities,
    DoneDelta,
    Msg,
    Provider,
    TextDelta,
    ThinkingDelta,
    ToolCallDone,
    ToolSpec,
    UsageDelta,
    text_block,
    tool_result_block,
)
from app.ai.openai_provider import PARSE_ERROR_TOOL_NAME
from app.ai.tools import TOOL_SPECS, WRITE_TOOL_NAMES, tools_for_kind
from app.config import MAX_TOKENS, MAX_TOOL_ITERATIONS
from app.config import settings as _settings
from app.db import get_setting, get_settings_map, session_scope
from app.errors import ProviderError, SignalPilotError
from app.models import Message, Run, RunStep, Source, SystemState, utcnow
from app import operator_settings
from app.trading import guards
from app.trading.entry_gate import EntryLawState, entry_law_blocks_finish, note_entry_tool
from app.trading.executor import RunContext
from app.util.logging import get_logger, redact

if TYPE_CHECKING:  # pragma: no cover - type-checker only, avoids an import cycle
    from app.runtime import Runtime

log = get_logger(__name__)

__all__ = ["run_agent"]

#: The three decisions `finish` accepts; anything else is recorded as "deferred".
DECISIONS: frozenset[str] = frozenset({"acted", "ignored", "deferred"})

#: Run statuses this module writes, matching CONTRACTS §7.
STATUS_RUNNING: str = "running"
STATUS_FINISHED: str = "finished"
STATUS_INCOMPLETE: str = "incomplete"
STATUS_ERROR: str = "error"

#: What the model is told when it talked instead of calling a tool.
NUDGE_TEXT: str = (
    "You did not call a tool in that turn. Every run has to end with the finish tool: call "
    "finish(decision, summary) now — decision is \"acted\" if you sent anything to the broker, "
    "\"ignored\" if this post needed no trading action, \"deferred\" if it was a real signal you "
    "chose not to act on. If you still need to check something first, call that tool instead, but "
    "do not answer in plain text again."
)

#: The same nudge when the previous turn was cut off by max_tokens.
NUDGE_TRUNCATED: str = (
    "That turn hit the output limit before it produced a tool call. Be brief: make the one tool "
    "call you need, or call finish(decision, summary) now with a short summary."
)

#: The final, tool-less turn's instruction. Nothing it says can reach the broker any more.
FINAL_TURN_TEXT: str = (
    "You have used every tool iteration this run is allowed, so the tools are now switched off and "
    "no further action can reach the broker. Write the record of this run for the operator in two "
    "or three sentences: the instrument, what you did or did not do, and why — and if something is "
    "still open or unfinished, say exactly what it is so a human can pick it up."
)

#: Cap on how much of one delta's text is put on the bus in a single event.
_EVENT_TEXT_LIMIT: int = 4000

#: Cap on the tool-result JSON handed back to the model and stored on the step row.
_RESULT_CHAR_LIMIT: int = 60_000

#: Matches the ticket in the watchdog's instruction ("Position #12345 on XAUUSDm ...").
_TICKET_RE: re.Pattern[str] = re.compile(r"#(\d{1,20})")


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Coerce anything into something the JSON column and the event bus can carry."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return {"repr": str(value)}


def _dumps(value: Any) -> str:
    """Serialise a tool result for the model, clipped so one reply cannot swamp the context."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > _RESULT_CHAR_LIMIT:
        return text[:_RESULT_CHAR_LIMIT] + f"… [truncated at {_RESULT_CHAR_LIMIT} characters]"
    return text


def _clip(text: str, limit: int = _EVENT_TEXT_LIMIT) -> str:
    """Clip a string for an event payload without hiding that it was clipped."""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _is_tool_result_turn(message: Msg) -> bool:
    """Report whether a user message consists only of tool_result blocks."""
    if str(message.get("role") or "") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, list) or not content:
        return False
    return all(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    )


def _segments(messages: list[Msg]) -> list[tuple[int, int]]:
    """Group everything after the opening turn into whole, self-contained segments.

    A segment is one assistant turn plus the ``user`` turn that carries its ``tool_result``
    blocks. Trimming has to cut on these boundaries: dropping an assistant turn while keeping
    the tool results that answer it leaves ``tool_result`` blocks with no ``tool_use`` to
    match, which is a 400 on both vendors.
    """
    out: list[tuple[int, int]] = []
    index = 1
    total = len(messages)
    while index < total:
        end = index
        if str(messages[index].get("role") or "") == "assistant":
            if index + 1 < total and _is_tool_result_turn(messages[index + 1]):
                end = index + 1
        out.append((index, end))
        index = end + 1
    return out


def _trim_history(messages: list[Msg]) -> bool:
    """Drop the images and the oldest whole turns; report whether anything actually went.

    The opening user turn is kept because it is the signal itself, but its images are not: an
    unresized chart is thousands of tokens and this is called precisely because the request no
    longer fits. Cuts land only on a segment that starts with an assistant turn, so the result
    never puts two ``user`` messages back to back.
    """
    changed = False

    if messages:
        first = messages[0]
        content = first.get("content")
        if isinstance(content, list):
            kept = [
                block
                for block in content
                if not (isinstance(block, dict) and block.get("type") == "image")
            ]
            dropped = len(content) - len(kept)
            if dropped > 0:
                kept.append(
                    text_block(
                        f"[{dropped} image(s) from this post were removed to fit the model's "
                        f"context window. Ground yourself with get_candles and get_quote instead "
                        f"of the chart.]"
                    )
                )
                first["content"] = kept
                changed = True

    segments = _segments(messages)
    cut_points = [
        start
        for start, _ in segments
        if str(messages[start].get("role") or "") == "assistant"
    ]
    if len(cut_points) >= 2:
        # Keep the most recent half of the exchange; that is what the model needs to continue.
        boundary = cut_points[len(cut_points) // 2]
        if boundary > 1:
            del messages[1:boundary]
            changed = True
            if messages and isinstance(messages[0].get("content"), list):
                messages[0]["content"].append(
                    text_block(
                        "[Earlier turns of this run were removed to fit the model's context "
                        "window. Re-read anything you need with the read tools rather than "
                        "assuming it.]"
                    )
                )

    return changed


def _decision_of(value: Any) -> str:
    """Normalize the model's decision onto the three values the rest of the system reads."""
    decision = str(value or "").strip().lower()
    if decision in DECISIONS:
        return decision
    log.warning("run_finish_unknown_decision decision=%r", value)
    return "deferred"


@dataclass
class _Turn:
    """One assistant turn, accumulated from the provider's delta stream."""

    thinking: str = ""
    text: str = ""
    stop_reason: str = "end_turn"
    assistant_blocks: list[Block] = field(default_factory=list)
    tool_calls: dict[str, ToolCallDone] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    provider_idx: int = 0
    thinking_idx: int = 0
    text_idx: int = 0

    @property
    def tool_use_blocks(self) -> list[Block]:
        """The turn's tool_use blocks in wire order — the order the tools must run in."""
        return [
            block
            for block in self.assistant_blocks
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]

    @property
    def empty(self) -> bool:
        """Report whether the provider produced nothing at all for this turn."""
        return not self.assistant_blocks and not self.order


# --------------------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------------------


class _AgentRun:
    """One agent run, from the ``runs`` row to the ``run.finished`` event."""

    def __init__(
        self,
        runtime: Runtime,
        *,
        run_id: str,
        message_row_id: int | None,
        kind: str,
        instruction: str | None,
    ) -> None:
        self.runtime = runtime
        self.settings = getattr(runtime, "settings", None) or _settings
        self.bus = getattr(runtime, "bus", None) or events.bus
        self.run_id = run_id
        self.kind = (kind or "signal").strip() or "signal"
        self.instruction = instruction
        self.message_row_id = message_row_id

        self.source_id: int | None = None
        self.source_title: str = ""
        self.signal_ref: str | None = None
        self.armed: bool = False

        self.provider: Provider | None = None
        self.provider_kind: str | None = None
        self.model: str | None = None
        self.caps: Capabilities | None = None
        self.max_tokens: int = int(getattr(self.settings, "MAX_TOKENS", MAX_TOKENS))
        self.max_iterations: int = max(
            1, int(getattr(self.settings, "MAX_TOOL_ITERATIONS", MAX_TOOL_ITERATIONS))
        )

        self.system_prompt: str = ""
        self.messages: list[Msg] = []
        self.context_meta: dict[str, Any] = {}
        #: The post the cheap classifier reads. Filled when a signal row is loaded.
        self.triage_text: str = ""
        self.triage_image_count: int = 0
        self.triage_verdict: str | None = None
        #: Set once the fast path has called the broker. None when it did not run.
        self.fast_entry: dict[str, Any] | None = None
        #: Set when the Soft #SOFT template path handled (or refused) the post.
        self.soft_template: dict[str, Any] | None = None
        #: Classic entry the server has already required this run to open.
        self.entry_law: EntryLawState = EntryLawState()

        self.step_idx: int = 0
        self.iterations: int = 0
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.provider_latency_ms: int = 0
        self.tool_calls: int = 0
        self.write_calls: int = 0
        self.trimmed: bool = False

        self.status: str = STATUS_RUNNING
        self.decision: str | None = None
        self.summary: str | None = None
        self.error: str | None = None
        self.started_at: float = time.monotonic()
        self._run_created: bool = False
        self._finalized: bool = False

    # ---- events and persistence -------------------------------------------

    def _emit(self, event_type: str, **data: Any) -> None:
        """Put one envelope on the bus; emitting never blocks and never raises."""
        self.bus.emit(event_type, run_id=self.run_id, kind=self.kind, **data)

    def _reserve(self) -> int:
        """Claim the next step index at the moment the step *happens*.

        Reserving is separate from writing because a turn's thinking is only complete once the
        stream ends, while the tool calls it led to arrive before that. Claiming the index when
        the first token of a step arrives is what makes `run_steps` read in the order the
        operator watched it happen: provider call, reasoning, answer, tool call, tool result.
        """
        self.step_idx += 1
        return self.step_idx

    async def _add_step(
        self,
        step_type: str,
        *,
        name: str | None = None,
        payload: Any = None,
        ok: bool | None = None,
    ) -> int:
        """Reserve an index and persist one run step in one go."""
        return await self._write_step(
            self._reserve(), step_type, name=name, payload=payload, ok=ok
        )

    async def _write_step(
        self,
        idx: int,
        step_type: str,
        *,
        name: str | None = None,
        payload: Any = None,
        ok: bool | None = None,
    ) -> int:
        """Persist one ordered run step at a reserved index; a failed write is never fatal."""
        try:
            async with session_scope() as session:
                session.add(
                    RunStep(
                        run_id=self.run_id,
                        idx=idx,
                        type=step_type,
                        name=name,
                        payload=_jsonable(payload if payload is not None else {}),
                        ok=ok,
                        created_at=utcnow(),
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the run outranks its own audit trail
            log.warning(
                "run_step_write_failed run_id=%s idx=%d type=%s error=%s",
                self.run_id,
                idx,
                step_type,
                exc,
            )
        return idx

    async def _create_run(self) -> None:
        """Write the ``runs`` row before anything slow happens, then announce the run."""
        async with session_scope() as session:
            session.add(
                Run(
                    id=self.run_id,
                    source_id=self.source_id,
                    message_row_id=self.message_row_id,
                    kind=self.kind,
                    status=STATUS_RUNNING,
                    armed_at_start=self.armed,
                    started_at=utcnow(),
                )
            )
        self._run_created = True
        self._emit(
            events.RUN_STARTED,
            message_row_id=self.message_row_id,
            source_id=self.source_id,
            source_title=self.source_title,
            signal_ref=self.signal_ref,
            armed=self.armed,
            status=STATUS_RUNNING,
        )

    async def _update_run(self, **fields: Any) -> None:
        """Patch the ``runs`` row; a store that cannot be written must not kill the run."""
        if not self._run_created:
            return
        try:
            async with session_scope() as session:
                row = await session.get(Run, self.run_id)
                if row is None:
                    return
                for key, value in fields.items():
                    setattr(row, key, value)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - see _add_step
            log.warning("run_update_failed run_id=%s error=%s", self.run_id, exc)

    # ---- setup ------------------------------------------------------------

    async def _load_target(self) -> Message | None:
        """Resolve the message row, the source and the armed flag this run starts from."""
        self.armed = await self.runtime.armed()
        message_row: Message | None = None
        async with session_scope() as session:
            if self.message_row_id is not None:
                message_row = await session.get(Message, int(self.message_row_id))
                if message_row is not None:
                    self.source_id = int(message_row.source_id)
                    self.signal_ref = message_row.signal_ref
            if self.source_id is None:
                active = await session.execute(
                    select(Source).where(Source.is_active.is_(True)).limit(1)
                )
                source = active.scalars().first()
                if source is not None:
                    self.source_id = int(source.id)
                    self.source_title = str(source.title or "")
            elif message_row is not None:
                source = await session.get(Source, int(message_row.source_id))
                if source is not None:
                    self.source_title = str(source.title or "")
            if message_row is not None:
                # Detach the values the context builder needs; the session closes here.
                session.expunge_all()
        return message_row

    async def _chain_already_closed(self) -> bool:
        """True when ingest already flattened this chain. Skips the provider call."""
        from app.trading.exposure import chain_exposure_closed

        try:
            return await chain_exposure_closed(self.signal_ref)
        except Exception as exc:  # noqa: BLE001 - a failed check must not open a voided chain
            log.warning("run_exposure_check_failed run_id=%s error=%s", self.run_id, exc)
            return True

    async def _resolve_provider(self) -> str | None:
        """Build the configured provider; return a refusal message when there is none."""
        try:
            resolved = await self.runtime.provider()
        except asyncio.CancelledError:
            raise
        except SignalPilotError as exc:
            return exc.message
        except Exception as exc:  # noqa: BLE001 - a broken setting is a message, not a crash
            return f"the provider could not be built: {exc}"
        if resolved is None:
            return (
                "no AI provider is configured — set the provider kind, key and model in Settings "
                "before the system can read a signal"
            )
        provider, model, caps = resolved
        self.provider = provider
        self.model = str(model or "")
        self.caps = caps
        self.provider_kind = str(getattr(provider, "kind", "") or "")
        try:
            async with session_scope() as session:
                configured = await get_setting(session, "provider.max_tokens", None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - fall back to the config default
            log.warning("run_max_tokens_read_failed run_id=%s error=%s", self.run_id, exc)
            configured = None
        if isinstance(configured, (int, float)) and not isinstance(configured, bool):
            if int(configured) > 0:
                self.max_tokens = int(configured)
        await self._update_run(provider_kind=self.provider_kind, model=self.model)
        return None

    async def _account_facts(self) -> tuple[str, str]:
        """Return (margin_mode, currency) from cached state, falling back to the account."""
        margin_mode = ""
        currency = ""
        try:
            async with session_scope() as session:
                state = await session.get(SystemState, 1)
                if state is not None:
                    margin_mode = str(state.margin_mode or "")
                    currency = str(state.account_currency or "")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the prompt says "unknown" and the model asks
            log.warning("run_state_read_failed run_id=%s error=%s", self.run_id, exc)
        if margin_mode and currency:
            return margin_mode, currency

        executor = getattr(self.runtime, "executor", None)
        if executor is None:
            return margin_mode, currency
        try:
            account = await executor.get_account()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead sidecar must not stop the run
            log.warning("run_account_read_failed run_id=%s error=%s", self.run_id, exc)
            return margin_mode, currency
        if isinstance(account, dict):
            margin_mode = str(account.get("margin_mode") or margin_mode or "")
            currency = str(account.get("currency") or currency or "")
        return margin_mode, currency

    async def _limits(self) -> dict[str, Any]:
        """Read the operator limits; only the ones actually set reach the prompt."""
        try:
            async with session_scope() as session:
                return await get_settings_map(session, "limits.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - an unreadable limit is not a reason to stop
            log.warning("run_limits_read_failed run_id=%s error=%s", self.run_id, exc)
            return {}

    async def _build_system_prompt(self) -> None:
        """Compose the system prompt from this account's real state, not from defaults."""
        margin_mode, currency = await self._account_facts()
        limits = await self._limits()
        ladder_volume = None
        standing_prompt = ""
        order_count = None
        lot_each = None
        be_after_tp1 = None
        try:
            async with session_scope() as session:
                stored = await get_setting(session, "trading.ladder_volume", None)
                standing_prompt = await operator_settings.stored_system_prompt(session)
                order_count = await operator_settings.orders_per_signal(session)
                lot_each = await operator_settings.order_lot_size(session)
                be_after_tp1 = await operator_settings.be_after_tp1_enabled(session)
                fixed_lots = await operator_settings.use_fixed_order_lots(session)
                entry_drift = await operator_settings.max_entry_drift_usd(session)
            if stored is not None:
                ladder_volume = float(stored)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a missing knob must not stop a run
            log.warning("run_ladder_volume_read_failed run_id=%s error=%s", self.run_id, exc)
            fixed_lots = None
            entry_drift = None
        self.system_prompt = prompts.build_system_prompt(
            margin_mode=margin_mode,
            armed=self.armed,
            source_title=self.source_title,
            limits=limits,
            currency=currency,
            ladder_volume=ladder_volume,
            standing_prompt=standing_prompt,
            orders_per_signal=order_count,
            order_lot_size=lot_each,
            be_after_tp1=be_after_tp1,
            use_fixed_order_lots=fixed_lots,
            max_entry_drift_usd=entry_drift,
        )

    async def _stopless_position(self) -> dict[str, Any]:
        """Find the position the SL watchdog is complaining about, for its context turn."""
        ticket: int | None = None
        match = _TICKET_RE.search(self.instruction or "")
        if match is not None:
            try:
                ticket = int(match.group(1))
            except ValueError:
                ticket = None

        executor = getattr(self.runtime, "executor", None)
        rows: list[dict[str, Any]] = []
        if executor is not None:
            try:
                payload = await executor.get_positions()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the instruction still names the ticket
                log.warning("run_watchdog_positions_failed run_id=%s error=%s", self.run_id, exc)
                payload = None
            if isinstance(payload, dict):
                candidates = payload.get("positions")
                if isinstance(candidates, list):
                    rows = [row for row in candidates if isinstance(row, dict)]

        if ticket is not None:
            for row in rows:
                try:
                    if int(row.get("ticket") or 0) == ticket:
                        return row
                except (TypeError, ValueError):
                    continue
        magic = int(getattr(self.settings, "MAGIC_NUMBER", 770077))
        for row in rows:
            try:
                if float(row.get("sl") or 0.0) == 0.0 and int(row.get("magic") or 0) == magic:
                    return row
            except (TypeError, ValueError):
                continue
        return {"ticket": ticket} if ticket is not None else {}

    async def _build_messages(self, message_row: Message | None) -> str | None:
        """Build the run's opening turn; return a refusal message when it cannot be built."""
        if message_row is not None:
            self.triage_text = str(message_row.text or "")
            self.triage_image_count = int(message_row.image_count or 0)
            async with session_scope() as session:
                messages, meta = await prompts.build_context(session, self.runtime, message_row)
            self.messages = messages
            self.context_meta = dict(meta)
            if self.instruction:
                content = self.messages[-1].get("content")
                if isinstance(content, list):
                    content.insert(
                        0,
                        text_block(
                            "=== OPERATOR INSTRUCTION FOR THIS RUN ===\n"
                            + " ".join(self.instruction.split())
                        ),
                    )
            return None

        if self.kind == "signal":
            return (
                "this run had no message to read — a signal run needs the ingested post it is "
                "about"
            )

        if self.kind == "sl_watchdog":
            position = await self._stopless_position()
            self.messages = prompts.watchdog_context(position, self.instruction or "")
            self.context_meta = {"ticket": position.get("ticket")}
            return None

        instruction = " ".join((self.instruction or "").split())
        if not instruction:
            return f"a {self.kind} run needs either a message or an instruction to act on"
        self.messages = [
            {
                "role": "user",
                "content": [
                    text_block(
                        "=== OPERATOR INSTRUCTION — THIS RUN IS NOT A CHANNEL SIGNAL ===\n"
                        f"{instruction}\n\n"
                        "Use the read tools to ground anything you are unsure of, act only on what "
                        "the instruction asks for, and end with finish(decision, summary)."
                    )
                ],
            }
        ]
        self.context_meta = {"instruction": True}
        return None

    # ---- one provider call -------------------------------------------------

    def _record_provider_call(
        self, turn: _Turn, started: float, *, ok: bool, error: str | None
    ) -> None:
        """Account for one provider call and put ``provider.request`` on the bus.

        This runs for every call, successful or not. It is the event acceptance criterion 8
        counts: a skipped voice note never reaches this loop, so the stream stays silent.
        """
        turn.latency_ms = int((time.monotonic() - started) * 1000)
        self.provider_latency_ms += turn.latency_ms
        self.input_tokens += turn.input_tokens
        self.output_tokens += turn.output_tokens
        self._emit(
            events.PROVIDER_REQUEST,
            provider_kind=self.provider_kind,
            model=self.model,
            iteration=self.iterations,
            input_tokens=turn.input_tokens,
            output_tokens=turn.output_tokens,
            latency_ms=turn.latency_ms,
            stop_reason=turn.stop_reason if ok else None,
            ok=ok,
            error=error,
        )

    async def _persist_provider_call(
        self, turn: _Turn, *, ok: bool, error: str | None, tools_enabled: bool
    ) -> None:
        """Persist the provider call as a step, with the context meta on the first one."""
        payload: dict[str, Any] = {
            "provider_kind": self.provider_kind,
            "model": self.model,
            "iteration": self.iterations,
            "input_tokens": turn.input_tokens,
            "output_tokens": turn.output_tokens,
            "latency_ms": turn.latency_ms,
            "stop_reason": turn.stop_reason if ok else None,
            "tools_enabled": tools_enabled,
            "max_tokens": self.max_tokens,
        }
        if error:
            payload["error"] = error
        if self.iterations <= 1 and self.context_meta:
            payload["context"] = self.context_meta
        await self._write_step(
            turn.provider_idx, "provider", name=self.provider_kind, payload=payload, ok=ok
        )

    async def _turn(self, tool_specs: list[ToolSpec]) -> _Turn:
        """Run exactly one provider call, streaming every delta onto the bus as it arrives."""
        assert self.provider is not None and self.caps is not None  # guarded by _resolve_provider
        self.iterations += 1
        turn = _Turn()
        turn.provider_idx = self._reserve()
        started = time.monotonic()
        try:
            stream = self.provider.run(
                system=self.system_prompt,
                messages=self.messages,
                tools=list(tool_specs),
                max_tokens=self.max_tokens,
                model=self.model or "",
                capabilities=self.caps,
            )
            async for delta in stream:
                if isinstance(delta, ThinkingDelta):
                    if delta.text:
                        if not turn.thinking_idx:
                            turn.thinking_idx = self._reserve()
                        turn.thinking += delta.text
                        self._emit(
                            events.RUN_THINKING,
                            idx=turn.thinking_idx,
                            iteration=self.iterations,
                            text=_clip(delta.text),
                        )
                elif isinstance(delta, TextDelta):
                    if delta.text:
                        if not turn.text_idx:
                            turn.text_idx = self._reserve()
                        turn.text += delta.text
                        self._emit(
                            events.RUN_TEXT,
                            idx=turn.text_idx,
                            iteration=self.iterations,
                            text=_clip(delta.text),
                        )
                elif isinstance(delta, ToolCallDone):
                    turn.tool_calls[delta.id] = delta
                    turn.order.append(delta.id)
                    await self._announce_tool_call(delta.id, delta.name, delta.input)
                elif isinstance(delta, UsageDelta):
                    # Both providers report cumulative usage for the turn, so the last one wins.
                    turn.input_tokens = int(delta.input_tokens or 0)
                    turn.output_tokens = int(delta.output_tokens or 0)
                elif isinstance(delta, DoneDelta):
                    turn.stop_reason = str(delta.stop_reason or "end_turn")
                    # VERBATIM: this list is appended to the conversation exactly as received,
                    # thinking signatures included. Editing it is a hard 400 (CONTRACTS §16.1).
                    turn.assistant_blocks = delta.assistant_blocks
        except asyncio.CancelledError:
            self._record_provider_call(turn, started, ok=False, error="cancelled")
            raise
        except SignalPilotError as exc:
            self._record_provider_call(turn, started, ok=False, error=exc.message)
            await self._persist_provider_call(
                turn, ok=False, error=exc.message, tools_enabled=bool(tool_specs)
            )
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced as an error run by the caller
            message = f"{type(exc).__name__}: {exc}"
            self._record_provider_call(turn, started, ok=False, error=message)
            await self._persist_provider_call(
                turn, ok=False, error=message, tools_enabled=bool(tool_specs)
            )
            raise
        self._record_provider_call(turn, started, ok=True, error=None)
        await self._persist_provider_call(
            turn, ok=True, error=None, tools_enabled=bool(tool_specs)
        )
        await self._persist_turn_text(turn)
        self.runtime.set_status("provider", True, None)
        return turn

    async def _persist_turn_text(self, turn: _Turn) -> None:
        """Persist this turn's reasoning and its visible text at the indices they started at."""
        if turn.thinking.strip():
            await self._write_step(
                turn.thinking_idx or self._reserve(),
                "thinking",
                payload={"text": turn.thinking, "iteration": self.iterations},
            )
        if turn.text.strip():
            await self._write_step(
                turn.text_idx or self._reserve(),
                "text",
                payload={"text": turn.text, "iteration": self.iterations},
            )

    async def _announce_tool_call(self, call_id: str, name: str, arguments: Any) -> None:
        """Persist and stream one tool call the moment its arguments finish streaming."""
        tool_name = str(name or "")
        idx = await self._add_step(
            "tool_call",
            name=tool_name,
            payload={
                "tool_use_id": call_id,
                "input": arguments,
                "iteration": self.iterations,
                "write": tool_name in WRITE_TOOL_NAMES,
            },
        )
        self._emit(
            events.RUN_TOOL_CALL,
            idx=idx,
            iteration=self.iterations,
            tool_use_id=call_id,
            name=tool_name,
            input=_jsonable(arguments),
            write=tool_name in WRITE_TOOL_NAMES,
        )

    # ---- one tool round ----------------------------------------------------

    async def _run_tools(self, turn: _Turn) -> dict[str, Any] | None:
        """Execute every tool_use of a turn in order; return the ``finish`` result if it came.

        The order is taken from ``assistant_blocks`` rather than from the delta stream because
        that is the order the provider actually sent, and the ``tool_result`` blocks must come
        back in exactly the same order.
        """
        ctx = RunContext(
            run_id=self.run_id,
            signal_ref=self.signal_ref,
            source_id=self.source_id,
            message_row_id=self.message_row_id,
            origin="agent",
        )
        results: list[Block] = []
        finish_result: dict[str, Any] | None = None

        for block in turn.tool_use_blocks:
            call_id = str(block.get("id") or "")
            name = str(block.get("name") or "")
            raw_input = block.get("input")
            arguments = raw_input if isinstance(raw_input, dict) else {}
            if call_id not in turn.tool_calls:
                # A tool_use the stream never reported as done: announce it now so the audit
                # trail still carries every call before its result.
                await self._announce_tool_call(call_id, name, arguments)
            self.tool_calls += 1
            if name in WRITE_TOOL_NAMES:
                self.write_calls += 1

            if name == PARSE_ERROR_TOOL_NAME:
                # The OpenAI provider could not parse function.arguments. Hand the explanation
                # back as a failed tool result so the model retries; never abort the run.
                message = str(
                    arguments.get("message")
                    or "the arguments for that tool call were not valid JSON — send them again "
                    "as a single JSON object"
                )
                result: dict[str, Any] = {
                    "ok": False,
                    "message": message,
                    "tool": arguments.get("tool"),
                    "error": arguments.get("error"),
                }
            else:
                result = await self.runtime.executor.dispatch(name, arguments, ctx)
                if not isinstance(result, dict):
                    result = {"ok": False, "message": f"{name} returned {type(result).__name__}"}
                note_entry_tool(self.entry_law, name, result)
                if name == "finish":
                    blocked = entry_law_blocks_finish(
                        must_open=self.entry_law.must_open,
                        opened=self.entry_law.opened,
                        hard_blocked=self.entry_law.hard_blocked,
                        decision=str(result.get("decision") or ""),
                    )
                    if blocked:
                        result = {
                            "ok": False,
                            "message": blocked,
                            "decision": result.get("decision"),
                            "summary": result.get("summary"),
                        }

            ok = bool(result.get("ok", False))
            idx = await self._add_step(
                "tool_result",
                name=name,
                payload={
                    "tool_use_id": call_id,
                    "result": result,
                    "iteration": self.iterations,
                    "write": name in WRITE_TOOL_NAMES,
                },
                ok=ok,
            )
            self._emit(
                events.RUN_TOOL_RESULT,
                idx=idx,
                iteration=self.iterations,
                tool_use_id=call_id,
                name=name,
                ok=ok,
                result=_jsonable(result),
                write=name in WRITE_TOOL_NAMES,
            )
            results.append(tool_result_block(call_id, _dumps(result), is_error=not ok))
            if name == "finish" and ok and finish_result is None:
                finish_result = result

        if results:
            self.messages.append({"role": "user", "content": results})
        return finish_result

    # ---- the loop ----------------------------------------------------------

    async def _iterate(self) -> None:
        """Drive the tool loop until ``finish``, the iteration cap, or a hard stop."""
        # §6.4: an sl_watchdog run's only job is to attach a stop, so it is never shown
        # open_market / place_pending / close_position / cancel_order. Scope enforced by the
        # schema, not by prose in the instruction.
        specs = tools_for_kind(self.kind)
        retried_after_trim = False
        transient_retries = 0

        while self.iterations < self.max_iterations:
            try:
                turn = await self._turn(specs)
            except ProviderError as exc:
                if exc.kind == "context_window":
                    if not retried_after_trim and _trim_history(self.messages):
                        retried_after_trim = True
                        self.trimmed = True
                        await self._add_step(
                            "error",
                            name="context_window",
                            payload={
                                "message": exc.message,
                                "iteration": self.iterations,
                                "action": "trimmed the oldest turns and retried once",
                            },
                            ok=False,
                        )
                        self._emit(
                            events.SYSTEM_WARNING,
                            reason="context_window",
                            message=(
                                "the conversation outgrew the model's context window — the oldest "
                                "turns were trimmed and the call retried"
                            ),
                            model=self.model,
                        )
                        log.warning(
                            "run_context_trimmed run_id=%s iteration=%d", self.run_id, self.iterations
                        )
                        continue
                    await self._fail_context_window(exc)
                    return

                # A transient failure at the provider must not cost a signal. Two real runs
                # were lost to one HTTP 500 from the gateway — nginx hiccuped, the run ended
                # as an error, and the channel post was never acted on. A trade opportunity
                # is time-limited but not instantaneous, so a few seconds of backoff is far
                # cheaper than dropping it. Hard failures (auth, not_found, bad_request) are
                # NOT retried: they fail identically forever and retrying only delays the
                # operator learning what is wrong.
                if exc.kind in _RETRYABLE_PROVIDER_KINDS and transient_retries < MAX_PROVIDER_RETRIES:
                    delay = _retry_delay(exc, transient_retries)
                    transient_retries += 1
                    await self._add_step(
                        "error",
                        name="provider_retry",
                        payload={
                            "message": exc.message,
                            "kind": exc.kind,
                            "status": exc.status,
                            "attempt": transient_retries,
                            "of": MAX_PROVIDER_RETRIES,
                            "retry_in_seconds": delay,
                        },
                        ok=False,
                    )
                    self._emit(
                        events.SYSTEM_WARNING,
                        reason="provider_retry",
                        message=(
                            f"{exc.message} — retrying in {delay:.0f}s "
                            f"({transient_retries}/{MAX_PROVIDER_RETRIES})"
                        ),
                        model=self.model,
                    )
                    log.warning(
                        "run_provider_retry run_id=%s kind=%s status=%s attempt=%d delay=%.1f",
                        self.run_id, exc.kind, exc.status, transient_retries, delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                await self._fail_provider(exc)
                return

            if turn.empty:
                await self._add_step(
                    "error",
                    name="empty_turn",
                    payload={"iteration": self.iterations, "stop_reason": turn.stop_reason},
                    ok=False,
                )
                await self._finalize(
                    STATUS_INCOMPLETE,
                    summary=(
                        "The model returned an empty turn, so this run has no decision. Nothing "
                        "was sent to the broker."
                    ),
                    error="the provider returned a turn with no content",
                )
                return

            # VERBATIM, in order, thinking signatures intact (CONTRACTS §16.1).
            self.messages.append({"role": "assistant", "content": turn.assistant_blocks})

            if turn.tool_use_blocks:
                finish_result = await self._run_tools(turn)
                if finish_result is not None:
                    await self._finalize(
                        STATUS_FINISHED,
                        decision=_decision_of(finish_result.get("decision")),
                        summary=str(finish_result.get("summary") or "").strip() or None,
                    )
                    return
                continue

            nudge = NUDGE_TRUNCATED if turn.stop_reason == "max_tokens" else NUDGE_TEXT
            self.messages.append({"role": "user", "content": [text_block(nudge)]})

        await self._final_turn()

    async def _final_turn(self) -> None:
        """Force one tool-less turn on the cap so the run always ends with a written record."""
        log.info(
            "run_iteration_cap run_id=%s iterations=%d", self.run_id, self.iterations
        )
        last = self.messages[-1] if self.messages else None
        if (
            isinstance(last, dict)
            and str(last.get("role") or "") == "user"
            and isinstance(last.get("content"), list)
        ):
            # The previous turn already ended on a user message (a nudge). Extend it rather than
            # stacking a second one: two consecutive user turns are a shape neither vendor owes
            # us an answer for.
            last["content"].append(text_block(FINAL_TURN_TEXT))
        else:
            self.messages.append({"role": "user", "content": [text_block(FINAL_TURN_TEXT)]})
        try:
            turn = await self._turn([])
        except ProviderError as exc:
            if exc.kind == "context_window":
                await self._fail_context_window(exc)
            else:
                await self._fail_provider(exc)
            return

        if turn.assistant_blocks:
            self.messages.append({"role": "assistant", "content": turn.assistant_blocks})

        # A tool-less body cannot legally produce a tool call, but a gateway that ignores the
        # absent tool list still can — and if what it produced was `finish`, that is the ending
        # this turn existed to get.
        for block in turn.tool_use_blocks:
            if str(block.get("name") or "") != "finish":
                continue
            raw_input = block.get("input")
            arguments = raw_input if isinstance(raw_input, dict) else {}
            summary = str(arguments.get("summary") or "").strip()
            if not summary:
                continue
            await self._add_step(
                "tool_result",
                name="finish",
                payload={
                    "tool_use_id": block.get("id"),
                    "result": {"ok": True, **arguments},
                    "iteration": self.iterations,
                    "final_turn": True,
                },
                ok=True,
            )
            await self._finalize(
                STATUS_FINISHED,
                decision=_decision_of(arguments.get("decision")),
                summary=summary,
            )
            return

        summary = turn.text.strip() or turn.thinking.strip()
        await self._finalize(
            STATUS_INCOMPLETE,
            summary=(
                summary
                or (
                    f"The run used all {self.max_iterations} tool iterations without calling "
                    f"finish, and the final turn produced no summary."
                )
            ),
            error=(
                f"the run reached the {self.max_iterations}-iteration cap without calling finish"
            ),
        )

    # ---- failure paths -----------------------------------------------------

    async def _fail_context_window(self, exc: ProviderError) -> None:
        """Record a context-window overflow that survived one trim as a hard stop."""
        message = (
            f"{exc.message} — the run was stopped after trimming did not make it fit"
            if self.trimmed
            else exc.message
        )
        await self._add_step(
            "error",
            name="context_window",
            payload={"message": message, "iteration": self.iterations, "detail": exc.detail},
            ok=False,
        )
        self._emit(
            events.SYSTEM_WARNING,
            reason="context_window",
            message=message,
            model=self.model,
            provider_kind=self.provider_kind,
        )
        log.warning("run_context_window run_id=%s error=%s", self.run_id, message)
        await self._finalize(STATUS_ERROR, error=message, summary=None)

    async def _fail_provider(self, exc: ProviderError) -> None:
        """Record a provider failure, redden the provider dot, and let the worker live on."""
        await self._add_step(
            "error",
            name=exc.kind,
            payload={
                "message": exc.message,
                "kind": exc.kind,
                "status": exc.status,
                "iteration": self.iterations,
                "detail": exc.detail,
            },
            ok=False,
        )
        self.runtime.set_status("provider", False, exc.message)
        self._emit(
            events.SYSTEM_WARNING,
            reason="provider_failed",
            message=exc.message,
            provider_kind=self.provider_kind,
            model=self.model,
            status=exc.status,
            error_kind=exc.kind,
        )
        log.warning(
            "run_provider_failed run_id=%s kind=%s error=%s", self.run_id, exc.kind, exc.message
        )
        await self._finalize(STATUS_ERROR, error=exc.message, summary=None)

    # ---- ending ------------------------------------------------------------

    async def _finalize(
        self,
        status: str,
        *,
        decision: str | None = None,
        summary: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Stamp the run row, emit ``run.finished`` exactly once, and build the return value."""
        if self._finalized:
            return self.result()
        if (
            isinstance(self.fast_entry, dict)
            and self.fast_entry.get("ok")
            and status == STATUS_FINISHED
            and decision != "acted"
        ):
            # The order is already at the broker. "ignored" would hide that fill.
            decision = "acted"
            prefix = "Fast entry already sent the order."
            summary = f"{prefix} {summary}".strip() if summary else prefix
        self._finalized = True
        self.status = status
        self.decision = decision
        self.summary = summary
        self.error = error
        if status == STATUS_ERROR and error:
            notifier = getattr(self.runtime, "notifier", None)
            if notifier is not None:
                try:
                    text = notifier.render_rejected(action="signal", signal_ref=self.signal_ref,
                                                    message=redact(str(error)))
                    notifier.fire("rejected", text)
                except Exception as exc:
                    log.warning("notify_run_error_failed error=%s", exc)
        await self._update_run(
            status=status,
            decision=decision,
            summary=summary,
            error=error,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            latency_ms=self.provider_latency_ms,
            iterations=self.iterations,
            finished_at=utcnow(),
        )
        self._emit(
            events.RUN_FINISHED,
            status=status,
            decision=decision,
            summary=summary,
            error=error,
            iterations=self.iterations,
            tool_calls=self.tool_calls,
            write_calls=self.write_calls,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            latency_ms=self.provider_latency_ms,
            wall_ms=int((time.monotonic() - self.started_at) * 1000),
            provider_kind=self.provider_kind,
            model=self.model,
            message_row_id=self.message_row_id,
            source_id=self.source_id,
            signal_ref=self.signal_ref,
            armed=self.armed,
            fast_entry=self.fast_entry,
            soft_template=self.soft_template,
        )
        log.info(
            "run_finished run_id=%s kind=%s status=%s decision=%s iterations=%d tools=%d "
            "in=%d out=%d ms=%d",
            self.run_id,
            self.kind,
            status,
            decision,
            self.iterations,
            self.tool_calls,
            self.input_tokens,
            self.output_tokens,
            self.provider_latency_ms,
        )
        return self.result()

    def result(self) -> dict[str, Any]:
        """The dict handed back to the ingest worker and to the watchdog."""
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "status": self.status,
            "decision": self.decision,
            "summary": self.summary,
            "error": self.error,
            "iterations": self.iterations,
            "tool_calls": self.tool_calls,
            "write_calls": self.write_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.provider_latency_ms,
            "provider_kind": self.provider_kind,
            "model": self.model,
            "armed_at_start": self.armed,
            "message_row_id": self.message_row_id,
            "source_id": self.source_id,
            "signal_ref": self.signal_ref,
            "trimmed": self.trimmed,
            "context": dict(self.context_meta),
            "fast_entry": self.fast_entry,
            "soft_template": self.soft_template,
        }

    # ---- entry point -------------------------------------------------------

    async def _triage_gate(self) -> dict[str, Any] | None:
        """Ask the configured model whether a signal post is actionable.

        Only ``kind="signal"`` is classified. A clear ``no`` finishes the run as ``ignored``
        and does not enter the tool loop. ``yes`` and ``uncertain`` (including a provider
        error) fall through so a real entry is never dropped by the classifier. This call
        does not increment ``iterations`` and does not change ``max_tokens``.
        """
        if self.kind != "signal" or self.provider is None or self.caps is None:
            return None
        # Local import: triage imports the provider types, and this module is imported
        # while the provider package is still loading in some tests.
        from app.ai.triage import classify_signal, triage_is_skip
        from app.reviewer.brief import load_active_brief

        channel_brief = ""
        data_dir = getattr(self.settings, "DATA_DIR", None)
        if data_dir:
            try:
                channel_brief = load_active_brief(data_dir)
            except Exception as exc:  # noqa: BLE001 - triage still runs without a playbook
                log.warning("triage_brief_unavailable error=%s", exc)
                channel_brief = ""

        started = time.monotonic()
        verdict = await classify_signal(
            self.provider,
            model=self.model or "",
            capabilities=self.caps,
            text=self.triage_text,
            image_count=self.triage_image_count,
            channel_brief=channel_brief,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        self.provider_latency_ms += latency_ms
        await self._add_step(
            "provider",
            name="triage",
            payload={
                "verdict": verdict,
                "model": self.model,
                "provider_kind": self.provider_kind,
                "max_tokens": self.max_tokens,
                "tools": False,
                "latency_ms": latency_ms,
                "channel_brief": bool(channel_brief.strip()),
            },
            ok=True,
        )
        self._emit(
            events.PROVIDER_REQUEST,
            provider_kind=self.provider_kind,
            model=self.model,
            iteration=0,
            latency_ms=latency_ms,
            ok=True,
            triage=True,
            verdict=verdict,
        )
        self.triage_verdict = verdict
        if not triage_is_skip(verdict):
            return None
        summary = (
            "triage said this post is not an actionable trade instruction, so the full "
            "tool loop did not run"
        )
        log.info("run_triage_skip run_id=%s model=%s", self.run_id, self.model)
        return await self._finalize(STATUS_FINISHED, decision="ignored", summary=summary)

    def _executor_ready(self) -> bool:
        """The fast path needs the real broker methods. A test double that is not one is skipped."""
        executor = getattr(self.runtime, "executor", None)
        return all(
            callable(getattr(executor, name, None))
            for name in ("resolve_symbol", "get_quote", "open_market", "place_pending")
        )

    def _append_fast_note(self, text: str) -> None:
        """Add the fast-entry record to the opening user turn, which is the only turn so far."""
        if not self.messages:
            self.messages.append({"role": "user", "content": [text_block(text)]})
            return
        content = self.messages[0].get("content")
        if isinstance(content, list):
            content.append(text_block(text))
            return
        self.messages.append({"role": "user", "content": [text_block(text)]})

    async def _maybe_soft_template(self, message_row: Message | None) -> dict[str, Any] | None:
        """Handle a Soft ``#SOFT`` post without calling the model.

        Returns a finalized run dict when this path owns the post (complete Soft match
        or Soft tag with an incomplete parse). Returns ``None`` when the post has no
        Soft tag so the existing LLM path continues unchanged.
        """
        if self.kind != "signal" or message_row is None:
            return None
        text = str(getattr(message_row, "text", None) or "")
        from app.ai.soft_template import (
            SOFT_MODEL,
            SOFT_PROVIDER_KIND,
            SoftParse,
            has_soft_tag,
            parse_soft,
        )

        if not has_soft_tag(text):
            return None

        # Soft owns this post from here: never fall through to triage / the LLM.
        self.provider_kind = SOFT_PROVIDER_KIND
        self.model = SOFT_MODEL
        await self._update_run(provider_kind=self.provider_kind, model=self.model)

        parsed: SoftParse = parse_soft(text)
        await self._add_step(
            "soft_template",
            name="parse",
            payload=parsed.as_dict(),
            ok=parsed.ok,
        )
        if not parsed.ok:
            reason = parsed.reason or "soft template incomplete"
            self.soft_template = {"ok": False, "reason": reason, "parse": parsed.as_dict()}
            log.info("soft_template_fail_closed run_id=%s reason=%s", self.run_id, reason)
            return await self._finalize(STATUS_ERROR, error=reason, summary=reason)

        if not self._executor_ready():
            message = (
                "soft template matched but the trading executor is not available, so the "
                "two Soft legs could not be opened"
            )
            self.soft_template = {
                "ok": False,
                "reason": message,
                "parse": parsed.as_dict(),
            }
            await self._add_step(
                "error", name="soft_template", payload={"message": message}, ok=False
            )
            return await self._finalize(STATUS_ERROR, error=message, summary=message)

        resolved = await self.runtime.executor.resolve_symbol(str(parsed.symbol))
        broker_symbol: str | None = None
        if isinstance(resolved, dict) and resolved.get("ok"):
            candidates = resolved.get("candidates")
            if isinstance(candidates, list):
                for row in candidates:
                    if isinstance(row, dict) and row.get("tradable", True) and row.get("name"):
                        broker_symbol = str(row["name"])
                        break
        if not broker_symbol:
            message = (
                f"soft template could not resolve symbol {parsed.symbol!r} on this account"
            )
            self.soft_template = {
                "ok": False,
                "reason": message,
                "parse": parsed.as_dict(),
            }
            await self._add_step(
                "error", name="soft_template", payload={"message": message}, ok=False
            )
            return await self._finalize(STATUS_ERROR, error=message, summary=message)

        ctx = RunContext(
            run_id=self.run_id,
            signal_ref=self.signal_ref,
            source_id=self.source_id,
            message_row_id=self.message_row_id,
            origin="agent",
        )
        arguments = {
            "symbol": broker_symbol,
            "side": parsed.side,
            "sl": parsed.sl,
            "tp": [parsed.tp1, parsed.tp2],
            "reason": "soft template #SOFT",
        }
        call_id = "soft-template"
        await self._add_step(
            "tool_call",
            name="open_market",
            payload={
                "tool_use_id": call_id,
                "input": arguments,
                "iteration": 0,
                "write": True,
                "soft_template": True,
            },
        )
        self._emit(
            events.RUN_TOOL_CALL,
            idx=self.step_idx,
            iteration=0,
            tool_use_id=call_id,
            name="open_market",
            input=_jsonable(arguments),
            write=True,
            soft_template=True,
        )
        self.tool_calls += 1
        self.write_calls += 1
        try:
            result = await self.runtime.executor.open_market(ctx, **arguments)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - Soft path still fail-closes, never LLM
            message = f"{type(exc).__name__}: {exc}"
            log.warning("soft_template_call_failed run_id=%s error=%s", self.run_id, message)
            result = {"ok": False, "message": message, "action": "open_market"}
        if not isinstance(result, dict):
            result = {"ok": False, "message": f"open_market returned {type(result).__name__}"}

        ok = bool(result.get("ok"))
        summary = str(result.get("message") or "").strip() or (
            "soft template opened two market legs" if ok else "soft template open refused"
        )
        await self._add_step(
            "tool_result",
            name="open_market",
            payload={
                "tool_use_id": call_id,
                "result": result,
                "iteration": 0,
                "write": True,
                "soft_template": True,
            },
            ok=ok,
        )
        self._emit(
            events.RUN_TOOL_RESULT,
            idx=self.step_idx,
            iteration=0,
            tool_use_id=call_id,
            name="open_market",
            ok=ok,
            result=_jsonable(result),
            write=True,
            soft_template=True,
        )
        tickets = result.get("tickets")
        if not isinstance(tickets, list):
            ticket = result.get("ticket")
            tickets = [ticket] if ticket else []
        self.soft_template = {
            "ok": ok,
            "action": str(result.get("action") or "open_market"),
            "symbol": result.get("symbol") or parsed.symbol,
            "side": result.get("side") or parsed.side,
            "sl": parsed.sl,
            "tp1": parsed.tp1,
            "tp2": parsed.tp2,
            "entry": parsed.entry,
            "tickets": tickets,
            "volume_source": result.get("volume_source"),
            "split_count": result.get("split_count"),
            "summary": summary,
            "parse": parsed.as_dict(),
        }
        if ok:
            entry_law = getattr(self, "entry_law", None)
            if entry_law is not None:
                entry_law.opened = True
            log.info(
                "soft_template_sent run_id=%s symbol=%s side=%s tickets=%s",
                self.run_id,
                self.soft_template["symbol"],
                self.soft_template["side"],
                tickets,
            )
            return await self._finalize(STATUS_FINISHED, decision="acted", summary=summary)

        log.info("soft_template_refused run_id=%s message=%s", self.run_id, summary)
        return await self._finalize(STATUS_ERROR, error=summary, summary=summary)

    async def _maybe_fast_entry(self) -> None:
        """Send one entry before the tool loop when triage said yes and the post can be read.

        Uncertain triage does not trade here: the full agent still runs, so a classifier
        failure cannot drop a signal and cannot open one blindly. A failure of this path
        is recorded and then left to that same loop. ``max_tokens`` and the iteration cap
        are not touched.
        """
        if self.kind != "signal" or self.triage_verdict != "yes" or not self._executor_ready():
            return
        from app.ai.fast_entry import ALREADY_SENT_TITLE, FastSkip, plan_fast_entry

        meta = self.context_meta or {}
        age = meta.get("signal_age_seconds")
        try:
            age_seconds = None if age is None else float(age)
        except (TypeError, ValueError):
            age_seconds = None
        stale_flag = meta.get("stale")
        stale = stale_flag if isinstance(stale_flag, bool) else None
        try:
            outcome = await plan_fast_entry(
                self.runtime.executor,
                text=self.triage_text,
                signal_ref=self.signal_ref,
                age_seconds=age_seconds,
                catchup=bool(meta.get("catchup")),
                stale=stale,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a planner bug must not drop the post
            log.warning("fast_entry_plan_failed run_id=%s error=%s", self.run_id, exc)
            return
        if isinstance(outcome, FastSkip):
            self._append_fast_note(outcome.note)
            self.fast_entry = {
                "ok": False,
                "action": "skip",
                "reason": outcome.reason,
                "intent": "skip",
                "tool": None,
                "symbol": outcome.symbol,
                "side": outcome.side,
                "entry": outcome.entry,
                "distance": outcome.distance,
                "limit": outcome.limit,
                "tickets": [],
                "now_hint": False,
                "summary": outcome.note,
            }
            log.info(
                "fast_entry_skipped run_id=%s reason=%s distance=%s",
                self.run_id,
                outcome.reason,
                outcome.distance,
            )
            return
        plan = outcome
        if plan is None:
            return

        ctx = RunContext(
            run_id=self.run_id,
            signal_ref=self.signal_ref,
            source_id=self.source_id,
            message_row_id=self.message_row_id,
            origin="agent",
        )
        call_id = "fast-entry"
        await self._add_step(
            "tool_call",
            name=plan.tool,
            payload={
                "tool_use_id": call_id,
                "input": plan.arguments,
                "iteration": 0,
                "write": True,
                "fast_entry": True,
                "intent": plan.intent,
                "distance": plan.distance,
                "limit": plan.limit,
            },
        )
        self._emit(
            events.RUN_TOOL_CALL,
            idx=self.step_idx,
            iteration=0,
            tool_use_id=call_id,
            name=plan.tool,
            input=_jsonable(plan.arguments),
            write=True,
            fast_entry=True,
        )
        self.tool_calls += 1
        self.write_calls += 1
        try:
            method = getattr(self.runtime.executor, plan.tool)
            result = await method(ctx, **plan.arguments)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - fall through to the full agent
            message = f"{type(exc).__name__}: {exc}"
            log.warning("fast_entry_call_failed run_id=%s error=%s", self.run_id, message)
            result = {"ok": False, "message": message, "action": plan.intent}
        if not isinstance(result, dict):
            result = {"ok": False, "message": f"{plan.tool} returned {type(result).__name__}"}

        ok = bool(result.get("ok"))
        action = str(result.get("action") or plan.intent)
        await self._add_step(
            "tool_result",
            name=plan.tool,
            payload={
                "tool_use_id": call_id,
                "result": result,
                "iteration": 0,
                "write": True,
                "fast_entry": True,
                "intent": plan.intent,
            },
            ok=ok,
        )
        self._emit(
            events.RUN_TOOL_RESULT,
            idx=self.step_idx,
            iteration=0,
            tool_use_id=call_id,
            name=plan.tool,
            ok=ok,
            result=_jsonable(result),
            write=True,
            fast_entry=True,
        )
        tickets = result.get("tickets")
        if not isinstance(tickets, list):
            ticket = result.get("ticket")
            tickets = [ticket] if ticket else []
        summary = str(result.get("message") or "").strip()
        self.fast_entry = {
            "ok": ok,
            "action": action,
            "intent": plan.intent,
            "tool": plan.tool,
            "symbol": result.get("symbol") or plan.symbol,
            "side": result.get("side") or plan.side,
            "entry": plan.entry,
            "distance": plan.distance,
            "limit": plan.limit,
            "tickets": tickets,
            "kind": result.get("kind"),
            "volume_source": result.get("volume_source"),
            "now_hint": plan.now_hint,
            "summary": summary,
        }
        if ok:
            self.entry_law.opened = True
            ticket_text = ", ".join(f"#{ticket}" for ticket in tickets) or "the tickets in the result"
            if action == "modify_existing":
                detail = (
                    "Those tickets already belonged to this chain. The posted stop and targets "
                    "were written onto them. No new pair was opened."
                )
            else:
                detail = "That order is already at the broker."
            self._append_fast_note(
                f"=== {ALREADY_SENT_TITLE} ===\n"
                f"Action: {action}. Symbol: {self.fast_entry['symbol']}. "
                f"Side: {self.fast_entry['side']}. Tickets: {ticket_text}.\n"
                f"{detail} {summary}\n"
                "Do not call open_market or place_pending again for this signal. "
                "You may notify, summarise, or modify these tickets if the post still asks "
                "for a level that was not applied. Then finish with decision \"acted\"."
            )
            log.info(
                "fast_entry_sent run_id=%s action=%s symbol=%s distance=%s limit=%s",
                self.run_id,
                action,
                plan.symbol,
                plan.distance,
                plan.limit,
            )
        else:
            self._append_fast_note(
                "=== FAST ENTRY ATTEMPT ===\n"
                f"A deterministic entry was attempted and the broker did not accept it: {summary}\n"
                "Decide with the tools. Do not repeat an identical refused order."
            )
            log.info("fast_entry_refused run_id=%s message=%s", self.run_id, summary)

    async def execute(self) -> dict[str, Any]:
        """Set the run up and drive it; every failure ends as a recorded error run."""
        message_row = await self._load_target()
        await self._create_run()

        if self.kind == "signal" and await self._chain_already_closed():
            summary = (
                "The channel deleted this signal or posted a full close. "
                "Exposure is closed without a model call."
            )
            log.info("run_exposure_closed run_id=%s signal_ref=%s", self.run_id, self.signal_ref)
            return await self._finalize(STATUS_FINISHED, decision="ignored", summary=summary)

        soft = await self._maybe_soft_template(message_row)
        if soft is not None:
            return soft

        refusal = await self._resolve_provider()
        if refusal is not None:
            await self._add_step(
                "error", name="provider", payload={"message": refusal}, ok=False
            )
            self.runtime.set_status("provider", False, refusal)
            self._emit(events.SYSTEM_WARNING, reason="provider_unconfigured", message=refusal)
            log.warning("run_no_provider run_id=%s reason=%s", self.run_id, refusal)
            return await self._finalize(STATUS_ERROR, error=refusal)

        if getattr(self.runtime, "executor", None) is None:
            message = (
                "the trading executor is not available, so no tool the model calls could reach "
                "the account — the run was stopped before it started"
            )
            await self._add_step(
                "error", name="executor", payload={"message": message}, ok=False
            )
            self._emit(events.SYSTEM_WARNING, reason="run_failed", message=message)
            log.error("run_no_executor run_id=%s", self.run_id)
            return await self._finalize(STATUS_ERROR, error=message)

        context_error = await self._build_messages(message_row)
        if context_error is not None:
            await self._add_step(
                "error", name="context", payload={"message": context_error}, ok=False
            )
            log.warning("run_no_context run_id=%s reason=%s", self.run_id, context_error)
            return await self._finalize(STATUS_ERROR, error=context_error)

        await self._build_system_prompt()

        skipped = await self._triage_gate()
        if skipped is not None:
            return skipped

        await self._maybe_fast_entry()

        if self.caps is not None and not self.caps.supports_tools:
            # Without tools the model can never call finish, so a full iteration budget of
            # nudging would buy nothing but billed requests. Say so once, in red.
            message = (
                f"the selected model {self.model!r} does not support tool use, so it cannot "
                f"trade or end a run with finish — choose a tool-capable model in Settings"
            )
            await self._add_step(
                "error", name="capabilities", payload={"message": message}, ok=False
            )
            self._emit(
                events.SYSTEM_WARNING,
                reason="provider_no_tools",
                message=message,
                model=self.model,
                provider_kind=self.provider_kind,
            )
            log.warning("run_model_without_tools run_id=%s model=%s", self.run_id, self.model)
            if isinstance(self.fast_entry, dict) and self.fast_entry.get("ok"):
                sent = str(self.fast_entry.get("summary") or "Fast entry sent the order.")
                return await self._finalize(STATUS_FINISHED, decision="acted", summary=sent)
            return await self._finalize(STATUS_INCOMPLETE, summary=message, error=message)

        await self._iterate()
        return await self._finalize(
            self.status if self._finalized else STATUS_INCOMPLETE,
            decision=self.decision,
            summary=self.summary,
            error=self.error,
        )


# --------------------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------------------


#: How many agent runs (triage or the full tool loop, including the SL watchdog) may be
#: inside ``execute`` at once. Broker writes are a separate, stricter lock in the executor:
#: a read-only celebration must not hold a real entry, and two opens must not interleave.
def _concurrent_run_limit() -> int:
    """Clamp the configured ingest-worker count onto the 1–3 run-slot ceiling."""
    try:
        raw = int(getattr(_settings, "INGEST_WORKERS", 3))
    except (TypeError, ValueError):
        raw = 3
    return max(1, min(3, raw))


#: One semaphore per running loop. Created on first use so a test's loop does not inherit a
#: semaphore bound to a previous test's loop. Production has one loop, so one semaphore.
_RUN_SLOTS: dict[int, asyncio.Semaphore] = {}


def run_slots() -> asyncio.Semaphore:
    """The agent-run semaphore for the running loop. At most three runs may hold it."""
    loop = asyncio.get_running_loop()
    key = id(loop)
    slots = _RUN_SLOTS.get(key)
    if slots is None:
        slots = asyncio.Semaphore(_concurrent_run_limit())
        _RUN_SLOTS[key] = slots
    return slots


#: One lock per reply-chain per running loop. Same rule as ``_RUN_SLOTS``: a lock created on
#: a finished test loop must not be reused on the next one.
_CHAIN_LOCKS: dict[int, dict[str, asyncio.Lock]] = {}


def _chain_lock(key: str) -> asyncio.Lock:
    """The mutex for one signal chain on the running loop.

    Created without awaiting. Two tasks cannot interleave inside this function, so the
    first caller to miss the key installs the lock the second caller then finds.
    """
    loop_id = id(asyncio.get_running_loop())
    bucket = _CHAIN_LOCKS.get(loop_id)
    if bucket is None:
        bucket = {}
        _CHAIN_LOCKS[loop_id] = bucket
    lock = bucket.get(key)
    if lock is None:
        lock = asyncio.Lock()
        bucket[key] = lock
    return lock


async def _chain_key_for(message_row_id: int | None, kind: str) -> str | None:
    """The chain key for a signal run, or None when this run must not wait on one.

    A lookup failure does not drop the post. The duplicate guard still refuses a second
    open once the first chain has tickets; waiting is the part that stops the race.
    """
    if kind != "signal" or message_row_id is None:
        return None
    try:
        return await guards.chain_run_key(int(message_row_id))
    except Exception as exc:  # noqa: BLE001 - a missing row must not stall the worker
        log.warning("chain_key_failed row_id=%s error=%s", message_row_id, exc)
        return None


#: Provider failures worth trying again. A 500/502/503/504/529 from a gateway is a hiccup,
#: and a 429 is an explicit "later" — neither says the request was wrong. Auth, not_found and
#: bad_request are absent on purpose: they fail the same way forever.
_RETRYABLE_PROVIDER_KINDS: Final[frozenset[str]] = frozenset({"overloaded", "rate_limit", "transport"})

#: How many times one turn may be retried before the run is failed.
MAX_PROVIDER_RETRIES: Final[int] = 3


def _retry_delay(exc: ProviderError, attempt: int) -> float:
    """Seconds to wait before retrying, honouring an explicit Retry-After when given."""
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    for key in ("retry_after", "retry_after_seconds", "Retry-After"):
        value = detail.get(key)
        if value is None:
            continue
        try:
            explicit = float(value)
        except (TypeError, ValueError):
            continue
        # Cap it: a gateway asking for ten minutes should not hold a worker slot that long.
        return max(1.0, min(explicit, 30.0))
    return min(2.0 * (3.0 ** attempt), 30.0)


async def run_agent(
    runtime: Runtime,
    *,
    message_row_id: int | None,
    kind: str = "signal",
    instruction: str | None = None,
) -> dict[str, Any]:
    """Run the agent once and return how it ended; this function does not raise.

    ``kind`` is ``"signal"`` for an ingested post (which needs ``message_row_id``),
    ``"sl_watchdog"`` for the stop-loss watchdog's one-job run, or ``"manual"`` for an operator
    instruction. The returned dict always carries ``run_id``, ``status``, ``decision``, ``summary``
    and ``error``; the ingest worker reads ``status`` and ``decision`` to mark its message row.

    An exception anywhere — a dead sidecar, a rejected key, a bug in a tool — is recorded on the
    ``runs`` row as ``status="error"`` and returned. It never reaches the caller, because a worker
    must survive to read the next message.

    Up to three runs may execute at once (see :func:`run_slots`). Posts that share a reply
    chain do not: the teaser holds the chain lock until its run finishes, and the ladder
    reply waits, so it sees the tickets the teaser opened. Unrelated posts still use the
    other slots. The duplicate ``signal_ref`` guard and the executor's write lock still
    admit only one broker write at a time.
    """
    run = _AgentRun(
        runtime,
        run_id=uuid.uuid4().hex,
        message_row_id=message_row_id,
        kind=kind,
        instruction=instruction,
    )
    notifier = getattr(runtime, "notifier", None)
    begin = getattr(notifier, "begin_run", None)
    if callable(begin):
        begin(run.run_id)
    try:
        chain_key = await _chain_key_for(message_row_id, kind)
        if chain_key:
            lock = _chain_lock(chain_key)
            if lock.locked():
                log.info(
                    "chain_run_queued key=%s row_id=%s",
                    chain_key,
                    message_row_id,
                )
            async with lock:
                async with run_slots():
                    return await run.execute()
        async with run_slots():
            return await run.execute()
    except asyncio.CancelledError:
        # Shutdown. Record what we can without awaiting anything that could raise again, then
        # let the cancellation continue: the ingest worker re-raises it too.
        try:
            await run._finalize(  # noqa: SLF001 - same module, deliberate
                STATUS_ERROR, error="the agent run was cancelled"
            )
        except BaseException:  # noqa: BLE001 - a cancelled task cannot insist on its epitaph
            log.warning("run_cancel_record_failed run_id=%s", run.run_id)
        raise
    except Exception as exc:  # noqa: BLE001 - nothing may escape into the ingest worker
        message = f"{type(exc).__name__}: {exc}"
        log.exception("run_crashed run_id=%s error=%s", run.run_id, message)
        try:
            await run._add_step(  # noqa: SLF001 - same module, deliberate
                "error", name="crash", payload={"message": message}, ok=False
            )
            run.bus.emit(
                events.SYSTEM_WARNING,
                reason="run_failed",
                run_id=run.run_id,
                message=message,
            )
            return await run._finalize(STATUS_ERROR, error=message)  # noqa: SLF001
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the store is gone too; answer from memory
            run.status = STATUS_ERROR
            run.error = message
            return run.result()

    finally:
        flush = getattr(notifier, "flush_run", None)
        if callable(flush):
            try:
                flush(run.run_id)
            except Exception as exc:
                log.warning("notify_run_flush_failed run_id=%s error=%s", run.run_id, exc)
