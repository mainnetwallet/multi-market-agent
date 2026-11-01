"""Builds the hub from the running app: event loader, chart history and (optionally) the real feeds."""

from __future__ import annotations

from typing import Any

from .. import eventview_source
from ..domain import VenueId
from .history import fetch_history, fetch_kalshi_history, fetch_limitless_history
from .hub import LiveHub
from .kalshi_ws import KalshiMarketFeed
from .limitless_ws import LimitlessMarketFeed
from .polybolt import CryptoPrices
from .polymarket_market_ws import PolymarketMarketFeed
from .polymarket_sports_ws import PolymarketSportsFeed


def build_hub(registry: Any, *, run_feeds: bool) -> LiveHub:
    """``run_feeds=False`` gives a REST-only hub (no outbound WebSocket); the app uses that in tests."""

    async def load_view(venue: str, ident: str) -> dict[str, Any] | None:
        adapter = registry.adapters.get(VenueId(venue))
        if adapter is None:
            return None
        if venue == "polymarket":
            if ident.isdigit():
                return await eventview_source.polymarket_view(adapter, event_id=ident)
            return await eventview_source.polymarket_view(adapter, slug=ident)
        if venue == "kalshi":
            return await eventview_source.kalshi_view(adapter, ticker=ident.upper())
        return await eventview_source.limitless_view(adapter, slug=ident)

    async def history(tokens: list[str], rng: str) -> dict[str, list[list[Any]]]:
        adapter = registry.adapters.get(VenueId.POLYMARKET)
        if adapter is None:
            return {}
        return await fetch_history(adapter.post_json, tokens, rng)

    venue_history: dict[str, Any] = {}
    kalshi = registry.adapters.get(VenueId.KALSHI)
    limitless = registry.adapters.get(VenueId.LIMITLESS)
    if kalshi is not None and hasattr(kalshi, "base_url"):

        async def kalshi_history(ids: list[str], rng: str) -> dict[str, list[list[Any]]]:
            return await fetch_kalshi_history(kalshi.get_json, kalshi.base_url, ids, rng)

        venue_history["kalshi"] = kalshi_history
    if limitless is not None and hasattr(limitless, "base_url"):

        async def limitless_history(ids: list[str], rng: str) -> dict[str, list[list[Any]]]:
            return await fetch_limitless_history(limitless.get_json, limitless.base_url, ids, rng)

        venue_history["limitless"] = limitless_history

    hub = LiveHub(load_view=load_view, history=history, venue_history=venue_history)
    adapter = registry.adapters.get(VenueId.POLYMARKET)
    crypto = CryptoPrices(getattr(adapter, "spot", None), None) if adapter is not None else None
    if not run_feeds:
        hub.attach(crypto=crypto)
        return hub

    market = sports = kalshi_feed = limitless_feed = None
    unavailable: dict[str, str] = {}
    if adapter is not None:
        market = PolymarketMarketFeed(on_quote=hub.on_quote, on_state=hub.on_feed_state, on_resolved=hub.on_resolved)
        sports = PolymarketSportsFeed(on_game=hub.on_game, on_state=hub.on_feed_state)
        # PolyBolt needs CLOB credentials, which only the signing modules may read: Coinbase until wired.
    if kalshi is not None and hasattr(kalshi, "ws_headers"):
        try:
            kalshi.ws_headers()  # a signed handshake is needed even for public channels: no key, no feed
        except Exception:
            unavailable["kalshi_ws"] = "no Kalshi API key configured; Kalshi events are REST-polled"
        else:
            kalshi_feed = KalshiMarketFeed(
                kalshi.ws_url(),
                kalshi.ws_headers,
                on_quote=lambda q: hub.on_quote(q, "kalshi"),
                on_state=hub.on_feed_state,
                on_settled=lambda t, won: hub.on_settled("kalshi", t, won),
            )
    if limitless is not None and hasattr(limitless, "market_ws_url"):
        limitless_feed = LimitlessMarketFeed(
            limitless.market_ws_url(),
            on_quote=lambda q: hub.on_quote(q, "limitless"),
            on_state=hub.on_feed_state,
            on_settled=lambda slug, won: hub.on_settled("limitless", slug, won),
        )
    hub.attach(
        market=market,
        sports=sports,
        crypto=crypto,
        kalshi=kalshi_feed,
        limitless=limitless_feed,
        unavailable=unavailable,
    )
    return hub
