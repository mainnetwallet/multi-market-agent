"""SQLAlchemy 2 models. Every trading row carries its venue."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..domain import utcnow


class Base(DeclarativeBase):
    pass


class DecimalString(TypeDecorator[Decimal]):
    """Exact decimal storage. Floats never touch money columns.

    SQLite has no exact decimal type, so it keeps the text form. PostgreSQL gets an
    unconstrained NUMERIC, which is exact and lets the database SUM and compare the
    values (a text column cannot be summed there).
    """

    impl = String(40)
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Numeric(asdecimal=True))
        return dialect.type_descriptor(String(40))

    def process_bind_param(self, value: Decimal | None, dialect: Any) -> Decimal | str | None:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value if isinstance(value, Decimal) else Decimal(str(value))
        return format(value, "f")

    def process_result_value(self, value: Decimal | str | None, dialect: Any) -> Decimal | None:
        if value is None:
            return None
        return value if isinstance(value, Decimal) else Decimal(str(value))


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime(timezone=False)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


def _uuid() -> str:
    return uuid.uuid4().hex


def _conversation_id() -> str:
    return str(uuid.uuid4())


class VenueStateRow(Base):
    __tablename__ = "venues"

    venue: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    ready: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    live: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    doctor_status: Mapped[str] = mapped_column(String(24), default="UNAVAILABLE", nullable=False)
    doctor_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class EventRow(Base):
    __tablename__ = "events"

    venue: Mapped[str] = mapped_column(String(32), primary_key=True)
    native_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    title: Mapped[str] = mapped_column(Text, default="", nullable=False)
    category: Mapped[str] = mapped_column(String(80), default="Other", nullable=False)
    end_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class MarketRow(Base):
    __tablename__ = "markets"

    venue: Mapped[str] = mapped_column(String(32), primary_key=True)
    native_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(80), default="Other", nullable=False)
    event_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    event_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    end_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="UNKNOWN", nullable=False)
    tick_size: Mapped[Decimal] = mapped_column(DecimalString(40), nullable=False)
    neg_risk: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    outcomes_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    fee_info: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    resolution_outcome: Mapped[str | None] = mapped_column(String(8), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (Index("ix_markets_venue_status", "venue", "status"),)


class OutcomeRow(Base):
    __tablename__ = "outcomes"

    venue: Mapped[str] = mapped_column(String(32), primary_key=True)
    market_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    outcome: Mapped[str] = mapped_column(String(8), primary_key=True)
    native_id: Mapped[str] = mapped_column(String(200), nullable=False)
    label: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (Index("ix_outcomes_market", "venue", "market_id"),)


class ResearchRow(Base):
    __tablename__ = "research_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    market_id: Mapped[str] = mapped_column(String(160), nullable=False)
    probability: Mapped[Decimal] = mapped_column(DecimalString(40), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    key_facts: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    model: Mapped[str] = mapped_column(String(120), default="unknown", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (Index("ix_research_lookup", "venue", "market_id", "created_at"),)


class ProbabilityRow(Base):
    __tablename__ = "probabilities"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    market_id: Mapped[str] = mapped_column(String(160), nullable=False)
    mid: Mapped[Decimal] = mapped_column(DecimalString(40), nullable=False)
    p_fair: Mapped[Decimal] = mapped_column(DecimalString(40), nullable=False)
    adjustment: Mapped[Decimal] = mapped_column(DecimalString(40), nullable=False)
    research_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    used_research: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (Index("ix_probability_lookup", "venue", "market_id", "created_at"),)


class ProposalRow(Base):
    __tablename__ = "proposals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    market_id: Mapped[str] = mapped_column(String(160), nullable=False)
    market_title: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(String(8), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    price: Mapped[Decimal] = mapped_column(DecimalString(40), nullable=False)
    size: Mapped[Decimal] = mapped_column(DecimalString(40), nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(DecimalString(40), nullable=False)
    fee_usd: Mapped[Decimal] = mapped_column(DecimalString(40), nullable=False)
    edge: Mapped[Decimal] = mapped_column(DecimalString(40), nullable=False)
    p_fair: Mapped[Decimal] = mapped_column(DecimalString(40), nullable=False)
    binding_cap: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    risks: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="PENDING", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    decided_via: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_proposals_status_expiry", "status", "expires_at"),
        Index("ix_proposals_venue_status", "venue", "status"),
    )


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    market_id: Mapped[str] = mapped_column(String(160), nullable=False)
    outcome: Mapped[str] = mapped_column(String(8), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    price: Mapped[Decimal] = mapped_column(DecimalString(40), nullable=False)
    size: Mapped[Decimal] = mapped_column(DecimalString(40), nullable=False)
    filled: Mapped[Decimal] = mapped_column(DecimalString(40), default=Decimal("0"), nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="CREATED", nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    venue_order_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    proposal_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    order_type: Mapped[str] = mapped_column(String(8), default="GTC", nullable=False)
    post_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    fee_usd: Mapped[Decimal] = mapped_column(DecimalString(40), default=Decimal("0"), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_redacted: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        Index("ix_orders_venue_state", "venue", "state"),
        Index("ix_orders_market", "market_id"),
    )


class FillRow(Base):
    __tablename__ = "fills"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    order_id: Mapped[str] = mapped_column(String(32), ForeignKey("orders.id"), nullable=False)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    market_id: Mapped[str] = mapped_column(String(160), nullable=False)
    outcome: Mapped[str] = mapped_column(String(8), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    price: Mapped[Decimal] = mapped_column(DecimalString(40), nullable=False)
    size: Mapped[Decimal] = mapped_column(DecimalString(40), nullable=False)
    fee_usd: Mapped[Decimal] = mapped_column(DecimalString(40), default=Decimal("0"), nullable=False)
    venue_fill_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    market_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("order_id", "venue_fill_id", name="uq_fill_order_venue"),
        Index("ix_fills_market", "venue", "market_id"),
    )


class PositionRow(Base):
    __tablename__ = "positions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    market_id: Mapped[str] = mapped_column(String(160), nullable=False)
    outcome: Mapped[str] = mapped_column(String(8), nullable=False)
    market_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    shares: Mapped[Decimal] = mapped_column(DecimalString(40), default=Decimal("0"), nullable=False)
    avg_price: Mapped[Decimal] = mapped_column(DecimalString(40), default=Decimal("0"), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(DecimalString(40), default=Decimal("0"), nullable=False)
    current_price: Mapped[Decimal | None] = mapped_column(DecimalString(40), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="OPEN", nullable=False)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("venue", "market_id", "outcome", name="uq_position"),)


class PnlRow(Base):
    __tablename__ = "pnl_ledger"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(DecimalString(40), nullable=False)
    ref: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (Index("ix_pnl_created", "venue", "created_at"),)


class RuleRow(Base):
    __tablename__ = "rules"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    market_id: Mapped[str] = mapped_column(String(160), nullable=False)
    outcome: Mapped[str] = mapped_column(String(8), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    trigger_price: Mapped[Decimal | None] = mapped_column(DecimalString(40), nullable=True)
    trail_pct: Mapped[Decimal | None] = mapped_column(DecimalString(40), nullable=True)
    trigger_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="ACTIVE", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    triggered_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_rules_venue_status", "venue", "status"),)


class DecisionRow(Base):
    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    market_id: Mapped[str] = mapped_column(String(160), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(8), nullable=True)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (Index("ix_decisions_market", "venue", "market_id", "created_at"),)


class AuditRow(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    actor: Mapped[str] = mapped_column(String(80), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    payload_redacted: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (Index("ix_audit_created", "created_at"),)


class KvRow(Base):
    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class IdempotencyRow(Base):
    """Replay store for money-moving POSTs: key -> the first response."""

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    scope: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (Index("ix_idempotency_created", "created_at"),)


class StepupRow(Base):
    """A single-use step-up token bound to one session and one action kind."""

    __tablename__ = "stepup_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(120), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (Index("ix_stepup_session", "session_id", "action"),)


class AllowlistRow(Base):
    """A final withdrawal recipient. Cooling applies before first use."""

    __tablename__ = "withdrawal_allowlist"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    address: Mapped[str] = mapped_column(String(120), nullable=False)
    network: Mapped[str] = mapped_column(String(40), nullable=False)
    asset: Mapped[str] = mapped_column(String(40), nullable=False)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    usable_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class WithdrawalRow(Base):
    """Funds lifecycle. Submitted exactly once; never auto-retried."""

    __tablename__ = "withdrawals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(DecimalString(40), nullable=False)
    address_id: Mapped[str] = mapped_column(String(32), nullable=False)
    recipient: Mapped[str] = mapped_column(String(120), nullable=False)
    network: Mapped[str] = mapped_column(String(40), nullable=False)
    asset: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="PENDING_HOLD", nullable=False)
    hold_until: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    bridge_address: Mapped[str | None] = mapped_column(String(120), nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(120), nullable=True)
    fee_usd: Mapped[Decimal] = mapped_column(DecimalString(40), default=Decimal("0"), nullable=False)
    eta_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (Index("ix_withdrawals_venue_created", "venue", "created_at"),)


class HardCeilingRow(Base):
    """A stored hard ceiling. Only max_trade_usd is operator-raisable."""

    __tablename__ = "hard_ceilings"

    name: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[Decimal] = mapped_column(DecimalString(40), nullable=False)
    actor: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class ConversationRow(Base):
    """One chat. Its messages, summary and run state hang off its id and are never shared."""

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_conversation_id)
    owner: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    summary_upto: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="idle", nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (Index("ix_conversations_owner_updated", "owner", "updated_at"),)


class ChatMessageRow(Base):
    """A message exists only inside one conversation: (conversation_id, seq) is its identity."""

    __tablename__ = "chat_messages"

    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    tools: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
