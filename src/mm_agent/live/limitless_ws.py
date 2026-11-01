"""Limitless market data over Socket.IO: ``wss://ws.limitless.exchange``, namespace ``/markets``.

Protocol (from the public docs; **not yet seen against the live server**, see ``WORKLOG.md``):

* Engine.IO v4 over a raw WebSocket: the server opens with ``0{json}``, pings with ``2`` (we answer ``3``);
  the namespace is joined with ``40/markets,`` and acknowledged with ``40/markets,...``;
* ``42/markets,["subscribe_market_prices",{"marketSlugs":[...]}]`` is public (no credentials). The server
  *replaces* the previous subscription, so every change re-sends the whole slug list, and a reconnect
  subscribes again from scratch;
* ``orderbookUpdate`` carries a full CLOB book snapshot for a market slug; ``newPriceData`` carries an AMM price;
  ``subscribe_market_lifecycle`` reports markets closing or resolving.

Prices are probabilities from 0 to 1. A value outside that range is ignored, never rescaled. An AMM price has no
book, so it travels as ``Quote.quoted`` (one quoted price, no bid or ask). Display only.
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

NAMESPACE = "/markets"
CHECK_SECONDS = 0.5
OPEN_TIMEOUT = 15.0
ZERO = Decimal(0)
ONE = Decimal(1)
SETTLED_STATES = {"resolved", "settled", "closed", "expired"}


class LimitlessFeedError(RuntimeError):
    """The server refused or dropped the stream; the feed reconnects with backoff."""


def _prob(value: Any) -> Decimal | None:
    out = to_dec(value)
    return out if out is not None and ZERO <= out <= ONE else None


def _best(rows: Any, *, highest: bool) -> Decimal | None:
    prices: list[Decimal] = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            price, size = _prob(row.get("price")), to_dec(row.get("size", "1"))
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            price, size = _prob(row[0]), to_dec(row[1])
        else:
            continue
        if price is not None and size is not None and size > 0:
            prices.append(price)
    if not prices:
        return None
    return max(prices) if highest else min(prices)


class LimitlessMarketFeed:
    name = "limitless_ws"

    def __init__(
        self,
        url: str,
        *,
        on_quote: Callable[[Quote], None],
        on_state: Callable[[str, bool], None] | None = None,
        on_settled: Callable[[str, bool | None], None] | None = None,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        connector: Callable[..., Any] = connect,
        clock: Callable[[], float] = time.monotonic,
        rand: Callable[[], float] | None = None,
    ) -> None:
        self.url = url
        self.stats = FeedStats(clock)
        self.ignored = 0
        self._on_quote = on_quote
        self._on_state = on_state
        self._on_settled = on_settled
        self._backoff = (backoff_base, backoff_cap)
        self._connector = connector
        self._rand = rand
        self._clock = clock
        self._assets: set[str] = set()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    # ---------------------------------------------------------------- owner API
    def watch(self, assets: Iterable[str]) -> None:
        new = set(assets) - self._assets
        if new:
            self._assets.update(new)
            self.stats.subs = len(self._assets)
            self._wake.set()

    def unwatch(self, assets: Iterable[str]) -> None:
        gone = set(assets) & self._assets
        if gone:
            self._assets.difference_update(gone)
            self.stats.subs = len(self._assets)
            self._wake.set()

    def is_stopping(self) -> bool:
        return self._stopping

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="limitless-market-ws")

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
                async with self._connector(self.url, open_timeout=OPEN_TIMEOUT, ping_interval=None) as ws:
                    await self._session(ws)
                    attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("limitless_ws_down", error=type(exc).__name__)
            finally:
                self._set_state(False)
            if self.is_stopping():
                return
            self.stats.reconnects += 1
            kw: dict[str, Any] = {"rand": self._rand} if self._rand is not None else {}
            await asyncio.sleep(backoff_delay(attempt, base=self._backoff[0], cap=self._backoff[1], **kw))
            attempt += 1

    async def _session(self, ws: Any) -> None:
        opened = _text(await asyncio.wait_for(ws.recv(), timeout=OPEN_TIMEOUT))
        if not opened.startswith("0"):
            raise LimitlessFeedError(f"unexpected first frame {opened[:40]!r}")
        info = _json(opened[1:])
        info = info if isinstance(info, dict) else {}
        silence_limit = (float(info.get("pingInterval") or 25000) + float(info.get("pingTimeout") or 20000)) / 1000.0
        await ws.send(f"40{NAMESPACE},")
        joined = False
        subscribed: tuple[str, ...] = ()
        last_frame = self._clock()
        while True:
            try:
                raw: str | bytes | None = await asyncio.wait_for(ws.recv(), timeout=CHECK_SECONDS)
            except TimeoutError:
                raw = None
            now = self._clock()
            if raw is None:
                if now - last_frame > silence_limit:
                    raise LimitlessFeedError("stream went silent")
            else:
                last_frame = now
                text = _text(raw)
                if text == "2":  # Engine.IO ping
                    self.stats.heartbeat()
                    await ws.send("3")
                elif text.startswith("1"):
                    raise LimitlessFeedError("server closed the stream")
                elif text.startswith(f"40{NAMESPACE}"):
                    joined = True
                    subscribed = ()
                    self._set_state(True)
                elif text.startswith(f"44{NAMESPACE}"):
                    raise LimitlessFeedError(f"namespace refused: {text[:120]}")
                elif text.startswith(f"41{NAMESPACE}"):
                    raise LimitlessFeedError("namespace disconnected")
                elif text.startswith("42"):
                    self.stats.message()
                    self.handle(text)
            if joined:
                wanted = tuple(sorted(self._assets))
                if wanted and wanted != subscribed:
                    first = not subscribed
                    await self._emit_event(ws, "subscribe_market_prices", {"marketSlugs": list(wanted)})
                    if first:
                        await self._emit_event(ws, "subscribe_market_lifecycle", None)
                    subscribed = wanted

    async def _emit_event(self, ws: Any, event: str, data: Any) -> None:
        payload = [event] if data is None else [event, data]
        await ws.send(f"42{NAMESPACE},{json.dumps(payload, separators=(',', ':'))}")

    def _set_state(self, connected: bool) -> None:
        if self.stats.connected == connected:
            return
        self.stats.connected = connected
        if self._on_state is not None:
            self._on_state(self.name, connected)

    # ---------------------------------------------------------------- message handling
    def handle(self, text: str) -> None:
        """One Socket.IO event frame (``42/markets,["name",{...}]``)."""

        parsed = _socketio_event(text)
        if parsed is None:
            return
        name, data = parsed
        body: dict[str, Any] = data if isinstance(data, dict) else {}
        if name == "orderbookUpdate":
            self._orderbook(body)
        elif name == "newPriceData":
            self._price(body)
        elif name in {"marketLifecycle", "market_lifecycle", "marketResolved", "marketClosed"}:
            self._lifecycle(body)
        elif name in {"exception", "error"}:
            if "lifecycle" in json.dumps(data).lower():  # a bonus channel; prices keep flowing without it
                logger.info("limitless_lifecycle_unavailable")
                return
            raise LimitlessFeedError(f"server error event: {str(data)[:140]}")

    def _slug(self, body: dict[str, Any]) -> str | None:
        slug = str(body.get("marketSlug") or body.get("slug") or "")
        if slug in self._assets:
            return slug
        if slug:
            return None
        self.ignored += 1  # an update that names no market cannot be matched to a row
        return None

    def _orderbook(self, body: dict[str, Any]) -> None:
        slug = self._slug(body)
        if slug is None:
            return
        raw = body.get("orderbook")
        book: dict[str, Any] = raw if isinstance(raw, dict) else body
        bid, ask = _best(book.get("bids"), highest=True), _best(book.get("asks"), highest=False)
        if bid is not None and ask is not None and bid > ask:  # a crossed book is wrong, not a price
            self.ignored += 1
            return
        self._on_quote(Quote(slug, bid, ask, None, _ts(body)))

    def _price(self, body: dict[str, Any]) -> None:
        slug = self._slug(body)
        if slug is None:
            return
        prices = body.get("updatedPrices")
        yes = _prob(prices.get("yes")) if isinstance(prices, dict) else None
        if yes is None:
            self.ignored += 1
            return
        self._on_quote(Quote(slug, None, None, None, _ts(body), quoted=yes))

    def _lifecycle(self, body: dict[str, Any]) -> None:
        slug = self._slug(body)
        if slug is None or self._on_settled is None:
            return
        state = str(body.get("status") or body.get("state") or body.get("event") or "").lower()
        if state not in SETTLED_STATES:
            return
        winner = body.get("winningOutcome", body.get("winningOutcomeIndex"))
        yes_won: bool | None = None
        if winner in (0, "0", "yes", "YES", "Yes"):
            yes_won = True
        elif winner in (1, "1", "no", "NO", "No"):
            yes_won = False
        self._on_settled(slug, yes_won)


def _text(raw: str | bytes) -> str:
    return raw.decode() if isinstance(raw, bytes) else raw


def _json(raw: str | bytes) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _ts(body: dict[str, Any]) -> str | None:
    raw = body.get("timestamp")
    return str(raw) if raw is not None else None


def _socketio_event(text: str) -> tuple[str, Any] | None:
    """Parse ``42/ns,["event",{...}]`` (a namespace is optional, and so is a numeric ack id)."""

    body = text[2:]
    if body.startswith("/"):
        _, _, body = body.partition(",")
    body = body.lstrip("0123456789")
    payload = _json(body)
    if isinstance(payload, list) and payload and isinstance(payload[0], str):
        return payload[0], payload[1] if len(payload) > 1 else None
    return None
