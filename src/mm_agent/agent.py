"""Trading orchestration: research -> fair value -> edge -> risk -> proposal ->
human approval -> re-validation -> execution -> monitoring."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any

from .books import BookCache, BookFeed, UnitGuard
from .config import Settings
from .domain import (
    BookSnapshot,
    ExecutionStatus,
    Market,
    MarketRef,
    MarketStatus,
    Mode,
    OrderRequest,
    OrderType,
    Outcome,
    Position,
    Proposal,
    ProposalStatus,
    RiskDecision,
    Rule,
    RuleKind,
    Side,
    VenueId,
    quantize_price,
    quantize_shares,
    stable_id,
    utcnow,
)
from .events import EventBus
from .execution import ExecutionGateway
from .locks import LockRegistry
from .probability import compute_fair_probability
from .research import ResearchService, report_from_row
from .risk import RiskEngine
from .scanner import Scanner
from .store import Store
from .strategy import Signal, StrategyEngine
from .util import fmt_dhaka, get_logger
from .venues.registry import VenueRegistry

logger = get_logger(__name__)
ZERO = Decimal("0")
ONE = Decimal("1")


class AgentService:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        registry: VenueRegistry,
        bus: EventBus,
        scanner: Scanner,
        books: BookCache,
        unit_guard: UnitGuard,
        research: ResearchService | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.registry = registry
        self.bus = bus
        self.scanner = scanner
        self.books = books
        self.unit_guard = unit_guard
        self.book_feed = BookFeed(settings, registry, store, books, bus)
        self.research_service = research or ResearchService(settings)
        self.strategy = StrategyEngine(settings)
        self.risk = RiskEngine(settings)
        self.locks = LockRegistry()
        self.execution = ExecutionGateway(settings, store, registry, books, self.locks, bus)
        self.paused = False
        # Set by the API once the full-control layer exists; optional by design.
        self.full_control: Any = None
        self._tasks: list[asyncio.Task[None]] = []
        self._expiry_task: asyncio.Task[None] | None = None
        self._auto_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        kv = self.store.get_kv("paused")
        self.paused = bool(kv.get("paused", False))
        overrides = self.store.get_kv("runtime_limits").get("values", {})
        if isinstance(overrides, dict):
            self.risk.restore({str(k): str(v) for k, v in overrides.items()})
        self._stop.clear()
        self.book_feed.start()
        self._expiry_task = asyncio.create_task(self._expiry_loop(), name="proposal-expiry")
        if self.settings.auto_propose:
            self._auto_task = asyncio.create_task(self._auto_loop(), name="auto-propose")

    async def stop(self) -> None:
        self._stop.set()
        await self.book_feed.stop()
        for task in (self._expiry_task, self._auto_task):
            if task:
                task.cancel()
        await asyncio.gather(
            *(t for t in (self._expiry_task, self._auto_task) if t is not None), return_exceptions=True
        )

    # ---------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        venue_summary = self.registry.summary()
        active = self.registry.active_or_none
        payload = {
            "active_venue": active.value if active else None,
            "badge": f"{(active.value if active else 'none').upper()} | "
            f"{self.registry.mode(active).value if active else 'PAPER'}",
            "paused": self.paused,
            "unit_guard": {"ok": self.unit_guard.ok, "reason": self.unit_guard.reason},
            "feed_diag": self.scanner.feed_diag().model_dump(mode="json"),
            "book_feed": self.book_feed.status(),
            "research": {
                "available": self.research_service.available,
                "reason": self.research_service.disabled_reason,
            },
            "venues": venue_summary["venues"],
            "limits": self.limits_dict(),
            "counts": self.store.status_counts(),
            "today_pnl_usd": str(self.store.today_pnl()),
            "server_time_dhaka": fmt_dhaka(utcnow()),
        }
        # The full-control layer is attached by the API once it exists; the field is
        # optional so existing clients keep working.
        if self.full_control is not None:
            payload["capabilities"] = self.full_control.capabilities()
        return payload

    def limits_dict(self) -> dict[str, Any]:
        lim = self.settings.limits
        limits: dict[str, Any] = {
            "bankroll_usd": str(lim.bankroll_usd),
            "max_trade_usd": str(self.risk.caps["max_trade_usd"]),
            "max_event_pct": str(self.risk.caps["max_event_pct"]),
            "max_total_exposure_pct": str(self.risk.caps["max_total_exposure_pct"]),
            "max_daily_loss_pct": str(self.risk.caps["max_daily_loss_pct"]),
            "max_open_positions": int(self.risk.caps["max_open_positions"]),
            "min_edge": str(lim.min_edge),
            "max_spread": str(lim.max_spread),
            "min_depth_multiple": str(lim.min_depth_multiple),
            "proposal_ttl_seconds": lim.proposal_ttl_seconds,
            "advisory_mode": lim.advisory_mode,
            "per_venue_bankroll_usd": {
                venue.value: str(self.settings.limit_bankroll(venue)) for venue in self.registry.enabled()
            },
            "hard_ceilings": self.risk.ceilings_dict(),
        }
        return limits

    # --------------------------------------------------------------- markets
    async def get_market(self, market_id: str, venue: VenueId | None = None) -> Market:
        venue = venue or self.registry.active
        market = self.store.get_market(venue, market_id)
        if market is not None:
            return market
        return await self.registry.get(venue).get_market(MarketRef(venue=venue, market_id=market_id))

    def markets(self, *, q: str | None = None, limit: int = 50, venue: VenueId | None = None) -> list[Market]:
        return self.store.list_markets(venue=venue or self.registry.active_or_none, q=q, limit=limit)

    async def books_for(self, market: Market, *, force: bool = False) -> dict[Outcome, BookSnapshot]:
        adapter = self.registry.get(market.venue)
        out: dict[Outcome, BookSnapshot] = {}
        for outcome in (Outcome.YES, Outcome.NO):
            ref = MarketRef(venue=market.venue, market_id=market.native_id, outcome=outcome)
            try:
                out[outcome] = await self.books.get(adapter, ref, force=force)
            except Exception as exc:
                logger.warning("book_fetch_failed", ref=ref.namespaced, error=str(exc)[:160])
        return out

    # -------------------------------------------------------------- research
    async def _get_research(self, market: Market) -> tuple[Any, str | None, list[str]]:
        row = self.store.latest_research(market.venue, market.native_id)
        if row is not None:
            report = report_from_row(row)
            if report.is_usable(self.settings.research_min_sources, self.settings.research_max_age_minutes):
                return report, row["id"], []
        outcome = await self.research_service.research(market)
        if outcome.report is None:
            return None, None, [outcome.reason or "RESEARCH_UNAVAILABLE"]
        report = outcome.report
        rid = self.store.save_research(
            market.venue,
            market.native_id,
            report.probability,
            report.summary,
            list(report.key_facts),
            [s.model_dump(mode="json") for s in report.sources],
            report.model,
            report.created_at,
        )
        return report, rid, []

    async def research_market(self, market_id: str, venue: VenueId | None = None) -> dict[str, Any]:
        market = await self.get_market(market_id, venue)
        report, rid, reasons = await self._get_research(market)
        if report is None:
            return {"ok": False, "market": market.ref, "reasons": reasons}
        return {
            "ok": True,
            "market": market.ref,
            "research_id": rid,
            "probability": str(report.probability),
            "summary": report.summary,
            "key_facts": list(report.key_facts),
            "sources": [s.model_dump(mode="json") for s in report.sources],
            "model": report.model,
            "created_at": report.created_at.isoformat(),
        }

    # ---------------------------------------------------------------- analyse
    async def analyze_market(
        self,
        market: Market,
        *,
        only_outcome: Outcome | None = None,
        create_proposal: bool = True,
        max_amount_usd: Decimal | None = None,
    ) -> dict[str, Any]:
        if self.paused:
            return {"ok": False, "reasons": ["AGENT_PAUSED"]}
        if not self.unit_guard.ok:
            return {"ok": False, "reasons": ["UNIT_CHECK_FAILED"]}
        if market.venue != self.registry.active_or_none:
            return {"ok": False, "reasons": ["NOT_ACTIVE_VENUE"]}
        mode = self.registry.mode(market.venue)
        loss_cap = self.settings.limit_bankroll(market.venue) * self.risk.caps["max_daily_loss_pct"] / Decimal("100")
        if self.store.today_pnl(mode) <= -loss_cap:
            await self.set_paused(True, actor="risk:daily-loss")
            await self.bus.publish(
                "risk.daily_loss_cap",
                venue=market.venue.value,
                pnl=str(self.store.today_pnl(mode)),
                cap_pct=str(self.risk.caps["max_daily_loss_pct"]),
            )
            return {"ok": False, "reasons": ["DAILY_LOSS_CAP_AGENT_PAUSED"]}
        if not self.registry.is_ready(market.venue):
            return {"ok": False, "reasons": ["VENUE_NOT_READY"]}
        if self.store.pending_proposal_for_market(market.venue, market.native_id):
            return {"ok": False, "reasons": ["PENDING_PROPOSAL"]}
        if any(
            o["market_id"] == market.native_id
            for o in self.store.list_orders(venue=market.venue, states={"UNKNOWN", "SUBMITTING"}, limit=200)
        ):
            return {"ok": False, "reasons": ["UNKNOWN_ORDER_BLOCKS_MARKET"]}
        books = await self.books_for(market, force=False)
        yes_book = books.get(Outcome.YES)
        if yes_book is None or yes_book.mid is None:
            return {"ok": False, "reasons": ["NO_MID"]}
        mid = yes_book.mid
        report, research_id, research_reasons = await self._get_research(market)
        fair = compute_fair_probability(
            mid,
            report,
            min_sources=self.settings.research_min_sources,
            max_age_minutes=self.settings.research_max_age_minutes,
            max_adjustment_pp=self.settings.llm_adjustment_max_pp,
            research_id=research_id,
        )
        self.store.save_probability(
            market.venue,
            market.native_id,
            mid,
            fair.p_fair,
            fair.adjustment,
            research_id,
            fair.used_research,
        )
        evaluation = self.strategy.evaluate(
            self.registry.get(market.venue),
            market,
            books,
            fair.p_fair,
            used_research=fair.used_research,
            research_reasons=tuple([*research_reasons, *fair.research_reasons]),
            unit_guard_ok=self.unit_guard.ok,
            only_outcome=only_outcome,
        )
        if evaluation.signal is None:
            self.store.save_decision(
                venue=market.venue,
                market_id=market.native_id,
                outcome=only_outcome,
                action="vetoed",
                reason_codes=evaluation.vetoes,
                summary=f"mid={mid}",
            )
            return {
                "ok": False,
                "market": market.ref,
                "mid": str(mid),
                "p_fair": str(fair.p_fair),
                "adjustment": str(fair.adjustment),
                "used_research": fair.used_research,
                "reasons": evaluation.vetoes,
            }
        signal = evaluation.signal
        risk = self._size_signal(signal, max_amount_usd=max_amount_usd)
        if not risk.approved:
            self.store.save_decision(
                venue=market.venue,
                market_id=market.native_id,
                outcome=signal.outcome,
                action="vetoed",
                reason_codes=list(risk.vetoes),
                summary=f"edge={signal.edge}",
            )
            return {
                "ok": False,
                "market": market.ref,
                "mid": str(mid),
                "p_fair": str(fair.p_fair),
                "edge": str(signal.edge),
                "reasons": list(risk.vetoes),
            }
        proposal = Proposal(
            id=stable_id(
                "proposal",
                market.venue.value,
                market.native_id,
                signal.outcome.value,
                utcnow().isoformat(),
                uuid.uuid4().hex,
            ),
            venue=market.venue,
            mode=self.registry.mode(market.venue),
            market_id=market.native_id,
            market_title=market.title,
            outcome=signal.outcome,
            side=Side.BUY,
            price=signal.price,
            size=risk.shares,
            amount_usd=risk.size_usd,
            fee_usd=risk.fee_usd,
            edge=risk.edge,
            p_fair=risk.p_fair,
            binding_cap=risk.binding_cap,
            reasons=tuple(evaluation.reasons),
            risks=tuple(signal.risks),
            expires_at=utcnow() + timedelta(seconds=self.settings.proposal_ttl_seconds),
        )
        if create_proposal:
            self.store.create_proposal(proposal)
            self.store.save_decision(
                venue=market.venue,
                market_id=market.native_id,
                outcome=proposal.outcome,
                action="proposed",
                reason_codes=list(evaluation.reasons),
                summary=f"{proposal.outcome.value} {proposal.size} @ {proposal.price}",
            )
            self.store.audit(
                "agent",
                "proposal.create",
                {"proposal_id": proposal.id, "market": market.ref, "amount_usd": str(proposal.amount_usd)},
            )
            await self.bus.publish("proposal.created", proposal=proposal.model_dump(mode="json"))
        return {
            "ok": True,
            "proposal": proposal.model_dump(mode="json"),
            "mid": str(mid),
            "p_fair": str(fair.p_fair),
            "adjustment": str(fair.adjustment),
            "used_research": fair.used_research,
            "edge": str(risk.edge),
            "binding_cap": risk.binding_cap,
            "reasons": evaluation.reasons,
        }

    def _size_signal(self, signal: Signal, *, max_amount_usd: Decimal | None = None) -> RiskDecision:
        mode = self.registry.mode(signal.venue)
        positions = self.store.list_positions(mode=mode)
        market_positions = [p for p in positions if p.market_id == signal.market.native_id and p.venue == signal.venue]
        snapshot = self.risk.build_snapshot(
            venue=signal.venue,
            market=signal.market,
            bankroll=self.settings.limit_bankroll(signal.venue),
            open_positions=len(positions),
            event_exposure=self.store.event_exposure(signal.venue, mode, signal.market.event_id),
            total_exposure=self.store.total_exposure(mode, signal.venue),
            daily_pnl=self.store.today_pnl(mode),
            market_open_position=bool(market_positions),
        )
        decision = self.risk.size(signal, snapshot, venue=signal.venue, mode=mode)
        if not decision.approved or max_amount_usd is None:
            return decision
        if max_amount_usd < decision.size_usd:
            shares = quantize_shares(max_amount_usd / signal.price, 2)
            amount = (shares * signal.price).quantize(Decimal("0.01"))
            if shares < signal.min_order_shares or amount <= 0:
                return decision.model_copy(update={"approved": False, "vetoes": ("AMOUNT_BELOW_MIN",)})
            fee = (signal.fee_per_share * shares).quantize(Decimal("0.01"))
            return decision.model_copy(
                update={
                    "binding_cap": "USER_AMOUNT",
                    "size_usd": amount,
                    "shares": shares,
                    "fee_usd": fee,
                }
            )
        return decision

    async def manual_buy(
        self,
        market_id: str,
        outcome: Outcome,
        amount_usd: Decimal | None,
        *,
        actor: str,
        execute_now: bool = False,
        venue: VenueId | None = None,
    ) -> dict[str, Any]:
        market = await self.get_market(market_id, venue)
        if amount_usd is not None and amount_usd > self.risk.caps["max_trade_usd"]:
            return {"ok": False, "reasons": ["AMOUNT_ABOVE_MAX_TRADE"]}
        result = await self.analyze_market(
            market, only_outcome=outcome, create_proposal=True, max_amount_usd=amount_usd
        )
        if not result.get("ok"):
            return result
        proposal = Proposal.model_validate(result["proposal"])
        self.store.audit(actor, "proposal.manual", {"proposal_id": proposal.id, "venue": market.venue.value})
        if not execute_now:
            return result
        return await self.approve(proposal.id, actor=actor, via="chat")

    # --------------------------------------------------------------- approve
    async def approve(
        self, proposal_id: str, *, actor: str, via: str, amount_usd: Decimal | None = None
    ) -> dict[str, Any]:
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None:
            return {"ok": False, "error": "proposal not found"}
        if self.paused:
            return {"ok": False, "error": "agent is paused; no new entries"}
        unknown = [
            order
            for order in self.store.list_orders(venue=proposal.venue, states={"UNKNOWN", "SUBMITTING"}, limit=200)
            if order["market_id"] == proposal.market_id
        ]
        if unknown:
            return {
                "ok": False,
                "error": "UNKNOWN/SUBMITTING order exists on this market; reconcile before approving",
            }
        if proposal.status is not ProposalStatus.PENDING:
            return {"ok": False, "error": f"proposal is {proposal.status.value}"}
        if proposal.is_expired():
            self.store.transition_proposal(proposal.id, ProposalStatus.EXPIRED, via=via)
            return {"ok": False, "error": "proposal expired"}
        if amount_usd is not None and (amount_usd <= 0 or amount_usd > proposal.amount_usd):
            return {"ok": False, "error": "amount change may only reduce the proposal amount"}
        if not self.store.transition_proposal(proposal.id, ProposalStatus.APPROVED, via=via):
            return {"ok": False, "error": "proposal already decided"}
        self.store.audit(actor, "proposal.approve", {"proposal_id": proposal.id, "via": via})
        await self.bus.publish("proposal.approved", proposal_id=proposal.id, actor=actor, venue=proposal.venue.value)
        lock = await self.locks.market(f"{proposal.venue.value}:{proposal.market_id}")
        async with lock:
            return await self._execute_proposal(proposal, actor=actor, amount_usd=amount_usd)

    async def _execute_proposal(self, proposal: Proposal, *, actor: str, amount_usd: Decimal | None) -> dict[str, Any]:
        try:
            market = await self.get_market(proposal.market_id, proposal.venue)
            if market.status is not MarketStatus.OPEN:
                return await self._fail_proposal(proposal, "MARKET_NOT_OPEN")
            if market.end_time is not None and market.end_time - utcnow() < timedelta(minutes=5):
                return await self._fail_proposal(proposal, "MARKET_CLOSES_SOON")
            if not self.unit_guard.ok:
                return await self._fail_proposal(proposal, "UNIT_CHECK_FAILED")
            if not market.fee_info or "taker_rate" not in market.fee_info:
                return await self._fail_proposal(proposal, "FEE_INFO_MISSING")
            if proposal.mode is Mode.LIVE and not self.settings.live_for(proposal.venue):
                return await self._fail_proposal(proposal, "LIVE_FLAG_DISABLED")
            adapter = self.registry.get(proposal.venue)
            ref = MarketRef(venue=proposal.venue, market_id=proposal.market_id, outcome=proposal.outcome)
            book = await self.books.get(adapter, ref, force=True)
            if book.age_seconds() > self.settings.book_fresh_seconds:
                return await self._fail_proposal(proposal, "STALE_BOOK")
            if book.best_ask is None or book.best_bid is None or book.spread is None:
                return await self._fail_proposal(proposal, "ONE_SIDED_BOOK")
            if book.spread > self.settings.limits.max_spread:
                return await self._fail_proposal(proposal, "SPREAD_TOO_WIDE")
            if book.synthetic_flag():
                return await self._fail_proposal(proposal, "SYNTHETIC_DEPTH")
            shares = proposal.size
            if amount_usd is not None:
                shares = quantize_shares(amount_usd / book.best_ask, 2)
            if shares <= 0:
                return await self._fail_proposal(proposal, "AMOUNT_ZERO")
            vwap = book.walk(shares)
            if vwap is None:
                return await self._fail_proposal(proposal, "INSUFFICIENT_DEPTH")
            if book.depth_shares() < shares * self.settings.limits.min_depth_multiple:
                return await self._fail_proposal(proposal, "DEPTH_TOO_THIN")
            tick = market.tick_size
            steps = (vwap / tick).to_integral_value(rounding=ROUND_CEILING)
            limit_price = quantize_price(steps * tick, tick)
            venue_fee = adapter.fee(limit_price, shares, Side.BUY, market)
            fee_per_share = venue_fee.fee_usd / shares if shares > 0 else ZERO
            edge = proposal.p_fair - limit_price - fee_per_share
            if edge < self.settings.effective_min_edge():
                return await self._fail_proposal(proposal, "EDGE_GONE")
            amount = (shares * limit_price).quantize(Decimal("0.01"))
            if amount > self.risk.caps["max_trade_usd"]:
                return await self._fail_proposal(proposal, "AMOUNT_ABOVE_MAX_TRADE")
            if self._risk_blocked(proposal, amount):
                return await self._fail_proposal(proposal, "RISK_CAP_RECHECK_FAILED")
            if not self.store.transition_proposal(proposal.id, ProposalStatus.EXECUTING, via=actor):
                return {"ok": False, "error": "proposal could not move to EXECUTING"}
            request = OrderRequest(
                venue=proposal.venue,
                market_id=proposal.market_id,
                outcome=proposal.outcome,
                side=Side.BUY,
                price=limit_price,
                size=shares,
                client_order_id=f"{proposal.venue.value[:3]}-{uuid.uuid4().hex[:22]}",
                reduce_only=False,
                order_type=OrderType.FAK,
                mode=proposal.mode,
            )
            result, local_id = await self.execution.submit(request, market, proposal_id=proposal.id)
            if result.status in {ExecutionStatus.FILLED, ExecutionStatus.PARTIAL, ExecutionStatus.RESTING}:
                self.store.transition_proposal(proposal.id, ProposalStatus.EXECUTED, via=actor)
            else:
                self.store.transition_proposal(
                    proposal.id,
                    ProposalStatus.FAILED,
                    via=actor,
                    error=f"{result.status.value}: {result.message}",
                )
            self.store.audit(
                actor,
                "proposal.executed",
                {
                    "proposal_id": proposal.id,
                    "order_id": local_id,
                    "status": result.status.value,
                    "filled": str(result.filled_size),
                },
            )
            await self.bus.publish(
                "proposal.executed",
                proposal_id=proposal.id,
                order_id=local_id,
                status=result.status.value,
                venue=proposal.venue.value,
            )
            return {
                "ok": result.status in {ExecutionStatus.FILLED, ExecutionStatus.PARTIAL, ExecutionStatus.RESTING},
                "proposal_id": proposal.id,
                "order_id": local_id,
                "status": result.status.value,
                "filled": str(result.filled_size),
                "remaining": str(result.remaining_size),
                "average_price": str(result.average_price) if result.average_price else None,
                "fee_usd": str(result.fee_usd),
                "message": result.message,
                "venue": proposal.venue.value,
                "mode": proposal.mode.value,
            }
        except Exception as exc:
            logger.exception("proposal_execution_error", proposal_id=proposal.id)
            await self._fail_proposal(proposal, f"EXECUTION_ERROR:{type(exc).__name__}")
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    def _risk_blocked(self, proposal: Proposal, amount: Decimal) -> bool:
        mode = proposal.mode
        bankroll = self.settings.limit_bankroll(proposal.venue)
        if self.store.today_pnl(mode) <= -(bankroll * self.risk.caps["max_daily_loss_pct"] / Decimal("100")):
            return True
        if amount > self.risk.caps["max_trade_usd"]:
            return True
        if self.store.total_exposure(mode, proposal.venue) + amount > bankroll * self.risk.caps[
            "max_total_exposure_pct"
        ] / Decimal("100"):
            return True
        market = self.store.get_market(proposal.venue, proposal.market_id)
        event_exposure = self.store.event_exposure(proposal.venue, mode, market.event_id if market else None)
        if event_exposure + amount > bankroll * self.risk.caps["max_event_pct"] / Decimal("100"):
            return True
        positions = self.store.list_positions(mode=mode)
        return bool(
            len(positions) >= int(self.risk.caps["max_open_positions"])
            and not any(p.market_id == proposal.market_id for p in positions)
        )

    def set_limit(self, name: str, value: str, *, actor: str) -> dict[str, Any]:
        """Lower a runtime cap. Raising is refused even if a hard ceiling allows it."""

        try:
            parsed = Decimal(str(value))
        except Exception:
            return {"ok": False, "error": "value must be numeric"}
        try:
            effective = self.risk.set_cap(name, parsed)
        except (ValueError, ArithmeticError) as exc:
            return {"ok": False, "error": str(exc)}
        self.store.set_kv("runtime_limits", {"values": self.risk.as_dict()})
        self.store.audit(actor, "limits.lower", {"name": name, "value": str(effective)})
        return {"ok": True, "name": name, "value": str(effective), "limits": self.limits_dict()}

    async def _fail_proposal(self, proposal: Proposal, reason: str) -> dict[str, Any]:
        self.store.transition_proposal(proposal.id, ProposalStatus.FAILED, via="revalidation", error=reason)
        self.store.save_decision(
            venue=proposal.venue,
            market_id=proposal.market_id,
            outcome=proposal.outcome,
            action="revalidation_failed",
            reason_codes=[reason],
            summary=proposal.id,
        )
        self.store.audit("revalidation", "proposal.failed", {"proposal_id": proposal.id, "reason": reason})
        await self.bus.publish("proposal.failed", proposal_id=proposal.id, reason=reason, venue=proposal.venue.value)
        return {"ok": False, "proposal_id": proposal.id, "error": reason, "status": "FAILED"}

    async def reject(self, proposal_id: str, *, actor: str, via: str = "ui") -> dict[str, Any]:
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None:
            return {"ok": False, "error": "proposal not found"}
        if not self.store.transition_proposal(proposal.id, ProposalStatus.REJECTED, via=via):
            return {"ok": False, "error": f"cannot reject {proposal.status.value}"}
        self.store.audit(actor, "proposal.reject", {"proposal_id": proposal.id})
        await self.bus.publish("proposal.rejected", proposal_id=proposal.id, actor=actor)
        return {"ok": True, "proposal_id": proposal.id, "status": "REJECTED"}

    # --------------------------------------------------------------- positions
    def positions(self, mode: Mode | None = None) -> list[Position]:
        return self.store.list_positions(mode=mode)

    async def close_position(
        self,
        market_id: str,
        outcome: Outcome | None,
        *,
        actor: str,
        amount_usd: Decimal | None = None,
    ) -> dict[str, Any]:
        positions = [
            p
            for p in self.store.list_positions()
            if p.market_id == market_id and (outcome is None or p.outcome is outcome)
        ]
        if not positions:
            return {"ok": False, "error": "no open position"}
        lock = await self.locks.market(f"{positions[0].venue.value}:{market_id}")
        async with lock:
            results: list[dict[str, Any]] = []
            for original in positions:
                position = original
                if amount_usd is not None:
                    adapter = self.registry.get(original.venue)
                    ref = MarketRef(venue=original.venue, market_id=original.market_id, outcome=original.outcome)
                    book = await self.books.get(adapter, ref, force=True)
                    if book.best_bid is None:
                        results.append({"status": "FAILED", "error": "no bid"})
                        continue
                    requested = quantize_shares(amount_usd / book.best_bid, 2)
                    position = original.model_copy(update={"shares": min(original.shares, requested)})
                    if position.shares <= 0:
                        results.append({"status": "FAILED", "error": "amount resolves to zero shares"})
                        continue
                try:
                    result, local_id = await self.execution.exit_position(position, actor=actor, reason="manual close")
                    results.append(
                        {
                            "order_id": local_id,
                            "status": result.status.value,
                            "filled": str(result.filled_size),
                            "message": result.message,
                        }
                    )
                except Exception as exc:
                    results.append({"status": "FAILED", "error": str(exc)[:200]})
        await self.bus.publish("position.close_requested", market_id=market_id, results=results)
        return {"ok": any(r.get("status") in {"FILLED", "PARTIAL"} for r in results), "results": results}

    # ------------------------------------------------------------------ rules
    def create_exit_rule(
        self,
        *,
        market_id: str,
        outcome: Outcome,
        kind: RuleKind,
        trigger_price: Decimal | None = None,
        trigger_at: Any = None,
        actor: str,
    ) -> dict[str, Any]:
        positions = [p for p in self.store.list_positions() if p.market_id == market_id and p.outcome is outcome]
        if not positions:
            return {"ok": False, "error": "no open position for that market/outcome"}
        position = positions[0]
        rule = Rule(
            venue=position.venue,
            mode=position.mode,
            market_id=market_id,
            outcome=outcome,
            kind=kind,
            trigger_price=trigger_price,
            trigger_at=trigger_at,
        )
        self.store.create_rule(rule)
        self.store.audit(actor, "rule.create", {"rule_id": rule.id, "kind": kind.value})
        return {"ok": True, "rule": rule.model_dump(mode="json")}

    def cancel_rule(self, rule_id: str, *, actor: str) -> dict[str, Any]:
        ok = self.store.cancel_rule(rule_id)
        self.store.audit(actor, "rule.cancel", {"rule_id": rule_id, "ok": ok})
        return {"ok": ok}

    def rules(self) -> list[dict[str, Any]]:
        return [r.model_dump(mode="json") for r in self.store.list_rules()]

    # -------------------------------------------------------------- pause/panic
    async def set_paused(self, paused: bool, *, actor: str) -> dict[str, Any]:
        self.paused = paused
        self.store.set_kv("paused", {"paused": paused, "at": utcnow().isoformat(), "actor": actor})
        self.store.audit(actor, "agent.pause" if paused else "agent.resume", {})
        await self.bus.publish("agent.paused" if paused else "agent.resumed", actor=actor)
        return {"ok": True, "paused": paused}

    async def panic(self, *, actor: str, confirm: bool = False) -> dict[str, Any]:
        if not confirm:
            return {"ok": False, "error": "panic requires a second confirmation"}
        await self.set_paused(True, actor=actor)
        cancelled_rules = self.store.cancel_all_rules()
        cancellations = await self.execution.cancel_all(actor=actor)
        self.store.audit(actor, "agent.panic", {"cancelled_rules": cancelled_rules, "cancellations": cancellations})
        await self.bus.publish("agent.panic", actor=actor, cancellations=cancellations, cancelled_rules=cancelled_rules)
        logger.error("panic_stop", actor=actor, cancellations=cancellations)
        return {
            "ok": True,
            "paused": True,
            "cancelled_rules": cancelled_rules,
            "cancellations": cancellations,
            "note": "Positions are not auto-sold. Review each venue manually.",
        }

    # ------------------------------------------------------------------- loops
    async def _expiry_loop(self) -> None:
        while not self._stop.is_set():
            try:
                expired = self.store.expire_proposals()
                if expired:
                    await self.bus.publish("proposal.expired", count=expired)
                await self.reconcile_pending_unknown()
            except Exception:
                logger.exception("expiry_loop_error")
            await asyncio.sleep(10)

    async def reconcile_pending_unknown(self) -> None:
        for order in self.store.orders_needing_reconcile():
            if order["state"] == "UNKNOWN":
                result = await self.execution.reconcile_unknown(order["id"])
                if result.get("resolved"):
                    await self.bus.publish("order.reconciled", order_id=order["id"], state=result.get("state"))

    async def _auto_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.auto_cycle()
            except Exception:
                logger.exception("auto_loop_error")
            await asyncio.sleep(120)

    async def auto_cycle(self) -> list[dict[str, Any]]:
        if self.paused or not self.unit_guard.ok or self.registry.active_or_none is None:
            return []
        if not self.research_service.available:
            return []
        active = self.registry.active
        created = 0
        results: list[dict[str, Any]] = []
        markets = self.store.list_markets(venue=active, status=MarketStatus.OPEN, limit=20)
        for market in markets:
            if created >= self.settings.proposals_per_cycle:
                break
            if self.store.pending_proposal_for_market(market.venue, market.native_id):
                continue
            outcome = await self.analyze_market(market, create_proposal=True)
            results.append(outcome)
            if outcome.get("ok"):
                created += 1
        return results
