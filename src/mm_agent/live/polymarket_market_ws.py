"""Polymarket market channel: ``wss://ws-subscriptions-clob.polymarket.com/ws/market``.

Protocol (from the public docs; **not yet seen against the live server**, see ``WORKLOG.md``):

* first frame ``{"assets_ids": [...], "type": "market", "custom_feature_enabled": true}``;
  later ``{"assets_ids": [...], "operation": "subscribe" | "unsubscribe"}``;
* the text ``PING`` every 10 s, the server answers ``PONG``;
* events ``book`` (full snapshot), ``price_change`` (``price_changes[]``, size ``"0"`` removes a level),
  ``last_trade_price``, ``tick_size_change``, and with the flag ``best_bid_ask``, ``new_market``,
  ``market_resolved``.

The feed keeps one local book per token and tells its owner the top of the book whenever it changes. It
never trusts a book across a reconnect: on a drop every local book is cleared and the server's fresh
``book`` snapshots rebuild them. Display only, nothing here can place an order.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterable
from typing import Any

from websockets.asyncio.client import connect

from ..util import get_logger
from .books import LocalBook, to_dec
from .common import FeedStats, Quote, backoff_delay

logger = get_logger(__name__)

MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
INITIAL_CHUNK = 200


class PolymarketMarketFeed:
    name = "polymarket_market"

    def __init__(
        self,
        url: str = MARKET_URL,
        *,
        on_quote: Callable[[Quote], None],
        on_state: Callable[[str, bool], None] | None = None,
        on_resolved: Callable[[list[str], str | None], None] | None = None,
        on_new_market: Callable[[dict[str, Any]], None] | None = None,
        ping_seconds: float = 10.0,
        silence_limit: float = 30.0,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        connector: Callable[..., Any] = connect,
        clock: Callable[[], float] = time.monotonic,
        rand: Callable[[], float] | None = None,
    ) -> None:
        self.url = url
        self.stats = FeedStats(clock)
        self.tick_sizes: dict[str, str] = {}
        self._on_quote = on_quote
        self._on_state = on_state
        self._on_resolved = on_resolved
        self._on_new_market = on_new_market
        self._ping = ping_seconds
        self._silence = silence_limit
        self._backoff = (backoff_base, backoff_cap)
        self._connector = connector
        self._rand = rand
        self._assets: set[str] = set()
        self._books: dict[str, LocalBook] = {}
        self._ops: list[dict[str, Any]] = []
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    # ---------------------------------------------------------------- owner API
    def watch(self, assets: Iterable[str]) -> None:
        new = sorted(set(assets) - self._assets)
        if not new:
            return
        self._assets.update(new)
        self.stats.subs = len(self._assets)
        if self.stats.connected:
            self._ops.append({"assets_ids": new, "operation": "subscribe"})
        self._wake.set()

    def unwatch(self, assets: Iterable[str]) -> None:
        gone = sorted(set(assets) & self._assets)
        if not gone:
            return
        self._assets.difference_update(gone)
        for asset in gone:
            self._books.pop(asset, None)
        self.stats.subs = len(self._assets)
        if self.stats.connected:
            self._ops.append({"assets_ids": gone, "operation": "unsubscribe"})
        self._wake.set()

    def is_stopping(self) -> bool:
        return self._stopping

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="polymarket-market-ws")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # ---------------------------------------------------------------- connection loop
    async def _run(self) -> None:
        attempt = 0
        while not self.is_stopping():
            if not self._assets:  # nothing to follow: stay disconnected until a client asks for something
                self._wake.clear()
                if not self._assets:
                    await self._wake.wait()
                continue
            try:
                async with self._connector(self.url, open_timeout=15, ping_interval=None) as ws:
                    await self._session(ws)
                    attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("polymarket_market_ws_down", error=type(exc).__name__)
            finally:
                self._set_state(False)
                self._books.clear()  # a book kept across a drop could be wrong; the next snapshot rebuilds it
            if self.is_stopping():
                return
            self.stats.reconnects += 1
            await asyncio.sleep(backoff_delay(attempt, base=self._backoff[0], cap=self._backoff[1], **self._rand_kw()))
            attempt += 1

    def _rand_kw(self) -> dict[str, Any]:
        return {"rand": self._rand} if self._rand is not None else {}

    async def _session(self, ws: Any) -> None:
        assets = sorted(self._assets)
        await ws.send(
            json.dumps({"assets_ids": assets[:INITIAL_CHUNK], "type": "market", "custom_feature_enabled": True})
        )
        for i in range(INITIAL_CHUNK, len(assets), INITIAL_CHUNK):
            await ws.send(json.dumps({"assets_ids": assets[i : i + INITIAL_CHUNK], "operation": "subscribe"}))
        self._ops.clear()
        self._set_state(True)
        late = sorted(self._assets - set(assets))  # watched while the first frame was being sent
        if late:
            await ws.send(json.dumps({"assets_ids": late, "operation": "subscribe"}))
        sender = asyncio.create_task(self._sender(ws))
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self._silence)
                except TimeoutError as exc:
                    raise ConnectionError("market feed went silent") from exc
                self.stats.message()
                self.handle(raw)
                if sender.done():
                    sender.result()
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    async def _sender(self, ws: Any) -> None:
        """Send queued subscribe/unsubscribe frames at once and ``PING`` every ``ping_seconds``."""

        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._ping)
            except TimeoutError:
                await ws.send("PING")
                continue
            self._wake.clear()
            ops, self._ops = self._ops, []
            for op in ops:
                await ws.send(json.dumps(op))

    def _set_state(self, connected: bool) -> None:
        if self.stats.connected == connected:
            return
        self.stats.connected = connected
        if self._on_state is not None:
            self._on_state(self.name, connected)

    # ---------------------------------------------------------------- message handling
    def handle(self, raw: str | bytes) -> None:
        text = raw.decode() if isinstance(raw, bytes) else raw
        if text.strip().upper() == "PONG":
            return
        try:
            data = json.loads(text)
        except ValueError:
            return
        for msg in data if isinstance(data, list) else [data]:
            if isinstance(msg, dict):
                self._event(msg)

    def _event(self, msg: dict[str, Any]) -> None:
        kind = str(msg.get("event_type") or msg.get("type") or "")
        ts = str(msg["timestamp"]) if msg.get("timestamp") is not None else None
        if kind == "book":
            asset = str(msg.get("asset_id") or "")
            if asset in self._assets:
                book = self._books.setdefault(asset, LocalBook())
                book.replace(msg.get("bids"), msg.get("asks"))
                self._emit(asset, ts)
        elif kind == "price_change":
            touched: set[str] = set()
            for change in msg.get("price_changes") or []:
                if not isinstance(change, dict):
                    continue
                asset = str(change.get("asset_id") or "")
                price, size = to_dec(change.get("price")), to_dec(change.get("size"))
                if asset not in self._assets or price is None or size is None:
                    continue
                book = self._books.setdefault(asset, LocalBook())
                book.apply(str(change.get("side") or ""), price, size)
                touched.add(asset)
            for asset in sorted(touched):
                self._emit(asset, ts)
        elif kind == "best_bid_ask":
            asset = str(msg.get("asset_id") or "")
            if asset in self._assets:
                known = self._books.get(asset)
                last = known.last if known is not None else None
                self._on_quote(Quote(asset, to_dec(msg.get("best_bid")), to_dec(msg.get("best_ask")), last, ts))
        elif kind == "last_trade_price":
            asset = str(msg.get("asset_id") or "")
            price = to_dec(msg.get("price"))
            if asset in self._assets and price is not None:
                self._books.setdefault(asset, LocalBook()).last = price
                self._emit(asset, ts)
        elif kind == "tick_size_change":
            asset = str(msg.get("asset_id") or "")
            tick = to_dec(msg.get("new_tick_size"))
            if asset and tick is not None:
                self.tick_sizes[asset] = str(tick)
                if asset in self._books:
                    self._books[asset].tick = tick
        elif kind == "market_resolved":
            ids = [str(a) for a in msg.get("assets_ids") or [] if a]
            winner = msg.get("winning_asset_id")
            if self._on_resolved is not None:
                self._on_resolved(ids, str(winner) if winner else None)
        elif kind == "new_market" and self._on_new_market is not None:
            self._on_new_market(msg)

    def _emit(self, asset: str, ts: str | None) -> None:
        book = self._books.get(asset)
        if book is not None:
            self._on_quote(Quote(asset, book.best_bid, book.best_ask, book.last, ts))
