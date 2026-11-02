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
- Kalshi and Limitless have no feed (P2b): their events are REST-polled every 5 s per event (budgeted) and say `source: "rest"`. (Superseded in P2b: both have feeds; Kalshi scores are polled, see Handoff - P6 audit.)
- History interval names `1m` (month) and `all` for the longer ranges are from the docs, unverified.
- Sibling chips / related list are still empty (P1 gap). The PWA and Android do not use `/v1/stream` yet (P3/P4).
Decisions: hub follows YES tokens only (NO = 1 - YES, the same complement the REST builder uses); a client that subscribes while
the feed is down gets `stale: true` until the first book arrives; seq is per event (not per client); a hub passed to `create_app`
is not started by it (main does), a hub it builds itself is REST-only and started lazily on the first stream connection.
Gotchas: the market feed stays disconnected until something is watched and reconnects on its own; `Hub.start()` is idempotent and
restarts dead tasks. Protocol session rule "never push" was overridden by the owner for this session (push to PR #33's branch).
Next step: owner runs `live_hub_check.py` and the P0 probe on a real machine; then P3 (PWA) which consumes `/v1/stream`
(P2b Kalshi/Limitless feeds can come before or after).

## Handoff - P3 (PWA live views driven by /v1/stream)
Done:
- ES modules under `src/mm_agent/static/js/`: `live.js` (pure snapshot/patch reducer with seq-gap resync, shared `LiveClient` for `/v1/stream`:
  reconnect with jittered backoff, resubscribe, idle liveness probe via a `snapshot` request, 4401 = signed out / 4404 = disabled stop retries,
  REST fallback `/v1/event/view` every 5 s only while the socket cannot deliver, kept chart points across REST snapshots), `chart.js`
  (multi-line SVG, ranges 1H/6H/1D/1W/1M/ALL via a `snapshot` op, top-4 legend, hover tip, midpoint series), `view.js` (shell + LIVE / DELAYED / OFFLINE
  indicator with data age and source on every view), `views/{game,bracket,candidates,updown,generic}.js` + `views/common.js`, `main.js` (`window.MMLive`),
  `css/live.css`. Layout is chosen from `view.layout` only; unknown layout -> generic list. Crests fall back to an initials badge; only https logos.
- Every layout renders from the EventView: game tabs from `sections` (no empty tab), Home/Draw/Away heroes, bracket rows with 24h change and Buy Yes/Buy No
  at the ask, candidates with logos, Up/Down with price-to-beat, live spot and countdown (the only timers: a 1 s UI clock; nothing fetches in it), sibling
  chips and related list (still empty until the live probe confirms the Gamma link fields). Row Yes/No opens the existing trade sheet pre-set to that outcome
  (`mmTrade` -> `TPREF` + `openMarket`); no order code touched.
- `index.html`: market detail `#evPanel` mounts the live view for sport / multi / crypto events; the old polling views (`spInit`, `muInit`, `cpInit`) now
  run only when `LIVE_VIEWS_ENABLED=false` (stream closes 4404 -> `onUnavailable` -> legacy) or `m.event_id` is missing. Pasting a polymarket.com /
  kalshi.com / limitless.exchange event link into the Discover search opens its view in a full-screen sheet (`/v1/event/view?url=`).
- Service worker cache bumped to v7 with the new assets; `pyproject.toml` package-data now includes `static/**/*` (the Docker image uses `pip install .`,
  so the sub-folders would have been missing from it).
- Tests: `tests/js/*.test.mjs` (24 node:test cases: reducer, gap/old seq, game/status/spot/chart patches, indicator flips when the stream is cut and recovers,
  reconnect + resubscribe, 4404/4401, slug-vs-canonical id, all six layouts rendered from views delivered by a fake WebSocket, escaping, formatting) run
  through `tests/test_live_js.py` (skipped when node is missing) using EventViews built by the real Python builders. Gate green: ruff, format, mypy, pytest.
  Also checked once by hand in jsdom: mount, patch, trade click, LIVE -> OFFLINE on a cut socket for all six layouts (not committed).
Not done / deviations (log):
- The discover list cards still use the legacy REST poll `dvSportTick` (8 s) and the ticket ladder keeps `tkLive` (2 s, `/v1/book`): the hub only carries
  top of book, the ladder needs depth, and list cards would need their own stream subscriptions. Both are fallbacks of the old UI, not of the new views.
  Candidate follow-up: subscribe visible sport cards through `MMLive.client` and drop `dvSportTick`.
- Legacy `svgLines`/`mkChart` timers remain for non-live events (binary markets, flag off).
- Kalshi / Limitless events render and update via the REST poll (hub `source: "rest"` -> indicator DELAYED) until P2b adds their feeds. (Superseded: P2b added their feeds.)
- Not run in a real browser or against real venues (LIVE_VERIFIED=false still applies); only jsdom + fakes. Owner: open a soccer game, a bracket market
  and an Up/Down window once on the deployed PWA and watch the indicator.
Decisions: sports indicator is LIVE while the stream delivers and always shows the vendor "may be delayed" note; REST fallback never faster than 5 s.
Next step: P4 (Android) or P2b; before either, the owner runs `scripts/probe_live_shapes.py` and `scripts/live_hub_check.py` on a real machine.

## Handoff - P2b follow-up (tests green)
- `f885050` was pushed with `tests/test_live_venue_feeds.py` "not yet run to green". Fixed in `6c107e6`: the fake Kalshi server awaited its script
  inside its own receive loop (deadlock when the script waits for a client frame such as `get_snapshot`) - it now runs as a task; the Limitless AMM test
  expected `ignored == 4`, the code correctly counts 3 (an update for an unwatched market is skipped silently). Full gate green, 486 tests.
- Not pushed from the interrupted session: hub-level multi-venue tests (`tests/test_live_hub_venues.py`: Kalshi/Limitless feeds wired through the hub,
  per-venue staleness, `(venue, asset)` index, `on_settled`). Still to write.

## Handoff - P4 (Android event views)
Done (written; the Android UI layer is NOT compiled, see below):
- `data/EventStore.kt`: pure snapshot/patch reducer on org.json mirroring `static/js/live.js` (deep row merge, game/status/spot/freshness/chart patches, chart cap 2000,
  old seq ignored, gap -> RESNAPSHOT, REST snapshot keeps chart points) and `LiveIndicator` (LIVE / DELAYED / OFFLINE, source, age, vendor-delay note for games).
- `data/LiveStream.kt`: OkHttp client for `/v1/stream` (Bearer): jittered backoff 1..15 s, resubscribe on open, idle snapshot probe (45 s, 10 s reply), 4401 -> `auth`, 4404 -> `disabled`, no retry.
- `data/EventViewModel.kt` + `data/EventTarget.kt`: follows one event; frames applied on the main thread; `/v1/event/view` fallback never faster than 5 s and only while the socket cannot deliver;
  a pasted link is read once by URL, then followed by the venue/event id the server resolved; `EventTarget.fromPaste` accepts https links to polymarket.com / kalshi.com / limitless.exchange only.
- `ui/event/`: `EventScreen` (layout dispatch from `view.layout`, unknown -> generic), `GameScreen` (scoreboard, Home/Draw/Away found by name, tabs only for non-empty sections), `BracketScreen`,
  `GenericScreen` (candidates, Up/Down with spot and price to beat, binary, generic), shared `EventCommon` (LiveBadge, Crest with initials fallback and https-only logos, Yes/No at the ask,
  Canvas multi-line chart with range chips). Entry: tap a Discover card, or paste a venue link in the Markets search. Yes/No opens the existing trade sheet; Back from it returns to the event.
- Tests: `EventStoreTest.kt` (16 cases). Run for real on a plain JVM with kotlinc 2.0.21 + org.json 20240303 and a tiny JUnit shim: 16 passed. That run also caught a real build break
  (`/v1/orders/*` inside a KDoc opens a nested comment in Kotlin) - fixed.
Not done / deviations:
- Not compiled with Gradle (no Android SDK or Maven access in the sandbox): `LiveStream`, `EventViewModel`, all Compose screens and the MainActivity wiring are syntax-checked only (kotlinc parse, no errors).
  First real build may need small import fixes. Owner: `gradle -p android testDebugUnitTest assembleDebug`.
- Candidates and Up/Down use the shared `GenericScreen` list (PWA has dedicated layouts); no tab/crest image cache beyond Coil defaults; chart has no hover tip; sibling chips / related list absent (server still sends none). (Superseded in P6: `CandidatesScreen` and `UpDownScreen` exist, not compiled.)
- `AUTO_REFRESH_MS` untouched (it is the account safety net, not an event poller).
- PriceButton in `DiscoverScreen.kt` changed from private to internal (reused for the Yes/No buttons).
Next step: P5 (hardening, load test, docs) - and first the owner builds the Android app once and runs the live probes (`scripts/probe_live_shapes.py`, `scripts/live_hub_check.py`).


## Handoff - P5 (hardening, load test, docs)
Done:
- Security review of `/v1/stream` and `/v1/event/view` (notes in `SECURITY.md`). Auth, frame size, venue/id allow-list, URL allow-list and
  error text were already fine. Four real gaps fixed: (1) a client's non-patch frames (errors, snapshots, status) were unbounded
  -> hard cap of 512 queued frames, over it the client is closed (4408); (2) every uncached subscribe cost the venue a REST lookup with no
  budget -> 30 lookups/min per socket (`rate_limited`); (3) the per-id lock table grew with every invented id -> released after each lookup;
  (4) no limit on sockets per account -> 8 per user, close code 4429. Also: stale/live status frames are coalesced per event for slow readers.
- Venue 429 / failures: the hub refreshed stale events over REST every 2 s whatever the venue answered. Now a failing venue is paused for
  `Retry-After` (read from `exc.retry_after` or `exc.response.headers`, max 120 s) else 4 s doubling to 30 s; a success clears it.
- Tests: `tests/test_live_hardening.py` (flood, lookup budget, shared lookups free, lock table, 16 malformed frames, silent feed ->
  stale -> recovery, reconnect storm with a reading and a never-reading client, slow reader sweep, 429 pause + resume, backoff, socket cap
  4429, no secrets in error frames). Gate green twice: ruff, format, mypy, pytest.
- `scripts/live_load_test.py` (in-process, no network). Run here: 200 clients (175 normal, 20 slow, 5 stuck) on 50 events, 3 events each,
  20 s, ~1530 quotes/s in: 2060 patches/s out (8.9 quotes folded into one patch per subscriber), peak 2192 patches/s vs limit 2340,
  max 64 frames queued in any client, 5/5 stuck clients evicted (idle limit shortened to 3 s for the test), CPU 24.5% of one core,
  RSS 126 -> 131 MB and flat after disconnect, hub empty afterwards. Hub-internal latency quote -> client: p50 51 ms, p95 156 ms
  (bounded by the 250 ms flush). These are sandbox numbers for the hub only: no network, no venue, one Python process.
- REST calls per minute, computed from the timers in `index.html` (not measured): before, one open sport game 5 s poll + 30 s chart = 14/min;
  a multi/bracket event 10 s + 45 s = 7.3/min; crypto Up/Down 2 s poll = 30/min; Discover sport cards 8 s tick = 7.5/min per batch;
  an open trade ticket adds 30/min (`tkPoll`, still there). After, an open event is 1 WebSocket snapshot + 1 history request on the server for
  everyone following it, 0 REST/min while the feed delivers; REST fallback never faster than 5 s per client (PWA/Android) and the hub's own
  stale refresh is 2 s, now with backoff. Ticket ladder and Discover list cards still poll (see P3 handoff).
- Docs: README (venue table row for history, live-stream section with feed table, limits and close codes, `LIVE_VIEWS_ENABLED`),
  `DOCS_FINDINGS.md` section 8, `SECURITY.md` live-routes section.
Not done / gaps:
- Nothing was run against a real venue or a real browser (`LIVE_VERIFIED=false`). Measured latency venue event -> client paint does not
  exist; only the hub-internal figure above. The 429 handling is tested with a fake exception, not a real 429.
- Android UI layer (P4) is still not compiled with Gradle.
- `/v1/event/view` has no per-user rate limit of its own.
- `tests/test_live_hub_venues.py` (multi-venue hub tests) is still unwritten. (Written in P6.)
- Socket release after a client leaves was seen working in isolation, but the TestClient did not reliably show it in the full suite, so
  the test only asserts the 4429 rejection.
Owner must still run once: `gradle -p android testDebugUnitTest assembleDebug`; `scripts/probe_live_shapes.py` and `scripts/live_hub_check.py`
on a real machine; open a soccer game, a bracket and an Up/Down on the deployed PWA and watch the indicator; `scripts/live_load_test.py`
on the production-size host. Also revoke the GitHub token pasted in chat.
Next step: owner runs the items above and reports back; then fix what the real build/probes show. STOP.


## Handoff - P6 audit (2026-11-02, PR #33)

Audit of docs/live-views P0-P5 against code, tests and docs. How each fact is known: **fakes** = tests with fake servers or fake
adapters (no network); **docs** = from venue documentation or synthetic fixtures only; **live** = seen on a real venue (nothing is).
`LIVE_VERIFIED=false`: no venue was reachable from this sandbox, so every venue shape stays unverified. No Android SDK, Gradle or browser here.
Baseline before the audit: ruff, format, mypy clean, 514 tests passed. After: 590 Python tests + 25 JS tests pass, gate green on every commit.

Status words: done / partial / missing / cannot verify here. "(fixed in P6)" marks gaps this audit closed.

| Item | Spec says | Status | Evidence |
|---|---|---|---|
| P0 probe script | read-only async httpx, scrubbed JSON, findings summary | done (never run live) | scripts/probe_live_shapes.py, tests/test_probe_live_shapes.py; Kalshi `milestones_for_event` capture added in P6 |
| P0 capture list | real shapes for soccer + companions, NBA, tennis, MMA, esports, bracket + siblings, candidates, mentions, weather, yes/no, Up/Down 5m and 1h, events vs keyset, Kalshi sports/milestone/live_data/range/binary, Limitless group/CLOB/recurring | partial | 20 synthetic fixtures, 0 real; tennis, MMA, esports, mentions, weather, Kalshi sports/binary/live_data, Limitless CLOB/recurring added in P6 (tests/test_live_fixture_layouts.py); still no synthetic Up/Down 1h, bracket sibling events or Kalshi milestone object |
| P0 DOCS_FINDINGS | answer the checklist (sibling fields, logo field, version, sportsMarketType, WS asset cap, PolyBolt auth, Limitless intervals, Kalshi milestone link) | cannot verify here | sections 7 and 9 list what to confirm; no answers possible offline |
| P0 no-network fallback | synthetic fixtures marked synthetic, LIVE_VERIFIED=false | done | every fixture has `"synthetic": true`; test_every_new_fixture_is_marked_synthetic_and_scrubbed, test_fixture_directory_has_nothing_unaccounted_for |
| P1 builders + 7-rule chooser | pure, Decimal-as-string, three venues | done (fakes/docs) | src/mm_agent/eventview.py; test_layout_per_fixture, test_prices_are_decimal_strings |
| P1 sports sections, companions, score orientation | companions by game_id, label map + prettify, `teams[].ordering` | done (fakes/docs); logo field name unverified | eventview_source.py (keyset only for game_id); test_soccer_score_follows_ordering_not_list_order, test_two_way_outcome_order_differs_from_home_away, test_unknown_market_type_gets_its_own_prettified_tab |
| P1 brackets, chips, related | ascending rows; chips and related from confirmed fields | partial | test_bracket_rows_ascending_and_chart_shell; builder makes chips (test_bracket_chips_from_siblings) but the source passes no siblings and no related list, so the server sends none (field not confirmed) |
| P1 discovery on keyset + version | `/events/keyset` with `version=v1&version=v2`, old path as fallback | missing (logged, not done) | discover.py and venues/polymarket.py still use `/events`; keyset reply shape unverified, a guess could break Discover. Probe captures both for the owner |
| P1 Kalshi / Limitless rows | strike/range rows in contracts; group children as rows | done (fakes/docs) | test_kalshi_ladder_in_contracts, test_limitless_group_children_are_rows |
| P1 /v1/event/view + URL parser | host allow-list, slug regexes, unknown layout never 404 | done (fakes) | test_parse_event_url_accepts/rejects, test_route_*, test_unknown_shape_is_generic_list_and_never_raises |
| P1 old routes unchanged | /v1/discover, /v1/market, /v1/live/event | done | existing suites still green |
| P1 tests | one per fixture, orientation, ordered brackets, shuffled/stripped, unknown shape, URL parser | done | tests/test_eventview.py, tests/test_live_fixture_layouts.py (stripped-field robustness for the new fixtures too) |
| P2a hub | ref-counted, 4/s coalescing, seq/source/source_ts/server_ts, bounded queues, caps, idle eviction, stale + REST fallback, backoff | done (fakes) | tests/test_live_hub.py, test_live_hardening.py (coalescing, ref count, slow client, stale/recovery, 429 pause) |
| P2a Polymarket market WS | book from book + price_change, size 0 removes, last_trade/tick_size/best_bid_ask/new_market/market_resolved, PING 10 s | done (fakes) | tests/test_live_feeds.py (size-zero removal, ping/pong, reconnect, tick size, resolved) |
| P2a sports WS | ping/pong, index by gameId, join by id and names, home/away, periods | done (fakes) | test_sports_ping_pong_and_home_away_score_orientation, test_game_score_follows_the_names_the_sports_feed_sends |
| P2a PolyBolt | only with CLOB creds, else Coinbase labelled `coinbase` | partial | polybolt.py exists but is not wired (needs credentials only the signing modules may read; auth frame unverified). Source label pinned in P6: tests/test_live_hub_spot.py (fixed in P6) |
| P2a charts | batch-prices-history, at most 20 tokens, live tail | done (fakes) | test_history_is_fetched_in_chunks_of_twenty_and_a_failed_chunk_is_skipped, test_chart_history_then_live_tail |
| P2a /v1/stream, /status, flag | same auth as /v1/ws, frames, seq gap -> snapshot, per-feed metrics, LIVE_VIEWS_ENABLED | done (fakes) | test_stream_*, test_status_reports_per_feed_metrics; close codes 4401/4404/4408/4429 in control_api.py |
| P2a against real feeds | how to run the hub on the real feeds | cannot verify here | scripts/live_hub_check.py exists, never run |
| P2b Kalshi WS | signed handshake, ticker/trade/orderbook_delta, local book, add/remove, get_snapshot after gap, lifecycle, no key -> unavailable | done (fakes) | tests/test_live_venue_feeds.py (kalshi_*), service.py `unavailable` |
| P2b Kalshi sports poll | GET /live_data/batch, at most 100 ids, every 2-3 s, source `kalshi_rest_polled` | done in P6 (fakes; reply layout docs only) | src/mm_agent/live/kalshi_sports.py, hub.on_kalshi_game, eventview_source milestone lookup; tests/test_live_kalshi_sports.py (38 tests) |
| P2b Kalshi play-by-play | `game_stats` where supported | missing | not built; endpoint unverified |
| P2b Kalshi charts | batch candlesticks | done (fakes) | test_kalshi_history_batches_tickers_and_uses_the_range |
| P2b Limitless WS | Socket.IO /markets, subscribe_market_prices, orderbookUpdate, lifecycle | done (fakes) | test_limitless_* in tests/test_live_venue_feeds.py |
| P2b Limitless navigation endpoints | replace the `categories[0]` trick if P0 confirmed them | missing (P0 confirmed nothing) | discover.py:228, venues/limitless.py:175,322 unchanged; recurring markets via Market Timeline also not built |
| P2b Limitless charts, README claim | historical-price; fix "no history endpoint" | done | test_limitless_history_*; README and DOCS_FINDINGS section 8 corrected |
| P2b hub-level tests | Kalshi/Limitless through the hub, per-venue staleness, (venue, asset) index, on_settled | done in P6 (fakes) | tests/test_live_hub_venues.py (11 tests) |
| P2b no credential in payloads or logs | secrets stay out | done (fakes) | test_kalshi_secrets_never_travel_in_payloads_or_logs; poller logs the error class only (test in test_live_kalshi_sports.py) |
| P3 modules + CSS | live.js, chart.js, views/*, CSS file | done | static/js/{live,chart,view,main}.js, views/*, static/css/live.css |
| P3 every layout renders from the stream | crests with initials, bracket rows, legend, chart ranges | done (fakes; no browser) | tests/js/views.test.mjs (6 layouts from a fake stream); Kalshi score line on candidates added in P6 |
| P3 sibling chips, related list | rendered | partial | PWA draws them when sent; server sends none (see P1) |
| P3 remove per-view setIntervals | none left except UI clocks and the REST fallback | partial (justified deviation) | `dvSportTick` (8 s, index.html about 626) and `tkPoll` (2 s ticket ladder) remain, plus legacy muLive/spLive/cpTick used only when LIVE_VIEWS_ENABLED is off. Not moved: the hub allows 12 subscriptions per socket and 30 new lookups per minute per socket, a Discover list shows more cards than that and each uncached card would cost the venue a lookup; the ladder needs book depth the stream does not carry |
| P3 indicator, delayed note, trade sheet, flag | LIVE/DELAYED/OFFLINE with age and source; Yes/No opens the sheet; respects LIVE_VIEWS_ENABLED | done (fakes; no browser) | tests/js/live.test.mjs, view.js, index.html flag comment |
| P3 paste a venue URL | opens via /v1/event/view | done | static/js/main.js, test_route_accepts_pasted_url_and_rejects_foreign_hosts |
| P3 done-when | PWA loads and signs in | cannot verify here | no browser; owner check below |
| P4 stream + reducer | LiveStream.kt, EventViewModel, pure reducer, unit tests | done, not compiled | data/LiveStream.kt, EventViewModel.kt, EventStore.kt; EventStoreTest.kt written, never run by Gradle |
| P4 screens | Game, Bracket, Candidates, UpDown, Generic | done in P6, NOT COMPILED | ui/event/*.kt; Candidates and UpDown were missing (shared GenericScreen). Helpers in data/EventLayout.kt: EventLayoutTest (9 tests) compiled with kotlinc 2.0.21 and passed on a plain JVM against shims of org.json and JUnit, not the real libraries; the Compose files only parse (no syntax errors) |
| P4 Up/Down details | countdown, price to beat vs spot, Up/Down at the ask | partial | "next window" chips not built on Android (PWA has them) |
| P4 crests, indicator, trade sheet, URL, AUTO_REFRESH_MS | Coil with initials, LIVE/DELAYED/OFFLINE, preset sheet, paste URL, 5 s safety net | done, not compiled | EventCommon.kt, EventTarget.fromPaste, MainActivity.kt (AUTO_REFRESH_MS = 5000) |
| P4 done-when | compiled where a toolchain exists | cannot verify here | no Android SDK or Gradle |
| P5 load test | about 200 clients on 50 events, CPU and memory | done (in-process, no network) | scripts/live_load_test.py; numbers in the P5 handoff |
| P5 REST calls before/after | numbers in WORKLOG | partial | computed from timers, not measured (P5 handoff); Discover cards and ticket ladder still poll |
| P5 failure drills | silent feed, reconnect storm, 429 Retry-After, malformed frames | done (fakes) | tests/test_live_hardening.py |
| P5 security review | auth, allow-list, no secrets, caps | done | SECURITY.md live section (updated in P6 with close codes and the new Kalshi reads) |
| P5 docs | README table, live section, endpoints, Limitless claim, flag; DOCS_FINDINGS | done | README, DOCS_FINDINGS sections 7-9; synced to the code in P6 (close codes 4401/4404/4408/4429, limits, LIVE_VIEWS_ENABLED, feed names) |
| P5 measured latency venue -> client paint | final report | cannot verify here | only the hub-internal p50 51 ms / p95 156 ms exists |

### Commits made in P6 (all on feat/live-category-views, gate green after each)
1. `8606ed8` P2b hub-level Kalshi/Limitless tests. 2. `63dd8d9` P0 second synthetic fixture set + layout tests.
3. `8354f87` P2b Kalshi score poller (`/live_data/batch`, `kalshi_rest_polled`), hub hook, milestone lookup, probe capture.
4. `0236eb2` P4 CandidatesScreen + UpDownScreen (not compiled). 5. `ca18e3d` P2a spot-source tests (PolyBolt gap pinned).
6. `2438d33` one-line score on multi_candidates (PWA + Android), service worker cache v8. 7. docs sync. 8. this handoff.

### Findings worth knowing
- `label_number` needs a non-letter after the digits: `49F` gives no threshold (candidate list), `49°F` works (bracket). Real labels unknown.
- Section id/label `game_lines` / "Game Lines" is used for non-game events too (bracket, mentions); the PWA and Android draw tabs only when there are several sections, so it is hidden today.
- structlog output goes to stdout, not the `logging` module, so a `caplog` assertion about logs proves nothing; the new tests read stdout.
- The Polymarket sports feed used to write into any event with a game block; it is now limited to Polymarket events so Kalshi scores keep their own source.
- Kalshi score line is shown on candidates only (the Kalshi sports layout today); the milestone link and the reply layout are docs-only guesses.

### Owner must still run
- `gradle -p android testDebugUnitTest assembleDebug` (nothing in android/ was built here).
- `python scripts/probe_live_shapes.py` and `python scripts/live_hub_check.py` on a machine that reaches the venues; replace the synthetic fixtures with the scrubbed real ones; check `/live_data/batch` parameter style (comma-joined vs repeated) and `/milestones?related_event_ticker=`; then set `LIVE_VERIFIED` only if they match.
- PWA by hand: a soccer game (score, tabs, delayed note), a bracket (ordered rows, chart ranges), an Up/Down (countdown, spot vs price to beat), a Kalshi sports event (score line); cut the network and watch the indicator flip.
- Revoke the GitHub token that was pasted into the chat; it was never used by this audit (GH_TOKEN was not set in the sandbox).


## Polymarket Data tab (read-only API gap work)

- Added `polymarket_explorer.py` (about 55 public rows: Gamma markets/tags/series/comments/sports/teams/profile,
  CLOB batch and variant prices, fee/tick/neg-risk, market info, time, health, sampling lists, rewards, rebates,
  Data combos/builders/accounting, relayer `/deployed`, geoblock) and `polymarket_account.py` (12 own-account reads).
- New routes: `/v1/polymarket/catalog`, `/explore/{key}`, `/account/{key}`, `/system`. UI: Markets > Data, plus
  Market insights in the market sheet. Service worker cache `mm-agent-v9`.
- Fixed: `caps.batch_orders` was advertised without code; now `False`.
- Tests: `tests/test_polymarket_explorer.py` (30 cases), `tests/js/explorer.test.mjs` (10), security canary.
- Not done: write routes, Perps, PolyBolt rewrite (F1), Android screen, live fixtures (sandbox cannot reach polymarket.com).
- Owner must run: `python scripts/pm_api_smoke.py`; open Markets > Data on a phone; revoke the GitHub token pasted in chat.
