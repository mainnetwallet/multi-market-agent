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

# tab id -> how to ask Gamma. ``tag`` is a Gamma tag slug; ``order`` a Gamma sort field;
# ``rank`` re-sorts the fetched page locally. ``unsupported`` tabs return a note, not rows.
TABS: dict[str, dict[str, Any]] = {
    "trending": {"label": "Trending", "order": "volume24hr"},
    "breaking": {"label": "Breaking", "order": "volume24hr", "rank": "change", "fetch": 100},
    "new": {"label": "New", "order": "startDate"},
    "midterms": {"label": "Midterms", "tag": "midterms", "order": "volume24hr"},
    "politics": {"label": "Politics", "tag": "politics", "order": "volume24hr"},
    "sports": {"label": "Sports", "tag": "sports", "order": "volume24hr"},
    "crypto": {"label": "Crypto", "tag": "crypto", "order": "volume24hr"},
    "esports": {"label": "Esports", "tag": "esports", "order": "volume24hr"},
    "iran": {"label": "Iran", "tag": "iran", "order": "volume24hr"},
    "finance": {"label": "Finance", "tag": "finance", "order": "volume24hr"},
    "geopolitics": {"label": "Geopolitics", "tag": "geopolitics", "order": "volume24hr"},
    "tech": {"label": "Tech", "tag": "tech", "order": "volume24hr"},
    "economy": {"label": "Economy", "tag": "economy", "order": "volume24hr"},
    "elections": {"label": "Elections", "tag": "elections", "order": "volume24hr"},
    "culture": {"label": "Culture", "tag": "culture", "order": "volume24hr"},
    "weather": {"label": "Weather", "tag": "weather", "order": "volume24hr"},
    "mentions": {"label": "Mentions", "tag": "mentions", "order": "volume24hr"},
    "combos": {
        "label": "Combos",
        "unsupported": "Combos are multi-leg markets. This agent does not trade them yet.",
    },
    "perps": {
        "label": "Perps",
        "unsupported": "Perpetuals have no public API this agent uses. Open Polymarket to trade them.",
    },
}


def tab_list() -> list[dict[str, Any]]:
    """Tabs in display order, with a flag for the ones the agent cannot serve."""

    return [
        {"id": key, "label": cfg["label"], "supported": "unsupported" not in cfg, "note": cfg.get("unsupported", "")}
        for key, cfg in TABS.items()
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
