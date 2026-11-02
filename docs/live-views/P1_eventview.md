# P1 - EventView + layout chooser + /v1/event/view (REST only)
Follow `00_CONTEXT.md` session protocol. Needs P0 done (fixtures in `tests/fixtures/live/`). Do **only** this phase, then STOP.
Read only: `WORKLOG.md`, `DOCS_FINDINGS.md`, `src/mm_agent/discover.py`, `src/mm_agent/venues/polymarket.py` (`discover`, `event_live`, `parse_gamma_market`), the `/v1/discover`, `/v1/market`, `/v1/live/event` routes in `control_api.py`, `tests/test_discover.py`, `tests/test_live_views.py`.

## Build
1. `src/mm_agent/eventview.py` (pure, typed, Decimal-as-string): EventView builders for Polymarket, Kalshi, Limitless following the schema in `00_CONTEXT.md`; `choose_layout()` with the 7 rules.
2. Polymarket sports: fetch companion events by `game_id`; build `sections` from companion events and `sportsMarketType` (label map + prettify fallback); carry `teams[]` (name, abbr, `ordering`, logo field found in P0);
   **fix score orientation** through `teams[].ordering`, not "first outcome = first number". Replace `parse_score` usage in the new path.
3. Brackets: rows ascending by threshold; chips from sibling events and related list from tags, using the fields P0 confirmed (if none confirmed, leave empty and log it).
4. Discovery: move Polymarket listing to `GET /events/keyset` with `after_cursor` and `version=v1&version=v2` if P0 confirmed it; keep the old path as fallback. Kalshi: strike/range rows, volume unit "contracts". Limitless: group children as rows.
5. `GET /v1/event/view?venue=&id=|slug=|url=` in `control_api.py`. URL parser accepts only `polymarket.com/event/<slug>`, `kalshi.com/markets/<ticker>`, `limitless.exchange/markets/<slug>` (host allow-list, existing slug regexes). An open market must never return 404 because of an unknown layout.
6. Keep `/v1/discover`, `/v1/market`, `/v1/live/event` working unchanged.

## Tests
One test per fixture -> expected layout; soccer gives Home/Draw/Away with correct score orientation (include a case where outcome order differs from home/away); brackets ordered; shuffled and field-stripped fixtures still render;
unknown shape -> `generic_list`; URL parser rejects other hosts and bad slugs.

## Done when
All above pass, gate green, Handoff written. STOP.
