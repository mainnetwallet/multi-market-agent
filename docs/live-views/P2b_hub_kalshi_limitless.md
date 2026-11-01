# P2b - Kalshi + Limitless feeds into the hub
Follow `00_CONTEXT.md` session protocol. Needs P2a done. Do **only** this phase, then STOP.
Read only: `WORKLOG.md`, `src/mm_agent/live/hub.py`, `src/mm_agent/venues/account_ws.py`, `src/mm_agent/venues/kalshi.py` (around WS url, `candlesticks`, discover), `src/mm_agent/venues/limitless.py` (Socket.IO url, history/discover).

## Build
- `live/kalshi_ws.py`: reuse the signed handshake from `account_ws.py` (key stays server-side). Subscribe `ticker` (+ `trade`) for visible markets and `orderbook_delta` for the opened market; local book from snapshot + deltas;
  `update_subscription` add/delete markets; `get_snapshot` after a gap; `market_lifecycle_v2` for new/closed. Without a Kalshi key: feed reports `unavailable` and the UI falls back to REST.
  Sports: poll `GET /live_data/batch` (<=100 ids) every 2-3 s, labelled `source:"kalshi_rest_polled"`; play-by-play from `game_stats` where supported. Charts: existing batch candlesticks.
- `live/limitless_ws.py`: Socket.IO `/markets`, `subscribe_market_prices` `{marketSlugs}`, handle `orderbookUpdate` and price events, `subscribe_market_lifecycle`. Charts from `GET /markets/{slug}/historical-price`.
  Replace the `categories[0]` category-name trick with the navigation endpoints if P0 confirmed them. Recurring markets: use Market Timeline.
- Fix the README claim "Limitless has no history endpoint" and update the venue capability table.

## Tests
Fake Kalshi WS (snapshot, deltas, gap -> get_snapshot) and fake Socket.IO server; no-key path; URL/slug validation; no credential in payloads or logs.

## Done when
Tests pass, gate green, Handoff written. STOP.
