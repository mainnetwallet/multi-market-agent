"""Polymarket public market-data: Data API v2 and the CLOB's public price routes.

Everything here is read-only and needs no credentials. It is the half of the official API
that the trading adapter does not cover: other wallets' positions and trades, holders, open
interest, leaderboards, resolution state, token lookup, deep price history, midpoints/spreads.

Sources (read 2026-11-01): https://data-api.polymarket.com/v2/openapi.json (the full Data API v2
contract), https://docs.polymarket.com/api-reference/data-api/overview and the CLOB OpenAPI
pages for ``/midpoint``, ``/midpoints``, ``/spread``, ``/last-trade-price``, ``/last-trades-prices``.

Rules kept here:

* Input is validated before any request leaves the process, so a bad id never reaches the venue.
* Responses are returned as the venue sent them (Data API v2 ``{"data", "pagination"}`` envelope).
  Amounts stay JSON numbers/strings exactly as sent; this module does no arithmetic on them.
* A 429 carries ``Retry-After``; it is surfaced, never retried silently, so a polling UI cannot
  turn into a request storm.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from typing import Any

import httpx

DATA_API = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_CONDITION = re.compile(r"0x[0-9a-fA-F]{64}")
_CONDITION_LOOSE = re.compile(r"0x[0-9a-fA-F]{62,64}")  # /v2/tokens accepts 62 or 64 hex digits
_TOKEN = re.compile(r"[0-9]{1,100}|0x[0-9a-fA-F]{1,80}")
_DIGITS = re.compile(r"[0-9]{1,12}")
_CURSOR = re.compile(r"[A-Za-z0-9_\-.=+/]{1,4096}")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _\-]{0,49}")

ACTIVITY_TYPES = frozenset(
    {
        "TRADE",
        "SPLIT",
        "MERGE",
        "REDEEM",
        "CONVERSION",
        "MIGRATION",
        "REWARD",
        "YIELD",
        "MAKER_REBATE",
        "TAKER_REBATE",
        "REFERRAL_REWARD",
        "DEPOSIT",
        "WITHDRAWAL",
        "DIRECTIONAL_SPLIT",
        "DIRECTIONAL_MERGE",
        "THRESHOLD_COMPOSE",
        "THRESHOLD_DECOMPOSE",
        "TIP",
    }
)


class DataAPIError(RuntimeError):
    """The venue answered with an error. ``status`` is the HTTP status, ``retry_after`` is in seconds."""

    def __init__(
        self, status: int, message: str, *, code: str = "", retry_after: float | None = None, url: str = ""
    ) -> None:
        self.status = status
        self.message = message
        self.code = code
        self.retry_after = retry_after
        self.url = url
        super().__init__(f"polymarket {status} {code or 'error'} at {url}: {message}")


# ── validators: (name, raw value) -> clean string, or ValueError ────────────────────────────

Validator = Callable[[str, Any], str]


def _text(value: Any) -> str:
    return str(value).strip()


def address(name: str, value: Any) -> str:
    text = _text(value)
    if not _ADDRESS.fullmatch(text):
        raise ValueError(f"{name} must be a 0x wallet address (40 hex digits)")
    return text.lower()


def _csv(name: str, value: Any, pattern: re.Pattern[str], limit: int, what: str) -> str:
    items = [part.strip() for part in _text(value).split(",") if part.strip()]
    if not items:
        raise ValueError(f"{name} is empty")
    unique = list(dict.fromkeys(item.lower() for item in items))
    if len(unique) > limit:
        raise ValueError(f"{name} takes at most {limit} distinct values")
    for item in unique:
        if not pattern.fullmatch(item):
            raise ValueError(f"{name} has a malformed {what}")
    return ",".join(unique)


def conditions(name: str, value: Any) -> str:
    return _csv(name, value, _CONDITION, 20, "condition id (0x + 64 hex digits)")


def event_ids(name: str, value: Any) -> str:
    return _csv(name, value, _DIGITS, 20, "event id (digits)")


def token_ids(name: str, value: Any) -> str:
    return _csv(name, value, _TOKEN, 50, "token id")


def one_token(name: str, value: Any) -> str:
    text = _text(value)
    if not _TOKEN.fullmatch(text):
        raise ValueError(f"{name} must be an outcome token id")
    return text


def hex64(name: str, value: Any) -> str:
    text = _text(value)
    if not _CONDITION.fullmatch(text):
        raise ValueError(f"{name} must be 0x followed by 64 hex digits")
    return text.lower()


def cursor(name: str, value: Any) -> str:
    text = _text(value)
    if not _CURSOR.fullmatch(text):
        raise ValueError(f"{name} is not a valid cursor")
    return text


def word(name: str, value: Any) -> str:
    text = _text(value)
    if not _WORD.fullmatch(text):
        raise ValueError(f"{name} may only hold letters, digits, spaces, '-' and '_' (max 50)")
    return text


def title(name: str, value: Any) -> str:
    text = _text(value)
    if len(text) > 200:
        raise ValueError(f"{name} is at most 200 characters")
    return text


def enum(*allowed: str, fold: str = "upper") -> Validator:
    def check(name: str, value: Any) -> str:
        text = _text(value)
        text = text.upper() if fold == "upper" else text.lower() if fold == "lower" else text
        if text not in allowed:
            raise ValueError(f"{name} must be one of: {', '.join(allowed)}")
        return text

    return check


def integer(low: int, high: int | None = None) -> Validator:
    def check(name: str, value: Any) -> str:
        try:
            number = int(str(value).strip())
        except ValueError:
            raise ValueError(f"{name} must be an integer") from None
        if number < low or (high is not None and number > high):
            bound = f"{low}..{high}" if high is not None else f">= {low}"
            raise ValueError(f"{name} must be {bound}")
        return str(number)

    return check


def number(name: str, value: Any) -> str:
    try:
        parsed = float(str(value).strip())
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{name} must be a finite number >= 0")
    return str(value).strip()


def boolean(name: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = _text(value).lower()
    if text in {"true", "1", "yes"}:
        return "true"
    if text in {"false", "0", "no"}:
        return "false"
    raise ValueError(f"{name} must be true or false")


def activity_types(name: str, value: Any) -> str:
    items = [part.strip().upper() for part in _text(value).split(",") if part.strip()]
    if not items:
        raise ValueError(f"{name} is empty")
    bad = [item for item in items if item not in ACTIVITY_TYPES]
    if bad:
        raise ValueError(f"{name} has an unknown type: {bad[0][:30]}")
    return ",".join(dict.fromkeys(items))


_LIMIT = integer(1, 1000)
_SIDE = enum("BUY", "SELL")
_PERIOD = enum("day", "week", "month", "all", fold="lower")
_EPOCH = integer(0)


class Endpoint:
    """One Data API v2 route: its path, the query parameters it accepts and how each is checked."""

    def __init__(
        self,
        path: str,
        params: Mapping[str, Validator],
        *,
        needs_any: tuple[str, ...] = (),
        needs_one: tuple[str, ...] = (),
    ) -> None:
        self.path = path
        self.params = dict(params)
        self.needs_any = needs_any  # at least one of these must be present
        self.needs_one = needs_one  # exactly one of these must be present


ENDPOINTS: dict[str, Endpoint] = {
    # wallet
    "positions": Endpoint(
        "/v2/positions",
        {
            "user": address,
            "condition": conditions,
            "limit": _LIMIT,
            "cursor": cursor,
            "status": enum("OPEN", "REDEEMABLE", "REDEEMABLE_LOST", "MERGEABLE", "CLOSED"),
            "event_id": event_ids,
            "title": title,
            "filter_type": enum("CASH", "TOKENS"),
            "filter_amount": number,
            "include_archived": boolean,
            "sort_by": enum(
                "CURRENT_VALUE", "PRICE", "TOKENS", "UNREALIZED_PNL", "REALIZED_PNL", "TOTAL_PNL", "TIMESTAMP"
            ),
            "sort_direction": enum("ASC", "DESC"),
            "start": _EPOCH,
            "end": _EPOCH,
        },
        needs_any=("user", "condition"),
    ),
    "value": Endpoint("/v2/value", {"user": address, "condition": conditions}, needs_any=("user",)),
    "approvals": Endpoint("/v2/approvals", {"user": address}, needs_any=("user",)),
    "user-stats": Endpoint("/v2/user-stats", {"user": address}, needs_any=("user",)),
    "user-pnl": Endpoint(
        "/v2/user-pnl",
        {
            "user": address,
            "interval": enum("max", "all", "1m", "1w", "1d", "12h", "6h", fold="lower"),
            "fidelity": enum("1d", "18h", "12h", "3h", "1h", fold="lower"),
        },
        needs_any=("user",),
    ),
    "user-volume": Endpoint("/v2/user-volume", {"user": address, "start": _EPOCH, "end": _EPOCH}, needs_any=("user",)),
    # feeds
    "trades": Endpoint(
        "/v2/trades",
        {
            "user": address,
            "condition": conditions,
            "event_id": event_ids,
            "side": _SIDE,
            "taker_only": boolean,
            "filter_type": enum("CASH", "TOKENS"),
            "filter_amount": number,
            "start": _EPOCH,
            "end": _EPOCH,
            "limit": _LIMIT,
            "cursor": cursor,
        },
    ),
    "activity": Endpoint(
        "/v2/activity",
        {
            "user": address,
            "type": activity_types,
            "condition": conditions,
            "event_id": event_ids,
            "side": _SIDE,
            "start": _EPOCH,
            "end": _EPOCH,
            "sort_by": enum("TIMESTAMP"),
            "sort_direction": enum("ASC", "DESC"),
            "exclude_deposits_withdrawals": boolean,
            "limit": _LIMIT,
            "cursor": cursor,
        },
        needs_any=("user",),
    ),
    # markets
    "holders": Endpoint(
        "/v2/holders",
        {
            "condition": conditions,
            "limit": _LIMIT,
            "cursor": cursor,
            "min_balance": number,
            "include_pnl": boolean,
        },
        needs_any=("condition",),
    ),
    "oi": Endpoint("/v2/oi", {"condition": conditions}),
    "live-volume": Endpoint("/v2/live-volume", {"event_id": event_ids}, needs_any=("event_id",)),
    "resolutions": Endpoint(
        "/v2/resolutions",
        {"question_id": hex64, "condition": conditions, "event_id": event_ids},
        needs_one=("question_id", "condition", "event_id"),
    ),
    "tokens": Endpoint(
        "/v2/tokens",
        {
            "token_id": token_ids,
            "condition": lambda n, v: _csv(n, v, _CONDITION_LOOSE, 10, "condition id"),
        },
        needs_one=("token_id", "condition"),
    ),
    "prices-history": Endpoint(
        "/v2/prices-history",
        {
            "token_id": one_token,
            "start": integer(1),
            "end": integer(1),
            "interval": enum("max", "all", "1m", "1w", "1d", "6h", "1h", fold="lower"),
            "bucket_seconds": integer(60, 86400),
            "as_of": integer(1),
            "limit": integer(1, 10000),
            "cursor": cursor,
        },
        needs_any=("token_id",),
    ),
    # boards
    "leaderboard": Endpoint(
        "/v2/leaderboard",
        {
            "time_period": _PERIOD,
            "category": word,
            "sort_by": enum("PNL", "VOLUME"),
            "user": address,
            "limit": _LIMIT,
            "cursor": cursor,
        },
    ),
    "biggest-winners": Endpoint(
        "/v2/biggest-winners",
        {"time_period": _PERIOD, "category": word, "limit": _LIMIT, "cursor": cursor},
    ),
    # service
    "status": Endpoint("/v2/status", {}),
}


def _clean(name: str, endpoint: Endpoint, raw: Mapping[str, Any]) -> dict[str, str]:
    """Validate ``raw`` against ``endpoint``. Unknown parameters are refused, not passed through."""

    unknown = sorted(key for key in raw if key not in endpoint.params and raw[key] not in (None, ""))
    if unknown:
        accepted = ", ".join(sorted(endpoint.params)) or "none"
        raise ValueError(f"{name}: unknown parameter {unknown[0][:40]!r}; accepted: {accepted}")
    out: dict[str, str] = {}
    for key, check in endpoint.params.items():
        value = raw.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        out[key] = check(key, value)
    if endpoint.needs_any and not any(key in out for key in endpoint.needs_any):
        raise ValueError(f"{name}: one of {', '.join(endpoint.needs_any)} is required")
    if endpoint.needs_one:
        given = [key for key in endpoint.needs_one if key in out]
        if len(given) != 1:
            raise ValueError(f"{name}: give exactly one of {', '.join(endpoint.needs_one)}")
    return out


def _cross_checks(name: str, out: dict[str, str]) -> None:
    """Rules the OpenAPI text states that a per-parameter check cannot see."""

    if name == "positions" and "user" not in out and "," in out.get("condition", ""):
        raise ValueError("positions: without user, exactly one condition is accepted")
    if name == "holders" and out.get("include_pnl") == "true":
        if "," in out["condition"]:
            raise ValueError("holders: include_pnl serves exactly one condition")
        if int(out.get("limit", "100")) > 100:
            raise ValueError("holders: include_pnl allows a limit of at most 100")
    if name == "prices-history":
        forms = [key for key in ("start", "interval", "as_of") if key in out]
        if len(forms) != 1:
            raise ValueError("prices-history: give exactly one of start, interval or as_of")
        if "end" in out and "start" not in out:
            raise ValueError("prices-history: end needs start")
    if name == "activity" and "sort_by" in out and out["sort_by"] != "TIMESTAMP":
        raise ValueError("activity: only TIMESTAMP is supported")
    if name == "activity" and "condition" in out and "event_id" in out:
        raise ValueError("activity: condition and event_id are mutually exclusive")


async def _send(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: Mapping[str, str] | None = None,
    body: Any = None,
) -> Any:
    response = await client.request(method, url, params=dict(params) if params else None, json=body)
    if response.status_code >= 400:
        message, code = response.text[:300], ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                message = str(payload.get("error") or message)[:300]
                code = str(payload.get("code") or "")
        except ValueError:
            pass
        retry = response.headers.get("Retry-After")
        try:
            retry_after = float(retry) if retry else None
        except ValueError:
            retry_after = None
        raise DataAPIError(response.status_code, message, code=code, retry_after=retry_after, url=url)
    try:
        return response.json()
    except ValueError:
        raise DataAPIError(502, "the venue sent a body that is not JSON", url=url) from None


class PolymarketData:
    """Data API v2, public. ``client`` is a callable so the adapter can hand over its started httpx client."""

    names = tuple(ENDPOINTS)

    def __init__(self, client: Callable[[], httpx.AsyncClient], base: str = DATA_API) -> None:
        self._client = client
        self._base = base.rstrip("/")

    async def fetch(self, name: str, params: Mapping[str, Any] | None = None) -> Any:
        endpoint = ENDPOINTS.get(name)
        if endpoint is None:
            raise ValueError(f"unknown data route {name[:40]!r}; use one of: {', '.join(ENDPOINTS)}")
        clean = _clean(name, endpoint, params or {})
        _cross_checks(name, clean)
        return await _send(self._client(), "GET", self._base + endpoint.path, params=clean)


class ClobPublic:
    """The CLOB's public price routes (no credentials). Responses are passed through unchanged."""

    MAX_LAST_TRADES = 500  # official limit for POST /last-trades-prices

    def __init__(self, client: Callable[[], httpx.AsyncClient], base: str = CLOB) -> None:
        self._client = client
        self._base = base.rstrip("/")

    async def _one(self, path: str, token_id: str, extra: Mapping[str, str] | None = None) -> Any:
        params = {"token_id": one_token("token_id", token_id), **(extra or {})}
        return await _send(self._client(), "GET", self._base + path, params=params)

    async def midpoint(self, token_id: str) -> Any:
        """``{"mid_price": "0.45"}``."""
        return await self._one("/midpoint", token_id)

    async def spread(self, token_id: str) -> Any:
        return await self._one("/spread", token_id)

    async def last_trade_price(self, token_id: str) -> Any:
        """Price and side of the most recent matched trade."""
        return await self._one("/last-trade-price", token_id)

    async def price(self, token_id: str, side: str) -> Any:
        """Best price on one side: BUY is the best ask a buyer pays, SELL the best bid a seller gets."""
        return await self._one("/price", token_id, {"side": enum("BUY", "SELL")("side", side)})

    async def midpoints(self, tokens: str | list[str]) -> Any:
        """Map of token id to midpoint, for up to 50 tokens in one request (``GET /midpoints``)."""
        csv = token_ids("token_ids", ",".join(tokens) if isinstance(tokens, list) else tokens)
        return await _send(self._client(), "GET", self._base + "/midpoints", params={"token_ids": csv})

    async def last_trades_prices(self, tokens: str | list[str]) -> Any:
        """Last trade price and side for up to 500 tokens (``POST /last-trades-prices``)."""
        items = tokens if isinstance(tokens, list) else _text(tokens).split(",")
        unique = list(dict.fromkeys(one_token("token_id", item) for item in items if _text(item)))
        if not unique:
            raise ValueError("token_ids is empty")
        if len(unique) > self.MAX_LAST_TRADES:
            raise ValueError(f"token_ids takes at most {self.MAX_LAST_TRADES} values")
        body = [{"token_id": token} for token in unique]
        return await _send(self._client(), "POST", self._base + "/last-trades-prices", body=body)
