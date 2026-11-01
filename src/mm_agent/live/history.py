"""Initial chart history for a view: ``POST /batch-prices-history`` (at most 20 tokens per call).

Response (docs): ``{"history": {"<token>": [{"t": <epoch s>, "p": <price>}]}}``. The live tail is grown by the hub
from midpoint ticks afterwards. Interval names for the longer ranges (``1m`` = a month, ``all``) follow the docs
and are unverified on the live endpoint.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from ..util import get_logger

logger = get_logger(__name__)

CLOB_BATCH_HISTORY = "https://clob.polymarket.com/batch-prices-history"
BATCH_SIZE = 20
RANGE_INTERVAL = {"1H": "1h", "6H": "6h", "1D": "1d", "1W": "1w", "1M": "1m", "ALL": "all"}
PostJson = Callable[..., Awaitable[Any]]


def interval_for(rng: str) -> str:
    return RANGE_INTERVAL.get((rng or "").upper(), RANGE_INTERVAL["1D"])


async def fetch_history(
    post_json: PostJson, tokens: list[str], rng: str, *, url: str = CLOB_BATCH_HISTORY
) -> dict[str, list[list[Any]]]:
    """``{token: [[epoch_seconds, "price"], ...]}`` oldest first. A failing chunk is skipped, not fatal."""

    out: dict[str, list[list[Any]]] = {}
    interval = interval_for(rng)
    for i in range(0, len(tokens), BATCH_SIZE):
        chunk = tokens[i : i + BATCH_SIZE]
        try:
            data = await post_json(url, json={"markets": chunk, "interval": interval})
        except Exception as exc:
            logger.info("history_chunk_failed", error=type(exc).__name__)
            continue
        history = data.get("history") if isinstance(data, dict) else None
        for token, rows in (history or {}).items() if isinstance(history, dict) else []:
            points: list[list[Any]] = []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                try:
                    t, p = int(row["t"]), Decimal(str(row["p"]))
                except (KeyError, TypeError, ValueError, InvalidOperation):
                    continue
                points.append([t, str(p)])
            points.sort(key=lambda r: r[0])
            out[str(token)] = points
    return out
