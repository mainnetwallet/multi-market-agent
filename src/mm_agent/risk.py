"""Risk vetoes and quarter-Kelly sizing. Decimal only; caps in code.

Runtime overrides can only lower a cap. They are persisted and audited.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .config import (
    HARD_MAX_DAILY_LOSS_PCT,
    HARD_MAX_EVENT_PCT,
    HARD_MAX_OPEN_POSITIONS,
    HARD_MAX_TOTAL_EXPOSURE_PCT,
    HARD_MAX_TRADE_USD,
    Settings,
)
from .domain import Market, Mode, RiskDecision, VenueId, quantize_shares
from .strategy import Signal

ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True)
class RiskSnapshot:
    bankroll_usd: Decimal
    open_positions: int
    event_exposure_usd: Decimal
    total_exposure_usd: Decimal
    daily_pnl_usd: Decimal
    market_open_position: bool = False


class RiskEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.caps: dict[str, Decimal] = {
            "max_trade_usd": settings.max_trade_usd,
            "max_event_pct": settings.max_event_pct,
            "max_total_exposure_pct": settings.max_total_exposure_pct,
            "max_daily_loss_pct": settings.max_daily_loss_pct,
            "max_open_positions": Decimal(settings.max_open_positions),
        }
        # Stored hard ceilings. Only "max_trade_usd" is operator-raisable (the $2 cap
        # stopped being a code constant); the rest stay pinned to the code constants.
        self.ceilings: dict[str, Decimal] = {
            "max_trade_usd": HARD_MAX_TRADE_USD,
            "max_event_pct": HARD_MAX_EVENT_PCT,
            "max_total_exposure_pct": HARD_MAX_TOTAL_EXPOSURE_PCT,
            "max_daily_loss_pct": HARD_MAX_DAILY_LOSS_PCT,
            "max_open_positions": Decimal(HARD_MAX_OPEN_POSITIONS),
        }

    def set_cap(self, name: str, value: Decimal) -> Decimal:
        if name not in self.caps:
            raise ValueError(f"unknown limit {name}")
        if name == "max_open_positions":
            if value != value.to_integral_value() or value < 1:
                raise ValueError("max_open_positions must be a positive integer")
            ceiling = min(self.caps[name], Decimal(HARD_MAX_OPEN_POSITIONS))
        else:
            ceiling = min(self.caps[name], self.ceilings[name])
        if value > ceiling:
            raise ValueError(f"{name} can only be lowered at runtime (current ceiling {ceiling})")
        self.caps[name] = value
        return value

    def set_ceiling(self, name: str, value: Decimal) -> Decimal:
        """Raise or lower a stored hard ceiling. Raising requires a step-up token.

        The effective cap follows the ceiling for "max_trade_usd" so that raising the
        ceiling actually raises what the operator can trade.
        """
        if name not in self.ceilings:
            raise ValueError(f"unknown hard ceiling {name}")
        if name == "max_open_positions":
            if value != value.to_integral_value() or value < 1:
                raise ValueError("max_open_positions must be a positive integer")
            value = min(value, Decimal(HARD_MAX_OPEN_POSITIONS))
        elif name != "max_trade_usd":
            value = min(value, self._hard_ceiling(name))
        if value <= 0:
            raise ValueError(f"{name} must be positive")
        self.ceilings[name] = value
        if name == "max_trade_usd":
            self.caps[name] = value
        return value

    def restore_ceilings(self, values: dict[str, str]) -> None:
        for name, raw in values.items():
            try:
                self.set_ceiling(name, Decimal(str(raw)))
            except (ValueError, ArithmeticError):
                continue

    @staticmethod
    def _hard_ceiling(name: str) -> Decimal:
        return {
            "max_trade_usd": HARD_MAX_TRADE_USD,
            "max_event_pct": HARD_MAX_EVENT_PCT,
            "max_total_exposure_pct": HARD_MAX_TOTAL_EXPOSURE_PCT,
            "max_daily_loss_pct": HARD_MAX_DAILY_LOSS_PCT,
        }.get(name, Decimal("0"))

    def restore(self, values: dict[str, str]) -> None:
        for name, raw in values.items():
            try:
                self.set_cap(name, Decimal(str(raw)))
            except (ValueError, ArithmeticError):
                continue

    def as_dict(self) -> dict[str, str]:
        return {k: str(v) for k, v in self.caps.items()}

    def ceilings_dict(self) -> dict[str, str]:
        return {k: str(v) for k, v in self.ceilings.items()}

    def size(
        self,
        signal: Signal,
        snapshot: RiskSnapshot,
        *,
        venue: VenueId,
        mode: Mode,
    ) -> RiskDecision:
        del venue, mode
        vetoes: list[str] = []
        market: Market = signal.market
        p = signal.p_side
        price = signal.price
        if not (ZERO < price < ONE):
            return RiskDecision(approved=False, vetoes=("PRICE_OUT_OF_RANGE",))
        if not market.is_tradable:
            vetoes.append("MARKET_NOT_OPEN")
        if signal.edge < self.settings.effective_min_edge():
            vetoes.append("EDGE_BELOW_MIN")
        if snapshot.daily_pnl_usd <= -(snapshot.bankroll_usd * self.caps["max_daily_loss_pct"] / Decimal("100")):
            vetoes.append("DAILY_LOSS_CAP")
        if snapshot.open_positions >= int(self.caps["max_open_positions"]) and not snapshot.market_open_position:
            vetoes.append("MAX_OPEN_POSITIONS")
        if p <= price:
            vetoes.append("NO_POSITIVE_EXPECTED_VALUE")
        if vetoes:
            return RiskDecision(approved=False, vetoes=tuple(vetoes), edge=signal.edge, p_fair=signal.p_fair)

        kelly_fraction = Decimal("0.25") * (p - price) / (ONE - price)
        if kelly_fraction <= 0:
            return RiskDecision(
                approved=False,
                vetoes=("KELLY_NOT_POSITIVE",),
                edge=signal.edge,
                p_fair=signal.p_fair,
                price=price,
            )
        kelly_usd = snapshot.bankroll_usd * kelly_fraction
        event_remaining = max(
            ZERO,
            snapshot.bankroll_usd * self.caps["max_event_pct"] / Decimal("100") - snapshot.event_exposure_usd,
        )
        total_remaining = max(
            ZERO,
            snapshot.bankroll_usd * self.caps["max_total_exposure_pct"] / Decimal("100") - snapshot.total_exposure_usd,
        )
        loss_cap_remaining = max(
            ZERO,
            snapshot.bankroll_usd * self.caps["max_daily_loss_pct"] / Decimal("100") + snapshot.daily_pnl_usd,
        )
        depth_cap_usd = (signal.book.depth_shares() / self.settings.limits.min_depth_multiple) * price
        caps = {
            "KELLY": kelly_usd,
            "MAX_TRADE": self.caps["max_trade_usd"],
            "EVENT_CAP": event_remaining,
            "TOTAL_EXPOSURE_CAP": total_remaining,
            "DAILY_LOSS_CAP": loss_cap_remaining,
            "DEPTH_CAP": depth_cap_usd,
        }
        binding = min(caps, key=lambda k: caps[k])
        size_usd = caps[binding].quantize(Decimal("0.01"))
        if size_usd <= 0:
            return RiskDecision(
                approved=False,
                vetoes=(f"{binding}_ZERO",),
                edge=signal.edge,
                p_fair=signal.p_fair,
                price=price,
            )
        shares = quantize_shares(size_usd / price, 2)
        if shares < signal.min_order_shares:
            return RiskDecision(
                approved=False,
                vetoes=("BELOW_VENUE_MIN_ORDER",),
                edge=signal.edge,
                p_fair=signal.p_fair,
                price=price,
                size_usd=size_usd,
                shares=shares,
            )
        actual_usd = (shares * price).quantize(Decimal("0.01"))
        if actual_usd < signal.min_order_notional:
            return RiskDecision(
                approved=False,
                vetoes=("BELOW_VENUE_MIN_NOTIONAL",),
                edge=signal.edge,
                p_fair=signal.p_fair,
                price=price,
                size_usd=actual_usd,
                shares=shares,
            )
        fee_estimate = (signal.fee_per_share * shares).quantize(Decimal("0.01"))
        if actual_usd + fee_estimate > caps[binding] + Decimal("0.01") and binding != "KELLY":
            excess = actual_usd + fee_estimate - caps[binding]
            extra_shares = quantize_shares(excess / (price + signal.fee_per_share), 2)
            shares = max(ZERO, shares - extra_shares)
            actual_usd = (shares * price).quantize(Decimal("0.01"))
            fee_estimate = (signal.fee_per_share * shares).quantize(Decimal("0.01"))
        if shares < signal.min_order_shares:
            return RiskDecision(
                approved=False,
                vetoes=("BELOW_VENUE_MIN_ORDER",),
                edge=signal.edge,
                p_fair=signal.p_fair,
                price=price,
                size_usd=actual_usd,
                shares=shares,
            )
        return RiskDecision(
            approved=True,
            binding_cap=binding,
            size_usd=actual_usd,
            shares=shares,
            edge=signal.edge,
            p_fair=signal.p_fair,
            price=price,
            fee_usd=fee_estimate,
        )

    def build_snapshot(
        self,
        *,
        venue: VenueId,
        market: Market,
        bankroll: Decimal,
        open_positions: int,
        event_exposure: Decimal,
        total_exposure: Decimal,
        daily_pnl: Decimal,
        market_open_position: bool,
    ) -> RiskSnapshot:
        del venue
        return RiskSnapshot(
            bankroll_usd=bankroll,
            open_positions=open_positions,
            event_exposure_usd=event_exposure,
            total_exposure_usd=total_exposure,
            daily_pnl_usd=daily_pnl,
            market_open_position=market_open_position,
        )
