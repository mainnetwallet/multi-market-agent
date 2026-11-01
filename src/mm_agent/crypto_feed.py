"""Live coin price for the "<coin> Up or Down" windows (read-only, public data).

Polymarket resolves these windows from its own oracle, and its public docs do not document an endpoint for the
"Price To Beat" or for the live oracle price. So the app takes the price from Coinbase's public market data:

* the live price is Coinbase's spot price, sampled at most once a second and kept in a short ring buffer,
* the 1-minute candles of the last hour seed the chart, so it is not empty when a window is opened,
* when Polymarket does send a price to beat (``eventMetadata.priceToBeat``) the app uses that; otherwise the
  price to beat is the open of the Coinbase candle at the window start, and the API says which one it is.

Nothing here can place an order; prices stay Decimal-as-string like every other price on the API.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

SPOT_URL = "https://api.coinbase.com/v2/prices/{product}/spot"
CANDLES_URL = "https://api.exchange.coinbase.com/products/{product}/candles"
_SYMBOL = re.compile(r"^[A-Z0-9]{2,10}$")

Fetch = Callable[..., Awaitable[Any]]


def _dec(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class SpotFeed:
    def __init__(
        self,
        fetch: Fetch,
        *,
        clock: Callable[[], float] = time.time,
        max_ticks: int = 900,
        tick_gap: float = 1.0,
        candle_ttl: float = 20.0,
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._max_ticks = max_ticks
        self._tick_gap = tick_gap
        self._candle_ttl = candle_ttl
        self._ticks: dict[str, deque[tuple[float, Decimal]]] = {}
        self._candles: dict[str, tuple[float, list[tuple[int, Decimal, Decimal]]]] = {}

    @staticmethod
    def valid_symbol(symbol: str) -> str:
        sym = (symbol or "").strip().upper()
        if not _SYMBOL.match(sym):
            raise ValueError("bad symbol")
        return sym

    async def _spot(self, symbol: str) -> Decimal:
        buf = self._ticks.setdefault(symbol, deque(maxlen=self._max_ticks))
        now = self._clock()
        if buf and now - buf[-1][0] < self._tick_gap:
            return buf[-1][1]
        data = await self._fetch(SPOT_URL.format(product=f"{symbol}-USD"))
        price = _dec(((data or {}).get("data") or {}).get("amount")) if isinstance(data, dict) else None
        if price is None or price <= 0:
            raise ValueError("no price from the feed")
        buf.append((now, price))
        return price

    async def _minute_candles(self, symbol: str) -> list[tuple[int, Decimal, Decimal]]:
        """(minute start epoch, open, close), oldest first. Empty when the feed has none."""

        now = self._clock()
        cached = self._candles.get(symbol)
        if cached and now - cached[0] < self._candle_ttl:
            return cached[1]
        end = datetime.fromtimestamp(now, tz=UTC)
        start = datetime.fromtimestamp(now - 3600, tz=UTC)
        rows: list[tuple[int, Decimal, Decimal]] = []
        try:
            data = await self._fetch(
                CANDLES_URL.format(product=f"{symbol}-USD"),
                params={"granularity": 60, "start": start.isoformat(), "end": end.isoformat()},
            )
            for row in data if isinstance(data, list) else []:
                # Coinbase: [time, low, high, open, close, volume]
                if isinstance(row, list) and len(row) >= 5:
                    o, c = _dec(row[3]), _dec(row[4])
                    if o is not None and c is not None:
                        rows.append((int(row[0]), o, c))
        except Exception:
            rows = cached[1] if cached else []
        rows.sort(key=lambda r: r[0])
        self._candles[symbol] = (now, rows)
        return rows

    async def snapshot(self, symbol: str, *, window_start: int | None = None) -> dict[str, Any]:
        sym = self.valid_symbol(symbol)
        price = await self._spot(sym)
        candles = await self._minute_candles(sym)
        ticks = list(self._ticks.get(sym, ()))
        start_price: Decimal | None = None
        if window_start is not None:
            minute = window_start - window_start % 60
            for minute_start, opened, _closed in candles:
                if minute_start == minute:
                    start_price = opened
                    break
            if start_price is None:  # no candle for it: the first live sample at or after the start
                for sampled_at, sample in ticks:
                    if sampled_at >= window_start:
                        start_price = sample
                        break
        return {
            "symbol": sym,
            "price": str(price),
            "as_of": self._clock(),
            "source": "coinbase",
            "start_price": str(start_price) if start_price is not None else None,
            "candles": [[t, str(c)] for t, _o, c in candles],
            "ticks": [[round(t, 1), str(p)] for t, p in ticks],
        }
