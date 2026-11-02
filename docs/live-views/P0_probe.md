# P0 - Probe real API shapes (small session)
Follow the session protocol in `00_CONTEXT.md`. Do **only** this phase, then STOP.

## First actions
```bash
git checkout -b feat/live-category-views 2>/dev/null || git checkout feat/live-category-views
python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
ruff check src tests scripts && mypy src && pytest -q 2>&1 | tail -15     # write the baseline into WORKLOG.md
```
Read only: `README.md` (Safety model), `DOCS_FINDINGS.md`, `src/mm_agent/venues/polymarket.py` (lines around `discover`, `event_live`, `parse_gamma_market`), `src/mm_agent/discover.py` (`shape_sport`, `shape_event`).

## Build
`scripts/probe_live_shapes.py`: async httpx, read-only, public endpoints, 10 s timeout, polite rate, saves **scrubbed** JSON to `tests/fixtures/live/<venue>/<name>.json`, prints a findings summary.
Capture:
- Polymarket soccer 3-way game: main event + **every** companion event (`GET /sports` -> soccer tag -> `GET /events/keyset?tag_id=..&closed=false&limit=5` -> `GET /events/keyset?game_id=..`)
- NBA/NFL/MLB game (spreads/totals), tennis, MMA, esports match
- Bracket + siblings: `GET /events/slug/anthropic-ipo-closing-market-cap-higher-strikes` and events sharing its tags/series
- Candidates list, a "mentions" event, weather temperature brackets, a plain Yes/No, crypto Up/Down 5 min and 1 h (tag `crypto`, sub-tags `5m`, `1h`)
- `/events` vs `/events/keyset`, with and without `version=v1&version=v2`: compare counts and protocol versions
- Kalshi: sports event + milestone + `/live_data/batch`; a range-bracket event; a binary (`/events?with_nested_markets=true&status=open`, `/milestones`)
- Limitless: group, CLOB and recurring market; `/markets/{slug}/historical-price` for each (`/markets/active?limit=25`)

## Record in `DOCS_FINDINGS.md` (and correct its outdated claims)
Which Gamma fields link sibling events and the related list; team logo field names; whether `/events` honours `version`; every `sportsMarketType` seen; max asset ids one market-WS connection accepts
(subscribe in growing batches); PolyBolt auth needs; Limitless history intervals and group behaviour; Kalshi milestone linkage; confirm or correct each [INFERRED] item from `00_CONTEXT.md` ("Verified gaps").

## No network?
Do not stall: write hand-built fixtures from the documented shapes, mark `"synthetic": true`, set `LIVE_VERIFIED=false`, and say in the summary which probe calls the owner must run once.

## Done when
Fixtures committed, `DOCS_FINDINGS.md` updated, quality gate green, Handoff block written. STOP.
