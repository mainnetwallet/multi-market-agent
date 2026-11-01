"""Kalshi market data over the WebSocket: ``wss://external-api-ws.kalshi.com/trade-api/ws/v2``.

Protocol (from the public docs; **not yet seen against the live server**, see ``WORKLOG.md``):

* the handshake is signed with the same key headers as the REST API, even for public channels. The key stays
  on the server: this module only receives a function that returns fresh signed headers;
* ``{"id": n, "cmd": "subscribe", "params": {"channels": [...], "market_tickers": [...]}}`` per channel, answered by
  ``{"type": "subscribed", "msg": {"channel", "sid"}}``;
* ``ticker`` (top of book + last price), ``trade``, ``orderbook_delta`` (``orderbook_snapshot`` then deltas, ``seq``
  per subscription) and ``market_lifecycle_v2``;
* ``{"cmd": "update_subscription", "params": {"sids": [...], "market_tickers": [...], "action": "add_markets" |
  "delete_markets" | "get_snapshot"}}``.

Kalshi books hold bids only: ``yes`` bids and ``no`` bids. The YES ask is ``1 - best NO bid``.

Units: the newer messages carry dollar strings (``*_dollars``, ``*_dollars_fp``). The older integer-cent fields are
accepted only as whole numbers from 0 to 100 and divided by 100. Anything else is ignored, and a crossed or
out-of-range quote is dropped, never shown.

A gap in ``seq`` clears the local books and asks for a fresh snapshot. A drop clears everything and the next
snapshot rebuilds it. Display only; nothing here can place an order.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import Any

from websockets.asyncio.client import connect

from ..util import get_logger
from .books import to_dec
from .common import FeedStats, Quote, backoff_delay

logger = get_logger(__name__)

DATA_CHANNELS = ("ticker", "trade", "orderbook_delta")
LIFECYCLE_CHANNEL = "market_lifecycle_v2"
INITIAL_CHUNK = 100
ONE = Decimal(1)
HUNDRED = Decimal(100)
SETTLED_EVENTS = {"determined", "settled", "resolved"}


class KalshiFeedError(RuntimeError):
    """The server refused a subscription; the session ends and the feed reconnects with backoff."""


def _price(body: dict[str, Any], name: str) -> Decimal | None:
    """A price in dollars from ``<name>_dollars`` or, failing that, a whole number of cents in ``<name>``."""

    dollars = body.get(f"{name}_dollars")
    if dollars is not None:
        value = to_dec(dollars)
        return value if value is not None and Decimal(0) <= value <= ONE else None
    legacy = body.get(name)
    if isinstance(legacy, int) and not isinstance(legacy, bool) and 0 <= legacy <= 100:
        return Decimal(legacy) / HUNDRED
    return None


def _levels(body: dict[str, Any], side: str) -> dict[Decimal, Decimal]:
    """``{price: size}`` from a snapshot's ``<side>_dollars_fp`` (or ``<side>_dollars``, or legacy cents ``<side>``)."""

    out: dict[Decimal, Decimal] = {}
    for key, cents in ((f"{side}_dollars_fp", False), (f"{side}_dollars", False), (side, True)):
        rows = body.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            price, size = to_dec(row[0]), to_dec(row[1])
            if price is None or size is None or size <= 0:
                continue
            if cents:
                if not isinstance(row[0], int) or isinstance(row[0], bool) or not 0 <= row[0] <= 100:
                    continue
                price = price / HUNDRED
            if Decimal(0) <= price <= ONE:
                out[price] = size
        return out
    return out


class _Book:
    """One market's two bid sides. ``ready`` once a snapshot has been applied since the last (re)connect or gap."""

    def __init__(self) -> None:
        self.yes: dict[Decimal, Decimal] = {}
        self.no: dict[Decimal, Decimal] = {}
        self.ready = False

    @property
    def bid(self) -> Decimal | None:
        return max(self.yes) if self.yes else None

    @property
    def ask(self) -> Decimal | None:
        return ONE - max(self.no) if self.no else None


class KalshiMarketFeed:
    name = "kalshi_ws"

    def __init__(
        self,
        url: str,
        headers: Callable[[], dict[str, str]],
        *,
        on_quote: Callable[[Quote], None],
        on_state: Callable[[str, bool], None] | None = None,
        on_settled: Callable[[str, bool | None], None] | None = None,
        heartbeat_seconds: float = 10.0,
        ack_timeout: float = 15.0,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        connector: Callable[..., Any] = connect,
        clock: Callable[[], float] = time.monotonic,
        rand: Callable[[], float] | None = None,
    ) -> None:
        self.url = url
        self.stats = FeedStats(clock)
        self.ignored = 0
        self._headers = headers
        self._on_quote = on_quote
        self._on_state = on_state
        self._on_settled = on_settled
        self._heartbeat = heartbeat_seconds
        self._ack_timeout = ack_timeout
        self._backoff = (backoff_base, backoff_cap)
        self._connector = connector
        self._rand = rand
        self._assets: set[str] = set()
        self._books: dict[str, _Book] = {}
        self._tick: dict[str, dict[str, Decimal | None]] = {}
        self._ops: list[tuple[str, list[str]]] = []
        self._next_id = 0
        self._cmd_channel: dict[int, str] = {}
        self._sids: dict[str, int] = {}
        self._seq: dict[int, int] = {}
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
            self._ops.append(("add_markets", new))
        self._wake.set()

    def unwatch(self, assets: Iterable[str]) -> None:
        gone = sorted(set(assets) & self._assets)
        if not gone:
            return
        self._assets.difference_update(gone)
        for asset in gone:
            self._books.pop(asset, None)
            self._tick.pop(asset, None)
        self.stats.subs = len(self._assets)
        if self.stats.connected:
            self._ops.append(("delete_markets", gone))
        self._wake.set()

    def is_stopping(self) -> bool:
        return self._stopping

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="kalshi-market-ws")

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
                # Fresh signed headers on every attempt: the venue rejects an old timestamp.
                async with self._connector(
                    self.url, additional_headers=self._headers(), open_timeout=15, ping_interval=None
                ) as ws:
                    await self._session(ws)
                    attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the message may name a header problem; never log the headers themselves
                logger.warning("kalshi_ws_down", error=type(exc).__name__)
            finally:
                self._set_state(False)
                self._reset_session()
            if self.is_stopping():
                return
            self.stats.reconnects += 1
            kw: dict[str, Any] = {"rand": self._rand} if self._rand is not None else {}
            await asyncio.sleep(backoff_delay(attempt, base=self._backoff[0], cap=self._backoff[1], **kw))
            attempt += 1

    def _reset_session(self) -> None:
        self._books.clear()  # a book kept across a drop could be wrong; the next snapshot rebuilds it
        self._tick.clear()
        self._cmd_channel.clear()
        self._sids.clear()
        self._seq.clear()
        self._ops.clear()

    async def _session(self, ws: Any) -> None:
        tickers = sorted(self._assets)
        for channel in DATA_CHANNELS:
            await self._send(
                ws, "subscribe", {"channels": [channel], "market_tickers": tickers[:INITIAL_CHUNK]}, channel
            )
        await self._send(ws, "subscribe", {"channels": [LIFECYCLE_CHANNEL]}, LIFECYCLE_CHANNEL)
        self._ops.clear()
        rest = tickers[INITIAL_CHUNK:]
        late = sorted(self._assets - set(tickers))  # watched while the first frames were being sent
        for i in range(0, len(rest), INITIAL_CHUNK):
            self._ops.append(("add_markets", rest[i : i + INITIAL_CHUNK]))
        if late:
            self._ops.append(("add_markets", late))
        self._set_state(True)
        sender = asyncio.create_task(self._sender(ws))
        try:
            while True:
                raw = await ws.recv()
                self.stats.message()
                self.handle(raw)
                if sender.done():
                    sender.result()
                if self._ops and all(c in self._sids for c in DATA_CHANNELS):
                    self._wake.set()
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    async def _send(self, ws: Any, cmd: str, params: dict[str, Any], channel: str = "") -> None:
        self._next_id += 1
        if channel:
            self._cmd_channel[self._next_id] = channel
        await ws.send(json.dumps({"id": self._next_id, "cmd": cmd, "params": params}))

    async def _sender(self, ws: Any) -> None:
        """Send queued market changes once every data channel has a ``sid``; keep the link alive with pings."""

        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._heartbeat)
            except TimeoutError:
                waiter = await ws.ping()
                try:
                    await asyncio.wait_for(waiter, timeout=self._heartbeat)
                except TimeoutError as exc:
                    raise ConnectionError("kalshi feed did not answer a ping") from exc
                self.stats.heartbeat()
                continue
            self._wake.clear()
            if not all(c in self._sids for c in DATA_CHANNELS):
                continue
            ops, self._ops = self._ops, []
            for action, tickers in ops:
                for channel in DATA_CHANNELS:
                    sid = self._sids[channel]
                    if action == "get_snapshot" and channel != "orderbook_delta":
                        continue
                    await self._send(
                        ws, "update_subscription", {"sids": [sid], "market_tickers": tickers, "action": action}
                    )

    def _set_state(self, connected: bool) -> None:
        if self.stats.connected == connected:
            return
        self.stats.connected = connected
        if self._on_state is not None:
            self._on_state(self.name, connected)

    # ---------------------------------------------------------------- message handling
    def handle(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(msg, dict):
            return
        kind = str(msg.get("type") or "")
        inner = msg.get("msg")
        body: dict[str, Any] = inner if isinstance(inner, dict) else {}
        if kind == "subscribed":
            self._subscribed(msg, body)
        elif kind == "error":
            self._error(msg, body)
        elif kind == "ticker":
            self._ticker(body)
        elif kind == "trade":
            self._trade(body)
        elif kind == "orderbook_snapshot":
            self._orderbook(msg, body, snapshot=True)
        elif kind == "orderbook_delta":
            self._orderbook(msg, body, snapshot=False)
        elif kind == LIFECYCLE_CHANNEL:
            self._lifecycle(body)

    def _subscribed(self, msg: dict[str, Any], body: dict[str, Any]) -> None:
        channel = str(body.get("channel") or self._cmd_channel.get(_int(msg.get("id")) or -1, ""))
        sid = _int(body.get("sid", msg.get("sid")))
        if channel and sid is not None:
            self._sids[channel] = sid
            if channel in DATA_CHANNELS:
                self._wake.set()

    def _error(self, msg: dict[str, Any], body: dict[str, Any]) -> None:
        channel = self._cmd_channel.get(_int(msg.get("id")) or -1, "")
        detail = f"{body.get('code')}: {str(body.get('msg'))[:120]}"
        if channel == LIFECYCLE_CHANNEL:  # settlement news is a bonus; trading data keeps flowing without it
            logger.info("kalshi_lifecycle_unavailable", detail=detail)
            return
        raise KalshiFeedError(f"kalshi refused a subscription ({channel or 'command'}) {detail}")

    def _ticker(self, body: dict[str, Any]) -> None:
        ticker = str(body.get("market_ticker") or "")
        if ticker not in self._assets:
            return
        values = self._tick.setdefault(ticker, {})
        seen = False
        for field, key in (("bid", "yes_bid"), ("ask", "yes_ask"), ("last", "price")):
            if key in body or f"{key}_dollars" in body:
                values[field] = _price(body, key)
                seen = True
        if not seen:
            self.ignored += 1
            return
        self._emit(ticker, _ts(body))

    def _trade(self, body: dict[str, Any]) -> None:
        ticker = str(body.get("market_ticker") or "")
        price = _price(body, "yes_price")
        if ticker not in self._assets or price is None:
            return
        self._tick.setdefault(ticker, {})["last"] = price
        self._emit(ticker, _ts(body))

    def _orderbook(self, msg: dict[str, Any], body: dict[str, Any], *, snapshot: bool) -> None:
        sid, seq = _int(msg.get("sid")), _int(msg.get("seq"))
        if sid is not None and seq is not None:
            previous = self._seq.get(sid)
            self._seq[sid] = seq
            if previous is not None and seq != previous + 1 and not snapshot:
                self._gap()
                return
        ticker = str(body.get("market_ticker") or "")
        if ticker not in self._assets:
            return
        if snapshot:
            book = self._books.setdefault(ticker, _Book())
            book.yes, book.no = _levels(body, "yes"), _levels(body, "no")
            book.ready = True
            self._emit(ticker, _ts(body))
            return
        known = self._books.get(ticker)
        price = _price(body, "price")
        side = str(body.get("side") or "").lower()
        delta = to_dec(body.get("delta_fp", body.get("delta")))
        if known is None or not known.ready or price is None or delta is None or side not in {"yes", "no"}:
            if known is not None and known.ready:
                self.ignored += 1
            return
        levels = known.yes if side == "yes" else known.no
        new = levels.get(price, Decimal(0)) + delta
        if new > 0:
            levels[price] = new
        else:
            levels.pop(price, None)
        self._emit(ticker, _ts(body))

    def _gap(self) -> None:
        """A missed delta: the local books can no longer be trusted. Drop them and ask for fresh snapshots."""

        self._books.clear()
        self._ops.append(("get_snapshot", sorted(self._assets)))
        self._wake.set()

    def _lifecycle(self, body: dict[str, Any]) -> None:
        ticker = str(body.get("market_ticker") or "")
        if ticker not in self._assets or self._on_settled is None:
            return
        if str(body.get("event_type") or "").lower() not in SETTLED_EVENTS:
            return
        result = str(body.get("result") or "").lower()
        self._on_settled(ticker, True if result == "yes" else False if result == "no" else None)

    def _emit(self, ticker: str, ts: str | None) -> None:
        book, tick = self._books.get(ticker), self._tick.get(ticker, {})
        if book is not None and book.ready:
            bid, ask = book.bid, book.ask
        elif "bid" in tick or "ask" in tick:
            bid, ask = tick.get("bid"), tick.get("ask")
        else:  # only a last trade so far: say nothing rather than blank out the bid and ask the view already has
            return
        if bid is not None and ask is not None and bid > ask:  # a crossed quote is wrong, not a price
            self.ignored += 1
            return
        self._on_quote(Quote(ticker, bid, ask, tick.get("last"), ts))


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _ts(body: dict[str, Any]) -> str | None:
    raw = body.get("ts", body.get("time"))
    return str(raw) if raw is not None else None
