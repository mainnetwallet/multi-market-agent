"""SQLAlchemy 2 models. Every trading row carries its venue and mode."""

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
    """Exact decimal storage. Floats never touch money columns."""

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Any) -> str | None:
        if value is None:
            return None
        return format(value, "f")

    def process_result_value(self, value: str | None, dialect: Any) -> Decimal | None:
        return Decimal(value) if value is not None else None


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


class VenueStateRow(Base):
    __tablename__ = "venue_states"

    venue: Mapped[str] = mapped_column(String(32), primary_key=True)
    ready: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    live: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    doctor_status: Mapped[str] = mapped_column(String(24), default="UNAVAILABLE", nullable=False)
    doctor_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
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
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (Index("ix_markets_venue_status", "venue", "status"),)


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
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
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
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("order_id", "venue_fill_id", name="uq_fill_order_venue"),
        Index("ix_fills_market", "venue", "market_id"),
    )


class PositionRow(Base):
    __tablename__ = "positions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
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

    __table_args__ = (UniqueConstraint("venue", "mode", "market_id", "outcome", name="uq_position"),)


class PnlRow(Base):
    __tablename__ = "pnl_ledger"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(DecimalString(40), nullable=False)
    ref: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (Index("ix_pnl_created", "venue", "mode", "created_at"),)


class RuleRow(Base):
    __tablename__ = "rules"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
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

    __table_args__ = (Index("ix_rules_venue_mode_status", "venue", "mode", "status"),)


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
