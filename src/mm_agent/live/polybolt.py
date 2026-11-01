"""Live coin prices for the Up or Down windows.

* ``PolyBoltFeed``: ``wss://ws-live-v2.polymarket.com/ws``. It needs the CLOB API credentials in an auth frame.
  Venue credentials are read only by the isolated ``*_signing`` modules, so this module never reads one: the
  caller passes ``auth_frame``, a function returning the frame. Without it the feed is not built and
  ``CryptoPrices`` uses Coinbase (``source: "coinbase"``). The frame layout and the ``price.crypto`` message
  shape come from the docs and are **unverified live**; any auth or parse failure falls back to Coinbase.
* ``CryptoPrices.latest(symbol)`` returns ``{"symbol", "price", "source", "ts"}`` or None.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from websockets.asyncio.client import connect

from ..crypto_feed import SpotFeed
from ..util import get_logger
from .common import FeedStats, backoff_delay

logger = get_logger(__name__)

POLYBOLT_URL = "wss://ws-live-v2.polymarket.com/ws"
MAX_SUBSCRIPTIONS = 64  # per connection
MAX_AGE_SECONDS = 15.0


class PolyBoltFeed:
    name = "polybolt"

    def __init__(
        self,
        auth_frame: Callable[[], dict[str, Any]],
        *,
        url: str = POLYBOLT_URL,
        on_state: Callable[[str, bool], None] | None = None,
        silence_limit: float = 30.0,
        connector: Callable[..., Any] = connect,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url
        self.stats = FeedStats(clock)
        self.prices: dict[str, tuple[float, Decimal]] = {}
        self._auth_frame = auth_frame
        self._on_state = on_state
        self._silence = silence_limit
        self._connector = connector
        self._clock = clock
        self._symbols: set[str] = set()
        self._task: asyncio.Task[None] | None = None

    def watch(self, symbol: str) -> bool:
        if symbol in self._symbols:
            return True
        if len(self._symbols) >= MAX_SUBSCRIPTIONS:
            return False
        self._symbols.add(symbol)
        self.stats.subs = len(self._symbols)
        return True

    def price(self, symbol: str) -> Decimal | None:
        got = self.prices.get(symbol.lower())
        if got is None or self._clock() - got[0] > MAX_AGE_SECONDS:
            return None
        return got[1]

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="polybolt")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        attempt = 0
        while True:
            try:
                async with self._connector(self.url, open_timeout=15, ping_interval=None) as ws:
                    await ws.send(json.dumps(self._auth_frame()))  # never logged
                    for symbol in sorted(self._symbols):
                        await ws.send(json.dumps({"type": "price.crypto", "symbol": f"{symbol.lower()}usd"}))
                    self.stats.connected = True
                    if self._on_state:
                        self._on_state(self.name, True)
                    attempt = 0
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=self._silence)
                        self.stats.message()
                        self.handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("polybolt_down", error=type(exc).__name__)  # the message may echo the frame: not logged
            finally:
                if self.stats.connected and self._on_state:
                    self._on_state(self.name, False)
                self.stats.connected = False
            self.stats.reconnects += 1
            await asyncio.sleep(backoff_delay(attempt))
            attempt += 1

    def handle(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        for item in msg if isinstance(msg, list) else [msg]:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").lower()
            try:
                value = Decimal(str(item.get("price") if item.get("price") is not None else item.get("value")))
            except (InvalidOperation, ValueError):
                continue
            if symbol.endswith("usd") and value > 0:
                self.prices[symbol[:-3]] = (self._clock(), value)


class CryptoPrices:
    """PolyBolt when configured and fresh, Coinbase otherwise. The answer always names its source."""

    def __init__(self, spot: SpotFeed | None, bolt: PolyBoltFeed | None = None) -> None:
        self._spot = spot
        self._bolt = bolt

    async def latest(self, symbol: str) -> dict[str, Any] | None:
        try:
            sym = SpotFeed.valid_symbol(symbol).lower()
        except ValueError:
            return None
        if self._bolt is not None:
            self._bolt.watch(sym)
            price = self._bolt.price(sym)
            if price is not None:
                return {"symbol": sym.upper(), "price": str(price), "source": "polybolt", "ts": time.time()}
        if self._spot is None:
            return None
        try:
            snap = await self._spot.snapshot(sym.upper())
        except Exception:
            return None
        return {"symbol": sym.upper(), "price": snap["price"], "source": "coinbase", "ts": snap["as_of"]}
