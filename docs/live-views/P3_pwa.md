# P3 - PWA views driven by /v1/stream
Follow `00_CONTEXT.md` session protocol. Needs P2a (P2b optional). Do **only** this phase, then STOP.
If the phase is too big for one session: do `game` + `bracket` first, log the rest in `WORKLOG.md` Handoff, commit and stop.
Read only: `WORKLOG.md`; in `src/mm_agent/static/index.html` use `grep -n` to locate and read just `dvSportTick`, `muInit`, `spInit`, `cpInit`, `svgLines`, `openMarket`, the market-detail markup and CSS variables.

## Build
- Split view code into ES modules under `src/mm_agent/static/js/`: `live.js` (WebSocket client, reconnect, snapshot/patch reducer), `chart.js`, `views/{game,bracket,candidates,updown,generic}.js`, plus a CSS file. Keep PWA behaviour and sign-in working.
- Render every layout from the EventView; game tabs from `sections`; crests with initials-badge fallback; bracket rows with 24h change, Buy Yes/Buy No (ask), top-4 legend, multi-line chart with ranges 1H/6H/1D/1W/1M/ALL, sibling chips, related list.
- Remove the per-view polling `setInterval`s for sports, multi, crypto, ticket-book and charts; keep only pure UI clocks (countdowns) and the REST fallback.
- **LIVE / DELAYED / OFFLINE** indicator with data age and source on every view; sports shows the vendor "may be delayed" note. Row Yes/No opens the existing trade sheet pre-set to that outcome. Respect `LIVE_VIEWS_ENABLED`.
- Paste a venue URL in the search box -> opens its view via `/v1/event/view`.

## Done when
All layouts render from fixtures served by a fake stream; indicator flips when the stream is cut; PWA loads and signs in; JS reducer unit tests pass (add a tiny test runner if none exists); gate green; Handoff written. STOP.
