"""Book/edge analysis. No venue-specific code here."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal

from .config import Settings
from .domain import (
    BookSnapshot,
    Market,
    Outcome,
    Side,
    VenueId,
    utcnow,
)
from .venues.base import Venue

ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True)
class Signal:
    venue: VenueId
    market: Market
    outcome: Outcome
    side: Side
    book: BookSnapshot
    price: Decimal
    vwap: Decimal
    fee_per_share: Decimal
    edge: Decimal
    p_fair: Decimal
    p_side: Decimal
    max_shares: Decimal
    spread: Decimal
    depth_multiple: Decimal
    min_order_shares: Decimal = Decimal("1")
    min_order_notional: Decimal = Decimal("0.01")
    reasons: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()

    @property
    def market_ref(self) -> str:
        return self.market.ref


@dataclass
class SignalEvaluation:
    signal: Signal | None
    vetoes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def _align_up(value: Decimal, tick: Decimal) -> Decimal:
    steps = (value / tick).to_integral_value(rounding=ROUND_CEILING)
    return steps * tick


class StrategyEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(
        self,
        venue: Venue,
        market: Market,
        books: dict[Outcome, BookSnapshot],
        p_fair: Decimal,
        *,
        used_research: bool,
        research_reasons: tuple[str, ...] = (),
        unit_guard_ok: bool,
        only_outcome: Outcome | None = None,
        mode_units: bool = True,
    ) -> SignalEvaluation:
        del mode_units
        vetoes: list[str] = []
        reasons: list[str] = []
        if not unit_guard_ok:
            return SignalEvaluation(None, ["UNIT_CHECK_FAILED"], reasons)
        if not used_research:
            vetoes.append("NO_USABLE_RESEARCH")
            vetoes.extend(research_reasons)
        if not market.is_tradable:
            vetoes.append("MARKET_NOT_OPEN")
        now = utcnow()
        if market.end_time is not None and market.end_time - now < timedelta(minutes=5):
            vetoes.append("MARKET_CLOSES_SOON")
        min_edge = self.settings.effective_min_edge()
        candidates: list[Signal] = []
        candidate_vetoes: dict[str, list[str]] = {}
        outcomes = (only_outcome,) if only_outcome else (Outcome.YES, Outcome.NO)
        for outcome in outcomes:
            book = books.get(outcome)
            if book is None:
                candidate_vetoes[outcome.value] = ["NO_BOOK"]
                continue
            side_vetoes: list[str] = []
            if book.age_seconds() > self.settings.book_fresh_seconds:
                side_vetoes.append("STALE_BOOK")
            if book.best_ask is None or book.best_bid is None or book.mid is None:
                side_vetoes.append("ONE_SIDED_BOOK")
                candidate_vetoes[outcome.value] = side_vetoes
                continue
            spread = book.spread or ZERO
            if spread > self.settings.limits.max_spread:
                side_vetoes.append("SPREAD_TOO_WIDE")
            if book.synthetic_flag():
                side_vetoes.append("SYNTHETIC_DEPTH")
            p_side = p_fair if outcome is Outcome.YES else ONE - p_fair
            budget_shares = self.settings.max_trade_usd / book.best_ask
            if budget_shares <= 0:
                candidate_vetoes[outcome.value] = ["NO_BUDGET"]
                continue
            vwap = book.walk(budget_shares)
            if vwap is None:
                candidate_vetoes[outcome.value] = ["INSUFFICIENT_DEPTH"]
                continue
            tick = market.tick_size
            price = _align_up(vwap, tick)
            if price >= ONE:
                candidate_vetoes[outcome.value] = ["PRICE_OUT_OF_RANGE"]
                continue
            fee_quote = venue.fee(price, budget_shares, Side.BUY, market)
            fee_per_share = fee_quote.fee_usd / budget_shares
            edge = p_side - price - fee_per_share
            depth = book.depth_shares()
            depth_multiple = depth / budget_shares if budget_shares > 0 else ZERO
            if depth_multiple < self.settings.limits.min_depth_multiple:
                side_vetoes.append("DEPTH_TOO_THIN")
            if edge < min_edge:
                side_vetoes.append("EDGE_BELOW_MIN")
            if price < book.best_ask:
                side_vetoes.append("TICK_MISMATCH")
            signal = Signal(
                venue=market.venue,
                market=market,
                outcome=outcome,
                side=Side.BUY,
                book=book,
                price=price,
                vwap=vwap,
                fee_per_share=fee_per_share,
                edge=edge,
                p_fair=p_fair,
                p_side=p_side,
                max_shares=budget_shares,
                spread=spread,
                depth_multiple=depth_multiple,
                min_order_shares=venue.caps.min_order_shares,
                min_order_notional=venue.caps.min_notional_usd,
                reasons=(
                    f"model p={p_side:.3f}",
                    f"ask={book.best_ask:.3f}",
                    f"spread={spread:.3f}",
                    f"edge={edge:.3f}",
                ),
                risks=(f"book age {book.age_seconds():.0f}s", f"depth {depth_multiple:.1f}x size"),
            )
            candidate_vetoes[outcome.value] = side_vetoes
            if not side_vetoes:
                candidates.append(signal)
        if not candidates:
            for outcome_key, items in candidate_vetoes.items():
                vetoes.extend(f"{outcome_key}_{code}" for code in items)
            return SignalEvaluation(None, sorted(set(vetoes)), reasons)
        best = max(candidates, key=lambda s: s.edge)
        reasons = [*best.reasons, f"side={best.outcome.value}"]
        return SignalEvaluation(best, [], reasons)
