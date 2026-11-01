"""The live hub: many clients, one upstream subscription per asset, push updates for event views.

* **One upstream per feed, ref-counted.** An asset is subscribed upstream when the first event that needs it
  is opened and unsubscribed when the last one is closed.
* **Latest value wins.** Every event keeps its current view; feed ticks change rows in place and are merged
  into one pending patch. A patch goes out at most once per ``flush_interval`` (default 0.25 s, so at most 4
  per second per event) and always carries the newest numbers, never a queue of old ones.
* **Every frame has ``seq`` (per event), ``source``, ``source_ts`` and ``server_ts``.** A client that sees a
  gap in ``seq`` asks for a snapshot. A slow client's queue drops its oldest patches, never a snapshot.
* **Fail closed on freshness.** A feed that is down or silent marks its events ``stale`` and the hub polls
  REST for them (at most every ``rest_interval`` seconds per event, and ``rest_per_tick`` events per second in
  total) until the feed is back; recovery sends a fresh snapshot.

Display only. Nothing here can place, change or cancel an order; trading from a view uses ``/v1/orders/*`` with
a fresh book at submit time.
"""

from __future__ import annotations

import asyncio
import copy
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ..util import get_logger
from .common import Quote
from .history import RANGE_INTERVAL
from .polymarket_sports_ws import GameState, norm_name

logger = get_logger(__name__)

LoadView = Callable[[str, str], Awaitable[dict[str, Any] | None]]
HistoryFn = Callable[[list[str], str], Awaitable[dict[str, list[list[Any]]]]]
Key = tuple[str, str]

VENUES = {"polymarket", "kalshi", "limitless"}
FEED_VENUE = {"polymarket_market": "polymarket", "kalshi_ws": "kalshi", "limitless_ws": "limitless"}
_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,129}$")
ONE = Decimal(1)
ZERO = Decimal(0)
TAIL_MAX = 2000


def fmt(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


def _dec(value: Any) -> Decimal | None:
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def _clamp(value: Decimal | None) -> Decimal | None:
    return None if value is None else min(max(value, ZERO), ONE)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


class ClientSink:
    """One connected client's outgoing queue. Patches are bounded (oldest dropped); snapshots are kept."""

    def __init__(
        self,
        client_id: str,
        *,
        max_patches: int = 64,
        max_frames: int = 512,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client_id = client_id
        self.keys: set[Key] = set()
        self.dropped = 0
        self.closed = False
        self.overflowed = False
        self.loads: deque[float] = deque()  # when this client last made the server look an event up
        self._max = max_patches
        self._max_frames = max_frames
        self._clock = clock
        self._frames: deque[dict[str, Any]] = deque()
        self._event = asyncio.Event()
        self.last_get = clock()

    def put(self, frame: dict[str, Any]) -> None:
        if self.closed:
            return
        if frame["type"] in ("snapshot", "status") and "venue" in frame:
            # a snapshot supersedes older queued patches of that event; a status only supersedes an older status,
            # so a stale/live flap does not pile up frames for a slow reader
            gone = {"patch"} if frame["type"] == "snapshot" else {"status"}
            before = len(self._frames)
            self._frames = deque(
                f
                for f in self._frames
                if not (f["type"] in gone and (f.get("venue"), f.get("id")) == (frame["venue"], frame["id"]))
            )
            self.dropped += before - len(self._frames)
        elif frame["type"] == "patch":
            while sum(1 for f in self._frames if f["type"] == "patch") >= self._max:
                for i, f in enumerate(self._frames):
                    if f["type"] == "patch":
                        del self._frames[i]
                        self.dropped += 1
                        break
        self._frames.append(frame)
        if len(self._frames) > self._max_frames:  # snapshots/errors are not coalesced: cap them too
            self.dropped += len(self._frames)
            self._frames.clear()
            self.overflowed = True
            self.closed = True
        self._event.set()

    async def get(self) -> dict[str, Any] | None:
        """The next frame; None once the sink is closed and drained."""

        while True:
            if self._frames:
                self.last_get = self._clock()
                return self._frames.popleft()
            if self.closed:
                return None
            self._event.clear()
            await self._event.wait()

    def pending(self) -> int:
        return len(self._frames)

    def close(self) -> None:
        self.closed = True
        self._event.set()


@dataclass
class _State:
    key: Key
    ident: str
    view: dict[str, Any]
    rows: dict[str, dict[str, Any]]
    tokens: dict[str, str]  # YES token -> market id
    sinks: set[ClientSink] = field(default_factory=set)
    seq: int = 0
    pending: dict[str, Any] = field(default_factory=dict)
    last_flush: float = 0.0
    last_rest: float = 0.0
    stale: bool = False
    source: str = "rest"
    source_ts: str | None = None
    price_ts: str | None = None
    uses_feed: bool = False
    awaiting_book: bool = False
    spot_symbol: str | None = None
    series: dict[str, dict[str, list[list[Any]]]] = field(default_factory=dict)
    last_tail: dict[str, float] = field(default_factory=dict)


class LiveHub:
    def __init__(
        self,
        *,
        load_view: LoadView,
        history: HistoryFn | None = None,
        venue_history: dict[str, HistoryFn] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        flush_interval: float = 0.25,
        max_subs_per_client: int = 12,
        max_events: int = 200,
        client_idle_seconds: float = 120.0,
        stale_after: float = 45.0,
        rest_interval: float = 2.0,
        poll_interval: float = 5.0,
        rest_per_tick: int = 10,
        max_patches: int = 64,
        max_loads_per_minute: int = 30,
    ) -> None:
        self._load_view = load_view
        self._history = history
        self._venue_history = venue_history or {}
        self._clock = clock
        self._wall = wall
        self.flush_interval = flush_interval
        self.max_subs = max_subs_per_client
        self.max_events = max_events
        self.client_idle = client_idle_seconds
        self.stale_after = stale_after
        self.rest_interval = rest_interval
        self.poll_interval = poll_interval
        self.rest_per_tick = rest_per_tick
        self.max_patches = max_patches
        self.max_loads = max_loads_per_minute
        self.market: Any = None  # PolymarketMarketFeed
        self.sports: Any = None  # PolymarketSportsFeed
        self.crypto: Any = None  # CryptoPrices
        self.kalshi: Any = None  # KalshiMarketFeed
        self.limitless: Any = None  # LimitlessMarketFeed
        self.unavailable: dict[str, str] = {}  # feed name -> why it is not running (shown in /status)
        self._feed_up: dict[str, bool] = {}
        self._states: dict[Key, _State] = {}
        self._alias: dict[Key, Key] = {}
        self._asset_index: dict[Key, set[Key]] = {}  # (venue, asset) -> the events that follow it
        self._clients: set[ClientSink] = set()
        self._rest_block: dict[str, tuple[float, int]] = {}  # venue -> (no REST refresh before, failures in a row)
        self._locks: dict[Key, tuple[asyncio.Lock, int]] = {}  # lock + number of tasks using it
        self._tasks: list[asyncio.Task[None]] = []
        self.dropped_total = 0

    # ------------------------------------------------------------------ wiring
    def attach(
        self,
        *,
        market: Any = None,
        sports: Any = None,
        crypto: Any = None,
        kalshi: Any = None,
        limitless: Any = None,
        unavailable: dict[str, str] | None = None,
    ) -> None:
        self.market, self.sports, self.crypto = market, sports, crypto
        self.kalshi, self.limitless = kalshi, limitless
        self.unavailable = dict(unavailable or {})
        self._feed_up = {f.name: bool(f.stats.connected) for f in self._market_feeds()}

    def _market_feeds(self) -> list[Any]:
        """The feeds that carry prices (the sports feed carries scores and is not tied to any one venue)."""

        return [f for f in (self.market, self.kalshi, self.limitless) if f is not None]

    def _feed_for(self, venue: str) -> Any:
        return {"polymarket": self.market, "kalshi": self.kalshi, "limitless": self.limitless}.get(venue)

    def _up(self, venue: str) -> bool:
        feed = self._feed_for(venue)
        return bool(feed is not None and self._feed_up.get(feed.name, False))

    def start(self) -> None:
        if self._tasks and not any(t.done() for t in self._tasks):
            return
        self._tasks = []
        self._tasks = [
            asyncio.create_task(self._loop(self.flush, self.flush_interval), name="live-flush"),
            asyncio.create_task(self._loop(self.housekeeping, 1.0), name="live-housekeeping"),
        ]
        for feed in (*self._market_feeds(), self.sports):
            if feed is not None:
                feed.start()

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for feed in (*self._market_feeds(), self.sports):
            if feed is not None:
                await feed.stop()
        for sink in list(self._clients):
            self.disconnect(sink)

    async def _loop(self, step: Callable[[], Any], every: float) -> None:
        while True:
            await asyncio.sleep(every)
            try:
                result = step()
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("live_hub_step_failed", error=type(exc).__name__)

    # ------------------------------------------------------------------ clients
    def connect(self, client_id: str = "") -> ClientSink:
        sink = ClientSink(client_id, max_patches=self.max_patches, clock=self._clock)
        self._clients.add(sink)
        return sink

    def disconnect(self, sink: ClientSink) -> None:
        for key in list(sink.keys):
            self._leave(sink, key)
        self.dropped_total += sink.dropped
        sink.dropped = 0
        self._clients.discard(sink)
        sink.close()

    async def handle(self, sink: ClientSink, msg: dict[str, Any]) -> None:
        """One client frame: ``{"op": "subscribe"|"unsubscribe"|"snapshot", "topic": "event", ...}``."""

        op = str(msg.get("op") or "")
        venue, ident = str(msg.get("venue") or ""), str(msg.get("id") or "")
        rng = str(msg.get("range") or "1D").upper()
        if rng not in RANGE_INTERVAL:
            rng = "1D"
        if op not in {"subscribe", "unsubscribe", "snapshot"}:
            return self._error(sink, "bad_op", "op must be subscribe, unsubscribe or snapshot", venue, ident)
        if msg.get("topic", "event") != "event":
            return self._error(sink, "bad_topic", "only the event topic exists", venue, ident)
        if venue not in VENUES or not _IDENT.match(ident):
            return self._error(sink, "bad_request", "venue or id is not valid", venue, ident)
        key = self._alias.get((venue, ident), (venue, ident))
        if op == "unsubscribe":
            self._leave(sink, key)
            return None
        if op == "snapshot":
            state = self._states.get(key)
            if state is None or sink not in state.sinks:
                return self._error(sink, "not_subscribed", "subscribe first", venue, ident)
            await self._send_snapshot(sink, state, rng, ident)
            return None
        if key in sink.keys and key in self._states:
            await self._send_snapshot(sink, self._states[key], rng, ident)
            return None
        if len(sink.keys) >= self.max_subs:
            return self._error(sink, "limit", f"at most {self.max_subs} subscriptions per connection", venue, ident)
        if key not in self._states:  # a lookup costs the venue a REST call: budget it per client
            now = self._clock()
            while sink.loads and now - sink.loads[0] > 60.0:
                sink.loads.popleft()
            if len(sink.loads) >= self.max_loads:
                return self._error(sink, "rate_limited", "too many event lookups, slow down", venue, ident)
            sink.loads.append(now)
        try:
            state = await self._ensure_state(venue, ident)
        except _Limit:
            return self._error(sink, "limit", "the server is following too many events", venue, ident)
        except Exception as exc:
            return self._error(sink, "load_failed", f"event lookup failed: {type(exc).__name__}", venue, ident)
        if state is None:
            return self._error(sink, "not_found", "event not found", venue, ident)
        state.sinks.add(sink)
        sink.keys.add(state.key)
        await self._send_snapshot(sink, state, rng, ident)
        return None

    def _error(self, sink: ClientSink, code: str, detail: str, venue: str, ident: str) -> None:
        sink.put(
            {"type": "error", "code": code, "detail": detail, "venue": venue, "id": ident, "server_ts": self._now()}
        )

    def _now(self) -> str:
        return _iso(self._wall())

    # ------------------------------------------------------------------ event state
    async def _ensure_state(self, venue: str, ident: str) -> _State | None:
        key = self._alias.get((venue, ident), (venue, ident))
        if key in self._states:
            return self._states[key]
        lock, users = self._locks.get((venue, ident), (asyncio.Lock(), 0))
        self._locks[(venue, ident)] = (lock, users + 1)
        try:
            async with lock:
                key = self._alias.get((venue, ident), (venue, ident))
                if key in self._states:
                    return self._states[key]
                view = await self._load_view(venue, ident)
                if not view:
                    return None
                canonical: Key = (venue, str(view.get("event_id") or ident))
                self._alias[(venue, ident)] = canonical
                if canonical in self._states:
                    return self._states[canonical]
                if len(self._states) >= self.max_events:
                    raise _Limit
                state = self._new_state(canonical, ident, view)
                self._states[canonical] = state
                self._watch(state)
                return state
        finally:  # the lock table must not grow with every id a client invents
            lock, users = self._locks[(venue, ident)]
            if users <= 1:
                del self._locks[(venue, ident)]
            else:
                self._locks[(venue, ident)] = (lock, users - 1)

    def _new_state(self, key: Key, ident: str, view: dict[str, Any]) -> _State:
        rows: dict[str, dict[str, Any]] = {}
        tokens: dict[str, str] = {}
        for section in view.get("sections") or []:
            for row in section.get("rows") or []:
                mid = str(row.get("market_id") or "")
                if mid:
                    rows[mid] = row
                ids = row.get("token_ids") or []
                if key[0] == "polymarket" and mid and ids:
                    tokens[str(ids[0])] = mid  # YES token; NO is its complement
                elif key[0] != "polymarket" and mid and row.get("status") == "open":
                    tokens[mid] = mid  # Kalshi tickers and Limitless slugs are their own asset ids
        crypto = (view.get("raw_extra") or {}).get("crypto")
        coin = crypto.get("coin") if isinstance(crypto, dict) else None
        state = _State(
            key=key,
            ident=ident,
            view=view,
            rows=rows,
            tokens=tokens,
            uses_feed=bool(tokens and self._feed_for(key[0]) is not None),
            spot_symbol=str(coin).upper() if coin and self.crypto is not None else None,
            price_ts=self._now(),
            last_rest=self._clock(),
        )
        up = self._up(key[0])
        state.source = "ws" if state.uses_feed and up else "rest"
        state.awaiting_book = state.uses_feed
        state.stale = state.uses_feed and not up
        self._link_game(state)
        return state

    def _watch(self, state: _State) -> None:
        fresh: list[str] = []
        venue = state.key[0]
        for token in state.tokens:
            owners = self._asset_index.setdefault((venue, token), set())
            if not owners:
                fresh.append(token)
            owners.add(state.key)
        feed = self._feed_for(venue)
        if fresh and feed is not None:
            feed.watch(fresh)

    def _unwatch(self, state: _State) -> None:
        gone: list[str] = []
        venue = state.key[0]
        for token in state.tokens:
            owners = self._asset_index.get((venue, token))
            if owners is None:
                continue
            owners.discard(state.key)
            if not owners:
                del self._asset_index[(venue, token)]
                gone.append(token)
        feed = self._feed_for(venue)
        if gone and feed is not None:
            feed.unwatch(gone)

    def _leave(self, sink: ClientSink, key: Key) -> None:
        sink.keys.discard(key)
        state = self._states.get(key)
        if state is None:
            return
        state.sinks.discard(sink)
        if not state.sinks:  # the last client left: stop following upstream
            self._unwatch(state)
            del self._states[key]
            for alias, target in list(self._alias.items()):
                if target == key:
                    del self._alias[alias]

    # ------------------------------------------------------------------ snapshots and charts
    async def _send_snapshot(self, sink: ClientSink, state: _State, rng: str, requested: str) -> None:
        await self._ensure_history(state, rng)
        sink.put(self._snapshot(state, rng, requested))

    def _snapshot(self, state: _State, rng: str, requested: str) -> dict[str, Any]:
        view = copy.deepcopy(state.view)
        view["freshness"] = self._freshness(state)
        chart = view.get("chart")
        if chart:
            series = state.series.get(rng)
            chart["history"] = "ok" if series is not None else "unavailable"
            chart["range"] = rng
            for line in chart.get("series") or []:
                line["points"] = list((series or {}).get(line["row_id"], []))
        return {
            "type": "snapshot",
            "topic": "event",
            "venue": state.key[0],
            "id": state.key[1],
            "requested": requested,
            "seq": state.seq,
            "source": state.source,
            "source_ts": state.source_ts,
            "server_ts": self._now(),
            "range": rng,
            "view": view,
        }

    def _freshness(self, state: _State) -> dict[str, Any]:
        return {"price_ts": state.price_ts, "source": state.source, "stale": state.stale}

    async def _ensure_history(self, state: _State, rng: str) -> None:
        chart = state.view.get("chart")
        venue = state.key[0]
        fetch = self._history if venue == "polymarket" else self._venue_history.get(venue)
        if rng in state.series or not chart or fetch is None:
            return
        by_token = {mid: tok for tok, mid in state.tokens.items()}
        if venue != "polymarket":  # Kalshi and Limitless charts follow every row, open or not
            by_token = {
                str(r.get("market_id")): str(r.get("market_id")) for r in state.rows.values() if r.get("market_id")
            }
        wanted = {
            line["row_id"]: by_token[line["row_id"]] for line in chart.get("series") or [] if line["row_id"] in by_token
        }
        if not wanted:
            return
        try:
            got = await fetch(list(wanted.values()), rng)
        except Exception as exc:
            logger.info("live_history_failed", error=type(exc).__name__)
            return
        if got:
            state.series[rng] = {row_id: list(got.get(token, [])) for row_id, token in wanted.items()}

    def _tail(self, state: _State, mid: str, chance: str) -> None:
        if not state.series:
            return
        now = self._wall()
        if now - state.last_tail.get(mid, 0.0) < 1.0:
            return
        state.last_tail[mid] = now
        point = [int(now), chance]
        added = False
        for series in state.series.values():
            if mid in series:
                series[mid].append(point)
                del series[mid][:-TAIL_MAX]
                added = True
        if added:
            state.pending.setdefault("chart", {}).setdefault(mid, []).append(point)

    # ------------------------------------------------------------------ feed callbacks
    def on_feed_state(self, name: str, connected: bool) -> None:
        venue = FEED_VENUE.get(name)
        if venue is None:
            return
        self._feed_up[name] = connected
        for state in self._states.values():
            if not state.uses_feed or state.key[0] != venue:
                continue
            if connected:
                state.awaiting_book = True  # the server resends every book; the first one proves it is live
            else:
                self._set_stale(state, True)

    def on_quote(self, quote: Quote, venue: str = "polymarket") -> None:
        for key in self._asset_index.get((venue, quote.asset), ()):
            state = self._states.get(key)
            if state is None:
                continue
            mid = state.tokens.get(quote.asset)
            row = state.rows.get(mid) if mid else None
            if mid is None or row is None:
                continue
            changed = self._apply_quote(state, row, mid, quote)
            state.source_ts = quote.source_ts or state.source_ts
            if state.stale or state.awaiting_book:
                state.awaiting_book = False
                self._recover(state)
            elif changed:
                state.price_ts = self._now()

    def _apply_quote(self, state: _State, row: dict[str, Any], mid: str, q: Quote) -> bool:
        bid, ask = _clamp(q.bid), _clamp(q.ask)
        old_chance = _dec(row.get("chance"))
        quoted = _clamp(q.quoted) if bid is None and ask is None else None
        if quoted is not None:  # one quoted price and no book: the chance and both buttons show it
            chance: Decimal | None = quoted
        elif bid is not None and ask is not None and ask >= bid:
            chance = (bid + ask) / 2
        elif q.last is not None:
            chance = _clamp(q.last)
        else:
            chance = old_chance
        patch: dict[str, Any] = {}
        if chance is not None and chance != old_chance:
            patch["chance"] = fmt(chance)
        if quoted is not None:
            yes = {"ask": fmt(quoted)}
            no = {"ask": fmt(ONE - quoted)}
        else:
            yes = {"ask": fmt(ask), "bid": fmt(bid)}
            no = {
                "ask": fmt(ONE - bid) if bid is not None else None,
                "bid": fmt(ONE - ask) if ask is not None else None,
            }
        for side, new in (("yes", yes), ("no", no)):
            current = row.get(side) or {}
            diff = {k: v for k, v in new.items() if _dec(current.get(k)) != _dec(v)}
            if diff:
                patch[side] = diff
        if not patch:
            return False
        row["chance"] = patch.get("chance", row.get("chance"))
        for side in ("yes", "no"):
            if side in patch:
                row.setdefault(side, {}).update(patch[side])
        state.pending.setdefault("rows", {}).setdefault(mid, {}).update(
            {
                k: ({**state.pending["rows"].get(mid, {}).get(k, {}), **v} if isinstance(v, dict) else v)
                for k, v in patch.items()
            }
        )
        if "chance" in patch and patch["chance"] is not None:
            self._tail(state, mid, patch["chance"])
        return True

    def on_resolved(self, asset_ids: list[str], winner: str | None) -> None:
        ids = set(asset_ids)
        for key in {k for a in ids for k in self._asset_index.get(("polymarket", a), ())}:
            state = self._states.get(key)
            if state is None:
                continue
            for token, mid in state.tokens.items():
                if token not in ids and (winner is None or token != winner):
                    continue
                row = state.rows.get(mid)
                if row is None:
                    continue
                row["status"] = "resolved"
                patch: dict[str, Any] = {"status": "resolved"}
                if winner is not None:
                    row["chance"] = patch["chance"] = "1" if winner == token else "0"
                state.pending.setdefault("rows", {}).setdefault(mid, {}).update(patch)
            self._close_if_done(state)

    def on_settled(self, venue: str, asset: str, yes_won: bool | None) -> None:
        """Kalshi or Limitless says one market is settled (``yes_won`` None: settled, result unknown)."""

        for key in tuple(self._asset_index.get((venue, asset), ())):
            state = self._states.get(key)
            mid = state.tokens.get(asset) if state else None
            row = state.rows.get(mid) if state and mid else None
            if state is None or mid is None or row is None:
                continue
            row["status"] = "resolved"
            patch: dict[str, Any] = {"status": "resolved"}
            if yes_won is not None:
                row["chance"] = patch["chance"] = "1" if yes_won else "0"
            state.pending.setdefault("rows", {}).setdefault(mid, {}).update(patch)
            self._close_if_done(state)

    @staticmethod
    def _close_if_done(state: _State) -> None:
        if state.rows and all(r.get("status") != "open" for r in state.rows.values()):
            state.view["status"] = "resolved"
            state.pending["status"] = "resolved"

    # ------------------------------------------------------------------ sports
    def _link_game(self, state: _State) -> None:
        game = state.view.get("game")
        if not game or self.sports is None:
            return
        found = self.sports.find(
            game.get("game_id"), (game.get("home") or {}).get("name", ""), (game.get("away") or {}).get("name", "")
        )
        if found is not None:
            self._apply_game(state, found, queue=False)

    def on_game(self, gs: GameState) -> None:
        for state in self._states.values():
            game = state.view.get("game")
            if not game:
                continue
            same_id = game.get("game_id") not in (None, "") and str(game["game_id"]) == gs.game_id
            names = {
                norm_name((game.get("home") or {}).get("name", "")),
                norm_name((game.get("away") or {}).get("name", "")),
            }
            if same_id or names == {norm_name(gs.home), norm_name(gs.away)}:
                self._apply_game(state, gs, queue=True)

    def _apply_game(self, state: _State, gs: GameState, *, queue: bool) -> None:
        game = state.view["game"]
        home, away = game["home"], game["away"]
        ws_home = norm_name(gs.home)
        swapped = (
            bool(ws_home) and ws_home == norm_name(away.get("name", "")) and ws_home != norm_name(home.get("name", ""))
        )
        # The feed's "<home>-<away>" score follows the feed's own team names. If the view lists the teams the
        # other way round, the feed's home score belongs to the view's away team (and the patch says so).
        patch: dict[str, Any] = {}
        pairs = (
            (("away", away, gs.home_score), ("home", home, gs.away_score))
            if swapped
            else (("home", home, gs.home_score), ("away", away, gs.away_score))
        )
        for label, team, score in pairs:
            if score is not None and team.get("score") != score:
                team["score"] = score
                patch.setdefault(label, {})["score"] = score
        if gs.home_score is None and gs.score_raw and game.get("score_raw") != gs.score_raw:
            game["score_raw"] = patch["score_raw"] = gs.score_raw
        for field_name, value in (
            ("period", gs.period_text or None),
            ("elapsed", gs.elapsed or None),
            ("live", gs.live),
            ("ended", gs.ended),
        ):
            if game.get(field_name) != value:
                game[field_name] = patch[field_name] = value
        if game.get("status_text") != gs.status:
            game["status_text"] = patch["status_text"] = gs.status
        if game.get("orientation") != "sports_ws":
            game["orientation"] = patch["orientation"] = "sports_ws"
        game["source"] = patch["source"] = "polymarket_sports"
        game["source_ts"] = patch["source_ts"] = gs.source_ts or self._now()
        status = "ended" if gs.ended else ("live" if gs.live else None)
        if status and state.view.get("status") != status:
            state.view["status"] = status
            state.pending["status"] = status
        if queue and patch:
            merged = state.pending.setdefault("game", {})
            for k, v in patch.items():
                if isinstance(v, dict):
                    merged.setdefault(k, {}).update(v)
                else:
                    merged[k] = v

    # ------------------------------------------------------------------ freshness
    def _set_stale(self, state: _State, stale: bool) -> None:
        if state.stale == stale:
            return
        state.stale = stale
        state.source = "rest" if stale or not state.uses_feed else "ws"
        state.pending["freshness"] = self._freshness(state)
        self._broadcast(state, {"type": "status", "stale": stale, "source": state.source})

    def _recover(self, state: _State) -> None:
        """The feed is delivering again: send everyone a fresh snapshot (the view already holds the latest)."""

        was_stale = state.stale
        state.stale = False
        state.source = "ws"
        state.price_ts = self._now()
        if not was_stale:
            return
        state.pending.clear()
        state.seq += 1
        self._broadcast(state, {"type": "status", "stale": False, "source": "ws"})
        for sink in state.sinks:
            sink.put(self._snapshot(state, str(state.view.get("chart", {}).get("range") or "1D"), state.ident))

    def _broadcast(self, state: _State, extra: dict[str, Any]) -> None:
        frame = {"venue": state.key[0], "id": state.key[1], "server_ts": self._now(), **extra}
        for sink in state.sinks:
            sink.put(dict(frame))

    # ------------------------------------------------------------------ periodic work
    def flush(self) -> int:
        """Send each event's merged pending patch; at most one per ``flush_interval`` per event."""

        sent = 0
        now = self._clock()
        for state in list(self._states.values()):
            if not state.pending or now - state.last_flush < self.flush_interval * 0.9:
                continue
            patch, state.pending = state.pending, {}
            state.last_flush = now
            state.seq += 1
            frame = {
                "type": "patch",
                "topic": "event",
                "venue": state.key[0],
                "id": state.key[1],
                "seq": state.seq,
                "source": state.source,
                "source_ts": state.source_ts,
                "server_ts": self._now(),
                "patch": {**patch, "freshness": self._freshness(state)},
            }
            for sink in state.sinks:
                sink.put(copy.deepcopy(frame))
            sent += 1
        return sent

    async def housekeeping(self) -> None:
        now = self._clock()
        self._check_stale()
        self.sweep(now)
        await self._poll_rest(now)
        await self._poll_spot()

    def _check_stale(self) -> None:
        silent: dict[str, bool] = {}
        for feed in self._market_feeds():
            age = feed.stats.age() or 0.0
            silent[FEED_VENUE.get(feed.name, "")] = bool(feed.stats.connected and age > self.stale_after)
        for state in self._states.values():
            venue = state.key[0]
            if state.uses_feed and (not self._up(venue) or silent.get(venue, False)):
                self._set_stale(state, True)

    def sweep(self, now: float) -> None:
        """Close clients that stopped reading: frames waiting, nothing taken for ``client_idle`` seconds."""

        for sink in list(self._clients):
            if sink.pending() and now - sink.last_get > self.client_idle:
                self.disconnect(sink)

    async def _poll_rest(self, now: float) -> None:
        due: list[_State] = []
        for state in self._states.values():
            every = self.rest_interval if state.stale else self.poll_interval
            if self._rest_block.get(state.key[0], (0.0, 0))[0] > now:
                continue  # the venue said slow down (429 / errors): leave it alone until the pause is over
            if (state.stale or not state.uses_feed) and now - state.last_rest >= every:
                due.append(state)
        due.sort(key=lambda s: s.last_rest)
        batch = due[: self.rest_per_tick]
        for state in batch:
            state.last_rest = now
        await asyncio.gather(*(self._refresh(s) for s in batch), return_exceptions=True)

    async def _refresh(self, state: _State) -> None:
        try:
            view = await self._load_view(state.key[0], state.ident)
        except Exception as exc:
            venue = state.key[0]
            failures = self._rest_block.get(venue, (0.0, 0))[1] + 1
            pause = _retry_after(exc) or min(30.0, 2.0 * 2 ** min(failures, 5))
            self._rest_block[venue] = (self._clock() + pause, failures)
            logger.info("live_rest_refresh_failed", venue=venue, error=type(exc).__name__, pause_s=round(pause, 1))
            return
        self._rest_block.pop(state.key[0], None)
        if not view or state.key not in self._states:
            return
        for section in view.get("sections") or []:
            for new in section.get("rows") or []:
                row = state.rows.get(str(new.get("market_id") or ""))
                if row is not None:
                    self._merge_rest_row(state, row, new)
        state.price_ts = self._now()
        state.source = "rest"
        if "freshness" not in state.pending:
            state.pending["freshness"] = self._freshness(state)

    def _merge_rest_row(self, state: _State, row: dict[str, Any], new: dict[str, Any]) -> None:
        mid = str(row.get("market_id"))
        patch: dict[str, Any] = {}
        if _dec(row.get("chance")) != _dec(new.get("chance")):
            patch["chance"] = new.get("chance")
        for side in ("yes", "no"):
            diff = {
                k: new.get(side, {}).get(k)
                for k in ("ask", "bid")
                if _dec((row.get(side) or {}).get(k)) != _dec((new.get(side) or {}).get(k))
            }
            if diff:
                patch[side] = diff
        if row.get("status") != new.get("status"):
            patch["status"] = new.get("status")
        if not patch:
            return
        for k, v in patch.items():
            if isinstance(v, dict):
                row.setdefault(k, {}).update(v)
            else:
                row[k] = v
        merged = state.pending.setdefault("rows", {}).setdefault(mid, {})
        for k, v in patch.items():
            if isinstance(v, dict):
                merged.setdefault(k, {}).update(v)
            else:
                merged[k] = v
        if patch.get("chance") is not None:
            self._tail(state, mid, str(patch["chance"]))

    async def _poll_spot(self) -> None:
        if self.crypto is None:
            return
        for state in list(self._states.values()):
            if not state.spot_symbol:
                continue
            spot = await self.crypto.latest(state.spot_symbol)
            if spot is None:
                continue
            if state.view.get("spot", {}).get("price") != spot["price"]:
                state.view["spot"] = spot
                state.pending["spot"] = spot

    # ------------------------------------------------------------------ metrics
    def metrics(self) -> dict[str, Any]:
        feeds = {f.name: f.stats.as_dict() for f in (*self._market_feeds(), self.sports) if f is not None}
        for name, reason in self.unavailable.items():
            feeds[name] = {"connected": False, "available": False, "reason": reason}
        dropped = self.dropped_total + sum(s.dropped for s in self._clients)
        return {
            "clients": len(self._clients),
            "events": len(self._states),
            "dropped": dropped,
            "stale_events": sum(1 for s in self._states.values() if s.stale),
            "feeds": feeds,
        }


class _Limit(Exception):
    pass


def _retry_after(exc: BaseException) -> float | None:
    """Seconds the venue asked us to wait (``Retry-After`` on a 429/503), capped at two minutes; None if unknown."""

    raw: Any = getattr(exc, "retry_after", None)
    if raw is None:
        headers = getattr(getattr(exc, "response", None), "headers", None)
        raw = headers.get("Retry-After") if headers is not None else None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return min(120.0, seconds) if seconds > 0 else None
