# PLAN — multi-market-agent

Built 2026-10-30 (Asia/Dhaka). Human-supervised; every order is real. This checklist is
the build record; tests/quality gate status is at the bottom.

## Phase 0 — Docs recon and skeleton

- [x] Fetch current official docs for Polymarket, Kalshi, Limitless; probe live public APIs.
- [x] Record URLs, observed units/wire shapes and every difference from the prompt in `DOCS_FINDINGS.md`.
- [x] Python 3.11 project, pinned dependencies, src layout, `.env.example`, `.gitignore`.
- [x] One price-normalization function; startup unit guard that halts trading on mismatch.
- [x] Repo layout: config/events/domain, venues, store, scanner, research, risk, execution, rules, API, UI, bot.

## Phase 1 — Read-only adapters and doctor

- [x] Polymarket Gamma discovery + CLOB book parser; official `polymarket-client` SDK for private reads/orders.
- [x] Kalshi event discovery, fixed-point market/book parser, RSA-PSS/Ed25519 request signing.
- [x] Limitless active-market and YES-book parser; HMAC request auth + EIP-712 order signer.
- [x] FakeVenue implementing the same normalized protocol, with fills/rests/timeouts/failures.
- [x] `scripts/doctor.py`: public read, authenticated read when creds exist, opt-in place/cancel test only with `--i-accept-real-money` or `--kalshi-demo`.
- [x] Live public doctor PASS: Polymarket OK, Limitless OK, Kalshi public/market reads pass (DEGRADED only because no credentials).
- [x] Contract tests over responses recorded live on 2026-10-30.

## Phase 2 — Store, scanner, books, diagnostics

- [x] SQLAlchemy 2 models for every table, all venue/mode-scoped.
- [x] Alembic initial migration; `alembic upgrade head` + `alembic check` verified.
- [x] Scanner loop across enabled venues with per-venue page/seen/kept/dropped/families/error counters.
- [x] `/status` includes `feed_diag`; `/health` (HEAD+GET) and `/ready`.
- [x] Book cache, spread/depth/VWAP/synthetic-depth analysis.
- [x] Startup unit guard; all trading halts on mismatch, exits remain monitored.

## Phase 3 — Research, probability, strategy, risk, proposals

- [x] OpenAI-compatible LLM client; Tavily-compatible web-search tool; no hardcoded model.
- [x] Research report requires >=2 verified live-search URLs and age <=90 min.
- [x] `p_fair = mid + capped adjustment` (default ±3pp); no research means no proposal.
- [x] Edge after fee/slippage, spread/depth/staleness/end-time/synthetic vetoes.
- [x] Quarter-Kelly sizing capped by $2/trade, 4%/event, 15% total, 5% daily loss, 8 positions; Decimal only.
- [x] Proposal TTL 5 minutes, reasons/risks/fee/binding cap, approve/reject/amount reduction.
- [x] Positions, fills, PnL ledger.
- [x] Daily report endpoint with realized PnL, exposure, decisions, probability bins, calibration status.

## Phase 4 — Live execution and safety state machine

- [x] Persist `CREATED`/`SUBMITTING` before network side effects.
- [x] Per-market lock and per-venue placement lock.
- [x] No paper mode: trading needs working credentials and a passing authenticated doctor check.
- [x] Re-validation at approval: market status, fresh book, spread/depth, edge, tick, caps, end time.
- [x] Timeout/5xx -> UNKNOWN; UNKNOWN never transitions back to SUBMITTING (state-transition test).
- [x] Reconcile via open orders; absence is not treated as proof of no fill.
- [x] Reduce-only marketable exits with slippage cap.
- [x] Live executors exercised against FakeVenue, not real venues; no real order was sent.

## Phase 5 — Rules, autopilot, chat

- [x] TP/SL/time-exit rules with atomic compare-and-set ACTIVE/BLOCKED -> TRIGGERED -> EXECUTING.
- [x] Stale book/feed-down blocks and alerts; rules run for every venue with positions.
- [x] One fire per rule; sibling rules cancelled after a successful exit.
- [x] Server-side slash commands, unique id prefixes, `/panic` second confirmation.
- [x] LLM tool set: markets, books, research, status, positions, orders, rules, limits, switch, trade, sell, cancel, rules, pause/resume/panic, set_limit.
- [x] LLM sees structured data only; tool output wrapped as DATA; no secrets.
- [x] Runtime limits can only be lowered.

## Phase 6 — Control API, PWA, Telegram, switching

- [x] FastAPI REST + WebSocket events + SSE chat; signed HttpOnly session, login rate limit.
- [x] PWA single HTML with Home/Markets/Chat/Positions/Settings, mobile bottom nav, desktop sidebar.
- [x] Permanent `VENUE | LIVE` badge; market book/research/Buy YES/Buy NO/exit-rule UI.
- [x] Telegram bot allowlisted, proposal cards with approve/reject, alerts, 6-hour heartbeat.
- [x] Single poller DB lease to avoid Telegram getUpdates conflict.
- [x] Runtime switch to ready venues only; positions/orders/rules stay alive across venues.

## Phase 7 — Deploy, CI, docs

- [x] Dockerfile (slim, non-root, healthcheck), compose, Render blueprint (always-on).
- [x] CI: ruff, format check, mypy, pytest, fresh-DB Alembic, pip-audit.
- [x] Backup/restore scripts, README runbook, SECURITY.md, DOCS_FINDINGS.md, .env.example.
- [x] Final secret scan of tree and git history.
- [x] Private GitHub repo `mainnetwallet/multi-market-agent` created and `main` pushed using the operator's existing authenticated GitHub CLI keyring; no token was written to the repo or remote URL.

## Post-build audit corrections

The operator asked for a second audit. These concrete gaps were closed:

- [x] Normalized `venues`, `events`, `outcomes` tables and migration, in addition to the JSON market snapshot.
- [x] Per-venue bankroll overrides (`BANKROLL_<VENUE>`), used by sizing, daily-loss pause and status.
- [x] Background book-feed loop with freshness/error counters; `/status` and the PWA show it.
- [x] Startup unit guard now samples every ready venue, not just the active one.
- [x] Reconciliation checks open orders **and** terminal trade history (Polymarket/Kalshi/Limitless/Fake adapters).
- [x] Approval refuses when a market has an UNKNOWN/SUBMITTING order.
- [x] Missing fee info is an explicit veto at analysis and approval.
- [x] Telegram alerts for daily-loss cap, venue auth failure and venue unavailability.
- [x] Bengali/Banglish summaries on server-side chat replies.
- [x] Brier calibration from resolved markets, refreshed by the scanner for held positions.
- [x] Kalshi order prices snap to the exact per-price tick band.
- [x] Current Limitless fee docs wired: collateral 5% × (1−price), legacy curves approximated.
- [x] `scripts/bootstrap_env.py` generates a private local `.env` with fresh admin/session secrets.
- [x] Tests for normalized tables, per-venue bankroll, book feed, fee-missing veto, UNKNOWN approval block, trade-history reconciliation, Bengali summary and Brier calibration.
- [x] Local instance started; login, HEAD `/health`, `/status`, markets and PWA verified over HTTP.

Remaining honest gaps are listed in README and BUILD_REPORT; the most important are
no live credentials/order proof, Kalshi strategy maths near sub-cent edge bands,
legacy Limitless fee-curve approximation, resting take-profit exits, native
venue WebSockets, and Android background push.

## Android app (operator-requested P8 extension)

- [x] Native Android WebView shell, first-run Render HTTPS URL setup.
- [x] Persistent WebView cookies, JS dialogs, back navigation, SSL fail-closed, external links.
- [x] Server URL validation (HTTPS only), server switch, reload, clear session.
- [x] Android unit tests for URL normalization.
- [x] GitHub Actions Android APK build and artifact upload; first APK release published.

## Quality gate

Last run 2026-10-30 after the audit fixes:

- `ruff check` — pass
- `ruff format --check` — pass
- `mypy src` strict — pass
- `pytest` — 48 passed
- `alembic upgrade head` + `alembic check` — pass
- Public doctor — Polymarket OK / Kalshi OK / Limitless OK (auth/order checks skipped, no credentials)
- GitHub CI — pass (after setuptools upgrade)
- Docker build + smoke — pass, container runs as UID 10001
- Android APK CI — pass; release `v1.0.0-android`
- Real order sent — **none**

## Deliberate v1 limitations

1. Kalshi sub-cent tick bands: central tick only; no LIVE until per-price snapping.
2. Limitless fee is a conservative estimate, not verified against a fill.
3. Polymarket open orders do not expose a client id; timeout reconciliation is best effort.
4. Resting limit exits are not implemented; exits are FAK reduce-only with a slippage cap.
5. Calibration is reported as insufficient until settled outcomes are imported.
6. Cross-venue matcher / arbitrage (optional P8) not built.
7. LLM research requires an operator-supplied LLM + web-search key.
8. Slash-command strings are English; LLM assistant language follows the user.

## Build decisions

- Normalize every venue price to Decimal probability in (0,1), never infer cents.
- Fail closed on the unknown; preserve exact exchange status strings in operator output.
- FakeVenue tests all core logic; recorded live responses test wire contracts.
- Keep secrets out of Settings fields; signing modules read them from the environment.
- Use the official Polymarket SDK rather than hand-rolling V2 signing; own the
  simpler Kalshi signing and Limitless HMAC/EIP-712 code with isolated secrets.
- No UI before public reads and the doctor worked.
