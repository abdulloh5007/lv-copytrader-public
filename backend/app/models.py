"""SQLAlchemy 2.0 ORM models for LV Copytrader — the whole persisted schema (CONTRACTS.md §7)."""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Return the current UTC instant as a timezone-naive datetime (the storage convention)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Declarative base shared by every LV Copytrader table."""


class AppSetting(Base):
    """Key/value settings row; `is_secret` values are Fernet-encrypted at rest."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_secret: Mapped[bool] = mapped_column(default=False, nullable=False)
    needs_reentry: Mapped[bool] = mapped_column(default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, nullable=False
    )


class Source(Base):
    """A signal source (a Telegram chat); exactly one row is `is_active` at a time."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), default="telegram", nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=False, nullable=False)
    last_message_id: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class Message(Base):
    """One ingested Telegram message revision — the unit the agent runs on."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    # RESTRICT, not SET NULL: a source with messages must not be deletable, because
    # ingested history is never orphaned or destroyed. Spelling it RESTRICT makes the
    # database refuse the delete with an honest foreign-key error; SET NULL on a NOT NULL
    # column reached the same outcome only by way of a confusing NOT NULL violation.
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    message_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    # NEVER NULL (BUILD_PROMPT §3.5): un-edited messages store 0. SQLite treats every NULL as
    # distinct, so a NULL here would let a Telethon reconnect replay insert a second row for the
    # same post and open the same trade twice — which is the one thing uq_message_dedup exists for.
    edit_date: Mapped[int] = mapped_column(default=0, nullable=False)
    revision: Mapped[int] = mapped_column(default=1, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    skip_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    catchup: Mapped[bool] = mapped_column(default=False, nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    permalink: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    received_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    grouped_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    image_count: Mapped[int] = mapped_column(default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Set when Telegram reports this channel post deleted. Every revision of the
    #: same message id is stamped. Open exposure on the chain is closed with it.
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    images: Mapped[list["MessageImage"]] = relationship(
        "MessageImage",
        order_by="MessageImage.order_index",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint("source_id", "message_id", "edit_date", name="uq_message_dedup"),
        Index("ix_messages_source_msg", "source_id", "message_id"),
    )

    @property
    def signal_ref(self) -> str:
        """Stable reference to the underlying post, identical across every edit revision."""
        return f"SIG:{self.source_id}:{self.message_id}"

    def __repr__(self) -> str:
        """Short debug representation."""
        return (
            f"<Message id={self.id} source_id={self.source_id} message_id={self.message_id} "
            f"rev={self.revision} kind={self.kind!r} status={self.status!r}>"
        )


class MessageImage(Base):
    """One downloaded image belonging to a message (albums keep every item)."""

    __tablename__ = "message_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_row_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True, nullable=False
    )
    order_index: Mapped[int] = mapped_column(nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[int | None] = mapped_column(nullable=True)
    height: Mapped[int | None] = mapped_column(nullable=True)
    bytes: Mapped[int] = mapped_column(nullable=False)


class Run(Base):
    """One agent run: a message (or watchdog trigger) processed to a `finish` decision."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    message_row_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), index=True, nullable=True
    )
    kind: Mapped[str] = mapped_column(String(16), default="signal", nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_tokens: Mapped[int] = mapped_column(default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(default=0, nullable=False)
    iterations: Mapped[int] = mapped_column(default=0, nullable=False)
    armed_at_start: Mapped[bool] = mapped_column(default=False, nullable=False)
    started_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    steps: Mapped[list["RunStep"]] = relationship(
        "RunStep",
        order_by="RunStep.idx",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    def __repr__(self) -> str:
        """Short debug representation."""
        return (
            f"<Run id={self.id} kind={self.kind!r} status={self.status!r} "
            f"decision={self.decision!r} iterations={self.iterations}>"
        )


class RunStep(Base):
    """One ordered step of a run: thinking, text, a tool call, its result, or an error."""

    __tablename__ = "run_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    idx: Mapped[int] = mapped_column(nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    ok: Mapped[bool | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class Trade(Base):
    """A position or pending order the system knows about, linked to the signal that caused it."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    run_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    message_row_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    signal_ref: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    ticket: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[int] = mapped_column(nullable=False)
    volume: Mapped[float] = mapped_column(nullable=False)
    requested_volume: Mapped[float | None] = mapped_column(nullable=True)
    entry_price: Mapped[float] = mapped_column(nullable=False)
    sl: Mapped[float] = mapped_column(default=0.0, nullable=False)
    tp: Mapped[float] = mapped_column(default=0.0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    magic: Mapped[int] = mapped_column(nullable=False)
    comment: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    external: Mapped[bool] = mapped_column(default=False, nullable=False)
    split_index: Mapped[int] = mapped_column(default=0, nullable=False)
    split_count: Mapped[int] = mapped_column(default=1, nullable=False)
    unmanaged_tps: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    #: ``"cover"`` / ``"runner"`` on a two-leg ladder; null on a classic split.
    ladder_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: Runner-after-cover policy chosen at open: ``be_after_cover`` | ``hold_channel_sl``.
    runner_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Server-suggested mode at the moment this ladder was opened (may differ from chosen).
    suggested_runner_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 1-based numbered-TP index the formula suggested (LONG excluded from N).
    suggested_runner_tp_index: Mapped[int | None] = mapped_column(nullable=True)
    #: 1-based index actually sent to the broker (suggested, or suggested ±1).
    chosen_runner_tp_index: Mapped[int | None] = mapped_column(nullable=True)
    #: Bias that produced the suggestion (0 / +1 / +2); never random.
    runner_tp_bias: Mapped[int | None] = mapped_column(nullable=True)
    open_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    close_cause: Mapped[str | None] = mapped_column(String(16), nullable=True)
    close_price: Mapped[float | None] = mapped_column(nullable=True)
    profit: Mapped[float | None] = mapped_column(nullable=True)
    profit_points: Mapped[float | None] = mapped_column(nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_trade_public_history", "magic", "external", "kind", "status", "closed_at"),
    )

    def __repr__(self) -> str:
        """Short debug representation."""
        return (
            f"<Trade id={self.id} ticket={self.ticket} {self.symbol!r} {self.side!r} "
            f"vol={self.volume} status={self.status!r} signal_ref={self.signal_ref!r}>"
        )


class LadderOutcome(Base):
    """One fully-closed ladder (both legs done), used to suggest the next runner mode.

    Written automatically from deals / trade rows when the last ticket of a ladder group
    closes. The AI does not fill this in. Aggregated per ``source_id`` over the last N
    rows to pick ``be_after_cover`` vs ``hold_channel_sl``.
    """

    __tablename__ = "ladder_outcomes"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), index=True, nullable=True
    )
    signal_ref: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    symbol: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    side: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    runner_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    suggested_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: ``raw_sl_hit`` | ``be_stopout`` | ``tp4_plus`` | ``other``
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    runner_close_cause: Mapped[str | None] = mapped_column(String(16), nullable=True)
    runner_profit: Mapped[float | None] = mapped_column(nullable=True)
    runner_tp: Mapped[float | None] = mapped_column(nullable=True)
    runner_sl: Mapped[float | None] = mapped_column(nullable=True)
    runner_close_price: Mapped[float | None] = mapped_column(nullable=True)
    cover_close_cause: Mapped[str | None] = mapped_column(String(16), nullable=True)
    cover_profit: Mapped[float | None] = mapped_column(nullable=True)
    suggested_runner_tp_index: Mapped[int | None] = mapped_column(nullable=True)
    chosen_runner_tp_index: Mapped[int | None] = mapped_column(nullable=True)
    runner_tp_bias: Mapped[int | None] = mapped_column(nullable=True)
    #: Whether the runner closed at its chosen TP (for future bias tuning).
    runner_hit_chosen_tp: Mapped[bool | None] = mapped_column(nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    def __repr__(self) -> str:
        """Short debug representation."""
        return (
            f"<LadderOutcome id={self.id} signal_ref={self.signal_ref!r} "
            f"kind={self.kind!r} runner_mode={self.runner_mode!r}>"
        )


class TradeEvent(Base):
    """Audit row for every write that touched a ticket, successful or rejected."""

    __tablename__ = "trade_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int | None] = mapped_column(
        ForeignKey("trades.id", ondelete="SET NULL"), nullable=True
    )
    ticket: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    retcode: Mapped[int | None] = mapped_column(nullable=True)
    retcode_name: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class StoplessEpisode(Base):
    """An open position found without a stop; tracks notification and fallback state."""

    __tablename__ = "stopless_episodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    trade_id: Mapped[int | None] = mapped_column(
        ForeignKey("trades.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    notified: Mapped[bool] = mapped_column(default=False, nullable=False)
    fallback_applied: Mapped[bool] = mapped_column(default=False, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)


class Notification(Base):
    """One outbound operator notification and whether it actually reached Telegram."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    ok: Mapped[bool] = mapped_column(default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ProbeCache(Base):
    """Cached vision/tools/thinking verdict for one (provider kind, base URL, model)."""

    __tablename__ = "probe_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(512), unique=True, index=True, nullable=False)
    supports_vision: Mapped[bool] = mapped_column(nullable=False)
    supports_tools: Mapped[bool] = mapped_column(nullable=False)
    thinking_form: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class SystemState(Base):
    """Volatile runtime facts, kept as the single row id=1 so a restart can show last-known state."""

    __tablename__ = "system_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1, autoincrement=False)
    mt5_connected: Mapped[bool] = mapped_column(default=False, nullable=False)
    mt5_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_connected: Mapped[bool] = mapped_column(default=False, nullable=False)
    telegram_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_ok: Mapped[bool] = mapped_column(default=False, nullable=False)
    provider_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    margin_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    account_currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_reconcile_at: Mapped[datetime | None] = mapped_column(nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, nullable=False
    )


def default_system_state() -> SystemState:
    """Build the seed `system_state` row (id=1) with every flag explicitly set."""
    return SystemState(
        id=1,
        mt5_connected=False,
        mt5_error=None,
        telegram_connected=False,
        telegram_error=None,
        provider_ok=False,
        provider_error=None,
        margin_mode=None,
        account_currency=None,
        last_reconcile_at=None,
        updated_at=utcnow(),
    )


ALL_TABLES: tuple[str, ...] = (
    AppSetting.__tablename__,
    Source.__tablename__,
    Message.__tablename__,
    MessageImage.__tablename__,
    Run.__tablename__,
    RunStep.__tablename__,
    Trade.__tablename__,
    LadderOutcome.__tablename__,
    TradeEvent.__tablename__,
    StoplessEpisode.__tablename__,
    Notification.__tablename__,
    ProbeCache.__tablename__,
    SystemState.__tablename__,
)


class WaitlistEntry(Base):
    """A consented launch signup. The raw address exists only encrypted at rest."""
    __tablename__ = "waitlist_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    email_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    consent_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class WaitlistThrottle(Base):
    """Restart-safe signup limits, without storing raw client addresses."""
    __tablename__ = "waitlist_throttle"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window: Mapped[int] = mapped_column(nullable=False)
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
