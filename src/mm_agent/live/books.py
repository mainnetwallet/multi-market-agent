"""A local order book for one token, kept from ``book`` snapshots and ``price_change`` deltas."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def to_dec(value: Any) -> Decimal | None:
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


class LocalBook:
    def __init__(self) -> None:
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.last: Decimal | None = None
        self.tick: Decimal | None = None

    def replace(self, bids: Any, asks: Any) -> None:
        self.bids = _levels(bids)
        self.asks = _levels(asks)

    def apply(self, side: str, price: Decimal, size: Decimal) -> None:
        """One level changed. Size 0 removes the level; ``BUY`` is the bid side."""

        book = self.bids if side.upper() == "BUY" else self.asks
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size

    @property
    def best_bid(self) -> Decimal | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return min(self.asks) if self.asks else None


def _levels(rows: Any) -> dict[Decimal, Decimal]:
    out: dict[Decimal, Decimal] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        price, size = to_dec(row.get("price")), to_dec(row.get("size"))
        if price is not None and size is not None and size > 0:
            out[price] = size
    return out
