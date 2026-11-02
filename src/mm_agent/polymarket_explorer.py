"""Registry-driven, read-only access to the Polymarket routes the app did not cover before.

One ``Route`` row describes one official endpoint: host, path (with ``{segments}``), query
parameters and how each is validated, response kind, cache time and the official rate limit.
``PolymarketExplorer.call`` is the only executor; the web UI's Data tab renders its forms from
``catalog()``, so a new endpoint is one row here and no UI work.

Rules kept here (same as ``polymarket_data``):

* Input is validated before any request leaves the process. Unknown parameters are refused.
* A 429 is surfaced with ``Retry-After``; it is never retried silently. A small per-host limiter
  stops the app from sending more than the official limits before the venue has to say no.
* Nothing here reads a key, secret or auth header. Account routes live in ``polymarket_account``.
* ``evidence``: V = path read in an official docs page, OpenAPI file or the SDK 0.15.0 source;
  U = not verified live. A 404 on a U row is reported as "not available", the path is never guessed.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from .polymarket_data import (
    DataAPIError,
    Validator,
    _send,
    address,
    boolean,
    conditions,
    cursor,
    enum,
    hex64,
    integer,
    number,
    one_token,
    token_ids,
    word,
)

HOSTS: dict[str, str] = {
    "gamma": "https://gamma-api.polymarket.com",
    "clob": "https://clob.polymarket.com",
    "data": "https://data-api.polymarket.com",
    "relayer": "https://relayer-v2.polymarket.com",
    "site": "https://polymarket.com",
}

# Official limits, requests per 10 seconds (docs.polymarket.com/api-reference/rate-limits).
HOST_LIMITS: dict[str, int] = {"gamma": 4000, "clob": 9000, "data": 1000, "relayer": 100, "site": 100}
WINDOW = 10.0

_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_.]{0,199}")
_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_.:]{0,199}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TEXT = re.compile(r"[^\x00-\x1f]{1,200}")


def slug(name: str, value: Any) -> str:
    text = str(value).strip()
    if not _SLUG.fullmatch(text):
        raise ValueError(f"{name} must be a slug (letters, digits, '-', '_', '.')")
    return text


def ident(name: str, value: Any) -> str:
    text = str(value).strip()
    if not _IDENT.fullmatch(text):
        raise ValueError(f"{name} is not a valid id")
    return text


def digits(name: str, value: Any) -> str:
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,12}", text):
        raise ValueError(f"{name} must be digits")
    return text


def date(name: str, value: Any) -> str:
    text = str(value).strip()
    if not _DATE.fullmatch(text):
        raise ValueError(f"{name} must be YYYY-MM-DD")
    return text


def text200(name: str, value: Any) -> str:
    text = str(value).strip()
    if not _TEXT.fullmatch(text):
        raise ValueError(f"{name} must be 1 to 200 printable characters")
    return text


def csv_of(check: Validator, limit: int) -> Validator:
    def run(name: str, value: Any) -> str:
        items = [part.strip() for part in str(value).split(",") if part.strip()]
        if not items:
            raise ValueError(f"{name} is empty")
        if len(items) > limit:
            raise ValueError(f"{name} takes at most {limit} values")
        return ",".join(dict.fromkeys(check(name, item) for item in items))

    return run


def tokens_side_pairs(name: str, value: Any) -> str:
    """``token:BUY,token:SELL`` pairs for CLOB ``/prices``."""
    items = [part.strip() for part in str(value).split(",") if part.strip()]
    if not items or len(items) > 500:
        raise ValueError(f"{name} takes 1 to 500 token:side pairs")
    out: list[str] = []
    for item in items:
        tok, _, side = item.partition(":")
        out.append(f"{one_token(name, tok)}:{enum('BUY', 'SELL')(name, side)}")
    return ",".join(out)


_LIMIT = integer(1, 1000)
_OFFSET = integer(0, 1_000_000)
_ORDER = word
_BOOL = boolean


@dataclass(frozen=True)
class Route:
    key: str
    group: str  # the Data tab sub-tab
    label: str
    host: str
    path: str  # may hold {segments}
    params: Mapping[str, Validator] = field(default_factory=dict)
    path_params: Mapping[str, Validator] = field(default_factory=dict)
    needs_any: tuple[str, ...] = ()
    needs_one: tuple[str, ...] = ()
    method: str = "GET"
    body_list: str = ""  # POST routes: this param (comma-separated) becomes the JSON array body
    body_item: str = "token_id"  # key of each body object
    kind: str = "json"  # json | text | bytes
    result: str = "object"  # UI hint: table | object | series | text | file
    ttl: float = 0.0  # cache seconds, 0 = never
    limit: int = 0  # official per-10s limit for this route, 0 = host default only
    evidence: str = "V"
    paging: str = "none"  # none | offset | keyset | cursor
    note: str = ""
    hint: Mapping[str, str] = field(default_factory=dict)  # param -> short placeholder for the form

    def describe(self) -> dict[str, Any]:
        def label(check: Validator) -> str:
            return getattr(check, "__name__", "text")

        return {
            "key": self.key,
            "group": self.group,
            "label": self.label,
            "host": self.host,
            "method": self.method,
            "path": self.path,
            "class": "R0",
            "path_params": list(self.path_params),
            "params": [
                {"name": name, "type": label(check), "hint": self.hint.get(name, "")}
                for name, check in {**self.path_params, **self.params}.items()
            ],
            "needs_any": list(self.needs_any),
            "needs_one": list(self.needs_one),
            "result": self.result,
            "kind": self.kind,
            "rate_limit": self.limit or HOST_LIMITS.get(self.host, 0),
            "evidence": self.evidence,
            "paging": self.paging,
            "note": self.note,
        }


def _r(key: str, group: str, label: str, host: str, path: str, **kw: Any) -> tuple[str, Route]:
    return key, Route(key, group, label, host, path, **kw)


_GAMMA_PAGE = {"limit": _LIMIT, "offset": _OFFSET, "order": _ORDER, "ascending": _BOOL}

ROUTES: dict[str, Route] = dict(
    [
        # ── Gamma: markets and events ─────────────────────────────────────────────────────
        _r(
            "gamma.markets.keyset",
            "markets",
            "Markets (cursor paging)",
            "gamma",
            "/markets/keyset",
            params={
                "limit": _LIMIT,
                "after_cursor": cursor,
                "closed": _BOOL,
                "order": _ORDER,
                "ascending": _BOOL,
                "tag_id": digits,
                "slug": csv_of(slug, 50),
                "condition_ids": conditions,
                "clob_token_ids": token_ids,
                "volume_num_min": number,
                "liquidity_num_min": number,
                "rfq_enabled": _BOOL,
            },
            result="table",
            paging="keyset",
            limit=300,
            note="offset is rejected here; send next_cursor back as after_cursor",
        ),
        _r(
            "gamma.market.slug",
            "markets",
            "Market by slug",
            "gamma",
            "/markets/slug/{slug}",
            path_params={"slug": slug},
            limit=300,
        ),
        _r(
            "gamma.market.tags",
            "markets",
            "Market tags",
            "gamma",
            "/markets/{id}/tags",
            path_params={"id": digits},
            result="table",
            evidence="V",
        ),
        _r(
            "gamma.event.slug",
            "markets",
            "Event by slug",
            "gamma",
            "/events/slug/{slug}",
            path_params={"slug": slug},
            limit=500,
        ),
        # ── Gamma: tags and series ────────────────────────────────────────────────────────
        _r(
            "gamma.tags",
            "tags",
            "All tags",
            "gamma",
            "/tags",
            params={
                "limit": _LIMIT,
                "offset": _OFFSET,
                "order": _ORDER,
                "ascending": _BOOL,
                "is_carousel": _BOOL,
                "include_template": _BOOL,
            },
            result="table",
            ttl=3600,
            limit=200,
            paging="offset",
        ),
        _r(
            "gamma.tag.id",
            "tags",
            "Tag by id",
            "gamma",
            "/tags/{id}",
            path_params={"id": digits},
            params={"include_template": _BOOL},
            ttl=3600,
            limit=200,
        ),
        _r(
            "gamma.tag.related.id",
            "tags",
            "Related tags (relationships) by id",
            "gamma",
            "/tags/{id}/related-tags",
            path_params={"id": digits},
            params={"omit_empty": _BOOL, "status": enum("active", "closed", "all", fold="lower")},
            result="table",
            limit=200,
        ),
        _r(
            "gamma.tag.related.slug",
            "tags",
            "Related tags (relationships) by slug",
            "gamma",
            "/tags/slug/{slug}/related-tags",
            path_params={"slug": slug},
            result="table",
            limit=200,
        ),
        _r(
            "gamma.tag.related_tags.id",
            "tags",
            "Related tag objects by id",
            "gamma",
            "/tags/{id}/related-tags/tags",
            path_params={"id": digits},
            params={"omit_empty": _BOOL, "status": enum("active", "closed", "all", fold="lower")},
            result="table",
            limit=200,
        ),
        _r(
            "gamma.series",
            "tags",
            "Series list",
            "gamma",
            "/series",
            params={**_GAMMA_PAGE, "closed": _BOOL, "slug": csv_of(slug, 50), "exclude_events": _BOOL},
            result="table",
            ttl=600,
            paging="offset",
        ),
        _r(
            "gamma.series.id",
            "tags",
            "Series by id",
            "gamma",
            "/series/{id}",
            path_params={"id": digits},
            ttl=600,
        ),
        # ── Gamma: comments ───────────────────────────────────────────────────────────────
        _r(
            "gamma.comments",
            "comments",
            "Comments on an event, series or market",
            "gamma",
            "/comments",
            params={
                "parent_entity_type": enum("Event", "Series", "market", fold="none"),
                "parent_entity_id": digits,
                "limit": _LIMIT,
                "offset": _OFFSET,
                "order": _ORDER,
                "ascending": _BOOL,
                "get_positions": _BOOL,
                "holders_only": _BOOL,
            },
            needs_any=("parent_entity_id",),
            result="table",
            ttl=30,
            limit=200,
            paging="offset",
        ),
        _r(
            "gamma.comments.keyset",
            "comments",
            "Comments (cursor paging)",
            "gamma",
            "/comments/keyset",
            params={
                "parent_entity_type": enum("Event", "Series", "market", fold="none"),
                "parent_entity_id": digits,
                "limit": _LIMIT,
                "after_cursor": cursor,
                "order": _ORDER,
                "ascending": _BOOL,
            },
            result="table",
            ttl=30,
            limit=200,
            paging="keyset",
        ),
        _r(
            "gamma.comment.thread",
            "comments",
            "Comment thread",
            "gamma",
            "/comments/{id}",
            path_params={"id": ident},
            params={"get_positions": _BOOL},
            result="table",
            ttl=30,
            limit=200,
        ),
        _r(
            "gamma.comments.user",
            "comments",
            "Comments by wallet",
            "gamma",
            "/comments/user_address/{address}",
            path_params={"address": address},
            params={"limit": _LIMIT, "offset": _OFFSET, "order": _ORDER, "ascending": _BOOL},
            result="table",
            ttl=30,
            limit=200,
            paging="offset",
        ),
        # ── Gamma: sports, teams, profile ─────────────────────────────────────────────────
        _r("gamma.sports", "sports", "Sports metadata", "gamma", "/sports", result="table", ttl=3600),
        _r(
            "gamma.sports.market_types",
            "sports",
            "Sports market types",
            "gamma",
            "/sports/market-types",
            ttl=3600,
        ),
        _r(
            "gamma.teams",
            "sports",
            "Teams",
            "gamma",
            "/teams",
            params={
                "limit": _LIMIT,
                "offset": _OFFSET,
                "order": _ORDER,
                "ascending": _BOOL,
                "league": csv_of(text200, 20),
                "name": csv_of(text200, 20),
                "abbreviation": csv_of(text200, 20),
            },
            result="table",
            ttl=21600,
            paging="offset",
        ),
        _r(
            "gamma.profile",
            "wallet",
            "Public profile",
            "gamma",
            "/public-profile",
            params={"address": address},
            needs_any=("address",),
            ttl=60,
        ),
        _r(
            "gamma.health",
            "system",
            "Gamma health",
            "gamma",
            "/status",
            kind="text",
            result="text",
            evidence="U",
            note="Plain text. If the venue answers 404 the row is reported as not available.",
        ),
        # ── CLOB public: batch and variants ───────────────────────────────────────────────
        _r(
            "clob.books",
            "prices",
            "Order books (many tokens)",
            "clob",
            "/books",
            method="POST",
            body_list="token_ids",
            params={"token_ids": csv_of(one_token, 500)},
            needs_any=("token_ids",),
            result="table",
            limit=500,
            note="POST body is [{token_id}, ...]; up to 500 tokens",
        ),
        _r(
            "clob.prices",
            "prices",
            "Best prices (token:side pairs)",
            "clob",
            "/prices",
            method="POST",
            body_list="pairs",
            body_item="pair",
            params={"pairs": tokens_side_pairs},
            needs_any=("pairs",),
            result="object",
            hint={"pairs": "tokenId:BUY,tokenId:SELL"},
        ),
        _r(
            "clob.midpoints.post",
            "prices",
            "Midpoints (POST form)",
            "clob",
            "/midpoints",
            method="POST",
            body_list="token_ids",
            params={"token_ids": csv_of(one_token, 500)},
            needs_any=("token_ids",),
            evidence="V",
        ),
        _r(
            "clob.spreads",
            "prices",
            "Spreads (many tokens)",
            "clob",
            "/spreads",
            method="POST",
            body_list="token_ids",
            params={"token_ids": csv_of(one_token, 500)},
            needs_any=("token_ids",),
        ),
        _r(
            "clob.last_trades.get",
            "prices",
            "Last trade prices (GET form)",
            "clob",
            "/last-trades-prices",
            params={"token_ids": csv_of(one_token, 500)},
            needs_any=("token_ids",),
            evidence="U",
            result="table",
            note="POST form is already covered; max 500 tokens",
        ),
        _r(
            "clob.fee_rate",
            "markets-insight",
            "Fee rate",
            "clob",
            "/fee-rate",
            params={"token_id": one_token},
            needs_any=("token_id",),
            ttl=300,
        ),
        _r(
            "clob.tick_size",
            "markets-insight",
            "Tick size",
            "clob",
            "/tick-size",
            params={"token_id": one_token},
            needs_any=("token_id",),
            ttl=300,
        ),
        _r(
            "clob.neg_risk",
            "markets-insight",
            "Negative-risk flag",
            "clob",
            "/neg-risk",
            params={"token_id": one_token},
            needs_any=("token_id",),
            ttl=300,
        ),
        _r(
            "clob.market.info",
            "markets-insight",
            "CLOB market info (tokens, tick, fees, rewards, RFQ)",
            "clob",
            "/clob-markets/{condition_id}",
            path_params={"condition_id": hex64},
            ttl=60,
        ),
        _r(
            "clob.market.by_token",
            "markets-insight",
            "Market by token id",
            "clob",
            "/markets-by-token/{token_id}",
            path_params={"token_id": one_token},
            ttl=60,
        ),
        _r(
            "clob.time",
            "system",
            "CLOB server time",
            "clob",
            "/time",
            kind="text",
            result="text",
            note="Bare integer (unix seconds); used for the clock-skew check",
        ),
        _r(
            "clob.health",
            "system",
            "CLOB health",
            "clob",
            "/ok",
            kind="text",
            result="text",
            limit=100,
            evidence="U",
        ),
        _r(
            "clob.simplified_markets",
            "markets-insight",
            "Simplified markets",
            "clob",
            "/simplified-markets",
            params={"next_cursor": cursor},
            result="table",
            paging="cursor",
            evidence="U",
        ),
        _r(
            "clob.sampling_markets",
            "markets-insight",
            "Sampling markets (reward-eligible)",
            "clob",
            "/sampling-markets",
            params={"next_cursor": cursor},
            result="table",
            paging="cursor",
            evidence="U",
        ),
        _r(
            "clob.sampling_simplified",
            "markets-insight",
            "Sampling simplified markets",
            "clob",
            "/sampling-simplified-markets",
            params={"next_cursor": cursor},
            result="table",
            paging="cursor",
            evidence="U",
        ),
        _r(
            "clob.markets",
            "markets-insight",
            "CLOB markets list",
            "clob",
            "/markets",
            params={"next_cursor": cursor},
            result="table",
            paging="cursor",
            evidence="U",
            note="Not in the current docs index; 404 means not available",
        ),
        # ── CLOB rewards and rebates (public) ─────────────────────────────────────────────
        _r(
            "clob.rewards.current",
            "rewards",
            "Reward configs (current)",
            "clob",
            "/rewards/markets/current",
            params={"next_cursor": cursor, "sponsored": _BOOL},
            result="table",
            paging="cursor",
            ttl=60,
        ),
        _r(
            "clob.rewards.market",
            "rewards",
            "Reward config for a market",
            "clob",
            "/rewards/markets/{condition_id}",
            path_params={"condition_id": hex64},
            params={"next_cursor": cursor, "sponsored": _BOOL},
            result="table",
            ttl=60,
        ),
        _r(
            "clob.rewards.multi",
            "rewards",
            "Reward markets (search and filters)",
            "clob",
            "/rewards/markets/multi",
            params={
                "q": text200,
                "tag_slug": slug,
                "event_id": digits,
                "order_by": word,
                "position": enum("ASC", "DESC"),
                "min_volume_24hr": number,
                "max_volume_24hr": number,
                "min_spread": number,
                "max_spread": number,
                "next_cursor": cursor,
                "page_size": integer(1, 500),
                "sponsored": _BOOL,
            },
            result="table",
            paging="cursor",
            ttl=60,
        ),
        _r(
            "clob.rebates.current",
            "rewards",
            "Maker rebates by date",
            "clob",
            "/rebates/current",
            params={"date": date, "maker_address": address},
            result="table",
            ttl=60,
        ),
        # ── Data API extras ───────────────────────────────────────────────────────────────
        _r(
            "data.positions.combos",
            "wallet",
            "Combo positions",
            "data",
            "/v2/positions/combos",
            params={
                "user": address,
                "status": word,
                "sort_by": word,
                "sort_direction": enum("ASC", "DESC"),
                "updated_after": integer(0),
                "updated_before": integer(0),
                "limit": _LIMIT,
                "cursor": cursor,
            },
            needs_any=("user",),
            result="table",
            paging="cursor",
            limit=150,
        ),
        _r(
            "data.activity.combos",
            "wallet",
            "Combo activity",
            "data",
            "/v2/activity/combos",
            params={"user": address, "limit": _LIMIT, "cursor": cursor, "start": integer(0), "end": integer(0)},
            needs_any=("user",),
            result="table",
            paging="cursor",
        ),
        _r(
            "data.builders.leaderboard",
            "boards",
            "Builders leaderboard",
            "data",
            "/v2/builders/leaderboard",
            params={
                "time_period": enum("day", "week", "month", "all", fold="lower"),
                "limit": _LIMIT,
                "cursor": cursor,
            },
            result="table",
            paging="cursor",
        ),
        _r(
            "data.builders.volume",
            "boards",
            "Builder volume by day",
            "data",
            "/v2/builders/volume",
            params={
                "interval": enum("day", "week", "month", "all", fold="lower"),
                "builder": word,
                "limit": integer(1, 90),
            },
            result="series",
        ),
        _r(
            "data.accounting",
            "wallet",
            "Accounting snapshot (ZIP: positions.csv, equity.csv)",
            "data",
            "/v1/accounting/snapshot",
            params={"user": address},
            needs_any=("user",),
            kind="bytes",
            result="file",
            note="Streamed as a download; never put into chat",
        ),
        _r(
            "data.traded",
            "wallet",
            "Markets traded (legacy alias)",
            "data",
            "/traded",
            params={"user": address},
            needs_any=("user",),
            evidence="U",
            note="v2 /v2/user-stats field trades already holds this count",
        ),
        _r(
            "data.health",
            "system",
            "Data API health",
            "data",
            "/",
            kind="text",
            result="text",
            evidence="U",
        ),
        # ── Relayer (public read) ─────────────────────────────────────────────────────────
        _r(
            "relayer.deployed",
            "wallet",
            "Is a wallet deployed (relayer)",
            "relayer",
            "/deployed",
            params={"address": address, "type": word},
            needs_any=("address",),
            evidence="V",
        ),
        # ── Misc ──────────────────────────────────────────────────────────────────────────
        _r(
            "site.geoblock",
            "system",
            "Geoblock check",
            "site",
            "/api/geoblock",
            ttl=300,
            note="Warn only: orders from blocked regions are rejected by the venue",
        ),
    ]
)

GROUPS: tuple[tuple[str, str], ...] = (
    ("wallet", "Wallet"),
    ("boards", "Boards"),
    ("markets-insight", "Market insights"),
    ("prices", "Prices"),
    ("sports", "Sports"),
    ("tags", "Tags & Series"),
    ("comments", "Comments"),
    ("rewards", "Rewards"),
    ("markets", "Markets"),
    ("system", "System"),
)


class HostLimiter:
    """A sliding 10-second window per host (and per route). Over the limit raises a 429, never sleeps."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def check(self, bucket: str, limit: int) -> None:
        if limit <= 0:
            return
        now = self._clock()
        hits = self._hits.setdefault(bucket, deque())
        while hits and now - hits[0] >= WINDOW:
            hits.popleft()
        if len(hits) >= limit:
            wait = max(0.1, WINDOW - (now - hits[0]))
            raise DataAPIError(429, f"local rate guard: {bucket} is at its limit", retry_after=wait)
        hits.append(now)


def _clean(route: Route, raw: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    allowed = {**route.path_params, **route.params}
    unknown = sorted(k for k in raw if k not in allowed and raw[k] not in (None, ""))
    if unknown:
        accepted = ", ".join(sorted(allowed)) or "none"
        raise ValueError(f"{route.key}: unknown parameter {unknown[0][:40]!r}; accepted: {accepted}")
    path_vals: dict[str, str] = {}
    for key, check in route.path_params.items():
        value = raw.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError(f"{route.key}: {key} is required")
        path_vals[key] = check(key, value)
    query: dict[str, str] = {}
    for key, check in route.params.items():
        value = raw.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        query[key] = check(key, value)
    if route.needs_any and not any(k in query for k in route.needs_any):
        raise ValueError(f"{route.key}: one of {', '.join(route.needs_any)} is required")
    if route.needs_one and sum(1 for k in route.needs_one if k in query) != 1:
        raise ValueError(f"{route.key}: give exactly one of {', '.join(route.needs_one)}")
    return path_vals, query


class PolymarketExplorer:
    """Executes ``ROUTES`` rows. ``client`` is a callable so the adapter can hand over its httpx client."""

    def __init__(
        self,
        client: Callable[[], httpx.AsyncClient],
        *,
        hosts: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._hosts = {**HOSTS, **(hosts or {})}
        self._limiter = HostLimiter(clock)
        self._clock = clock
        self._cache: dict[str, tuple[float, Any]] = {}

    @staticmethod
    def catalog() -> dict[str, Any]:
        return {
            "groups": [{"id": gid, "label": lbl} for gid, lbl in GROUPS],
            "routes": [route.describe() for route in ROUTES.values()],
        }

    async def call(self, key: str, params: Mapping[str, Any] | None = None) -> Any:
        route = ROUTES.get(key)
        if route is None:
            raise KeyError(key)
        path_vals, query = _clean(route, params or {})
        path = route.path
        for name, value in path_vals.items():
            path = path.replace("{" + name + "}", quote(value, safe=""))
        url = self._hosts[route.host].rstrip("/") + path

        cache_key = ""
        if route.ttl and route.method == "GET" and route.kind != "bytes":
            cache_key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted(query.items()))
            hit = self._cache.get(cache_key)
            if hit and hit[0] > self._clock():
                return hit[1]

        self._limiter.check(route.host, HOST_LIMITS.get(route.host, 0))
        if route.limit:
            self._limiter.check(route.key, route.limit)

        try:
            result = await self._send(route, url, query)
        except DataAPIError as exc:
            if exc.status == 404 and route.evidence == "U":
                raise DataAPIError(404, "not available on the venue (path not verified)", url=url) from exc
            raise
        if cache_key:
            self._cache[cache_key] = (self._clock() + route.ttl, result)
            if len(self._cache) > 512:
                self._cache.pop(next(iter(self._cache)))
        return result

    async def _send(self, route: Route, url: str, query: dict[str, str]) -> Any:
        client = self._client()
        if route.method == "POST":
            raw = query.pop(route.body_list, "")
            items = [part for part in raw.split(",") if part]
            if route.body_item == "pair":
                body: Any = []
                for item in items:
                    tok, _, side = item.partition(":")
                    body.append({"token_id": tok, "side": side})
            else:
                body = [{route.body_item: item} for item in items]
            return await _send(client, "POST", url, params=query or None, body=body)
        if route.kind == "json":
            return await _send(client, "GET", url, params=query or None)
        response = await client.request("GET", url, params=query or None)
        if response.status_code >= 400:
            retry = response.headers.get("Retry-After")
            try:
                retry_after = float(retry) if retry else None
            except ValueError:
                retry_after = None
            raise DataAPIError(response.status_code, response.text[:300], retry_after=retry_after, url=url)
        if route.kind == "bytes":
            return response.content
        return response.text.strip()[:2000]


def clock_skew(server_time: Any, local_time: float) -> dict[str, Any]:
    """Skew between the venue clock (``/time`` seconds) and ours, with a verdict the UI can show."""

    try:
        server = float(str(server_time).strip())
    except ValueError:
        return {"ok": False, "detail": "server time is not a number"}
    skew = local_time - server
    return {"ok": abs(skew) < 5.0, "skew_seconds": round(skew, 2), "server_time": int(server)}
