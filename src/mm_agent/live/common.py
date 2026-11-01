"""Small shared pieces for the live feeds: stats, backoff, the quote type."""

from __future__ import annotations

import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Quote:
    """The top of one token's book plus the last trade. Prices are Decimals, never floats."""

    asset: str
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    source_ts: str | None = None
    quoted: Decimal | None = None  # one quoted price with no book behind it (an AMM market): no bid, no ask


def backoff_delay(
    attempt: int, *, base: float = 1.0, cap: float = 30.0, rand: Callable[[], float] = random.random
) -> float:
    """Exponential backoff with full-range jitter: between half and the whole of ``base * 2**attempt`` (capped)."""

    ceiling = min(cap, base * (2 ** max(attempt, 0)))
    return float(ceiling * (0.5 + 0.5 * rand()))


class FeedStats:
    """The numbers ``/status`` shows for one upstream feed."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.connected = False
        self.reconnects = 0
        self.subs = 0
        self.dropped = 0
        self.total = 0
        self.last_msg: float | None = None
        self._recent: deque[float] = deque(maxlen=2000)

    def message(self) -> None:
        now = self._clock()
        self.last_msg = now
        self.total += 1
        self._recent.append(now)

    def heartbeat(self) -> None:
        """The connection answered a keep-alive: it is alive, but this is not a data message."""

        self.last_msg = self._clock()

    def age(self) -> float | None:
        return None if self.last_msg is None else max(0.0, self._clock() - self.last_msg)

    def per_second(self, window: float = 10.0) -> float:
        now = self._clock()
        return round(sum(1 for t in self._recent if now - t <= window) / window, 2)

    def as_dict(self) -> dict[str, Any]:
        age = self.age()
        return {
            "connected": self.connected,
            "reconnects": self.reconnects,
            "msgs_per_s": self.per_second(),
            "last_msg_age_s": None if age is None else round(age, 1),
            "subs": self.subs,
            "dropped": self.dropped,
        }
