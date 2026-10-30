"""Persistence repository.

Rules:
* state transitions are committed before external side effects,
* an UNKNOWN order can never transition back to SUBMITTING,
* every row is scoped by venue (and mode where money is involved).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, create_engine, event, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..domain import (
    Market,
    MarketStatus,
    Mode,
    Outcome,
    OutcomeInfo,
    Position,
    Proposal,
    ProposalStatus,
    Rule,
    RuleKind,
    RuleStatus,
    Side,
    VenueId,
    utcnow,
)
from ..util import get_logger, redact
from ._result import rowcount
from .models import (
    AuditRow,
    Base,
    DecisionRow,
    EventRow,
    FillRow,
    KvRow,
    MarketRow,
    OrderRow,
    OutcomeRow,
    PnlRow,
    PositionRow,
    ProbabilityRow,
    ProposalRow,
    ResearchRow,
    RuleRow,
    VenueStateRow,
)

logger = get_logger(__name__)

ORDER_TRANSITIONS: dict[str, set[str]] = {
    "CREATED": {"SUBMITTING", "FAILED", "CANCELLED", "NOT_FILLED"},
    "SUBMITTING": {"OPEN", "PARTIAL", "FILLED", "NOT_FILLED", "FAILED", "UNKNOWN", "ERROR", "CANCELLED"},
    "OPEN": {"PARTIAL", "FILLED", "CANCELLED", "UNKNOWN", "FAILED"},
    "PARTIAL": {"PARTIAL", "FILLED", "CANCELLED", "UNKNOWN", "FAILED"},
    "UNKNOWN": {"FILLED", "PARTIAL", "OPEN", "NOT_FILLED", "CANCELLED", "FAILED"},
    "FILLED": set(),
    "NOT_FILLED": set(),
    "FAILED": set(),
    "CANCELLED": set(),
    "ERROR": {"UNKNOWN", "FAILED", "CANCELLED"},
}

PROPOSAL_TRANSITIONS: dict[str, set[str]] = {
    "PENDING": {"APPROVED", "REJECTED", "EXPIRED"},
    "APPROVED": {"EXECUTING", "FAILED", "EXPIRED"},
    "EXECUTING": {"EXECUTED", "FAILED"},
    "EXECUTED": set(),
    "REJECTED": set(),
    "EXPIRED": set(),
    "FAILED": set(),
}


def _dec(value: Decimal | None) -> Decimal:
    return value if value is not None else Decimal("0")


class Store:
    def __init__(self, database_url: str) -> None:
        connect_args: dict[str, Any] = {}
        if database_url.startswith("sqlite"):
            connect_args = {"check_same_thread": False, "timeout": 30}
        self.engine: Engine = create_engine(database_url, future=True, connect_args=connect_args)
        if database_url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._sqlite_pragmas)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False, class_=Session)

    @staticmethod
    def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    def drop_all(self) -> None:
        Base.metadata.drop_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def healthcheck(self) -> bool:
        try:
            with self.session() as s:
                s.execute(select(1))
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ kv
    def set_kv(self, key: str, value: dict[str, Any]) -> None:
        with self.session() as s:
            row = s.get(KvRow, key)
            if row is None:
                s.add(KvRow(key=key, value=redact(value), updated_at=utcnow()))
            else:
                row.value = redact(value)
                row.updated_at = utcnow()

    def get_kv(self, key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.session() as s:
            row = s.get(KvRow, key)
            return dict(row.value) if row else dict(default or {})

    def try_poller_lease(self, name: str, owner: str, ttl_seconds: int = 240) -> bool:
        """Single-poller lease persisted in the database.

        PostgreSQL deployments also hold a session advisory lock while polling;
        SQLite uses this lease because it has no advisory locks.
        """

        now = utcnow()
        expires = now + timedelta(seconds=ttl_seconds)
        with self.session() as s:
            row = s.get(KvRow, f"lease:{name}")
            if row is None:
                try:
                    s.add(
                        KvRow(
                            key=f"lease:{name}",
                            value={"owner": owner, "expires_at": expires.isoformat()},
                            updated_at=now,
                        )
                    )
                    s.flush()
                    return True
                except IntegrityError:
                    s.rollback()
                    return False
            value = dict(row.value or {})
            current_owner = value.get("owner")
            try:
                current_expiry = datetime.fromisoformat(str(value.get("expires_at")))
            except (TypeError, ValueError):
                current_expiry = datetime.min.replace(tzinfo=UTC)
            if current_owner == owner or current_expiry <= now:
                result = s.execute(
                    update(KvRow)
                    .where(KvRow.key == f"lease:{name}")
                    .values(
                        value={"owner": owner, "expires_at": expires.isoformat()},
                        updated_at=now,
                    )
                )
                return bool(rowcount(result))
            return False

    def release_poller_lease(self, name: str, owner: str) -> None:
        with self.session() as s:
            row = s.get(KvRow, f"lease:{name}")
            if row and dict(row.value or {}).get("owner") == owner:
                s.delete(row)

    # ----------------------------------------------------------- venue state
    def set_venue_state(
        self,
        venue: VenueId,
        *,
        ready: bool,
        reason: str,
        live: bool,
        doctor_status: str,
        doctor_json: dict[str, Any] | None = None,
        checked_at: datetime | None = None,
        display_name: str = "",
        enabled: bool = True,
    ) -> None:
        now = utcnow()
        with self.session() as s:
            row = s.get(VenueStateRow, venue.value)
            if row is None:
                row = VenueStateRow(venue=venue.value)
                s.add(row)
            row.display_name = display_name or row.display_name or venue.value
            row.enabled = enabled
            row.ready = ready
            row.reason = reason
            row.live = live
            row.doctor_status = doctor_status
            row.doctor_json = redact(doctor_json or {})
            row.checked_at = checked_at or now
            row.updated_at = now

    def venue_states(self) -> dict[str, dict[str, Any]]:
        with self.session() as s:
            rows = s.scalars(select(VenueStateRow)).all()
            return {
                r.venue: {
                    "display_name": r.display_name,
                    "enabled": r.enabled,
                    "ready": r.ready,
                    "reason": r.reason,
                    "live": r.live,
                    "doctor_status": r.doctor_status,
                    "doctor": r.doctor_json,
                    "checked_at": r.checked_at,
                }
                for r in rows
            }

    # --------------------------------------------------------------- markets
    def upsert_market(self, market: Market) -> None:
        now = utcnow()
        with self.session() as s:
            if market.event_id:
                event = s.get(EventRow, (market.venue.value, market.event_id))
                if event is None:
                    event = EventRow(venue=market.venue.value, native_id=market.event_id)
                    s.add(event)
                event.title = market.event_title or market.title
                event.category = market.category
                event.end_time = market.end_time
                event.updated_at = now
            row = s.get(MarketRow, (market.venue.value, market.native_id))
            if row is None:
                row = MarketRow(venue=market.venue.value, native_id=market.native_id)
                s.add(row)
            row.title = market.title
            row.category = market.category
            row.event_id = market.event_id
            row.event_title = market.event_title
            row.end_time = market.end_time
            row.status = market.status.value
            row.tick_size = market.tick_size
            row.neg_risk = market.neg_risk
            row.outcomes_json = [o.model_dump(mode="json") for o in market.outcomes]
            row.fee_info = redact(market.fee_info)
            row.url = market.url
            row.raw_updated_at = market.raw_updated_at
            row.resolution_outcome = market.resolution_outcome.value if market.resolution_outcome else None
            row.updated_at = now
            for info in market.outcomes:
                outcome = s.get(OutcomeRow, (market.venue.value, market.native_id, info.outcome.value))
                if outcome is None:
                    outcome = OutcomeRow(
                        venue=market.venue.value,
                        market_id=market.native_id,
                        outcome=info.outcome.value,
                    )
                    s.add(outcome)
                outcome.native_id = info.native_id
                outcome.label = info.label
                outcome.updated_at = now

    def get_market(self, venue: VenueId, market_id: str) -> Market | None:
        with self.session() as s:
            row = s.get(MarketRow, (venue.value, market_id))
            return self._market(row) if row else None

    def list_markets(
        self,
        *,
        venue: VenueId | None = None,
        status: MarketStatus | None = MarketStatus.OPEN,
        q: str | None = None,
        limit: int = 100,
    ) -> list[Market]:
        stmt = select(MarketRow)
        if venue:
            stmt = stmt.where(MarketRow.venue == venue.value)
        if status:
            stmt = stmt.where(MarketRow.status == status.value)
        if q:
            stmt = stmt.where(MarketRow.title.ilike(f"%{q.strip()}%"))
        stmt = stmt.order_by(MarketRow.end_time.is_(None), MarketRow.end_time).limit(limit)
        with self.session() as s:
            return [self._market(r) for r in s.scalars(stmt).all()]

    def list_events(self, *, venue: VenueId | None = None, limit: int = 100) -> list[dict[str, Any]]:
        stmt = select(EventRow).order_by(EventRow.end_time.is_(None), EventRow.end_time).limit(limit)
        if venue:
            stmt = stmt.where(EventRow.venue == venue.value)
        with self.session() as s:
            return [
                {
                    "venue": r.venue,
                    "native_id": r.native_id,
                    "title": r.title,
                    "category": r.category,
                    "end_time": r.end_time,
                }
                for r in s.scalars(stmt).all()
            ]

    def list_outcomes(self, venue: VenueId, market_id: str) -> list[dict[str, Any]]:
        stmt = select(OutcomeRow).where(OutcomeRow.venue == venue.value, OutcomeRow.market_id == market_id)
        with self.session() as s:
            return [
                {
                    "outcome": r.outcome,
                    "native_id": r.native_id,
                    "label": r.label,
                }
                for r in s.scalars(stmt).all()
            ]

    def market_count(self, venue: VenueId | None = None) -> int:
        stmt = select(func.count()).select_from(MarketRow)
        if venue:
            stmt = stmt.where(MarketRow.venue == venue.value)
        with self.session() as s:
            return int(s.scalar(stmt) or 0)

    @staticmethod
    def _market(row: MarketRow) -> Market:
        return Market(
            venue=VenueId(row.venue),
            native_id=row.native_id,
            title=row.title,
            category=row.category,
            event_id=row.event_id,
            event_title=row.event_title,
            end_time=row.end_time,
            status=MarketStatus(row.status),
            tick_size=row.tick_size,
            neg_risk=row.neg_risk,
            outcomes=tuple(OutcomeInfo.model_validate(x) for x in row.outcomes_json),
            fee_info=dict(row.fee_info or {}),
            url=row.url,
            raw_updated_at=row.raw_updated_at,
            resolution_outcome=Outcome(row.resolution_outcome) if row.resolution_outcome else None,
        )

    # -------------------------------------------------------------- research
    def save_research(
        self,
        venue: VenueId,
        market_id: str,
        probability: Decimal,
        summary: str,
        key_facts: list[str],
        sources: list[dict[str, Any]],
        model: str,
        created_at: datetime | None = None,
    ) -> str:
        from ..domain import stable_id

        rid = stable_id("research", venue.value, market_id, (created_at or utcnow()).isoformat())
        with self.session() as s:
            if s.get(ResearchRow, rid) is None:
                s.add(
                    ResearchRow(
                        id=rid,
                        venue=venue.value,
                        market_id=market_id,
                        probability=probability,
                        summary=summary,
                        key_facts=key_facts,
                        sources=redact(sources),
                        model=model,
                        created_at=created_at or utcnow(),
                    )
                )
        return rid

    def latest_research(self, venue: VenueId, market_id: str) -> dict[str, Any] | None:
        stmt = (
            select(ResearchRow)
            .where(ResearchRow.venue == venue.value, ResearchRow.market_id == market_id)
            .order_by(ResearchRow.created_at.desc())
            .limit(1)
        )
        with self.session() as s:
            row = s.scalar(stmt)
            if not row:
                return None
            return {
                "id": row.id,
                "venue": row.venue,
                "market_id": row.market_id,
                "probability": row.probability,
                "summary": row.summary,
                "key_facts": row.key_facts,
                "sources": row.sources,
                "model": row.model,
                "created_at": row.created_at,
            }

    def save_probability(
        self,
        venue: VenueId,
        market_id: str,
        mid: Decimal,
        p_fair: Decimal,
        adjustment: Decimal,
        research_id: str | None,
        used_research: bool,
    ) -> None:
        with self.session() as s:
            s.add(
                ProbabilityRow(
                    venue=venue.value,
                    market_id=market_id,
                    mid=mid,
                    p_fair=p_fair,
                    adjustment=adjustment,
                    research_id=research_id,
                    used_research=used_research,
                )
            )

    # ------------------------------------------------------------- proposals
    def create_proposal(self, proposal: Proposal) -> None:
        with self.session() as s:
            s.add(
                ProposalRow(
                    id=proposal.id,
                    venue=proposal.venue.value,
                    mode=proposal.mode.value,
                    market_id=proposal.market_id,
                    market_title=proposal.market_title,
                    outcome=proposal.outcome.value,
                    side=proposal.side.value,
                    price=proposal.price,
                    size=proposal.size,
                    amount_usd=proposal.amount_usd,
                    fee_usd=proposal.fee_usd,
                    edge=proposal.edge,
                    p_fair=proposal.p_fair,
                    binding_cap=proposal.binding_cap,
                    reasons=list(proposal.reasons),
                    risks=list(proposal.risks),
                    status=proposal.status.value,
                    created_at=proposal.created_at,
                    expires_at=proposal.expires_at,
                )
            )

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        with self.session() as s:
            row = self._resolve_proposal(s, proposal_id)
            return self._proposal(row) if row else None

    def list_proposals(self, limit: int = 50, status: ProposalStatus | None = None) -> list[Proposal]:
        stmt = select(ProposalRow).order_by(ProposalRow.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(ProposalRow.status == status.value)
        with self.session() as s:
            return [self._proposal(r) for r in s.scalars(stmt).all()]

    def transition_proposal(
        self,
        proposal_id: str,
        new_status: ProposalStatus,
        *,
        via: str | None = None,
        error: str | None = None,
    ) -> bool:
        now = utcnow()
        with self.session() as s:
            row = self._resolve_proposal(s, proposal_id)
            if row is None:
                return False
            if new_status.value not in PROPOSAL_TRANSITIONS.get(row.status, set()):
                return False
            previous = row.status
            result = s.execute(
                update(ProposalRow)
                .where(ProposalRow.id == row.id, ProposalRow.status == previous)
                .values(
                    status=new_status.value,
                    decided_at=now if new_status is not ProposalStatus.EXECUTING else row.decided_at,
                    decided_via=via,
                    error=error,
                )
            )
            return bool(rowcount(result))

    def expire_proposals(self) -> int:
        now = utcnow()
        with self.session() as s:
            result = s.execute(
                update(ProposalRow)
                .where(ProposalRow.status == ProposalStatus.PENDING.value, ProposalRow.expires_at <= now)
                .values(status=ProposalStatus.EXPIRED.value, decided_at=now)
            )
            return rowcount(result)

    def pending_proposal_for_market(self, venue: VenueId, market_id: str) -> Proposal | None:
        stmt = (
            select(ProposalRow)
            .where(
                ProposalRow.venue == venue.value,
                ProposalRow.market_id == market_id,
                ProposalRow.status == ProposalStatus.PENDING.value,
            )
            .limit(1)
        )
        with self.session() as s:
            row = s.scalar(stmt)
            return self._proposal(row) if row else None

    def _resolve_proposal(self, s: Session, proposal_id: str) -> ProposalRow | None:
        row = s.get(ProposalRow, proposal_id)
        if row is not None:
            return row
        if len(proposal_id) >= 4:
            matches = s.scalars(select(ProposalRow).where(ProposalRow.id.like(f"{proposal_id}%")).limit(2)).all()
            if len(matches) == 1:
                return matches[0]
        return None

    @staticmethod
    def _proposal(row: ProposalRow) -> Proposal:
        return Proposal(
            id=row.id,
            venue=VenueId(row.venue),
            mode=Mode(row.mode),
            market_id=row.market_id,
            market_title=row.market_title,
            outcome=Outcome(row.outcome),
            side=Side(row.side),
            price=row.price,
            size=row.size,
            amount_usd=row.amount_usd,
            fee_usd=row.fee_usd,
            edge=row.edge,
            p_fair=row.p_fair,
            binding_cap=row.binding_cap,
            reasons=tuple(row.reasons or []),
            risks=tuple(row.risks or []),
            status=ProposalStatus(row.status),
            created_at=row.created_at,
            expires_at=row.expires_at,
            decided_at=row.decided_at,
            decided_via=row.decided_via,
            error=row.error,
        )

    # ---------------------------------------------------------------- orders
    def create_order(
        self,
        *,
        venue: VenueId,
        mode: Mode,
        market_id: str,
        outcome: Outcome,
        side: Side,
        price: Decimal,
        size: Decimal,
        client_order_id: str,
        proposal_id: str | None,
        reduce_only: bool = False,
    ) -> str:
        from ..domain import stable_id

        local_id = stable_id("order", venue.value, client_order_id)
        with self.session() as s:
            try:
                s.add(
                    OrderRow(
                        id=local_id,
                        venue=venue.value,
                        mode=mode.value,
                        market_id=market_id,
                        outcome=outcome.value,
                        side=side.value,
                        price=price,
                        size=size,
                        state="CREATED",
                        client_order_id=client_order_id,
                        proposal_id=proposal_id,
                        reduce_only=reduce_only,
                    )
                )
                s.flush()
            except IntegrityError:
                s.rollback()
                existing = s.scalar(select(OrderRow).where(OrderRow.client_order_id == client_order_id))
                if existing is None:
                    raise
                return existing.id
        return local_id

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.get(OrderRow, order_id)
            if row is None:
                matches = s.scalars(select(OrderRow).where(OrderRow.id.like(f"{order_id}%")).limit(2)).all()
                row = matches[0] if len(matches) == 1 else None
            return self._order(row) if row else None

    def get_order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.scalar(select(OrderRow).where(OrderRow.client_order_id == client_order_id))
            return self._order(row) if row else None

    def list_orders(
        self,
        *,
        venue: VenueId | None = None,
        mode: Mode | None = None,
        limit: int = 100,
        states: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        stmt = select(OrderRow).order_by(OrderRow.created_at.desc()).limit(limit)
        if venue:
            stmt = stmt.where(OrderRow.venue == venue.value)
        if mode:
            stmt = stmt.where(OrderRow.mode == mode.value)
        if states:
            stmt = stmt.where(OrderRow.state.in_(states))
        with self.session() as s:
            return [self._order(r) for r in s.scalars(stmt).all()]

    def orders_needing_reconcile(self) -> list[dict[str, Any]]:
        return self.list_orders(states={"UNKNOWN", "SUBMITTING", "ERROR"}, limit=200)

    def transition_order(
        self,
        order_id: str,
        new_state: str,
        *,
        venue_order_id: str | None = None,
        filled: Decimal | None = None,
        fee_usd: Decimal | None = None,
        error: str | None = None,
        raw: dict[str, Any] | None = None,
        reconciled: bool = False,
    ) -> bool:
        with self.session() as s:
            row = s.get(OrderRow, order_id)
            if row is None:
                return False
            if new_state not in ORDER_TRANSITIONS.get(row.state, set()):
                logger.warning("illegal_order_transition", order_id=order_id, current=row.state, requested=new_state)
                return False
            values: dict[str, Any] = {
                "state": new_state,
                "updated_at": utcnow(),
                "error": error,
            }
            if venue_order_id:
                values["venue_order_id"] = venue_order_id
            if filled is not None:
                values["filled"] = filled
            if fee_usd is not None:
                values["fee_usd"] = fee_usd
            if raw is not None:
                values["raw_redacted"] = redact(raw)
            if reconciled:
                values["reconciled_at"] = utcnow()
            if new_state == "SUBMITTING":
                values["submitted_at"] = utcnow()
            result = s.execute(
                update(OrderRow).where(OrderRow.id == order_id, OrderRow.state == row.state).values(**values)
            )
            return bool(rowcount(result))

    @staticmethod
    def _order(row: OrderRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "short_id": row.id[:8],
            "venue": row.venue,
            "mode": row.mode,
            "market_id": row.market_id,
            "outcome": row.outcome,
            "side": row.side,
            "price": row.price,
            "size": row.size,
            "filled": row.filled,
            "state": row.state,
            "client_order_id": row.client_order_id,
            "venue_order_id": row.venue_order_id,
            "reduce_only": row.reduce_only,
            "proposal_id": row.proposal_id,
            "fee_usd": row.fee_usd,
            "error": row.error,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    # ----------------------------------------------------------------- fills
    def apply_fill(
        self,
        *,
        order_id: str,
        venue: VenueId,
        mode: Mode,
        market_id: str,
        outcome: Outcome,
        side: Side,
        price: Decimal,
        size: Decimal,
        fee_usd: Decimal,
        venue_fill_id: str | None,
        market_title: str | None = None,
    ) -> bool:
        """Idempotently record a fill and update position + PnL in one commit."""

        if size <= 0:
            return False
        with self.session() as s:
            row = s.get(OrderRow, order_id)
            if row is None:
                raise ValueError(f"unknown local order {order_id}")
            if venue_fill_id:
                existing = s.scalar(
                    select(FillRow).where(FillRow.order_id == order_id, FillRow.venue_fill_id == venue_fill_id)
                )
                if existing is not None:
                    return False
            fill = FillRow(
                order_id=order_id,
                venue=venue.value,
                market_id=market_id,
                outcome=outcome.value,
                side=side.value,
                price=price,
                size=size,
                fee_usd=fee_usd,
                venue_fill_id=venue_fill_id,
            )
            s.add(fill)
            row.filled = _dec(row.filled) + size
            row.updated_at = utcnow()

            pos = s.scalar(
                select(PositionRow).where(
                    PositionRow.venue == venue.value,
                    PositionRow.mode == mode.value,
                    PositionRow.market_id == market_id,
                    PositionRow.outcome == outcome.value,
                )
            )
            if pos is None:
                pos = PositionRow(
                    venue=venue.value,
                    mode=mode.value,
                    market_id=market_id,
                    outcome=outcome.value,
                    market_title=market_title,
                    shares=Decimal("0"),
                    avg_price=Decimal("0"),
                )
                s.add(pos)
            if side is Side.BUY:
                old_shares = _dec(pos.shares)
                new_shares = old_shares + size
                pos.avg_price = (
                    (old_shares * _dec(pos.avg_price) + size * price) / new_shares if new_shares > 0 else Decimal("0")
                )
                pos.shares = new_shares
                pos.status = "OPEN"
                if market_title:
                    pos.market_title = market_title
            else:
                sell_shares = min(size, _dec(pos.shares))
                realized = (price - _dec(pos.avg_price)) * sell_shares
                pos.realized_pnl = _dec(pos.realized_pnl) + realized
                pos.shares = _dec(pos.shares) - sell_shares
                if pos.shares <= 0:
                    pos.shares = Decimal("0")
                    pos.status = "CLOSED"
                s.add(
                    PnlRow(
                        venue=venue.value,
                        mode=mode.value,
                        kind="realized",
                        amount_usd=realized,
                        ref=market_id,
                        note=f"sell {sell_shares} {outcome.value}",
                    )
                )
            if fee_usd:
                s.add(
                    PnlRow(
                        venue=venue.value,
                        mode=mode.value,
                        kind="fee",
                        amount_usd=-fee_usd,
                        ref=market_id,
                        note="trading fee",
                    )
                )
            pos.current_price = price
            pos.updated_at = utcnow()
            return True

    def fills_for_order(self, order_id: str) -> list[dict[str, Any]]:
        stmt = select(FillRow).where(FillRow.order_id == order_id).order_by(FillRow.created_at)
        with self.session() as s:
            return [
                {
                    "id": r.id,
                    "price": r.price,
                    "size": r.size,
                    "fee_usd": r.fee_usd,
                    "side": r.side,
                    "venue_fill_id": r.venue_fill_id,
                    "created_at": r.created_at,
                }
                for r in s.scalars(stmt).all()
            ]

    # ------------------------------------------------------------- positions
    def list_positions(self, *, mode: Mode | None = None, venue: VenueId | None = None) -> list[Position]:
        stmt = select(PositionRow).where(PositionRow.shares > 0).order_by(PositionRow.updated_at.desc())
        if mode:
            stmt = stmt.where(PositionRow.mode == mode.value)
        if venue:
            stmt = stmt.where(PositionRow.venue == venue.value)
        with self.session() as s:
            rows = s.scalars(stmt).all()
            return [
                Position(
                    venue=VenueId(r.venue),
                    market_id=r.market_id,
                    outcome=Outcome(r.outcome),
                    shares=r.shares,
                    avg_price=r.avg_price,
                    current_price=r.current_price,
                    realized_pnl=r.realized_pnl,
                    unrealized_pnl=(
                        (r.current_price - r.avg_price) * r.shares if r.current_price is not None else Decimal("0")
                    ),
                    market_title=r.market_title,
                    mode=Mode(r.mode),
                )
                for r in rows
            ]

    def open_positions_count(self, mode: Mode | None = None) -> int:
        stmt = select(func.count()).select_from(PositionRow).where(PositionRow.shares > 0)
        if mode:
            stmt = stmt.where(PositionRow.mode == mode.value)
        with self.session() as s:
            return int(s.scalar(stmt) or 0)

    def total_exposure(self, mode: Mode, venue: VenueId | None = None) -> Decimal:
        stmt = select(func.coalesce(func.sum(PositionRow.shares * PositionRow.avg_price), 0)).where(
            PositionRow.shares > 0, PositionRow.mode == mode.value
        )
        if venue:
            stmt = stmt.where(PositionRow.venue == venue.value)
        with self.session() as s:
            return Decimal(str(s.scalar(stmt) or 0))

    def event_exposure(self, venue: VenueId, mode: Mode, event_id: str | None) -> Decimal:
        if not event_id:
            return Decimal("0")
        with self.session() as s:
            market_ids = s.scalars(
                select(MarketRow.native_id).where(MarketRow.venue == venue.value, MarketRow.event_id == event_id)
            ).all()
            if not market_ids:
                return Decimal("0")
            total = s.scalar(
                select(func.coalesce(func.sum(PositionRow.shares * PositionRow.avg_price), 0)).where(
                    PositionRow.venue == venue.value,
                    PositionRow.mode == mode.value,
                    PositionRow.shares > 0,
                    PositionRow.market_id.in_(market_ids),
                )
            )
            return Decimal(str(total or 0))

    def realized_pnl_since(self, since: datetime, mode: Mode | None = None) -> Decimal:
        stmt = select(func.coalesce(func.sum(PnlRow.amount_usd), 0)).where(PnlRow.created_at >= since)
        if mode:
            stmt = stmt.where(PnlRow.mode == mode.value)
        with self.session() as s:
            return Decimal(str(s.scalar(stmt) or 0))

    def today_pnl(self, mode: Mode | None = None) -> Decimal:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.realized_pnl_since(start, mode)

    def update_position_price(self, venue: VenueId, market_id: str, outcome: Outcome, price: Decimal) -> None:
        with self.session() as s:
            row = s.scalar(
                select(PositionRow).where(
                    PositionRow.venue == venue.value,
                    PositionRow.market_id == market_id,
                    PositionRow.outcome == outcome.value,
                )
            )
            if row:
                row.current_price = price
                row.updated_at = utcnow()

    # ----------------------------------------------------------------- rules
    def create_rule(self, rule: Rule) -> None:
        with self.session() as s:
            s.add(
                RuleRow(
                    id=rule.id,
                    venue=rule.venue.value,
                    mode=rule.mode.value,
                    market_id=rule.market_id,
                    outcome=rule.outcome.value,
                    kind=rule.kind.value,
                    trigger_price=rule.trigger_price,
                    trail_pct=rule.trail_pct,
                    trigger_at=rule.trigger_at,
                    status=rule.status.value,
                    created_at=rule.created_at,
                )
            )

    def list_rules(self, *, status: RuleStatus | None = None, mode: Mode | None = None) -> list[Rule]:
        stmt = select(RuleRow).order_by(RuleRow.created_at.desc())
        if status:
            stmt = stmt.where(RuleRow.status == status.value)
        if mode:
            stmt = stmt.where(RuleRow.mode == mode.value)
        with self.session() as s:
            return [self._rule(r) for r in s.scalars(stmt).all()]

    def get_rule(self, rule_id: str) -> Rule | None:
        with self.session() as s:
            row = s.get(RuleRow, rule_id)
            if row is None and len(rule_id) >= 4:
                matches = s.scalars(select(RuleRow).where(RuleRow.id.like(f"{rule_id}%")).limit(2)).all()
                row = matches[0] if len(matches) == 1 else None
            return self._rule(row) if row else None

    def claim_rule(self, rule_id: str) -> bool:
        """Atomically move ACTIVE -> TRIGGERED exactly once."""

        with self.session() as s:
            result = s.execute(
                update(RuleRow)
                .where(
                    RuleRow.id == rule_id,
                    RuleRow.status.in_([RuleStatus.ACTIVE.value, RuleStatus.BLOCKED.value]),
                )
                .values(status=RuleStatus.TRIGGERED.value, triggered_at=utcnow())
            )
            return bool(rowcount(result))

    def transition_rule(self, rule_id: str, from_status: RuleStatus, to_status: RuleStatus) -> bool:
        with self.session() as s:
            result = s.execute(
                update(RuleRow)
                .where(RuleRow.id == rule_id, RuleRow.status == from_status.value)
                .values(
                    status=to_status.value,
                    attempts=RuleRow.attempts + 1,
                    last_error=None,
                )
            )
            return bool(rowcount(result))

    def cancel_rule(self, rule_id: str) -> bool:
        with self.session() as s:
            result = s.execute(
                update(RuleRow)
                .where(
                    RuleRow.id == rule_id,
                    RuleRow.status.in_([RuleStatus.ACTIVE.value, RuleStatus.PENDING.value]),
                )
                .values(status=RuleStatus.CANCELLED.value)
            )
            return bool(rowcount(result))

    def mark_rule(self, rule_id: str, status: RuleStatus, error: str | None = None) -> None:
        with self.session() as s:
            result = s.execute(
                update(RuleRow)
                .where(RuleRow.id == rule_id)
                .values(status=status.value, last_error=error, attempts=RuleRow.attempts + 1)
            )
            if not rowcount(result):
                logger.warning("rule_not_found", rule_id=rule_id)

    def cancel_all_rules(self) -> int:
        with self.session() as s:
            result = s.execute(
                update(RuleRow)
                .where(RuleRow.status.in_([RuleStatus.ACTIVE.value, RuleStatus.BLOCKED.value]))
                .values(status=RuleStatus.CANCELLED.value)
            )
            return rowcount(result)

    def active_rules_for_market(self, venue: VenueId, market_id: str) -> list[Rule]:
        stmt = select(RuleRow).where(
            RuleRow.venue == venue.value,
            RuleRow.market_id == market_id,
            RuleRow.status == RuleStatus.ACTIVE.value,
        )
        with self.session() as s:
            return [self._rule(r) for r in s.scalars(stmt).all()]

    @staticmethod
    def _rule(row: RuleRow) -> Rule:
        return Rule(
            id=row.id,
            venue=VenueId(row.venue),
            mode=Mode(row.mode),
            market_id=row.market_id,
            outcome=Outcome(row.outcome),
            kind=RuleKind(row.kind),
            trigger_price=row.trigger_price,
            trail_pct=row.trail_pct,
            trigger_at=row.trigger_at,
            status=RuleStatus(row.status),
            created_at=row.created_at,
            triggered_at=row.triggered_at,
            attempts=row.attempts,
        )

    # -------------------------------------------------------------- decisions
    def save_decision(
        self,
        *,
        venue: VenueId,
        market_id: str,
        outcome: Outcome | None,
        action: str,
        reason_codes: list[str],
        summary: str = "",
    ) -> None:
        with self.session() as s:
            s.add(
                DecisionRow(
                    venue=venue.value,
                    market_id=market_id,
                    outcome=outcome.value if outcome else None,
                    action=action,
                    reason_codes=reason_codes,
                    summary=summary,
                )
            )

    # ----------------------------------------------------------------- audit
    def audit(self, actor: str, action: str, payload: dict[str, Any] | None = None) -> None:
        with self.session() as s:
            s.add(AuditRow(actor=actor, action=action, payload_redacted=redact(payload or {})))

    def recent_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        stmt = select(AuditRow).order_by(AuditRow.created_at.desc()).limit(limit)
        with self.session() as s:
            return [
                {
                    "id": r.id,
                    "actor": r.actor,
                    "action": r.action,
                    "payload": r.payload_redacted,
                    "created_at": r.created_at,
                }
                for r in s.scalars(stmt).all()
            ]

    # ------------------------------------------------------------------ misc
    def status_counts(self) -> dict[str, int]:
        with self.session() as s:
            return {
                "markets": int(s.scalar(select(func.count()).select_from(MarketRow)) or 0),
                "open_positions": int(
                    s.scalar(select(func.count()).select_from(PositionRow).where(PositionRow.shares > 0)) or 0
                ),
                "pending_proposals": int(
                    s.scalar(
                        select(func.count())
                        .select_from(ProposalRow)
                        .where(ProposalRow.status == ProposalStatus.PENDING.value)
                    )
                    or 0
                ),
                "unknown_orders": int(
                    s.scalar(select(func.count()).select_from(OrderRow).where(OrderRow.state == "UNKNOWN")) or 0
                ),
            }

    def seed_demo_market(self, market: Market) -> None:
        """Only used by scripts/tests; never by the live agent."""

        self.upsert_market(market)
