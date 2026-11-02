"""Initial chart history for a view: ``POST /batch-prices-history`` (at most 20 tokens per call).

Response (docs): ``{"history": {"<token>": [{"t": <epoch s>, "p": <price>}]}}``. The live tail is grown by the hub
from midpoint ticks afterwards. Interval names for the longer ranges (``1m`` = a month, ``all``) follow the docs
and are unverified on the live endpoint.
"""

from __future__ import annotations

import re
import time
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


# ---------------------------------------------------------------------------------- Kalshi and Limitless
# Both venues give a YES series only. Shapes below follow the docs and are NOT verified live: a value that is
# not a probability from 0 to 1 is skipped, never rescaled, and a series with no usable point is simply absent
# (the view then says "history not available").
GetJson = Callable[..., Awaitable[Any]]
RANGE_SECONDS = {"1H": 3600, "6H": 21600, "1D": 86400, "1W": 7 * 86400, "1M": 30 * 86400, "ALL": 365 * 86400}
KALSHI_PERIOD_MINUTES = {"1H": 1, "6H": 1, "1D": 1, "1W": 60, "1M": 60, "ALL": 1440}
KALSHI_BATCH = 100
_SLUG_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_ZERO, _ONE = Decimal(0), Decimal(1)


def _prob(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() and _ZERO <= out <= _ONE else None


def _candle_price(candle: dict[str, Any], group: str) -> Decimal | None:
    """``candle[group].close_dollars``; a bare ``close`` counts only as a decimal, never as whole cents."""

    inner = candle.get(group)
    if not isinstance(inner, dict):
        return None
    if inner.get("close_dollars") is not None:
        return _prob(inner["close_dollars"])
    raw = inner.get("close")
    return _prob(raw) if isinstance(raw, (str, float)) else None


def kalshi_points(candles: Any) -> list[list[Any]]:
    """Midpoint of the closing YES bid and ask (the chart rule); the closing trade price when a side is missing."""

    points: list[list[Any]] = []
    for candle in candles if isinstance(candles, list) else []:
        if not isinstance(candle, dict):
            continue
        t = candle.get("end_period_ts", candle.get("timestamp"))
        if not isinstance(t, int) or isinstance(t, bool):
            continue
        bid, ask = _candle_price(candle, "yes_bid"), _candle_price(candle, "yes_ask")
        price = (
            (bid + ask) / 2 if bid is not None and ask is not None and bid <= ask else _candle_price(candle, "price")
        )
        if price is not None:
            points.append([t, str(price.normalize())])
    points.sort(key=lambda r: r[0])
    return points


async def fetch_kalshi_history(
    get_json: GetJson, base_url: str, tickers: list[str], rng: str, *, now: Callable[[], float] = time.time
) -> dict[str, list[list[Any]]]:
    """``GET /markets/candlesticks`` for up to 100 tickers per call -> ``{ticker: [[epoch_seconds, "price"], ...]}``."""

    rng = rng.upper() if rng.upper() in RANGE_SECONDS else "1D"
    end = int(now())
    out: dict[str, list[list[Any]]] = {}
    for i in range(0, len(tickers), KALSHI_BATCH):
        chunk = [t for t in tickers[i : i + KALSHI_BATCH] if _SLUG_OK.match(t)]
        if not chunk:
            continue
        try:
            data = await get_json(
                base_url + "/markets/candlesticks",
                params={
                    "market_tickers": ",".join(chunk),
                    "start_ts": end - RANGE_SECONDS[rng],
                    "end_ts": end,
                    "period_interval": KALSHI_PERIOD_MINUTES[rng],
                },
            )
        except Exception as exc:
            logger.info("kalshi_history_failed", error=type(exc).__name__)
            continue
        markets = data.get("markets") if isinstance(data, dict) else None
        for market in markets if isinstance(markets, list) else []:
            if isinstance(market, dict) and market.get("market_ticker") in chunk:
                points = kalshi_points(market.get("candlesticks"))
                if points:
                    out[str(market["market_ticker"])] = points
    return out


def limitless_points(data: Any) -> list[list[Any]]:
    """Points from ``historical-price``: a list, or a dict holding one, of ``{timestamp, price}`` or ``[t, p]``."""

    rows = data
    if isinstance(data, dict):
        rows = next((data[k] for k in ("prices", "data", "history", "points") if isinstance(data.get(k), list)), [])
    points: list[list[Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            t = next((row[k] for k in ("timestamp", "t", "time", "createdAt") if row.get(k) is not None), None)
            p = next((row[k] for k in ("price", "p", "yes", "value") if row.get(k) is not None), None)
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            t, p = row[0], row[1]
        else:
            continue
        try:
            seconds = int(Decimal(str(t)))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if seconds > 10**11:  # milliseconds
            seconds //= 1000
        price = _prob(p)
        if price is not None:
            points.append([seconds, str(price.normalize())])
    points.sort(key=lambda r: r[0])  # the venue sends newest first
    return points


async def fetch_limitless_history(
    get_json: GetJson, base_url: str, slugs: list[str], rng: str, *, now: Callable[[], float] = time.time
) -> dict[str, list[list[Any]]]:
    """``GET /markets/{slug}/historical-price``, cut to the range. The interval parameter is unconfirmed, so the
    plain call goes first and ``interval=`` is tried only when it gives nothing."""

    rng = rng.upper() if rng.upper() in RANGE_SECONDS else "1D"
    cutoff = int(now()) - RANGE_SECONDS[rng]
    out: dict[str, list[list[Any]]] = {}
    for slug in slugs:
        if not _SLUG_OK.match(slug):
            continue
        points: list[list[Any]] = []
        for params in (None, {"interval": interval_for(rng)}):
            try:
                points = limitless_points(await get_json(f"{base_url}/markets/{slug}/historical-price", params=params))
            except Exception as exc:
                logger.info("limitless_history_failed", error=type(exc).__name__)
                continue
            if points:
                break
        points = [p for p in points if p[0] >= cutoff]
        if points:
            out[slug] = points
    return out
