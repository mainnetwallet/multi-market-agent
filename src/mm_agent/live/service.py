"""Builds the hub from the running app: event loader, chart history and (optionally) the real feeds."""

from __future__ import annotations

from typing import Any

from .. import eventview_source
from ..domain import VenueId
from .history import fetch_history
from .hub import LiveHub
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

    hub = LiveHub(load_view=load_view, history=history)
    adapter = registry.adapters.get(VenueId.POLYMARKET)
    if run_feeds and adapter is not None:
        hub.attach(
            market=PolymarketMarketFeed(on_quote=hub.on_quote, on_state=hub.on_feed_state, on_resolved=hub.on_resolved),
            sports=PolymarketSportsFeed(on_game=hub.on_game, on_state=hub.on_feed_state),
            # PolyBolt needs CLOB credentials, which only the signing modules may read: Coinbase until wired.
            crypto=CryptoPrices(getattr(adapter, "spot", None), None),
        )
    else:
        hub.attach(crypto=CryptoPrices(getattr(adapter, "spot", None), None) if adapter is not None else None)
    return hub
