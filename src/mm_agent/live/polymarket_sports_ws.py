"""Polymarket sports channel: ``wss://sports-api.polymarket.com/ws``.

No subscribe frame: the server pushes every game. It sends the text ``ping`` every 5 s and wants ``pong``
back within 10 s. Each message is a bare object ``gameId, leagueAbbreviation, homeTeam, awayTeam, status,
live, ended, score ("<home>-<away>"), period, elapsed``. Polymarket itself warns the data may be delayed or
wrong, so the view shows its source and the hub never lets it decide anything but what is drawn.

Home and away come from the message's own ``homeTeam`` / ``awayTeam`` and the score is read in that order;
the first outcome of a market is never assumed to be the home team.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import connect

from ..util import get_logger
from .common import FeedStats, backoff_delay

logger = get_logger(__name__)

SPORTS_URL = "wss://sports-api.polymarket.com/ws"
_SCORE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")
_END = re.compile(r"^End\s+(\d+)$", re.I)
_FRACTION = re.compile(r"^(\d+)/(\d+)$")
_PERIODS = {
    "1H": "1st half",
    "2H": "2nd half",
    "HT": "Half-time",
    "FT": "Full time",
    "FT OT": "Full time (extra time)",
    "FT NR": "Full time (no result)",
}


def period_label(period: str) -> str:
    """A readable period. Unknown strings are shown as sent, never guessed."""

    raw = (period or "").strip()
    if raw in _PERIODS:
        return _PERIODS[raw]
    if m := _END.match(raw):
        return f"End of period {m.group(1)}"
    if m := _FRACTION.match(raw):
        return f"Period {m.group(1)} of {m.group(2)}"
    return raw


def norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


@dataclass(frozen=True)
class GameState:
    game_id: str
    league: str
    home: str
    away: str
    status: str  # per-sport, case-sensitive, shown as sent
    live: bool
    ended: bool
    score_raw: str
    home_score: int | None
    away_score: int | None
    period: str
    period_text: str
    elapsed: str
    source_ts: str | None


def parse_game(msg: dict[str, Any], *, now_iso: str | None = None) -> GameState | None:
    game_id = msg.get("gameId")
    if game_id in (None, ""):
        return None
    score_raw = str(msg.get("score") or "")
    m = _SCORE.match(score_raw)
    period = str(msg.get("period") or "")
    return GameState(
        game_id=str(game_id),
        league=str(msg.get("leagueAbbreviation") or ""),
        home=str(msg.get("homeTeam") or ""),
        away=str(msg.get("awayTeam") or ""),
        status=str(msg.get("status") or ""),
        live=bool(msg.get("live")),
        ended=bool(msg.get("ended")),
        score_raw=score_raw,
        home_score=int(m.group(1)) if m else None,
        away_score=int(m.group(2)) if m else None,
        period=period,
        period_text=period_label(period),
        elapsed=str(msg.get("elapsed") or ""),
        source_ts=now_iso,
    )


class PolymarketSportsFeed:
    name = "polymarket_sports"

    def __init__(
        self,
        url: str = SPORTS_URL,
        *,
        on_game: Callable[[GameState], None],
        on_state: Callable[[str, bool], None] | None = None,
        silence_limit: float = 20.0,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        max_games: int = 2000,
        connector: Callable[..., Any] = connect,
        clock: Callable[[], float] = time.monotonic,
        rand: Callable[[], float] | None = None,
    ) -> None:
        self.url = url
        self.stats = FeedStats(clock)
        self.games: OrderedDict[str, GameState] = OrderedDict()
        self._on_game = on_game
        self._on_state = on_state
        self._silence = silence_limit
        self._backoff = (backoff_base, backoff_cap)
        self._max_games = max_games
        self._connector = connector
        self._rand = rand
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    def watch(self, _: Any) -> None:  # the channel is a firehose: nothing to subscribe
        return

    unwatch = watch

    def find(self, game_id: Any = None, home: str = "", away: str = "") -> GameState | None:
        """The latest state for a game id, else the newest game whose two team names both match."""

        if game_id not in (None, "") and str(game_id) in self.games:
            return self.games[str(game_id)]
        if home and away:
            h, a = norm_name(home), norm_name(away)
            for state in reversed(self.games.values()):
                names = {norm_name(state.home), norm_name(state.away)}
                if {h, a} == names:
                    return state
        return None

    def is_stopping(self) -> bool:
        return self._stopping

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="polymarket-sports-ws")

    async def stop(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        attempt = 0
        while not self.is_stopping():
            try:
                async with self._connector(self.url, open_timeout=15, ping_interval=None) as ws:
                    self._set_state(True)
                    attempt = 0
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self._silence)
                        except TimeoutError as exc:
                            raise ConnectionError("sports feed went silent") from exc
                        self.stats.message()
                        text = raw.decode() if isinstance(raw, bytes) else raw
                        if text.strip().lower() == "ping":
                            await ws.send("pong")
                            continue
                        self.handle(text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("polymarket_sports_ws_down", error=type(exc).__name__)
            finally:
                self._set_state(False)
            if self.is_stopping():
                return
            self.stats.reconnects += 1
            kw: dict[str, Any] = {"rand": self._rand} if self._rand is not None else {}
            await asyncio.sleep(backoff_delay(attempt, base=self._backoff[0], cap=self._backoff[1], **kw))
            attempt += 1

    def _set_state(self, connected: bool) -> None:
        if self.stats.connected == connected:
            return
        self.stats.connected = connected
        if self._on_state is not None:
            self._on_state(self.name, connected)

    def handle(self, text: str) -> None:
        try:
            data = json.loads(text)
        except ValueError:
            return
        for msg in data if isinstance(data, list) else [data]:
            if not isinstance(msg, dict):
                continue
            state = parse_game(msg)
            if state is None:
                continue
            self.games[state.game_id] = state
            self.games.move_to_end(state.game_id)
            while len(self.games) > self._max_games:
                self.games.popitem(last=False)
            self._on_game(state)
