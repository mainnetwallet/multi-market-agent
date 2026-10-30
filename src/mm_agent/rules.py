"""Exit rules: take-profit, stop-loss and time-exit, at most once.

A stale book or feed failure blocks the rule and alerts. It never bypasses a
guard. Pausing entries does not stop the rules monitor.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from .agent import AgentService
from .books import BookCache
from .config import Settings
from .domain import (
    MarketRef,
    Mode,
    Outcome,
    Position,
    Rule,
    RuleKind,
    RuleStatus,
    utcnow,
)
from .events import EventBus
from .store import Store
from .util import get_logger
from .venues.registry import VenueRegistry

logger = get_logger(__name__)


class RulesEngine:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        registry: VenueRegistry,
        books: BookCache,
        agent: AgentService,
        bus: EventBus,
    ) -> None:
        self.settings = settings
        self.store = store
        self.registry = registry
        self.books = books
        self.agent = agent
        self.bus = bus
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._blocked_alerts: set[str] = set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="rules-engine")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("rules_loop_error")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5)
            except TimeoutError:
                continue

    async def run_once(self) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        positions = self.store.list_positions()
        by_key: dict[tuple[str, Mode, str, Outcome], Position] = {
            (p.venue.value, p.mode, p.market_id, p.outcome): p for p in positions
        }
        rules = self.store.list_rules(status=RuleStatus.ACTIVE) + self.store.list_rules(status=RuleStatus.BLOCKED)
        # Rules must run for every venue with positions, not just the active one.
        for rule in rules:
            key = (rule.venue.value, rule.mode, rule.market_id, rule.outcome)
            position = by_key.get(key)
            if position is None or position.shares <= 0:
                self.store.mark_rule(rule.id, RuleStatus.DONE, "position closed")
                continue
            result = await self._evaluate(rule, position)
            if result:
                results.append(result)
        return results

    async def _evaluate(self, rule: Rule, position: Position) -> dict[str, object] | None:
        if not self.registry.is_ready(rule.venue):
            await self._block(rule, "venue unavailable")
            return None
        adapter = self.registry.get(rule.venue)
        ref = MarketRef(venue=rule.venue, market_id=rule.market_id, outcome=rule.outcome)
        try:
            book = await self.books.get(adapter, ref, force=True)
        except Exception as exc:
            await self._block(rule, f"feed error: {type(exc).__name__}")
            return None
        if book.age_seconds() > self.settings.book_fresh_seconds:
            await self._block(rule, "stale book")
            return None
        if book.best_bid is None:
            await self._block(rule, "no bid")
            return None
        triggered, detail = self._triggered(rule, book.best_bid)
        if not triggered:
            return None
        if not self.store.claim_rule(rule.id):
            return None
        if not self.store.transition_rule(rule.id, RuleStatus.TRIGGERED, RuleStatus.EXECUTING):
            self.store.mark_rule(rule.id, RuleStatus.FAILED, "could not claim execution")
            return {"rule_id": rule.id, "status": "FAILED"}
        await self.bus.publish(
            "rule.triggered",
            rule_id=rule.id,
            kind=rule.kind.value,
            market=ref.namespaced,
            detail=detail,
            price=str(book.best_bid),
        )
        try:
            async with await self.agent.locks.market(f"{rule.venue.value}:{rule.market_id}"):
                result, local_id = await self.agent.execution.exit_position(
                    position, actor="rules-engine", reason=f"{rule.kind.value} {detail}"
                )
            success = result.status.value in {"FILLED", "PARTIAL", "RESTING"}
            self.store.mark_rule(rule.id, RuleStatus.DONE if success else RuleStatus.FAILED, result.message)
            self._cancel_siblings(rule)
            self.store.audit(
                "rules-engine",
                "rule.fire",
                {
                    "rule_id": rule.id,
                    "order_id": local_id,
                    "status": result.status.value,
                    "detail": detail,
                },
            )
            return {
                "rule_id": rule.id,
                "kind": rule.kind.value,
                "status": result.status.value,
                "order_id": local_id,
            }
        except Exception as exc:
            self.store.mark_rule(rule.id, RuleStatus.FAILED, str(exc)[:300])
            logger.exception("rule_exit_failed", rule_id=rule.id)
            return {"rule_id": rule.id, "status": "FAILED", "error": str(exc)[:200]}

    def _triggered(self, rule: Rule, bid: Decimal) -> tuple[bool, str]:
        if rule.kind is RuleKind.TAKE_PROFIT:
            return bid >= (rule.trigger_price or Decimal("2")), f"bid {bid} >= {rule.trigger_price}"
        if rule.kind is RuleKind.STOP_LOSS:
            return bid <= (rule.trigger_price or Decimal("-1")), f"bid {bid} <= {rule.trigger_price}"
        at = rule.trigger_at or utcnow()
        return utcnow() >= at, f"time exit {at.isoformat()}"

    def _cancel_siblings(self, rule: Rule) -> None:
        for sibling in self.store.list_rules():
            if (
                sibling.id != rule.id
                and sibling.market_id == rule.market_id
                and sibling.venue is rule.venue
                and sibling.mode is rule.mode
                and sibling.status in {RuleStatus.ACTIVE, RuleStatus.BLOCKED}
            ):
                self.store.cancel_rule(sibling.id)

    async def _block(self, rule: Rule, reason: str) -> None:
        if rule.status is RuleStatus.ACTIVE:
            self.store.mark_rule(rule.id, RuleStatus.BLOCKED, reason)
        if rule.id not in self._blocked_alerts:
            self._blocked_alerts.add(rule.id)
            self.store.audit("rules-engine", "rule.blocked", {"rule_id": rule.id, "reason": reason})
            await self.bus.publish("rule.blocked", rule_id=rule.id, reason=reason, venue=rule.venue.value)
            logger.warning("rule_blocked", rule_id=rule.id, reason=reason)
