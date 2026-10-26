"""A short yes/no call on the provider the operator already configured.

Celebrations and commentary used to occupy the only agent worker for a full tool loop.
This call asks one question — is the post a trading signal or an actionable trade
instruction? — and returns before any tool is offered. It does not pick a model: the
caller passes the Settings provider and model. It does not lower the full agent's
``max_tokens`` or iteration cap; it spends its own small budget, with extended thinking
left off so the answer stays a word.

Anything other than a clear ``no`` is treated as uncertain by the caller, which then runs
the full agent. A classifier hiccup must not drop a real entry.

When the channel reviewer has finished a playbook, that text is appended to this
prompt so celebration posts are easier to skip. The playbook is a hint about the
channel, not a second copy of the post. No playbook means this prompt is unchanged.
"""

from __future__ import annotations

import re
from typing import Any

from app.ai.base import Capabilities, DoneDelta, Provider, TextDelta, text_block
from app.config import TRIAGE_MAX_TOKENS
from app.reviewer.brief import BriefIsolationError, load_active_brief
from app.util.logging import get_logger

log = get_logger(__name__)

#: How much of the post is sent. The question is about the post, not its whole history.
TRIAGE_TEXT_CHARS: int = 4000

TRIAGE_SYSTEM: str = (
    "You classify one Telegram post for a trading desk. Reply with exactly one word: yes or no.\n"
    "yes — the post is a trading signal or an actionable trade instruction: a new entry "
    "(even when the stop or the targets are missing), a pending order, a stop or take-profit "
    "update, a close, a partial, or any instruction that should change a position.\n"
    "no — the post does not ask for a trade action: celebration, commentary, greeting, a "
    "target-hit note that does not change a position, or anything else that should be ignored.\n"
    "Do not explain. Do not call tools. Do not invent prices."
)

_YES: frozenset[str] = frozenset({"yes", "y"})
_NO: frozenset[str] = frozenset({"no", "n"})
_TOKEN: re.Pattern[str] = re.compile(r"[a-zA-Z]+")

#: Same ceiling the reviewer writes. Triage never appends more than this, even if a
#: hand-edited file is longer.
_TRIAGE_BRIEF_LINES: int = 60

_BRIEF_INTRO: str = (
    "Channel playbook for the source this post came from. It describes which shapes "
    "are real signals and which are celebration or noise to skip. It is a hint about "
    "this channel, not a description of the post in front of you. If the post is a "
    "real entry or an instruction that should change a position, answer yes even when "
    "the playbook does not mention that wording."
)


def render_triage_system(channel_brief: str | None, *, max_lines: int | None = None) -> str:
    """The triage system prompt, with the active playbook appended when there is one."""
    body = (channel_brief or "").strip()
    if not body:
        return TRIAGE_SYSTEM
    limit = _TRIAGE_BRIEF_LINES
    if max_lines is not None:
        try:
            limit = max(1, min(_TRIAGE_BRIEF_LINES, int(max_lines)))
        except (TypeError, ValueError):
            limit = _TRIAGE_BRIEF_LINES
    clipped = "\n".join(body.splitlines()[:limit]).strip()
    if not clipped:
        return TRIAGE_SYSTEM
    return f"{TRIAGE_SYSTEM}\n\n{_BRIEF_INTRO}\n\n{clipped}"


def system_for_triage(data_dir: str | None) -> str:
    """Load the active brief and build the triage prompt. Backups are never opened."""
    if not data_dir:
        return TRIAGE_SYSTEM
    try:
        brief = load_active_brief(data_dir)
    except BriefIsolationError:
        log.warning("triage_brief_isolated")
        return TRIAGE_SYSTEM
    except Exception as exc:  # noqa: BLE001 - a missing playbook must not drop the post
        log.warning("triage_brief_unavailable error=%s", exc)
        return TRIAGE_SYSTEM
    return render_triage_system(brief)


def parse_triage_verdict(raw: str) -> str:
    """Map the model's reply to ``yes``, ``no``, or ``uncertain``.

    Only the first word is read. A sentence, a hedge, or an empty turn is uncertain so
    the caller can fail open into the full agent. This is the classifier's answer, not a
    filter on the channel's wording.
    """
    match = _TOKEN.search(raw or "")
    if match is None:
        return "uncertain"
    word = match.group(0).lower()
    if word in _YES:
        return "yes"
    if word in _NO:
        return "no"
    return "uncertain"


def _user_text(text: str, image_count: int) -> str:
    """The post, clipped, plus a note when it carries images the cheap call does not attach."""
    body = (text or "")[:TRIAGE_TEXT_CHARS]
    if not body.strip():
        body = "(no text)"
    if image_count > 0:
        body += (
            f"\n\n[The post includes {int(image_count)} image(s). "
            "A chart can be an actionable signal even with little text.]"
        )
    return body


async def classify_signal(
    provider: Provider,
    *,
    model: str,
    capabilities: Capabilities,
    text: str,
    image_count: int = 0,
    max_tokens: int | None = None,
    channel_brief: str | None = None,
) -> str:
    """Ask ``provider`` / ``model`` whether the post is actionable.

    Returns ``yes``, ``no``, or ``uncertain``. Tools are not offered. Extended thinking is
    not requested on this call; the full agent keeps whatever thinking form Settings probed.
    Provider errors become ``uncertain`` rather than a skipped signal.
    """
    budget = int(max_tokens) if max_tokens is not None else int(TRIAGE_MAX_TOKENS)
    quiet = Capabilities(
        supports_vision=False,
        supports_tools=bool(getattr(capabilities, "supports_tools", False)),
        thinking_form=None,
        source="triage",
    )
    chunks: list[str] = []
    try:
        stream = provider.run(
            system=render_triage_system(channel_brief),
            messages=[{"role": "user", "content": [text_block(_user_text(text, image_count))]}],
            tools=[],
            max_tokens=max(1, budget),
            model=model or "",
            capabilities=quiet,
        )
        async for delta in stream:
            if isinstance(delta, TextDelta) and delta.text:
                chunks.append(delta.text)
            elif isinstance(delta, DoneDelta):
                break
    except Exception as exc:  # noqa: BLE001 - a failed classifier must not drop the post
        log.warning("triage_failed model=%s error=%s", model, exc)
        return "uncertain"
    verdict = parse_triage_verdict("".join(chunks))
    log.info("triage model=%s verdict=%s chars=%d", model, verdict, len("".join(chunks)))
    return verdict


def triage_is_skip(verdict: Any) -> bool:
    """True only for a clear no. Uncertain and yes both continue to the full agent."""
    return str(verdict or "") == "no"
