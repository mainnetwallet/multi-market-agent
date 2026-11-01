"""Kalshi live sports data: ``GET /live_data/batch`` (at most 100 milestone ids per call), polled every 2-3 s.

The Kalshi docs this plan was written from list no push channel for scores, so this is polling and says so: every game
update is labelled ``kalshi_rest_polled``. The response layout below is from the docs only (``DOCS_FINDINGS.md``
section 9) and has not met a live server, so the parser accepts a few spellings and treats anything it does not
understand as "no data" instead of guessing a score. Public GET only: no signing, no order. Display data only.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from ..util import get_logger
from .common import FeedStats, backoff_delay, retry_after

logger = get_logger(__name__)

SOURCE = "kalshi_rest_polled"
BATCH_MAX = 100  # documented maximum of ids per call
MAX_WATCHED = 500  # never follow more games than this at once
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_LIVE = {"live", "in_progress", "inprogress", "in progress", "active", "playing", "started"}
_ENDED = {"final", "finished", "ended", "complete", "completed", "closed", "over"}

Fetch = Callable[[list[str]], Awaitable[Any]]


@dataclass(frozen=True)
class KalshiGame:
    """One game's current score, as Kalshi's live data reports it. Scores are whole numbers or None, never guessed."""

    milestone_id: str
    home: str
    away: str
    home_score: int | None
    away_score: int | None
    period: str
    live: bool
    ended: bool
    status: str
    source_ts: str | None = None


# ------------------------------------------------------------------------------------------------ parsing
def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("live_datas", "live_data", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
            if isinstance(value, dict):
                return [value]
    return []


def _name(details: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = details.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("display_name")
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    return ""


def _score(details: dict[str, Any], *keys: str) -> int | None:
    """A score is a whole number. A float, a bool, a negative or any other text is not a score."""

    for key in keys:
        value = details.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and 0 <= value < 10_000:
            return value
        if isinstance(value, str) and value.strip().isdigit() and len(value.strip()) <= 4:
            return int(value.strip())
    return None


def _period(details: dict[str, Any]) -> str:
    for key in ("period", "quarter", "half", "inning", "game_period"):
        value = details.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()[:24]
    return ""


def _ts(details: dict[str, Any]) -> str | None:
    for key in ("updated_at", "updated_ts", "last_updated", "timestamp"):
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:40]
    return None


def parse_live_data(payload: Any) -> list[KalshiGame]:
    """The games in a ``/live_data/batch`` reply. Unknown layouts and rows without any usable field give nothing."""

    out: list[KalshiGame] = []
    for row in _rows(payload):
        milestone = row.get("milestone_id") or row.get("id")
        if not isinstance(milestone, str) or not _ID.match(milestone):
            continue
        raw_details = row.get("details")
        details: dict[str, Any] = raw_details if isinstance(raw_details, dict) else row
        home = _name(details, "home_name", "home_team", "home")
        away = _name(details, "away_name", "away_team", "away")
        home_score = _score(details, "home_points", "home_score")
        away_score = _score(details, "away_points", "away_score")
        status = _name(details, "status", "game_status", "state").lower()
        if not (home or away or status) and home_score is None and away_score is None:
            continue
        ended = status in _ENDED
        out.append(
            KalshiGame(
                milestone_id=milestone,
                home=home,
                away=away,
                home_score=home_score,
                away_score=away_score,
                period=_period(details),
                live=status in _LIVE and not ended,
                ended=ended,
                status=status,
                source_ts=_ts(details),
            )
        )
    return out


# ------------------------------------------------------------------------------------------------- polling
class KalshiLiveDataFeed:
    """Polls the milestones somebody is watching, a batch of at most 100 at a time, and reports each game.

    Idle (no request at all) while nothing is watched. A failing call pauses the feed for the venue's ``Retry-After``
    or an exponential backoff, and never raises out of the loop.
    """

    name = "kalshi_live_data"

    def __init__(
        self,
        fetch: Fetch,
        *,
        on_game: Callable[[KalshiGame], None],
        interval: float = 2.5,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.fetch = fetch
        self._on_game = on_game
        self.interval = interval
        self.stats = FeedStats(clock) if clock else FeedStats()
        self._ids: set[str] = set()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # -- what to follow
    def watch(self, ids: Iterable[str]) -> None:
        for milestone in ids:
            if isinstance(milestone, str) and _ID.match(milestone) and len(self._ids) < MAX_WATCHED:
                self._ids.add(milestone)
        self.stats.subs = len(self._ids)
        self._wake.set()

    def unwatch(self, ids: Iterable[str]) -> None:
        self._ids.difference_update(ids)
        self.stats.subs = len(self._ids)

    @property
    def watched(self) -> list[str]:
        return sorted(self._ids)

    # -- polling
    async def poll_once(self) -> int:
        """One cycle over everything watched. Returns how many games were reported. Errors propagate to ``_run``."""

        ids = self.watched
        reported = 0
        for start in range(0, len(ids), BATCH_MAX):
            chunk = ids[start : start + BATCH_MAX]
            payload = await self.fetch(chunk)
            self.stats.connected = True
            for game in parse_live_data(payload):
                if game.milestone_id in self._ids:  # only what was asked for
                    self.stats.message()
                    reported += 1
                    self._on_game(game)
        return reported

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="kalshi-live-data")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.stats.connected = False

    async def _run(self) -> None:
        failures = 0
        while True:
            self._wake.clear()
            if not self._ids:
                self.stats.connected = False
                await self._wake.wait()
                continue
            try:
                await self.poll_once()
                failures = 0
                delay = self.interval
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                if self.stats.connected:
                    self.stats.reconnects += 1
                self.stats.connected = False
                delay = retry_after(exc) or backoff_delay(failures, base=self.interval, cap=30.0)
                logger.info("kalshi_live_data_failed", error=type(exc).__name__, pause_s=round(delay, 1))
            await asyncio.sleep(delay)
