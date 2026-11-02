"""Normalized domain types.

Everything above the venue adapters speaks this language:
* prices are Decimal probabilities in [0, 1] (displayed as cents),
* sizes are Decimal shares,
* ids are namespaced ``venue:native_id``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CENT = Decimal("0.01")
ZERO = Decimal("0")
ONE = Decimal("1")


class VenueId(StrEnum):
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"
    LIMITLESS = "limitless"
    FAKE = "fake"  # test-only id for tests/fake_venue.py; the app never registers it


class Outcome(StrEnum):
    YES = "YES"
    NO = "NO"

    @property
    def opposite(self) -> Outcome:
        return Outcome.NO if self is Outcome.YES else Outcome.YES


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    """Venue order types. Which ones a venue accepts lives in its capabilities."""

    GTC = "GTC"
    GTD = "GTD"
    FOK = "FOK"
    FAK = "FAK"
    IOC = "IOC"


class WithdrawalStatus(StrEnum):
    """Funds lifecycle. A withdrawal is submitted exactly once, never auto-retried."""

    PENDING_HOLD = "PENDING_HOLD"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class MarketStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    RESOLVED = "RESOLVED"
    PAUSED = "PAUSED"
    UNKNOWN = "UNKNOWN"


class ProposalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


class OrderState(StrEnum):
    """Persisted order lifecycle.

    ``UNKNOWN`` is terminal until reconciliation. It must never be resent.
    """

    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    NOT_FILLED = "NOT_FILLED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class ExecutionStatus(StrEnum):
    """Status reported to the operator, based on exchange state."""

    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    RESTING = "RESTING"
    NOT_FILLED = "NOT_FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class RuleKind(StrEnum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TIME_EXIT = "TIME_EXIT"


class RuleStatus(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    TRIGGERED = "TRIGGERED"
    EXECUTING = "EXECUTING"
    DONE = "DONE"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class DoctorStatus(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    SKIPPED = "SKIPPED"


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"not a decimal: {value!r}") from exc


def normalize_probability(value: Any, *, label: str = "price") -> Decimal:
    """The ONLY probability parser used by venue adapters.

    Rejects cents, percentages and every out-of-range number instead of
    guessing. A venue API unit change therefore fails closed.
    """

    try:
        d = to_decimal(value)
    except (ValueError, TypeError) as exc:
        raise UnitMismatchError(f"{label}={value!r} is not numeric") from exc
    if not (ZERO < d < ONE):
        raise UnitMismatchError(f"{label}={d} is not a probability in (0, 1)")
    return d


class UnitMismatchError(ValueError):
    """A venue returned a quantity in an unexpected unit."""


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def quantize_shares(value: Decimal, places: int = 2) -> Decimal:
    quantum = Decimal(1).scaleb(-places)
    return value.quantize(quantum, rounding=ROUND_DOWN)


def quantize_price(price: Decimal, tick: Decimal) -> Decimal:
    """Snap a BUY limit down to the venue tick. Buyers never overpay by rounding."""

    if tick <= ZERO:
        raise ValueError("tick must be positive")
    steps = (price / tick).to_integral_value(rounding=ROUND_DOWN)
    return (steps * tick).quantize(tick)


def display_cents(price: Decimal) -> str:
    return f"{price * 100:.1f}c"


def parse_iso(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class MarketRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue: VenueId
    market_id: str = Field(min_length=1)
    outcome: Outcome | None = None

    @property
    def namespaced(self) -> str:
        base = f"{self.venue.value}:{self.market_id}"
        return f"{base}:{self.outcome.value}" if self.outcome else base

    @classmethod
    def parse(cls, value: str) -> MarketRef:
        parts = value.split(":")
        if len(parts) < 2:
            raise ValueError(f"not a namespaced ref: {value!r}")
        outcome = Outcome(parts[2].upper()) if len(parts) >= 3 else None
        return cls(venue=VenueId(parts[0]), market_id=parts[1], outcome=outcome)


class OutcomeInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: Outcome
    native_id: str
    label: str

    @property
    def key(self) -> str:
        return f"{self.outcome.value}:{self.native_id}"


class VenueCaps(BaseModel):
    model_config = ConfigDict(frozen=True)

    realtime: bool = False
    sandbox: bool = False
    neg_risk: bool = False
    supports_reduce_only: bool = True
    min_order_shares: Decimal = Decimal("1")
    min_notional_usd: Decimal = Decimal("1")
    default_tick: Decimal = CENT
    # Manual-trading capabilities, verified against each venue's docs (2026-10-30).
    order_types: tuple[OrderType, ...] = (OrderType.GTC,)
    # Minutes an expiry must be at least away, for venues that document a floor.
    min_expiry_minutes: int = 0
    post_only: bool = False
    amend: bool = False
    batch_orders: bool = False
    approvals_needed: bool = False
    collateral_asset: str = "USD"
    # Funds features are off unless the venue actually exposes an API for them.
    deposit: bool = False
    withdraw: bool = False
    redeem: bool = False
    funds_history: bool = False
    # Free-text reason shown to the operator when a capability is unavailable.
    unsupported_reason: dict[str, str] = {}


class Market(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue: VenueId
    native_id: str
    title: str
    category: str = "Other"
    event_id: str | None = None
    event_title: str | None = None
    end_time: datetime | None = None
    status: MarketStatus = MarketStatus.UNKNOWN
    tick_size: Decimal = CENT
    neg_risk: bool = False
    outcomes: tuple[OutcomeInfo, ...]
    fee_info: dict[str, Any] = Field(default_factory=dict)
    url: str | None = None
    raw_updated_at: datetime | None = None
    resolution_outcome: Outcome | None = None

    @property
    def ref(self) -> str:
        return f"{self.venue.value}:{self.native_id}"

    @property
    def is_tradable(self) -> bool:
        return self.status is MarketStatus.OPEN

    def outcome(self, outcome: Outcome) -> OutcomeInfo:
        for item in self.outcomes:
            if item.outcome is outcome:
                return item
        raise KeyError(f"{self.ref} has no {outcome.value} outcome")

    def outcome_ref(self, outcome: Outcome) -> str:
        return MarketRef(venue=self.venue, market_id=self.native_id, outcome=outcome).namespaced


class BookLevel(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: Decimal
    size: Decimal

    @field_validator("price")
    @classmethod
    def _price_probability(cls, v: Decimal) -> Decimal:
        return normalize_probability(v)

    @field_validator("size")
    @classmethod
    def _size_positive(cls, v: Decimal) -> Decimal:
        if v <= ZERO:
            raise ValueError("book size must be positive")
        return v


class BookSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue: VenueId
    market_id: str
    outcome: Outcome
    bids: tuple[BookLevel, ...] = ()
    asks: tuple[BookLevel, ...] = ()
    timestamp: datetime = Field(default_factory=utcnow)
    source: Literal["rest", "websocket", "fake"] = "rest"
    sequence: int | None = None

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def key(self) -> str:
        return MarketRef(venue=self.venue, market_id=self.market_id, outcome=self.outcome).namespaced

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.timestamp).total_seconds()

    def is_crossed(self) -> bool:
        return bool(self.best_bid and self.best_ask and self.best_bid >= self.best_ask)

    def depth_shares(self, within: Decimal = Decimal("0.02")) -> Decimal:
        if self.best_ask is None:
            return ZERO
        return sum(
            (level.size for level in self.asks if level.price <= self.best_ask + within),
            start=ZERO,
        )

    def walk(self, shares: Decimal) -> Decimal | None:
        """Volume-weighted average ask price for ``shares``, or None if thin."""

        remaining = shares
        notional = ZERO
        for level in self.asks:
            take = min(level.size, remaining)
            if take <= ZERO:
                break
            notional += take * level.price
            remaining -= take
            if remaining <= ZERO:
                break
        if remaining > ZERO:
            return None
        return notional / shares

    def synthetic_flag(self) -> bool:
        """Cheap guard against absurd one-level liquidity masquerading as depth."""

        if not self.asks:
            return False
        top = self.asks[0].size
        rest = sum((level.size for level in self.asks[1:]), start=ZERO)
        return top > Decimal("10000") and rest < top / Decimal("50")


class Balance(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue: VenueId
    available_usd: Decimal
    total_usd: Decimal | None = None
    currency: str = "USD"
    fetched_at: datetime = Field(default_factory=utcnow)


class Position(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue: VenueId
    market_id: str
    outcome: Outcome
    shares: Decimal
    avg_price: Decimal
    current_price: Decimal | None = None
    realized_pnl: Decimal = ZERO
    unrealized_pnl: Decimal = ZERO
    market_title: str | None = None


class VenueOrder(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue: VenueId
    market_id: str
    order_id: str
    client_order_id: str | None = None
    outcome: Outcome | None = None
    side: Side
    price: Decimal
    size: Decimal
    filled: Decimal = ZERO
    status: str = "OPEN"
    reduce_only: bool = False
    created_at: datetime | None = None
    raw_redacted: dict[str, Any] = Field(default_factory=dict)


class VenueTrade(BaseModel):
    """A terminal trade reported by a venue, used for UNKNOWN reconciliation."""

    model_config = ConfigDict(frozen=True)

    venue: VenueId
    market_id: str
    trade_id: str
    order_id: str | None = None
    client_order_id: str | None = None
    outcome: Outcome | None = None
    side: Side
    price: Decimal
    size: Decimal
    fee_usd: Decimal = ZERO
    created_at: datetime | None = None


class FeeQuote(BaseModel):
    model_config = ConfigDict(frozen=True)

    fee_usd: Decimal
    rate: Decimal | None = None
    source: str


class OrderRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue: VenueId
    market_id: str
    outcome: Outcome
    side: Side
    price: Decimal
    size: Decimal
    client_order_id: str
    reduce_only: bool = False
    order_type: OrderType = OrderType.GTC
    expires_at: datetime | None = None
    post_only: bool = False

    @field_validator("price")
    @classmethod
    def _price(cls, v: Decimal) -> Decimal:
        return normalize_probability(v)

    @field_validator("size")
    @classmethod
    def _size(cls, v: Decimal) -> Decimal:
        if v <= ZERO:
            raise ValueError("size must be positive")
        return v

    @field_validator("client_order_id")
    @classmethod
    def _client_id(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("client_order_id too short for idempotency")
        return v

    @property
    def notional(self) -> Decimal:
        return self.price * self.size


class OrderResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue: VenueId
    client_order_id: str
    venue_order_id: str | None = None
    status: ExecutionStatus
    requested_size: Decimal
    filled_size: Decimal = ZERO
    remaining_size: Decimal = ZERO
    average_price: Decimal | None = None
    fee_usd: Decimal = ZERO
    message: str = ""
    raw_redacted: dict[str, Any] = Field(default_factory=dict)
    at: datetime = Field(default_factory=utcnow)

    @property
    def is_uncertain(self) -> bool:
        return self.status in {ExecutionStatus.UNKNOWN, ExecutionStatus.ERROR}

    @property
    def is_fill(self) -> bool:
        return self.status in {ExecutionStatus.FILLED, ExecutionStatus.PARTIAL}


class CancelResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue: VenueId
    ok: bool
    cancelled: int = 0
    message: str = ""


class Source(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    title: str = ""
    published_at: datetime | None = None
    accessed_at: datetime = Field(default_factory=utcnow)

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise ValueError("source URL must be http(s)")
        return value


class ResearchReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue: VenueId
    market_id: str
    probability: Decimal
    summary: str
    key_facts: tuple[str, ...] = ()
    sources: tuple[Source, ...] = ()
    model: str = "unknown"
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("probability")
    @classmethod
    def _prob(cls, v: Decimal) -> Decimal:
        return normalize_probability(v)

    def is_usable(self, min_sources: int, max_age_minutes: int, now: datetime | None = None) -> bool:
        if len(self.sources) < min_sources:
            return False
        age = ((now or utcnow()) - self.created_at).total_seconds()
        return age <= max_age_minutes * 60 and age >= -60


class DoctorCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    status: DoctorStatus
    detail: str = ""
    latency_ms: int | None = None


class DoctorReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    venue: VenueId
    status: DoctorStatus
    checks: tuple[DoctorCheck, ...] = ()
    checked_at: datetime = Field(default_factory=utcnow)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {DoctorStatus.OK, DoctorStatus.DEGRADED}


class Proposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    venue: VenueId
    market_id: str
    market_title: str
    outcome: Outcome
    side: Side = Side.BUY
    price: Decimal
    size: Decimal
    amount_usd: Decimal
    fee_usd: Decimal
    edge: Decimal
    p_fair: Decimal
    binding_cap: str
    reasons: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    status: ProposalStatus = ProposalStatus.PENDING
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    decided_at: datetime | None = None
    decided_via: str | None = None
    error: str | None = None

    @property
    def short_id(self) -> str:
        return self.id[:8]

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or utcnow()) >= self.expires_at


class Rule(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    venue: VenueId
    market_id: str
    outcome: Outcome
    kind: RuleKind
    trigger_price: Decimal | None = None
    trail_pct: Decimal | None = None
    trigger_at: datetime | None = None
    status: RuleStatus = RuleStatus.ACTIVE
    created_at: datetime = Field(default_factory=utcnow)
    triggered_at: datetime | None = None
    attempts: int = 0

    @model_validator(mode="after")
    def _trigger_present(self) -> Rule:
        if self.kind in {RuleKind.TAKE_PROFIT, RuleKind.STOP_LOSS} and self.trigger_price is None:
            raise ValueError("price rule requires trigger_price")
        if self.kind is RuleKind.TIME_EXIT and self.trigger_at is None:
            raise ValueError("time rule requires trigger_at")
        return self


class RiskDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    approved: bool
    vetoes: tuple[str, ...] = ()
    binding_cap: str = ""
    size_usd: Decimal = ZERO
    shares: Decimal = ZERO
    edge: Decimal = ZERO
    p_fair: Decimal = ZERO
    price: Decimal = ZERO
    fee_usd: Decimal = ZERO


class FeedVenueDiag(BaseModel):
    pages: int = 0
    seen: int = 0
    kept: int = 0
    dropped_closed: int = 0
    dropped_no_tokens: int = 0
    dropped_no_book: int = 0
    dropped_parse: int = 0
    dropped_duplicate: int = 0
    families: dict[str, int] = Field(default_factory=dict)
    error: str | None = None
    last_run: datetime | None = None
    last_ok: datetime | None = None


class FeedDiag(BaseModel):
    venues: dict[str, FeedVenueDiag] = Field(default_factory=dict)


def stable_id(*parts: str) -> str:
    payload = json.dumps(parts, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()[:32]
