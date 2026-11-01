# WORKLOG - live category views (branch feat/live-category-views)

## Baseline (before P0)
ruff check, ruff format --check, mypy src: clean. pytest: all pass (161 tests). Python 3.12.3.

## Handoff - P0 (probe)
Done:
- Plan copied to `docs/live-views/`.
- `scripts/probe_live_shapes.py`: read-only (GET only), public endpoints, 10 s timeout, scrubs credential-like keys,
  saves to `tests/fixtures/live/<venue>/<n>.json`; `--synthetic` or automatic fallback when no venue is reachable.
- `tests/test_probe_live_shapes.py`: scrub, synthetic marking, GET-only, unreachable-venue behaviour.
- `DOCS_FINDINGS.md` section 7; two outdated Limitless history claims marked superseded.
Not done / **LIVE_VERIFIED=false**: no venue reachable from the sandbox, so every fixture is synthetic and every
[INFERRED] item is unconfirmed. Owner must run `python scripts/probe_live_shapes.py` once on a real machine and commit
the result.
Decisions: synthetic soccer fixture lists the away team first to exercise score orientation; README Limitless-history
claim left for P2b/P5 (docs only).
Gotchas: no secrets written anywhere; the leaked GitHub token must be revoked.
Next step: P1 (`docs/live-views/P1_eventview.md`), against synthetic fixtures unless the owner supplies live ones.

## Handoff - P1 (EventView + layout chooser + /v1/event/view, REST only)
Done:
- `src/mm_agent/eventview.py` (pure, Decimal-as-string, no floats): `build_polymarket_view` (companion events folded into tabs),
  `build_kalshi_view`, `build_limitless_view`, `choose_layout` (7 rules, data-driven), `parse_event_url` (https + host allow-list,
  no userinfo/port, slug regexes).
- `src/mm_agent/eventview_source.py`: public reads only (Gamma `/events`, companions via `/events/keyset?game_id=`, Kalshi
  `/events/{ticker}`, Limitless `/markets/{slug}`). A failing companion call still renders the main event.
- `GET /v1/event/view?venue=&id=|slug=|url=` in `control_api.py`, auth required; 400 bad input, 404 missing/disabled, 502 venue error.
  An unknown shape returns `generic_list`, never 404. `/v1/discover`, `/v1/market`, `/v1/live/event` untouched.
- `LIVE_VIEWS_ENABLED` setting (default true) + `.env.example`.
- Score orientation fixed in the new path: home/away from `teams[].ordering`; no ordering -> first team = home, `game.orientation="assumed"`.
- 33 new tests in `tests/test_eventview.py`; gate green (ruff, format, mypy, pytest).
Decisions (log): open non-game events get status `live` (= trading); sibling `chips` and `related` stay EMPTY until the live probe confirms
the Gamma link fields (builder accepts `siblings=`/`related=`); item 4 (move discovery to `/events/keyset` + `version=v1&version=v2`) NOT done:
P0 was not verified live (LIVE_VERIFIED=false), so the old `/events` path stays. Row side objects carry an extra `outcome` (YES|NO) so a
team row in a 2-way game buys the right outcome. Kalshi sports (milestones) and Limitless history are P2b.
Gotchas: the keyset/`game_id` response shape, the logo field name (tries logo/logoUrl/image/icon) and `sportsMarketType` values are
still unverified; everything is tested against synthetic fixtures only.
Next step: P2a (live hub + Polymarket feeds + `/v1/stream`). Owner: run the P0 probe on a real machine first if possible.

## Handoff - P2a (live hub + Polymarket feeds + /v1/stream)
Done:
- New package `src/mm_agent/live/`: `hub.py` (ref-counted upstream, latest-value store, coalescing to <= 4 patches/s per event,
  `seq` + `source` + `source_ts` + `server_ts` on every frame, bounded per-client queue that drops oldest patches and never
  snapshots, subscription caps, idle-client eviction, stale handling with REST fallback and a fresh snapshot on recovery,
  REST budget per tick); `polymarket_market_ws.py` (subscribe/add/remove frames, `PING` every 10 s, local book per token,
  size "0" removal, `last_trade_price`, `tick_size_change`, `best_bid_ask`, `new_market`, `market_resolved`, backoff with jitter,
  books cleared on every drop); `polymarket_sports_ws.py` (`ping`/`pong`, index by `gameId`, join by team names, period labels);
  `polybolt.py` (feed + `CryptoPrices`: Coinbase unless a PolyBolt auth frame is injected); `history.py`
  (`/batch-prices-history`, <= 20 tokens per call); `service.py` (`build_hub`).
- `WS /v1/stream` in `control_api.py` (same auth as `/v1/ws`, `/v1/ws` unchanged; closes 4401 unauthenticated, 4404 when
  `LIVE_VIEWS_ENABLED=false`). Client frames: subscribe | unsubscribe | snapshot. Server frames: hello, snapshot, patch, status, error.
  `/status` and `/v1/status` gain `live`: clients, events, dropped, stale_events and per-feed
  `connected, reconnects, msgs_per_s, last_msg_age_s, subs, dropped`.
- `main.py` builds the hub (feeds only when `LIVE_VIEWS_ENABLED`), starts it in startup, stops it first in shutdown.
- Polymarket rows in EventView now carry `token_ids` [yes, no] (display data; the hub follows the YES token and derives NO).
- `scripts/live_hub_check.py`: read-only run against the real servers.
- 38 new tests (`tests/test_live_feeds.py`, `tests/test_live_hub.py`), fake local WebSocket servers only. Gate green:
  ruff, ruff format --check, mypy src, pytest (426 -> 464 tests collected, all pass).
- A test caught a real bug: when the view lists the teams the other way round from the sports feed, the score patch used the
  feed's home/away labels instead of the view's. Fixed (score follows the team names the feed sent; patch labels follow the view).
Not done / LIVE_VERIFIED=false:
- Nothing here has met a real Polymarket server (sandbox has no route to it). Every frame name and field is from the docs and tested
  against fakes only. Run `python scripts/live_hub_check.py --slug <event-slug>` once on a real machine and compare with
  `00_CONTEXT.md`; record differences in `DOCS_FINDINGS.md`.
- PolyBolt is not wired: it needs CLOB API credentials, which only `*_signing` modules may read, and its auth-frame layout is unverified.
  `CryptoPrices` therefore reports `source: "coinbase"`. Wiring is a small follow-up that injects `auth_frame` from the signing module.
- Kalshi and Limitless have no feed (P2b): their events are REST-polled every 5 s per event (budgeted) and say `source: "rest"`.
- History interval names `1m` (month) and `all` for the longer ranges are from the docs, unverified.
- Sibling chips / related list are still empty (P1 gap). The PWA and Android do not use `/v1/stream` yet (P3/P4).
Decisions: hub follows YES tokens only (NO = 1 - YES, the same complement the REST builder uses); a client that subscribes while
the feed is down gets `stale: true` until the first book arrives; seq is per event (not per client); a hub passed to `create_app`
is not started by it (main does), a hub it builds itself is REST-only and started lazily on the first stream connection.
Gotchas: the market feed stays disconnected until something is watched and reconnects on its own; `Hub.start()` is idempotent and
restarts dead tasks. Protocol session rule "never push" was overridden by the owner for this session (push to PR #33's branch).
Next step: owner runs `live_hub_check.py` and the P0 probe on a real machine; then P3 (PWA) which consumes `/v1/stream`
(P2b Kalshi/Limitless feeds can come before or after).
