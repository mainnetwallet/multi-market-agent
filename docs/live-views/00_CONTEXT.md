# CONTEXT - read this first in EVERY session (live per-category market views)

Repo: `multi-market-agent` (Python 3.11 asyncio + FastAPI, PWA in `src/mm_agent/static/index.html`, Android in `android/`).
Work happens in small phases, one phase per session. Each phase has its own file (`P0_...` to `P5_...`).

## Session protocol (follow exactly, every session)
1. Read this file, then `WORKLOG.md` (if missing, create it), then the phase file you were given. **Do only that phase.**
2. `git status`; make sure you are on branch `feat/live-category-views` (create it if missing). Never push or merge.
3. Read only the files the phase lists. For big files (`index.html`, `control_api.py`, `chat.py`) use `grep -n` / `sed -n 'a,bp'`, never print the whole file.
   Keep command output short (`pytest -q 2>&1 | tail -30`, `git diff --stat`).
4. Implement, add tests, then run: `ruff check src tests scripts && ruff format --check src tests scripts && mypy src && pytest -q`.
5. Commit with a clear message. Append a **Handoff** block to `WORKLOG.md`: what is done, what is not, decisions made, gotchas, exact next step.
6. Print a summary of at most 10 lines and **STOP**. Do not start the next phase.
Do not ask questions. If something is ambiguous use "Default decisions" below, log the choice in `WORKLOG.md`, and continue.

## Mission
Every market category opens in its own native view like polymarket.com (soccer game with Home/Draw/Away, crests, score, tabs
Game Lines/Goalscorers/Exact Score/Halves/Players/Assists; bracket market like "Anthropic IPO Closing Market Cap" with ordered ranges,
24h change, Buy Yes/Buy No, multi-line chart, sibling chips, related list; crypto Up/Down; candidates; plain Yes/No). Data updates by **push**
(visible within about 1 s of the venue publishing it). Rendering is shape-driven: any opened market renders; unknown shape -> generic list.
Works for Polymarket, Kalshi, Limitless; where a venue lacks an API the UI says so. Rule seen on polymarket.com: **chance and chart use the midpoint; buttons show the ask.**

## Hard rules
- Never change order placement, risk, caps, funds, signing, idempotency or auth. Trading from new views uses existing `/v1/orders/*` and the existing trade sheet.
  Live-hub data is display only; submit-time validation keeps using fresh books (`agent.books_for(..., force=True)`). Fail closed stays.
- Prices/money are Decimal-as-string. Never float on the server.
- Secrets never reach clients, logs, fixtures or commits. A GitHub token was pasted in a chat: treat as leaked, never write it anywhere.
- No real order from code, tests, probes or scripts. Probes are read-only on public endpoints.
- Quality gate green at the end of every session. Prefer new modules; touch existing files minimally.

## Verified gaps in this repo
- Live updates are REST polling: PWA timers (sports 5 s, multi 10 s, crypto 2 s, ticket 2 s, charts 30-45 s, list cards 8 s); `Polymarket.subscribe_books`
  is `poll_books(interval=2.0)`; `/v1/ws` carries only account/order/proposal topics.
- Android has no sport/crypto/multi views (`grep -rn "sport\|crypto\|live/event" android/app/src/main/java` is empty).
- `discover.shape_sport()` only accepts a 2-outcome moneyline with team-named outcomes (`_NON_TEAM_OUTCOMES` has yes/no), so a 3-way soccer game
  (three Yes/No markets) very likely falls back to a plain `multi` list. Only the main event of a game is fetched (no companion events).
- `discover.parse_score` assumes the first number belongs to the first outcome (docs say score is "<home>-<away>").
- Discovery uses `GET /events` offsets, never sends `version`; Up/Down price comes from Coinbase REST; Kalshi uses only private WS channels;
  Limitless uses Socket.IO only for positions; README wrongly says Limitless has no history endpoint.

## Venue facts (from the venues' own docs; confirm on live responses in P0)
**Polymarket**
- Market WS `wss://ws-subscriptions-clob.polymarket.com/ws/market`. Subscribe `{"assets_ids":[...],"type":"market","custom_feature_enabled":true}`;
  add/remove `{"assets_ids":[...],"operation":"subscribe"|"unsubscribe"}`; send text `PING` every 10 s (server answers `PONG`). Events: `book`,
  `price_change` (`price_changes[]`, size "0" removes a level), `last_trade_price`, `tick_size_change`, and with the flag `best_bid_ask`, `new_market`, `market_resolved`.
- Sports WS `wss://sports-api.polymarket.com/ws`: no subscribe frame; server sends text `ping` every 5 s, reply `pong` within 10 s. Bare messages:
  `gameId, leagueAbbreviation, homeTeam, awayTeam, status, live, ended, score ("<home>-<away>"), period, elapsed`. Periods `1H 2H HT FT "FT OT" "FT NR"`, `End N`, `k/N`.
  Status strings are per sport, case-sensitive. Polymarket warns this data may be delayed or wrong.
- PolyBolt (replaces RTDS) `wss://ws-live-v2.polymarket.com/ws`: needs CLOB API creds (auth frame), then `price.crypto` `{symbol:"btcusd"}`; 2-minute snapshot; 64 subscriptions per connection.
- Gamma: `GET /events/keyset` (`after_cursor`, no `offset`), `tag_id`, `game_id`, `closed`, `version=v1&version=v2` (default is v1 only), `GET /events/slug/{slug}`, `GET /sports`,
  `GET /sports/market-types`, `GET /teams?league=`. Event `teams[]` has `ordering: home|away`. A game's markets are split over a main event and companion events
  joined by `gameId` (slug suffixes like `-more-markets`, `-halftime-result`, `-second-half-result`, `-exact-score`, `-first-to-score`, `-player-props`; discover, do not hard-code).
  Markets carry `sportsMarketType` (`moneyline`, `spreads`, `totals`, `first_half_totals`, `both_teams_to_score`, `soccer_team_totals`, ...).
- CLOB `POST https://clob.polymarket.com/batch-prices-history` `{markets:[token ids, max 20], interval: max|all|1m|1w|1d|6h|1h, start_ts?, end_ts?, fidelity?}` -> `{history:{<token>:[{t,p}]}}`.
- Rate limits (queued, not rejected): Gamma `/events` 500/10 s, listings 900/10 s; CLOB `/prices-history` 1000/10 s, `/book` 1500/10 s; Data v2 `/v2/prices-history` 200/10 s.

**Kalshi**: WS `wss://external-api-ws.kalshi.com/trade-api/ws/v2` (demo `external-api-ws.demo.kalshi.co`). Handshake needs signed API-key headers even for public channels.
Public channels `ticker`, `trade`, `market_lifecycle_v2`, `multivariate_market_lifecycle`, `multivariate`; `orderbook_delta` needs a market list, sends `orderbook_snapshot` then deltas,
supports `update_subscription` (`add_markets`, `delete_markets`, `get_snapshot`) and `use_yes_price`. Sports REST: `GET /live_data/milestone/{id}`, `GET /live_data/batch` (max 100 ids),
`GET /live_data/milestone/{id}/game_stats` (play-by-play). Volume is in contracts, not dollars.

**Limitless**: REST `https://api.limitless.exchange` (OpenAPI `/api-json`), WS `wss://ws.limitless.exchange` Socket.IO namespace `/markets`: `subscribe_market_prices` `{marketSlugs:[...]}`
(public; AMM prices + CLOB book snapshots; event `orderbookUpdate`), `subscribe_market_lifecycle`. REST `GET /markets/{slug}/historical-price` (YES series, newest first; confirm intervals),
Market Timeline, Navigation Tree / Market Page by Path / List Markets for a Page, Oracle Candlesticks. `/markets/active` rejects `limit` > 25.

## EventView schema (one for all venues and both clients)
```jsonc
{ "venue":"", "event_id":"", "slug":"", "url":"", "title":"", "category":{"id":"","path":[]},
  "layout":"game_3way|game_2way|bracket_ladder|multi_candidates|updown_window|binary|generic_list",
  "image":"", "volume":"", "volume_unit":"usd|contracts", "start_time":"", "end_time":"",
  "status":"scheduled|live|ended|closed|resolved",
  "game":{"game_id":0,"league":"","period":"","elapsed":"","live":true,"ended":false,
          "home":{"name":"","abbr":"","logo":"","score":0},"away":{"name":"","abbr":"","logo":"","score":0},
          "source":"","source_ts":""},
  "sections":[{"id":"","label":"","rows":[]}],
  "chart":{"series":[{"row_id":"","label":"","color":"","points":[[0,"0.35"]]}],"ranges":["1H","6H","1D","1W","1M","ALL"],"default":"1D","point_rule":"midpoint"},
  "chips":[{"id":"","label":"","event_ref":""}], "related":[{"event_ref":"","title":"","chance":"","image":""}],
  "freshness":{"price_ts":"","source":"ws|rest","stale":false}, "raw_extra":{} }
```
`Row` = `{row_id, market_id, label, sub_label?, logo?, chance, change_1d, yes:{label,ask,bid}, no:{label,ask,bid}, volume, closes, status, line?}`; every row keeps the `market_id` the trade sheet needs.

## choose_layout (decide from data, never from category name or URL; first match wins)
1 `updown_window` (existing `_CRYPTO_SLUG` logic or Up/Down outcomes) - 2 `game_3way` (moneyline = three Yes/No markets) - 3 `game_2way` (two team outcomes) -
4 `bracket_ladder` (several markets with unique numeric thresholds, ascending) - 5 `multi_candidates` (several Yes/No without thresholds, chance desc) - 6 `binary` - 7 `generic_list` (always renders).
Game sections come from companion events + each market's `sportsMarketType` (label map with a prettify fallback). Never show empty tabs.

## Default decisions (never ask)
Unknown shape -> `generic_list`. Missing logo -> initials badge. Missing history -> "history not available for this venue", no fake line. Update cap 4/s per subscription.
REST fallback poll no faster than 2 s per event and under 50% of a venue's documented limit. Kalshi volume stays "contracts". If a live response disagrees with these docs, trust the
live response and record it in `DOCS_FINDINGS.md`. Feature flag `LIVE_VIEWS_ENABLED` (default true) keeps old views as fallback. If the sandbox has no venue network access, use hand-built
fixtures marked `"synthetic": true`, set `LIVE_VERIFIED=false` in `WORKLOG.md`, and list in the final summary what the owner must verify once on a real machine.

## Stop early only if
the baseline gate cannot run at all; a change would touch order/risk/funds/signing; a secret would be written to disk or sent to a client; a probe would place an order.
