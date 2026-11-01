"""Market discovery for the app's Markets page (Polymarket-style tabs and cards).

Pure helpers only: tab configuration and shaping Gamma ``/events`` rows into the
small card payload the app draws. Network calls live in the venue adapter.
Prices stay Decimal-as-string, like every other money/price value on the API.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

# Polymarket: the tab bar mirrors polymarket.com's own header (Trending, Breaking, New, Midterms | Politics,
# Sports, Crypto, ...). Gamma has no endpoint that returns that bar (``GET /tags?is_carousel=true`` lists only a
# few tags), so the order below is copied from the website. To follow a change on the site, edit ``NAV_TABS``:
# every entry is ``tag`` = the Gamma tag slug (the path segment polymarket.com links to) plus a label.
# The slug is turned into a numeric tag id with ``GET /tags/slug/{slug}``, the documented way to filter events.
# ``TABS`` are the sort modes (orderings, not categories); ``order`` is a Gamma sort field and ``rank``
# re-sorts the fetched page locally.
TABS: dict[str, dict[str, Any]] = {
    "trending": {"label": "Trending", "order": "volume24hr"},
    "breaking": {"label": "Breaking", "order": "volume24hr", "rank": "change", "fetch": 100},
    "new": {"label": "New", "order": "startDate"},
}

# Category tabs in the website's order. Sub-categories are not listed here: they are read from Gamma at runtime
# (``GET /tags/slug/{slug}/related-tags/tags``, see ``polymarket_sub_tags``). A sub-category tab id is
# ``<category id>:<tag slug>`` (for example ``crypto:1h``) and "All" is the category id itself.
# ``highlight`` draws the tab in the accent gradient (Midterms on the site); ``separator_before`` puts the thin
# divider the site shows between the featured tabs and the categories.
NAV_TABS: tuple[dict[str, Any], ...] = (
    {"id": "midterms", "label": "Midterms", "tag": "midterms", "highlight": True},
    {"id": "politics", "label": "Politics", "tag": "politics", "separator_before": True},
    {"id": "sports", "label": "Sports", "tag": "sports"},
    {"id": "crypto", "label": "Crypto", "tag": "crypto"},
    {"id": "esports", "label": "Esports", "tag": "esports"},
    {"id": "iran", "label": "Iran", "tag": "iran"},
    {"id": "finance", "label": "Finance", "tag": "finance"},
    {"id": "geopolitics", "label": "Geopolitics", "tag": "geopolitics"},
    {"id": "tech", "label": "Tech", "tag": "tech"},
    {"id": "elections", "label": "Elections", "tag": "elections"},
    {"id": "culture", "label": "Culture", "tag": "pop-culture"},
    {"id": "economy", "label": "Economy", "tag": "economy"},
    {"id": "weather", "label": "Weather", "tag": "weather"},
    {"id": "mentions", "label": "Mentions", "tag": "mentions"},
    {"id": "art", "label": "Art", "tag": "art"},
)
NAV_BY_ID: dict[str, dict[str, Any]] = {t["id"]: t for t in NAV_TABS}


_SLUG_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def polymarket_tab_cfg(tab: str) -> dict[str, Any] | None:
    """Query settings for a Polymarket tab id: a sort mode, a website category, or one of its sub-categories.

    A sub-category id is ``<category id>:<tag slug>``. The slug comes from Gamma, so it is only checked for a safe
    shape here (it ends up in a URL path); a slug Gamma does not know is reported when the tag is looked up.
    """

    if tab in TABS:
        return TABS[tab]
    parent_id, _, sub_slug = tab.partition(":")
    nav = NAV_BY_ID.get(parent_id)
    if nav is None:
        return None
    if not sub_slug:
        return {"label": nav["label"], "tag_slug": nav["tag"], "order": "volume24hr"}
    if not _SLUG_OK.match(sub_slug):
        return None
    return {"label": f"{nav['label']} / {sub_slug}", "tag_slug": sub_slug, "order": "volume24hr"}


def polymarket_sub_tags(data: Any, parent_slug: str = "") -> list[dict[str, str]]:
    """Gamma ``/tags/slug/{slug}/related-tags/tags`` -> sub-category rows ``{tag_id, slug, label}``, in Gamma's order.

    Hidden tags (``forceHide``), tags without an id, slug or label, unsafe slugs, the parent itself and duplicates
    are dropped. Nothing is added or renamed: the list is what Gamma returns.
    """

    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = {parent_slug.lower()} if parent_slug else set()
    for raw in data:
        if not isinstance(raw, dict) or raw.get("forceHide"):
            continue
        tag_id, slug, label = _text(raw.get("id")), _text(raw.get("slug")), _text(raw.get("label"))
        if not tag_id or not label or not _SLUG_OK.match(slug) or slug.lower() in seen:
            continue
        seen.add(slug.lower())
        out.append({"tag_id": tag_id, "slug": slug, "label": label})
    return out


# Sub-categories polymarket.com shows that Gamma's ``related-tags`` list does not return (checked: 5 Min, 15 Min and
# 4 Hours are missing from Crypto's list). They are added to what Gamma lists, never instead of it. ``slug`` is the
# Gamma tag slug, which is lowercase and is NOT the website's URL segment (the site links ``/crypto/5M``,
# ``/crypto/15M`` and ``/crypto/4hour``, the tags are ``5m``, ``15m`` and ``4h``); it is turned into a tag id with
# ``GET /tags/slug/{slug}`` the first time the sub-category is opened, like any other slug. A row without ``after``
# goes at the front; one with ``after`` goes right behind the Gamma row with that slug (else behind the front rows).
PINNED_SUBS: dict[str, tuple[dict[str, str], ...]] = {
    "crypto": (
        {"slug": "5m", "label": "5 Min"},
        {"slug": "15m", "label": "15 Min"},
        {"slug": "4h", "label": "4 Hours", "after": "1h"},
    ),
}


def with_pinned_subs(category_id: str, rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Gamma's sub-category rows (``polymarket_sub_tags``) plus the pinned ones Gamma does not list.

    A pinned slug Gamma does list is left exactly as Gamma has it (its label, tag id and place), so the day Gamma
    starts returning it nothing is doubled. A category without pinned rows comes back unchanged.
    """

    pinned = PINNED_SUBS.get(category_id, ())
    have = {r["slug"].lower() for r in rows}

    def row(p: dict[str, str]) -> dict[str, str]:
        return {"tag_id": "", "slug": p["slug"], "label": p["label"]}

    missing = [p for p in pinned if p["slug"].lower() not in have]
    front = [row(p) for p in missing if "after" not in p]
    out = front + list(rows)
    for p in missing:
        after = p.get("after")
        if after is None:
            continue
        index = next((i for i, r in enumerate(out) if r["slug"].lower() == after.lower()), None)
        out.insert(len(front) if index is None else index + 1, row(p))
    return out


def polymarket_tab_list(subs: dict[str, list[dict[str, str]]] | None = None) -> list[dict[str, Any]]:
    """The tab bar for the app: sort modes, then the website categories, with display flags.

    ``subs`` maps a category id to its sub-category rows (``polymarket_sub_tags``); a category without any has none.
    """

    subs = subs or {}
    tabs = [{**t, "highlight": False, "separator_before": False, "subtabs": []} for t in tab_list("polymarket")]
    for nav in NAV_TABS:
        tabs.append(
            {
                "id": nav["id"],
                "label": nav["label"],
                "supported": True,
                "note": "",
                "highlight": bool(nav.get("highlight")),
                "separator_before": bool(nav.get("separator_before")),
                "subtabs": [
                    {"id": f"{nav['id']}:{sub['slug']}", "label": sub["label"], "separator_before": False}
                    for sub in subs.get(nav["id"], [])
                ],
            }
        )
    return tabs


# Kalshi: only the two sort modes are fixed. The categories come from Kalshi's own list
# (``GET /search/tags_by_categories``, see ``kalshi_category_tabs``). Kalshi has no category filter
# on ``/events``, so a category tab scans a few pages and keeps the events whose ``category`` is that
# exact category. ``rank`` re-sorts the fetched batch locally.
KALSHI_TABS: dict[str, dict[str, Any]] = {
    "trending": {"label": "Trending", "rank": "volume"},
    "new": {"label": "New", "rank": "newest"},
}


def _slug(text: str) -> str:
    return "-".join("".join(c if c.isalnum() else " " for c in text.lower()).split())


def kalshi_category_tabs(data: Any) -> list[dict[str, str]]:
    """Kalshi ``/search/tags_by_categories`` -> category tabs, one per category name Kalshi lists."""

    mapping = data.get("tags_by_categories") if isinstance(data, dict) else None
    if not isinstance(mapping, dict):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set(KALSHI_TABS)
    for name in mapping:
        label = _text(name)
        slug = _slug(label)
        if not label or not slug or slug in seen:
            continue
        seen.add(slug)
        out.append({"id": slug, "label": label, "category": label})
    return out


# Limitless: ``sort`` is the API's ``sortBy``. Only the sort modes are fixed; the categories come from
# Limitless's own list (``GET /markets/categories/count`` plus ``GET /markets/active/{categoryId}``).
LIMITLESS_TABS: dict[str, dict[str, Any]] = {
    "trending": {"label": "Trending", "sort": "volume"},
    "new": {"label": "New", "sort": "newest"},
    "liquid": {"label": "Liquid", "sort": "liquidity"},
}


def limitless_category_counts(data: Any) -> list[tuple[str, int]]:
    """``/markets/categories/count`` -> ``(category id, active markets)``, busiest first, empty ones dropped.

    The ids are numeric and carry no names; see ``limitless_category_name``.
    """

    mapping = data.get("category") if isinstance(data, dict) else None
    if not isinstance(mapping, dict):
        return []
    out: list[tuple[str, int]] = []
    for key, value in mapping.items():
        cid = _text(key)
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if cid.isdigit() and count > 0:
            out.append((cid, count))
    return sorted(out, key=lambda x: x[1], reverse=True)


def limitless_category_name(rows: Any) -> str:
    """The name of a category, read the documented way: ``categories[0]`` of one of its markets."""

    if not isinstance(rows, list):
        return ""
    for row in rows:
        if isinstance(row, dict):
            cats = [c for c in json_list(row.get("categories")) if _text(c)]
            if cats:
                return _text(cats[0])
    return ""


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


def shape_market(raw: dict[str, Any], *, allow_closed: bool = False) -> dict[str, Any] | None:
    """One Gamma market -> a card row. None when it cannot be traded as Yes/No."""

    market_id = _text(raw.get("id"))
    if not market_id:
        return None
    if not allow_closed:
        if raw.get("closed") or raw.get("archived"):
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


COIN_NAMES = {
    "btc": "Bitcoin",
    "eth": "Ethereum",
    "sol": "Solana",
    "xrp": "XRP",
    "doge": "Dogecoin",
    "bnb": "BNB",
    "hype": "Hyperliquid",
}
_NAME_TO_COIN = {name.lower(): sym for sym, name in COIN_NAMES.items()}
# Polymarket's rolling crypto windows: ``btc-updown-5m-1761971100`` = coin, window length, window START (epoch s).
_CRYPTO_SLUG = re.compile(r"^(?P<coin>[a-z0-9]+)-updown-(?P<span>\d+)(?P<unit>[mhd])-(?P<ts>\d{9,11})$")
_SPAN_SECONDS = {"m": 60, "h": 3600, "d": 86400}
_NON_TEAM_OUTCOMES = {"yes", "no", "over", "under", "up", "down"}


def _epoch(value: Any) -> int | None:
    """Epoch seconds from an ISO string (Z or offset), or None."""

    text = _text(value)
    if not text:
        return None
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def parse_score(value: Any) -> dict[str, Any] | None:
    """Gamma's ``score`` string -> ``{"raw", "sets": [[a, b], ...]}``.

    The wire format is not documented, so this is deliberately tolerant: segments separated by , | or ;
    each ``a-b`` (also ``a:b``). Anything else keeps only ``raw`` and the UI shows it as text.
    ``a`` belongs to the first outcome (team A), ``b`` to the second, in the order Polymarket lists them.
    """

    text = _text(value)
    if not text:
        return None
    sets: list[list[int]] = []
    for part in re.split(r"[,|;]", text):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*[-:]\s*(\d+)(?:\s*\(.*\))?", part)
        if m is None:
            return {"raw": text, "sets": []}
        sets.append([int(m.group(1)), int(m.group(2))])
    return {"raw": text, "sets": sets}


def shape_sport(raw: dict[str, Any]) -> dict[str, Any] | None:
    """The live-game part of a sports event: teams with price, score, period, live/ended flags."""

    rows = [m for m in raw.get("markets") or [] if isinstance(m, dict)]
    is_game = bool(
        _text(raw.get("score"))
        or _text(raw.get("period"))
        or _text(raw.get("gameStatus"))
        or raw.get("live")
        or any(m.get("gameId") or m.get("sportsMarketType") for m in rows)
    )
    if not is_game:
        return None
    pick: dict[str, Any] | None = None
    for m in rows:  # the moneyline is the market whose two outcomes are the two teams
        labels = [str(x) for x in json_list(m.get("outcomes"))]
        if len(labels) != 2 or any(x.strip().lower() in _NON_TEAM_OUTCOMES for x in labels):
            continue
        if _text(m.get("sportsMarketType")).lower() == "moneyline":
            pick = m
            break
        pick = pick or m
    teams: list[dict[str, Any]] = []
    market_id = None
    if pick is not None:
        labels = [str(x) for x in json_list(pick.get("outcomes"))]
        prices = json_list(pick.get("outcomePrices"))
        market_id = _text(pick.get("id")) or None
        for i, label in enumerate(labels):
            price = dec(prices[i]) if i < len(prices) else None
            teams.append(
                {
                    "name": label,
                    "price": str(price) if price is not None else None,
                    "outcome": "YES" if i == 0 else "NO",
                }
            )
    if len(teams) != 2:
        return None
    return {
        "market_id": market_id,
        "league": _text(raw.get("seriesSlug")) or None,
        "live": bool(raw.get("live")),
        "ended": bool(raw.get("ended")),
        "period": _text(raw.get("period")) or None,
        "elapsed": _text(raw.get("elapsed")) or None,
        "status": _text(raw.get("gameStatus")) or None,
        "score": parse_score(raw.get("score")),
        "start_time": _text(raw.get("startTime")) or _text(pick.get("gameStartTime") if pick else None) or None,
        "teams": teams,
    }


def shape_crypto(raw: dict[str, Any]) -> dict[str, Any] | None:
    """A rolling "<coin> Up or Down" window: coin, window start/end, Up/Down prices, price to beat (if sent)."""

    slug = _text(raw.get("slug"))
    title = _text(raw.get("title"))
    coin = None
    start = end = span = None
    prefix = None
    m = _CRYPTO_SLUG.match(slug)
    if m:
        coin = m.group("coin")
        span = int(m.group("span")) * _SPAN_SECONDS[m.group("unit")]
        start = int(m.group("ts"))
        end = start + span
        prefix = f"{coin}-updown-{m.group('span')}{m.group('unit')}"
    elif "up or down" in title.lower():
        head = title.lower().split("up or down")[0].strip()
        coin = _NAME_TO_COIN.get(head) or (head if head in COIN_NAMES else None)
        start = _epoch(raw.get("startTime")) or _epoch(raw.get("eventStartTime"))
        end = _epoch(raw.get("endDate"))
        span = end - start if start is not None and end is not None and end > start else None
    if coin is None or start is None or end is None:
        return None
    up = down = None
    for mk in raw.get("markets") or []:
        if not isinstance(mk, dict):
            continue
        labels = [str(x).strip().lower() for x in json_list(mk.get("outcomes"))]
        prices = json_list(mk.get("outcomePrices"))
        if labels[:2] == ["up", "down"] and len(prices) >= 2:
            up, down = dec(prices[0]), dec(prices[1])
            break
    meta_raw = raw.get("eventMetadata")
    meta: dict[str, Any] = meta_raw if isinstance(meta_raw, dict) else {}
    ptb = dec(meta.get("priceToBeat") if meta.get("priceToBeat") is not None else meta.get("price_to_beat"))
    windows: list[dict[str, Any]] = []
    if prefix and span:
        windows = [{"slug": f"{prefix}-{start + span * k}", "start_ts": start + span * k} for k in range(1, 5)]
    return {
        "coin": coin,
        "symbol": coin.upper(),
        "name": COIN_NAMES.get(coin, coin.upper()),
        "span_seconds": span,
        "start_ts": start,
        "end_ts": end,
        "up_price": str(up) if up is not None else None,
        "down_price": str(down) if down is not None else None,
        "price_to_beat": str(ptb) if ptb is not None else None,
        "price_to_beat_source": "polymarket" if ptb is not None else None,
        "windows": windows,
    }


def shape_event(raw: dict[str, Any], *, allow_closed: bool = False) -> dict[str, Any] | None:
    """One Gamma event -> one card. None when it has no open Yes/No market.

    ``allow_closed`` keeps a finished event (the live endpoint still has to show the final score).
    ``kind`` tells the app how to draw it: ``sport`` (teams + score), ``crypto_updown`` (price to beat)
    or plain ``market``.
    """

    if (raw.get("closed") or raw.get("archived")) and not allow_closed:
        return None
    markets = [
        m
        for m in (shape_market(x, allow_closed=allow_closed) for x in raw.get("markets") or [] if isinstance(x, dict))
        if m
    ]
    sport = shape_sport(raw)
    crypto = None if sport else shape_crypto(raw)
    if not markets and not (sport and allow_closed):
        return None
    markets.sort(key=lambda m: dec(m["yes_price"]) or Decimal(0), reverse=True)
    changes = [abs(c) for c in (dec(m["change_1d"]) for m in markets) if c is not None]
    tags = [_text(t.get("label")) for t in raw.get("tags") or [] if isinstance(t, dict) and _text(t.get("label"))]
    slug = _text(raw.get("slug"))
    return {
        "event_id": _text(raw.get("id")),
        "title": _text(raw.get("title")) or (markets[0]["question"] if markets else ""),
        "slug": slug,
        "url": f"https://polymarket.com/event/{slug}" if slug else None,
        "image": _text(raw.get("image")) or _text(raw.get("icon")) or None,
        "volume": str(dec(raw.get("volume")) or Decimal(0)),
        "volume_24h": str(dec(raw.get("volume24hr")) or Decimal(0)),
        "liquidity": str(dec(raw.get("liquidity")) or Decimal(0)),
        "end_time": _text(raw.get("endDate")) or (markets[0]["end_time"] if markets else None),
        "tags": tags[:5],
        "market_count": len(markets),
        "max_change_1d": str(max(changes)) if changes else None,
        "kind": "sport" if sport else ("crypto_updown" if crypto else "market"),
        "sport": sport,
        "crypto": crypto,
        "markets": markets,
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
        "markets": markets,
    }


def shape_kalshi_events(
    rows: Any, *, match: list[str] | None = None, category: str | None = None, q: str | None = None
) -> list[dict[str, Any]]:
    """Cards from Kalshi events. ``category`` keeps only events whose category is exactly that name."""

    if not isinstance(rows, list):
        return []
    needle = (q or "").strip().lower()
    wanted = (category or "").strip().lower()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        if match and not _matches(_text(raw.get("category")), match):
            continue
        if wanted and _text(raw.get("category")).lower() != wanted:
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
        "markets": markets,
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
