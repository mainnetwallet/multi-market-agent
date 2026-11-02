# P2a - Live hub + Polymarket feeds + /v1/stream
Follow `00_CONTEXT.md` session protocol. Needs P1 done. Do **only** this phase, then STOP.
Read only: `WORKLOG.md`, `src/mm_agent/eventview.py`, `src/mm_agent/events.py` (event bus), `src/mm_agent/venues/account_ws.py` (reconnect pattern), the `/v1/ws` route in `control_api.py`, `src/mm_agent/crypto_feed.py`, `src/mm_agent/main.py` (startup wiring).

## Build (new package `src/mm_agent/live/`)
- `hub.py`: one upstream connection per feed, many client subscriptions, ref-counted (subscribe upstream on first client, unsubscribe on last), latest-value store keyed by `(venue, id)`,
  coalescing to at most 4 updates/s per subscription (latest wins), every message has `seq`, `source`, `source_ts`, `server_ts`; per-client bounded queue (drop oldest patches, never snapshots);
  subscription caps and idle eviction; stale handling (silent or reconnecting feed -> `stale: true` and REST polling fallback until back); exponential backoff with jitter.
- `polymarket_market_ws.py`: protocol in `00_CONTEXT.md`; local book per token from `book` + `price_change` (size "0" removes a level); handle `last_trade_price`, `tick_size_change`, `best_bid_ask`, `new_market`, `market_resolved`; `PING` every 10 s.
- `polymarket_sports_ws.py`: `ping`/`pong`, index by `gameId`, join to events by `gameId` and team names; map score with home/away; map periods and per-sport status strings.
- `polybolt.py`: only when CLOB API creds are configured; otherwise fall back to `crypto_feed.py` with `source:"coinbase"`.
- Charts: initial history via `POST /batch-prices-history` (<=20 tokens per call), then grow the tail from live midpoint ticks.
- `WS /v1/stream` in `control_api.py` (same auth as `/v1/ws`; leave `/v1/ws` unchanged). Client frames `{"op":"subscribe"|"unsubscribe"|"snapshot","topic":"event","venue","id","range"}`; server frames `snapshot`, `patch`, `status`, `error`. A `seq` gap makes the client ask for a snapshot.
- `/status` per-feed metrics: `connected, reconnects, msgs_per_s, last_msg_age_s, subs, dropped`. Feature flag `LIVE_VIEWS_ENABLED`.

## Tests (fake WebSocket servers, no real network)
Snapshot then patches; coalescing; `PING`/`PONG` and `ping`/`pong`; reconnect + fresh snapshot; stale flag + REST fallback + recovery; ref-counted unsubscribe; slow-client protection; size "0" removal; tick-size change; `market_resolved`; unauthenticated client rejected.

## Done when
Tests pass, gate green, Handoff written (include how to run the hub against the real feeds). STOP.
