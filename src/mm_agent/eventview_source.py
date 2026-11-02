"""Fetch the raw venue payloads for one EventView. Public reads only; the pure builders live in eventview.py."""

from __future__ import annotations

import re
import time
from typing import Any

from .eventview import build_kalshi_view, build_limitless_view, build_polymarket_view
from .util import get_logger

log = get_logger("eventview_source")
GAMMA = "https://gamma-api.polymarket.com"
_PM_SLUG = re.compile(r"^[a-z0-9][a-z0-9\-]{0,120}$")
_KALSHI_TICKER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_LMTS_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,120}$")


def _event_rows(data: Any) -> list[dict[str, Any]]:
    rows = data.get("events") if isinstance(data, dict) else data
    return [r for r in rows or [] if isinstance(r, dict)] if isinstance(rows, list) else []


async def polymarket_view(adapter: Any, *, event_id: str = "", slug: str = "") -> dict[str, Any] | None:
    if event_id:
        if not event_id.isdigit():
            raise ValueError("bad event id")
        raw: Any = await adapter.get_json(GAMMA + f"/events/{event_id}")
    else:
        if not _PM_SLUG.match(slug):
            raise ValueError("bad event slug")
        rows = await adapter.get_json(GAMMA + "/events", params={"slug": slug})
        raw = rows[0] if isinstance(rows, list) and rows else None
    if not isinstance(raw, dict):
        return None
    game_id = raw.get("gameId") or next(
        (m.get("gameId") for m in raw.get("markets") or [] if isinstance(m, dict)), None
    )
    companions: list[dict[str, Any]] = []
    if game_id and str(game_id).isdigit():  # the other tabs of a game live in companion events with the same gameId
        try:
            found = await adapter.get_json(
                GAMMA + "/events/keyset", params={"game_id": str(game_id), "closed": "false"}
            )
            companions = [e for e in _event_rows(found) if _eid(e) != _eid(raw)]
        except Exception as exc:  # the main event alone still renders
            log.info("companion events unavailable: %s", type(exc).__name__)
    return build_polymarket_view(raw, companions=companions)


def _eid(event: dict[str, Any]) -> str:
    return str(event.get("id") or event.get("slug") or "")


_MILESTONE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_MILESTONES: dict[str, tuple[float, list[str]]] = {}  # event ticker -> (valid until, milestone ids)
MILESTONE_TTL = 600.0  # a game's milestone does not change; do not look it up on every REST refresh
MILESTONE_RETRY = 120.0  # after a failed or empty lookup, wait this long before asking again


async def _kalshi_milestones(adapter: Any, ticker: str, category: str) -> list[str]:
    """Milestone ids for a Kalshi Sports event (``GET /milestones?related_event_ticker=``; docs only, unverified).

    Best effort: any failure means "no live score", never a failed view. Public read, cached per ticker.
    """

    if category.strip().lower() != "sports":
        return []
    now = time.monotonic()
    hit = _MILESTONES.get(ticker)
    if hit is not None and hit[0] > now:
        return hit[1]
    ids: list[str] = []
    try:
        data = await adapter.get_json(
            adapter.base_url + "/milestones", params={"related_event_ticker": ticker, "limit": 10}
        )
        rows = data.get("milestones") if isinstance(data, dict) else None
        ids = [str(m["id"]) for m in rows or [] if isinstance(m, dict) and _MILESTONE_ID.match(str(m.get("id") or ""))][
            :5
        ]
    except Exception as exc:  # the event still renders without a score
        log.info("kalshi milestones unavailable: %s", type(exc).__name__)
    if len(_MILESTONES) >= 256:
        _MILESTONES.clear()
    _MILESTONES[ticker] = (now + (MILESTONE_TTL if ids else MILESTONE_RETRY), ids)
    return ids


async def kalshi_view(adapter: Any, *, ticker: str) -> dict[str, Any] | None:
    if not _KALSHI_TICKER.match(ticker):
        raise ValueError("bad event ticker")
    data = await adapter.get_json(adapter.base_url + f"/events/{ticker}", params={"with_nested_markets": "true"})
    if not isinstance(data, dict):
        return None
    nested = data.get("event")
    event: dict[str, Any] = nested if isinstance(nested, dict) else data
    if not event.get("markets") and isinstance(data.get("markets"), list):
        event = {**event, "markets": data["markets"]}
    if not event.get("event_ticker") and not event.get("markets"):
        return None
    milestones = await _kalshi_milestones(adapter, ticker, str(event.get("category") or ""))
    return build_kalshi_view(event, milestone_ids=milestones)


async def limitless_view(adapter: Any, *, slug: str) -> dict[str, Any] | None:
    if not _LMTS_SLUG.match(slug):
        raise ValueError("bad market slug")
    data = await adapter.get_json(adapter.base_url + f"/markets/{slug}")
    return build_limitless_view(data) if isinstance(data, dict) and data else None
