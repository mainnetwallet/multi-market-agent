"""EventView: one venue-neutral description of an event, for every client (PWA and Android).

Pure and typed: raw venue payloads in, one dict out. No network, no clock (``now`` is passed in) and no
floats: every price and amount is a Decimal rendered as a string. The layout is decided from the data
(``choose_layout``), never from a category name or a URL, so any opened market renders: an unknown shape
falls back to ``generic_list``.

Rule seen on polymarket.com: the chance and the chart use the midpoint; the Buy buttons show the ask.
Display only. Orders still go through ``/v1/orders/*`` with a fresh book at submit time.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from .discover import _text, dec, json_list, parse_score, shape_crypto

ONE = Decimal(1)
ZERO = Decimal(0)

LAYOUTS = (
    "updown_window",
    "game_3way",
    "game_2way",
    "bracket_ladder",
    "multi_candidates",
    "binary",
    "generic_list",
)
CHART_RANGES = ["1H", "6H", "1D", "1W", "1M", "ALL"]
_PALETTE = ["#2d9cdb", "#f2994a", "#27ae60", "#bb6bd9", "#eb5757", "#56ccf2"]
_NON_TEAM = {"yes", "no", "over", "under", "up", "down"}


def _s(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _clamp(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return min(max(value, ZERO), ONE)


# ------------------------------------------------------------------------------ numbers in labels

_NUM = re.compile(r"(?<![\w.])(-?\d[\d,]*\.?\d*)\s*([kmbt])?\b", re.I)
_MULT = {"k": Decimal(10) ** 3, "m": Decimal(10) ** 6, "b": Decimal(10) ** 9, "t": Decimal(10) ** 12}


def label_number(label: str) -> Decimal | None:
    """The first number in a label such as ``$300B`` or ``>2.5`` (K/M/B/T suffixes scaled), or None."""

    m = _NUM.search(label or "")
    if m is None:
        return None
    number = dec(m.group(1).replace(",", ""))
    if number is None:
        return None
    return number * _MULT.get((m.group(2) or "").lower(), ONE)


# ------------------------------------------------------------------------------ rows


def _side(label: str, ask: Decimal | None, bid: Decimal | None, outcome: str) -> dict[str, Any]:
    return {"label": label, "ask": _s(_clamp(ask)), "bid": _s(_clamp(bid)), "outcome": outcome}


def _midpoint(bid: Decimal | None, ask: Decimal | None, fallback: Decimal | None) -> Decimal | None:
    if bid is not None and ask is not None and ask > 0 and ask >= bid:
        return (bid + ask) / 2
    return fallback


def _pm_row(raw: dict[str, Any], *, kind: str = "") -> dict[str, Any] | None:
    """A Polymarket two-outcome market as a row. None when it has no id or unreadable prices."""

    mid = _text(raw.get("id"))
    labels = [str(x) for x in json_list(raw.get("outcomes"))]
    prices = [dec(p) for p in json_list(raw.get("outcomePrices"))]
    if not mid or len(labels) != 2 or len(prices) < 2 or prices[0] is None or prices[1] is None:
        return None
    bid, ask = dec(raw.get("bestBid")), dec(raw.get("bestAsk"))
    chance = _midpoint(bid, ask, prices[0])
    yes_ask = ask if ask is not None else prices[0]
    yes_bid = bid
    no_ask = ONE - bid if bid is not None else prices[1]
    no_bid = ONE - ask if ask is not None else None
    title = _text(raw.get("question")) or mid
    status = "closed" if raw.get("closed") else ("resolved" if raw.get("umaResolutionStatus") == "resolved" else "open")
    return {
        "row_id": mid,
        "market_id": mid,
        "label": _text(raw.get("groupItemTitle")) or title,
        "sub_label": title if _text(raw.get("groupItemTitle")) else None,
        "logo": _text(raw.get("icon")) or _text(raw.get("image")) or None,
        "chance": _s(_clamp(chance)),
        "change_1d": _s(dec(raw.get("oneDayPriceChange"))),
        "yes": _side(labels[0], yes_ask, yes_bid, "YES"),
        "no": _side(labels[1], no_ask, no_bid, "NO"),
        "volume": _s(dec(raw.get("volume")) or ZERO),
        "closes": _text(raw.get("endDate")) or None,
        "status": status,
        "line": _s(dec(raw.get("line"))),
        "market_type": _text(raw.get("sportsMarketType")) or kind or None,
        "token_ids": [str(t) for t in json_list(raw.get("clobTokenIds"))[:2]],
        "_threshold": dec(raw.get("groupItemThreshold")),
        "_labels": labels,
    }


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not k.startswith("_")}


# ------------------------------------------------------------------------------ layout


def _threshold(row: dict[str, Any]) -> Decimal | None:
    """The ordering number of a row: the venue's own threshold if sent, else a number inside the label."""

    t = row.get("_threshold")
    if isinstance(t, Decimal):
        return t
    return label_number(str(row.get("label") or ""))


def choose_layout(rows: list[dict[str, Any]], *, updown: bool = False, has_game: bool = False) -> str:
    """Pick the layout from the data. First match wins; ``generic_list`` always renders.

    1 updown_window, 2 game_3way (a moneyline made of three Yes/No markets), 3 game_2way (a market whose two
    outcomes are the two teams), 4 bracket_ladder (unique numeric thresholds), 5 multi_candidates, 6 binary,
    7 generic_list.
    """

    if updown:
        return "updown_window"
    moneyline = [r for r in rows if r.get("market_type") == "moneyline"]
    if has_game and len(moneyline) == 3:
        return "game_3way"
    if has_game and any(_is_team_market(r) for r in rows):
        return "game_2way"
    usable = [r for r in rows if r.get("yes") and r.get("no") and not r.get("_odd")]
    if len(usable) != len(rows) or not rows:
        return "generic_list"
    if len(rows) > 1:
        thresholds = [_threshold(r) for r in rows]
        if all(t is not None for t in thresholds) and len(set(thresholds)) == len(rows):
            return "bracket_ladder"
        return "multi_candidates"
    return "binary"


def _is_team_market(row: dict[str, Any]) -> bool:
    labels = [x.strip().lower() for x in row.get("_labels") or []]
    return len(labels) == 2 and not any(x in _NON_TEAM for x in labels)


# ------------------------------------------------------------------------------ polymarket sections

_SECTION_LABELS = {
    "game_lines": "Game Lines",
    "goalscorers": "Goalscorers",
    "exact_score": "Exact Score",
    "halves": "Halves",
    "players": "Players",
    "assists": "Assists",
}
_GAME_LINE_TYPES = {
    "moneyline",
    "spreads",
    "totals",
    "both_teams_to_score",
    "soccer_team_totals",
    "team_totals",
    "draw_no_bet",
}


def prettify(text: str) -> str:
    return " ".join(w.capitalize() for w in re.split(r"[_\-\s]+", text.strip()) if w)


def section_for(market_type: str, event_suffix: str = "") -> tuple[str, str]:
    """(section id, label) for a market from its ``sportsMarketType``; unknown types get their own prettified tab."""

    t = (market_type or "").lower()
    if not t:
        t = event_suffix.lower().replace("-", "_")
    if not t or t in _GAME_LINE_TYPES:
        return "game_lines", _SECTION_LABELS["game_lines"]
    for needle, sid in (("exact", "exact_score"), ("correct_score", "exact_score"), ("scorer", "goalscorers")):
        if needle in t:
            return sid, _SECTION_LABELS[sid]
    if "assist" in t:
        return "assists", _SECTION_LABELS["assists"]
    if "half" in t:
        return "halves", _SECTION_LABELS["halves"]
    if "player" in t:
        return "players", _SECTION_LABELS["players"]
    sid = re.sub(r"[^a-z0-9]+", "_", t).strip("_") or "other"
    return sid, prettify(t)


def _suffix(main_slug: str, slug: str) -> str:
    if main_slug and slug.startswith(main_slug + "-"):
        return slug[len(main_slug) + 1 :]
    return ""


# ------------------------------------------------------------------------------ game block

_SIMPLE_SCORE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")


def _team(raw: dict[str, Any]) -> dict[str, Any]:
    logo = next((_text(raw.get(k)) for k in ("logo", "logoUrl", "image", "icon") if _text(raw.get(k))), "")
    name = _text(raw.get("name")) or _text(raw.get("displayName"))
    return {
        "name": name,
        "abbr": _text(raw.get("abbreviation")) or _text(raw.get("abbr")) or name[:3].upper(),
        "logo": logo or None,
        "score": None,
    }


def build_game(raw: dict[str, Any], team_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The live-game block. Home and away come from ``teams[].ordering``, never from outcome order.

    The score string is ``<home>-<away>``. Without ``ordering`` the first team listed is taken as home and
    ``orientation`` says ``assumed`` so a client can show that.
    """

    teams_in = [t for t in raw.get("teams") or [] if isinstance(t, dict)]
    home = away = None
    orientation = "ordering"
    for t in teams_in:
        order = _text(t.get("ordering")).lower()
        if order == "home" and home is None:
            home = _team(t)
        elif order == "away" and away is None:
            away = _team(t)
    if home is None or away is None:
        orientation = "assumed"
        names = [_team(t) for t in teams_in]
        if len(names) < 2 and team_rows:
            names = [_team({"name": x}) for x in team_rows[0].get("_labels") or []]
        if len(names) < 2:
            return None
        home, away = names[0], names[1]
    score_raw = _text(raw.get("score")) or None
    m = _SIMPLE_SCORE.match(score_raw or "")
    if m:
        home["score"], away["score"] = int(m.group(1)), int(m.group(2))
    game_id = raw.get("gameId") or next(
        (r.get("gameId") for r in raw.get("markets") or [] if isinstance(r, dict)), None
    )
    return {
        "game_id": game_id,
        "league": _text(raw.get("seriesSlug")) or None,
        "period": _text(raw.get("period")) or None,
        "elapsed": _text(raw.get("elapsed")) or None,
        "live": bool(raw.get("live")),
        "ended": bool(raw.get("ended")),
        "home": home,
        "away": away,
        "score_raw": score_raw,
        "score_sets": (parse_score(score_raw) or {}).get("sets") if score_raw and not m else None,
        "orientation": orientation,
        "source": "gamma",
        "source_ts": None,
    }


# ------------------------------------------------------------------------------ builders


def _iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _chart(rows: list[dict[str, Any]], layout: str) -> dict[str, Any] | None:
    if layout == "generic_list" or not rows:
        return None
    top = sorted(rows, key=lambda r: dec(r.get("chance")) or ZERO, reverse=True)[:4]
    series = [
        {"row_id": r["row_id"], "label": r["label"], "color": _PALETTE[i % len(_PALETTE)], "points": []}
        for i, r in enumerate(top)
    ]
    return {"series": series, "ranges": CHART_RANGES, "default": "1D", "point_rule": "midpoint"}


def _status(raw: dict[str, Any], game: dict[str, Any] | None, now: datetime) -> str:
    if raw.get("closed") or raw.get("archived"):
        return "resolved" if raw.get("resolved") else "closed"
    if game is not None:
        if game["ended"]:
            return "ended"
        if game["live"]:
            return "live"
        start = _text(raw.get("startTime")) or _text(raw.get("startDate"))
        try:
            if start and datetime.fromisoformat(start.replace("Z", "+00:00")) > now:
                return "scheduled"
        except ValueError:
            pass
    return "live"  # open and trading


def _view(
    *,
    venue: str,
    raw: dict[str, Any],
    layout: str,
    rows: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    now: datetime,
    game: dict[str, Any] | None = None,
    volume_unit: str = "usd",
    url: str | None = None,
    chips: list[dict[str, Any]] | None = None,
    related: list[dict[str, Any]] | None = None,
    category_path: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sections = [s for s in sections if s["rows"]]  # never an empty tab
    path = category_path or []
    return {
        "venue": venue,
        "event_id": _text(raw.get("id")) or _text(raw.get("event_ticker")) or _text(raw.get("slug")),
        "slug": _text(raw.get("slug")) or _text(raw.get("event_ticker")),
        "url": url,
        "title": _text(raw.get("title")) or (rows[0]["label"] if rows else ""),
        "category": {"id": path[0].lower() if path else "", "path": path},
        "layout": layout,
        "image": _text(raw.get("image")) or _text(raw.get("icon")) or _text(raw.get("imageUrl")) or None,
        "volume": _s(dec(raw.get("volume")) or sum((dec(r.get("volume")) or ZERO for r in rows), ZERO)),
        "volume_unit": volume_unit,
        "start_time": _text(raw.get("startTime")) or _text(raw.get("startDate")) or None,
        "end_time": _text(raw.get("endDate")) or _text(raw.get("close_time")) or None,
        "status": _status(raw, game, now),
        "game": game,
        "sections": sections,
        "chart": _chart(rows, layout),
        "chips": chips or [],
        "related": related or [],
        "freshness": {"price_ts": _iso(now), "source": "rest", "stale": False},
        "raw_extra": extra or {},
    }


def _sort_rows(rows: list[dict[str, Any]], layout: str) -> list[dict[str, Any]]:
    if layout == "bracket_ladder":
        return sorted(rows, key=lambda r: _threshold(r) or ZERO)
    if layout == "multi_candidates":
        return sorted(rows, key=lambda r: dec(r.get("chance")) or ZERO, reverse=True)
    return rows


def _chips(siblings: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for ev in siblings or []:
        slug = _text(ev.get("slug"))
        if slug:
            out.append({"id": _text(ev.get("id")) or slug, "label": _text(ev.get("title")) or slug, "event_ref": slug})
    return out


def build_polymarket_view(
    main: dict[str, Any],
    *,
    companions: list[dict[str, Any]] | None = None,
    siblings: list[dict[str, Any]] | None = None,
    related: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """EventView of a Gamma event, with its companion events (same ``gameId``) folded in as tabs.

    ``siblings`` and ``related`` are Gamma events the caller found through whichever link field the live probe
    confirmed; with none they stay empty rather than guessed.
    """

    now = now or datetime.now(UTC)
    slug = _text(main.get("slug"))
    crypto = shape_crypto(main)
    events = [main, *[c for c in companions or [] if isinstance(c, dict) and c is not main]]
    sections: dict[str, dict[str, Any]] = {}
    all_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ev in events:
        suffix = "" if ev is main else _suffix(slug, _text(ev.get("slug")))
        for mk in ev.get("markets") or []:
            if not isinstance(mk, dict):
                continue
            row = _pm_row(mk)
            if row is None:
                bad = _text(mk.get("id"))
                if bad and bad not in seen:  # three or more outcomes, or unreadable: still shown, as text
                    seen.add(bad)
                    all_rows.append(_odd_row(mk))
                continue
            if row["row_id"] in seen:
                continue
            seen.add(row["row_id"])
            sid, label = section_for(_text(mk.get("sportsMarketType")), suffix)
            sections.setdefault(sid, {"id": sid, "label": label, "rows": []})["rows"].append(row)
            all_rows.append(row)
    has_game = bool(
        main.get("gameId")
        or any(
            isinstance(m, dict) and (m.get("gameId") or m.get("sportsMarketType")) for m in main.get("markets") or []
        )
    )
    layout = choose_layout(all_rows, updown=crypto is not None, has_game=has_game)
    game = build_game(main, all_rows) if layout in ("game_3way", "game_2way") else None
    if layout in ("game_3way", "game_2way") and game is None:
        layout = choose_layout(all_rows, updown=False, has_game=False)
    if layout == "game_2way":
        _expand_two_way(sections)
    order = ["game_lines", "goalscorers", "exact_score", "halves", "players", "assists"]
    ordered = sorted(sections.values(), key=lambda s: order.index(s["id"]) if s["id"] in order else len(order))
    if layout in ("bracket_ladder", "multi_candidates"):
        for s in ordered:
            s["rows"] = _sort_rows(s["rows"], layout)
    elif layout == "generic_list":
        ordered = [{"id": "all", "label": "Markets", "rows": all_rows}]
    tags = [_text(t.get("label")) for t in main.get("tags") or [] if isinstance(t, dict) and _text(t.get("label"))]
    extra: dict[str, Any] = {"crypto": crypto} if crypto else {}
    if layout == "generic_list":
        extra["reason"] = "no layout matched"
    view = _view(
        venue="polymarket",
        raw=main,
        layout=layout,
        rows=all_rows,
        sections=[{**s, "rows": [_public(r) for r in s["rows"]]} for s in ordered],
        now=now,
        game=game,
        url=f"https://polymarket.com/event/{slug}" if slug else None,
        chips=_chips(siblings),
        related=[
            {"event_ref": _text(r.get("slug")), "title": _text(r.get("title")), "chance": None, "image": None}
            for r in related or []
            if _text(r.get("slug"))
        ],
        category_path=tags[:3],
        extra=extra,
    )
    return view


def _odd_row(mk: dict[str, Any]) -> dict[str, Any]:
    """A market this view cannot price as Yes/No (three or more outcomes): listed, not tradable from here."""

    labels = [str(x) for x in json_list(mk.get("outcomes"))]
    prices = [_s(dec(p)) for p in json_list(mk.get("outcomePrices"))]
    return {
        "row_id": _text(mk.get("id")),
        "market_id": _text(mk.get("id")),
        "label": _text(mk.get("groupItemTitle")) or _text(mk.get("question")) or _text(mk.get("id")),
        "sub_label": None,
        "logo": None,
        "chance": None,
        "change_1d": None,
        "yes": None,
        "no": None,
        "volume": _s(dec(mk.get("volume")) or ZERO),
        "closes": _text(mk.get("endDate")) or None,
        "status": "open",
        "line": None,
        "market_type": _text(mk.get("sportsMarketType")) or None,
        "outcomes": [{"label": lab, "price": prices[i] if i < len(prices) else None} for i, lab in enumerate(labels)],
        "_odd": True,
    }


def _expand_two_way(sections: dict[str, dict[str, Any]]) -> None:
    """A team-vs-team market becomes two rows, one per team, each buying that team's outcome."""

    for sec in sections.values():
        rows: list[dict[str, Any]] = []
        for r in sec["rows"]:
            if not _is_team_market(r):
                rows.append(r)
                continue
            first, second = dict(r), dict(r)
            first.update(row_id=f"{r['row_id']}:0", label=r["yes"]["label"], chance=r["chance"])
            second.update(
                row_id=f"{r['row_id']}:1",
                label=r["no"]["label"],
                chance=_s(ONE - (dec(r["chance"]) or ZERO)) if r["chance"] is not None else None,
                yes=r["no"],
                no=r["yes"],
            )
            rows += [first, second]
        sec["rows"] = rows


def _kalshi_row(m: dict[str, Any]) -> dict[str, Any] | None:
    ticker = _text(m.get("ticker"))
    if not ticker or m.get("mve_collection_ticker"):
        return None
    bid, ask = _clamp(dec(m.get("yes_bid_dollars"))), _clamp(dec(m.get("yes_ask_dollars")))
    nbid, nask = _clamp(dec(m.get("no_bid_dollars"))), _clamp(dec(m.get("no_ask_dollars")))
    last = _clamp(dec(m.get("last_price_dollars")))
    chance = _midpoint(bid, ask, last if last and last > 0 else (ask or bid))
    if chance is None:
        return None
    prev = _clamp(dec(m.get("previous_price_dollars")))
    change = last - prev if last and prev and last > 0 and prev > 0 else None
    title = _text(m.get("title")) or ticker
    strike = dec(m.get("floor_strike"))
    if strike is None:
        strike = dec(m.get("cap_strike"))
    status = _text(m.get("status")).lower()
    return {
        "row_id": ticker,
        "market_id": ticker,
        "label": _text(m.get("yes_sub_title")) or title,
        "sub_label": title,
        "logo": None,
        "chance": _s(chance),
        "change_1d": _s(change),
        "yes": _side("Yes", ask if ask else chance, bid, "YES"),
        "no": _side("No", nask if nask else ONE - (bid if bid is not None else chance), nbid, "NO"),
        "volume": _s(dec(m.get("volume_fp")) or ZERO),  # contracts, not dollars
        "closes": _text(m.get("close_time")) or None,
        "status": "open" if status in {"active", "open", "initialized"} else (status or "closed"),
        "line": None,
        "market_type": None,
        "_threshold": strike,
        "_labels": ["Yes", "No"],
    }


def build_kalshi_view(raw: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """EventView of a Kalshi event with nested markets. Strike markets become a ladder; volume is contracts."""

    now = now or datetime.now(UTC)
    rows = [r for r in (_kalshi_row(m) for m in raw.get("markets") or [] if isinstance(m, dict)) if r]
    layout = choose_layout(rows)
    rows = _sort_rows(rows, layout)
    ticker = _text(raw.get("event_ticker"))
    category = _text(raw.get("category"))
    src = {**raw, "id": ticker, "slug": ticker}
    return _view(
        venue="kalshi",
        raw=src,
        layout=layout,
        rows=rows,
        sections=[{"id": "all", "label": "Markets", "rows": [_public(r) for r in rows]}],
        now=now,
        volume_unit="contracts",
        url=f"https://kalshi.com/markets/{ticker.lower()}" if ticker else None,
        category_path=[category] if category else [],
        extra={"volume_note": "Kalshi volume is in contracts"},
    )


def _limitless_row(m: dict[str, Any]) -> dict[str, Any] | None:
    slug = _text(m.get("slug"))
    prices = [_clamp(dec(p)) for p in json_list(m.get("prices"))]
    if not slug or len(prices) < 2 or prices[0] is None or prices[1] is None:
        return None
    title = _text(m.get("title")) or _text(m.get("proxyTitle")) or slug
    return {
        "row_id": slug,
        "market_id": slug,
        "label": title,
        "sub_label": None,
        "logo": None,
        "chance": _s(prices[0]),
        "change_1d": None,
        "yes": _side("Yes", prices[0], None, "YES"),
        "no": _side("No", prices[1], None, "NO"),
        "volume": _s(dec(m.get("volume")) or ZERO),
        "closes": _text(m.get("expirationDate")) or None,
        "status": "closed" if m.get("expired") else "open",
        "line": None,
        "market_type": None,
        "_threshold": None,
        "_labels": ["Yes", "No"],
    }


def build_limitless_view(raw: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """EventView of a Limitless market: a group's children become rows, a single market one row."""

    now = now or datetime.now(UTC)
    nested = [x for x in raw.get("markets") or [] if isinstance(x, dict)]
    rows = [r for r in (_limitless_row(m) for m in (nested or [raw])) if r]
    layout = choose_layout(rows)
    rows = _sort_rows(rows, layout)
    slug = _text(raw.get("slug"))
    cats = [str(c) for c in raw.get("categories") or [] if c]
    src = {**raw, "id": _text(raw.get("groupId")) or _text(raw.get("id")) or slug}
    return _view(
        venue="limitless",
        raw=src,
        layout=layout,
        rows=rows,
        sections=[{"id": "all", "label": "Markets", "rows": [_public(r) for r in rows]}],
        now=now,
        url=f"https://limitless.exchange/markets/{slug}" if slug else None,
        category_path=cats[:3],
        extra={"price_note": "Limitless gives no bid/ask here; buttons show the quoted price"},
    )


# ------------------------------------------------------------------------------ URL parser

_HOSTS = {
    "polymarket.com": "polymarket",
    "www.polymarket.com": "polymarket",
    "kalshi.com": "kalshi",
    "www.kalshi.com": "kalshi",
    "limitless.exchange": "limitless",
    "www.limitless.exchange": "limitless",
}
_PM_SLUG = re.compile(r"^[a-z0-9][a-z0-9\-]{0,120}$")
_KALSHI_TICKER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_LMTS_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,120}$")


def parse_event_url(url: str) -> tuple[str, str]:
    """``(venue, slug or ticker)`` from a pasted event link. Raises ValueError for anything else.

    Only https links on the three venues' own hosts are accepted, with no credentials or port.
    """

    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise ValueError("not a valid url") from exc
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in _HOSTS or parts.username or parts.password or parts.port:
        raise ValueError("unsupported link: use an event link from polymarket.com, kalshi.com or limitless.exchange")
    venue = _HOSTS[host]
    segs = [s for s in parts.path.split("/") if s]
    if venue == "polymarket":
        if len(segs) >= 2 and segs[0] == "event" and _PM_SLUG.match(segs[1]):
            return venue, segs[1]
    elif venue == "kalshi":
        if len(segs) >= 2 and segs[0] == "markets" and _KALSHI_TICKER.match(segs[-1]):
            return venue, segs[-1].upper()
    elif len(segs) >= 2 and segs[0] == "markets" and _LMTS_SLUG.match(segs[1]):
        return venue, segs[1]
    raise ValueError("unsupported link: not an event page")
