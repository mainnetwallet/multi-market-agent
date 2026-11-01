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
