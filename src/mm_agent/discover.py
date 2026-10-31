"""Market discovery for the app's Markets page (Polymarket-style tabs and cards).

Pure helpers only: tab configuration and shaping Gamma ``/events`` rows into the
small card payload the app draws. Network calls live in the venue adapter.
Prices stay Decimal-as-string, like every other money/price value on the API.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

MAX_MARKETS_PER_CARD = 6

# Polymarket: only the three sort modes live here. They are orderings, not categories.
# Every category tab (Politics, Sports, Culture, ...) comes from Gamma's own tag list
# (``GET /tags?is_carousel=true``, the categories polymarket.com shows) at runtime; see
# ``tag_tabs`` and ``PolymarketVenue.discover_tabs``. ``order`` is a Gamma sort field and
# ``rank`` re-sorts the fetched page locally.
TABS: dict[str, dict[str, Any]] = {
    "trending": {"label": "Trending", "order": "volume24hr"},
    "breaking": {"label": "Breaking", "order": "volume24hr", "rank": "change", "fetch": 100},
    "new": {"label": "New", "order": "startDate"},
}


def tag_tabs(rows: Any) -> list[dict[str, str]]:
    """Gamma ``/tags`` rows -> category tabs, in the order Gamma returned them.

    A tag needs an id, a slug and a label, and is dropped when Gamma marks it ``forceHide``.
    A slug that equals a sort-mode id is dropped too, so a tag can never shadow Trending/New.
    """

    if not isinstance(rows, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set(TABS)
    for raw in rows:
        if not isinstance(raw, dict) or raw.get("forceHide") is True:
            continue
        tag_id, slug, label = _text(raw.get("id")), _text(raw.get("slug")), _text(raw.get("label"))
        if not tag_id or not slug or not label or slug in seen:
            continue
        seen.add(slug)
        out.append({"id": slug, "label": label, "tag_id": tag_id})
    return out


# Kalshi has no tag API for events, so a tab is a list of substrings matched against the
# event's ``category`` (case-insensitive). ``rank`` re-sorts the fetched batch locally.
KALSHI_TABS: dict[str, dict[str, Any]] = {
    "trending": {"label": "Trending", "rank": "volume"},
    "new": {"label": "New", "rank": "newest"},
    "politics": {"label": "Politics", "match": ["politic"]},
    "elections": {"label": "Elections", "match": ["election"]},
    "sports": {"label": "Sports", "match": ["sport"]},
    "crypto": {"label": "Crypto", "match": ["crypto"]},
    "economics": {"label": "Economics", "match": ["econom"]},
    "financials": {"label": "Financials", "match": ["financ"]},
    "companies": {"label": "Companies", "match": ["compan"]},
    "tech": {"label": "Tech & Science", "match": ["tech", "science"]},
    "weather": {"label": "Weather", "match": ["climate", "weather"]},
    "culture": {"label": "Culture", "match": ["culture", "entertain"]},
    "world": {"label": "World", "match": ["world"]},
    "health": {"label": "Health", "match": ["health"]},
    "mentions": {"label": "Mentions", "match": ["mention"]},
}

# Limitless: ``sort`` is the API's ``sortBy``; ``match`` filters on the market's category labels.
LIMITLESS_TABS: dict[str, dict[str, Any]] = {
    "trending": {"label": "Trending", "sort": "volume"},
    "new": {"label": "New", "sort": "newest"},
    "liquid": {"label": "Liquid", "sort": "liquidity"},
    "crypto": {"label": "Crypto", "sort": "volume", "match": ["crypto", "bitcoin", "ethereum"]},
    "sports": {"label": "Sports", "sort": "volume", "match": ["sport"]},
    "politics": {"label": "Politics", "sort": "volume", "match": ["politic", "election"]},
    "economy": {"label": "Economy", "sort": "volume", "match": ["econom", "financ"]},
    "tech": {"label": "Tech", "sort": "volume", "match": ["tech", "ai"]},
    "culture": {"label": "Culture", "sort": "volume", "match": ["culture", "entertain", "music", "film"]},
}

VENUE_TABS: dict[str, dict[str, dict[str, Any]]] = {
    "polymarket": TABS,
    "kalshi": KALSHI_TABS,
    "limitless": LIMITLESS_TABS,
}


def tabs_for(venue: str) -> dict[str, dict[str, Any]]:
    return VENUE_TABS.get(venue, {})


def tab_list(venue: str = "polymarket") -> list[dict[str, Any]]:
    """Tabs in display order, with a flag for the ones the agent cannot serve."""

    return [
        {"id": key, "label": cfg["label"], "supported": "unsupported" not in cfg, "note": cfg.get("unsupported", "")}
        for key, cfg in tabs_for(venue).items()
    ]


def json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return list(value) if isinstance(value, (list, tuple)) else []


def dec(value: Any) -> Decimal | None:
    try:
        if value is None or value == "":
            return None
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def shape_market(raw: dict[str, Any]) -> dict[str, Any] | None:
    """One Gamma market -> a card row. None when it cannot be traded as Yes/No."""

    market_id = _text(raw.get("id"))
    if not market_id or raw.get("closed") or raw.get("archived"):
        return None
    if raw.get("active") is False or raw.get("acceptingOrders") is False:
        return None
    labels = [str(x) for x in json_list(raw.get("outcomes"))]
    prices = json_list(raw.get("outcomePrices"))
    if len(labels) != 2 or len(prices) < 2:
        return None
    yes, no = dec(prices[0]), dec(prices[1])
    if yes is None or no is None:
        return None
    change = dec(raw.get("oneDayPriceChange"))
    question = _text(raw.get("question")) or market_id
    return {
        "market_id": market_id,
        "question": question,
        "label": _text(raw.get("groupItemTitle")) or question,
        "yes_label": labels[0],
        "no_label": labels[1],
        "yes_price": str(yes),
        "no_price": str(no),
        "change_1d": str(change) if change is not None else None,
        "volume": str(dec(raw.get("volume")) or Decimal(0)),
        "end_time": _text(raw.get("endDate")) or None,
    }


def shape_event(raw: dict[str, Any]) -> dict[str, Any] | None:
    """One Gamma event -> one card. None when it has no open Yes/No market."""

    if raw.get("closed") or raw.get("archived"):
        return None
    markets = [m for m in (shape_market(x) for x in raw.get("markets") or [] if isinstance(x, dict)) if m]
    if not markets:
        return None
    markets.sort(key=lambda m: dec(m["yes_price"]) or Decimal(0), reverse=True)
    changes = [abs(c) for c in (dec(m["change_1d"]) for m in markets) if c is not None]
    tags = [_text(t.get("label")) for t in raw.get("tags") or [] if isinstance(t, dict) and _text(t.get("label"))]
    slug = _text(raw.get("slug"))
    return {
        "event_id": _text(raw.get("id")),
        "title": _text(raw.get("title")) or markets[0]["question"],
        "slug": slug,
        "url": f"https://polymarket.com/event/{slug}" if slug else None,
        "image": _text(raw.get("image")) or _text(raw.get("icon")) or None,
        "volume": str(dec(raw.get("volume")) or Decimal(0)),
        "volume_24h": str(dec(raw.get("volume24hr")) or Decimal(0)),
        "liquidity": str(dec(raw.get("liquidity")) or Decimal(0)),
        "end_time": _text(raw.get("endDate")) or markets[0]["end_time"],
        "tags": tags[:5],
        "market_count": len(markets),
        "max_change_1d": str(max(changes)) if changes else None,
        "markets": markets[:MAX_MARKETS_PER_CARD],
    }


def shape_events(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        item = shape_event(raw)
        if item is None or item["event_id"] in seen:
            continue
        seen.add(item["event_id"])
        out.append(item)
    return out


def rank_by_change(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Breaking = the biggest 24h price moves among the busiest events."""

    return sorted(items, key=lambda i: dec(i.get("max_change_1d")) or Decimal(0), reverse=True)


# --------------------------------------------------------------------------- Kalshi

ZERO = Decimal(0)
ONE = Decimal(1)


def _clamp01(value: Decimal | None) -> Decimal | None:
    if value is None or value < 0 or value > 1:
        return None
    return value


def _matches(text: str, needles: list[str]) -> bool:
    low = text.lower()
    return any(n in low for n in needles)


def shape_kalshi_market(raw: dict[str, Any]) -> dict[str, Any] | None:
    """One Kalshi binary market -> a card row. None for combos, closed or unpriced markets."""

    ticker = _text(raw.get("ticker"))
    if not ticker or raw.get("mve_collection_ticker"):
        return None
    if raw.get("market_type") not in (None, "binary"):
        return None
    if _text(raw.get("status")).lower() not in {"active", "open", "initialized"}:
        return None
    bid = _clamp01(dec(raw.get("yes_bid_dollars")))
    ask = _clamp01(dec(raw.get("yes_ask_dollars")))
    no_ask = _clamp01(dec(raw.get("no_ask_dollars")))
    last = _clamp01(dec(raw.get("last_price_dollars")))
    if bid is not None and ask is not None and ask > 0:
        chance: Decimal | None = (bid + ask) / 2
    elif last is not None and last > 0:
        chance = last
    else:
        chance = ask or bid
    if chance is None:
        return None
    # The buttons show what a buy costs now (the ask); the headline "chance" is the mid.
    yes_price = ask if ask is not None and ask > 0 else chance
    no_price = no_ask if no_ask is not None and no_ask > 0 else ONE - chance
    prev = _clamp01(dec(raw.get("previous_price_dollars")))
    change = (last - prev) if last is not None and prev is not None and prev > 0 and last > 0 else None
    title = _text(raw.get("title")) or ticker
    sub = _text(raw.get("yes_sub_title"))
    return {
        "market_id": ticker,
        "question": title,
        "label": sub or title,
        "yes_label": "Yes",
        "no_label": "No",
        "yes_price": str(yes_price),
        "no_price": str(no_price),
        "chance": str(chance),
        "change_1d": str(change) if change is not None else None,
        "volume": str(dec(raw.get("volume_fp")) or ZERO),
        "volume_24h": str(dec(raw.get("volume_24h_fp")) or ZERO),
        "end_time": _text(raw.get("close_time")) or _text(raw.get("expiration_time")) or None,
        "created": _text(raw.get("created_time")) or None,
    }


def shape_kalshi_event(raw: dict[str, Any]) -> dict[str, Any] | None:
    markets = [m for m in (shape_kalshi_market(x) for x in raw.get("markets") or [] if isinstance(x, dict)) if m]
    if not markets:
        return None
    markets.sort(key=lambda m: dec(m["chance"]) or ZERO, reverse=True)
    changes = [abs(c) for c in (dec(m["change_1d"]) for m in markets) if c is not None]
    event_ticker = _text(raw.get("event_ticker"))
    category = _text(raw.get("category"))
    ends = sorted(m["end_time"] for m in markets if m["end_time"])
    created = sorted(m["created"] for m in markets if m["created"])
    return {
        "event_id": event_ticker or markets[0]["market_id"],
        "title": _text(raw.get("title")) or markets[0]["question"],
        "slug": event_ticker,
        "url": f"https://kalshi.com/markets/{event_ticker.lower()}" if event_ticker else None,
        "image": None,
        "volume": str(sum((dec(m["volume"]) or ZERO for m in markets), ZERO)),
        "volume_24h": str(sum((dec(m["volume_24h"]) or ZERO for m in markets), ZERO)),
        "volume_unit": "contracts",
        "liquidity": "0",
        "end_time": ends[0] if ends else None,
        "created": created[0] if created else None,
        "tags": [category] if category else [],
        "category": category,
        "market_count": len(markets),
        "max_change_1d": str(max(changes)) if changes else None,
        "markets": markets[:MAX_MARKETS_PER_CARD],
    }


def shape_kalshi_events(rows: Any, *, match: list[str] | None = None, q: str | None = None) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    needle = (q or "").strip().lower()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        if match and not _matches(_text(raw.get("category")), match):
            continue
        item = shape_kalshi_event(raw)
        if item is None or item["event_id"] in seen:
            continue
        if needle:
            hay = " ".join([item["title"], item["event_id"], *[m["label"] for m in item["markets"]]]).lower()
            if needle not in hay:
                continue
        seen.add(item["event_id"])
        out.append(item)
    return out


def rank_items(items: list[dict[str, Any]], how: str | None) -> list[dict[str, Any]]:
    """Local re-sort of one fetched batch: ``volume`` (24h, then total) or ``newest``."""

    if how == "volume":
        return sorted(
            items,
            key=lambda i: (dec(i.get("volume_24h")) or ZERO, dec(i.get("volume")) or ZERO),
            reverse=True,
        )
    if how == "newest":
        return sorted(items, key=lambda i: i.get("created") or "", reverse=True)
    return items


# ------------------------------------------------------------------------- Limitless


def _ms_to_iso(value: Any) -> str | None:
    from datetime import UTC, datetime

    try:
        ms = float(value)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _limitless_volume(raw: dict[str, Any]) -> Decimal:
    formatted = dec(raw.get("volumeFormatted"))
    if formatted is not None:
        return formatted
    # ``volume`` is the raw collateral amount in 6-decimal USDC base units.
    base = dec(raw.get("volume"))
    return base / Decimal(1_000_000) if base is not None else ZERO


def shape_limitless_market(raw: dict[str, Any]) -> dict[str, Any] | None:
    """One Limitless CLOB market -> a card row. None when it cannot be traded as Yes/No."""

    slug = _text(raw.get("slug"))
    if not slug or raw.get("expired") or raw.get("hidden"):
        return None
    if _text(raw.get("tradeType") or "clob").lower() != "clob":
        return None
    if _text(raw.get("status")).upper() not in {"CREATED", "FUNDED", "OPEN"}:
        return None
    prices = json_list(raw.get("prices"))
    if len(prices) < 2:
        return None
    yes, no = _clamp01(dec(prices[0])), _clamp01(dec(prices[1]))
    if yes is None or no is None:
        return None
    title = _text(raw.get("title")) or _text(raw.get("proxyTitle")) or slug
    return {
        "market_id": slug,
        "question": title,
        "label": title,
        "yes_label": "Yes",
        "no_label": "No",
        "yes_price": str(yes),
        "no_price": str(no),
        "change_1d": None,
        "volume": str(_limitless_volume(raw)),
        "end_time": _ms_to_iso(raw.get("expirationTimestamp")),
        "created": _text(raw.get("createdAt")) or None,
    }


def shape_limitless_event(raw: dict[str, Any]) -> dict[str, Any] | None:
    """A single market, or a group market with nested outcomes, -> one card."""

    nested = [x for x in raw.get("markets") or [] if isinstance(x, dict)]
    rows = nested if nested else [raw]
    markets = [m for m in (shape_limitless_market(x) for x in rows) if m]
    if not markets:
        return None
    markets.sort(key=lambda m: dec(m["yes_price"]) or ZERO, reverse=True)
    categories = [str(c) for c in raw.get("categories") or [] if c]
    slug = _text(raw.get("slug")) or markets[0]["market_id"]
    ends = sorted(m["end_time"] for m in markets if m["end_time"])
    return {
        "event_id": _text(raw.get("groupId")) or _text(raw.get("id")) or slug,
        "title": _text(raw.get("title")) or markets[0]["question"],
        "slug": slug,
        "url": f"https://limitless.exchange/markets/{slug}",
        "image": _text(raw.get("imageUrl")) or _text(raw.get("logo")) or None,
        "volume": str(_limitless_volume(raw) if not nested else sum((dec(m["volume"]) or ZERO for m in markets), ZERO)),
        "volume_24h": "0",
        "liquidity": "0",
        "end_time": ends[0] if ends else None,
        "created": _text(raw.get("createdAt")) or None,
        "tags": categories[:5],
        "categories": categories,
        "market_count": len(markets),
        "max_change_1d": None,
        "markets": markets[:MAX_MARKETS_PER_CARD],
    }


def shape_limitless_events(rows: Any, *, match: list[str] | None = None, q: str | None = None) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    needle = (q or "").strip().lower()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        item = shape_limitless_event(raw)
        if item is None or item["event_id"] in seen:
            continue
        if match and not any(_matches(c, match) for c in item["categories"]):
            continue
        if needle and needle not in item["title"].lower():
            continue
        seen.add(item["event_id"])
        out.append(item)
    return out
