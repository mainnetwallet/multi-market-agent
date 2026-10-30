# multi-market-agent

A human-supervised prediction-market trading agent for **Polymarket**, **Kalshi**
and **Limitless**, switchable at runtime. One asyncio process runs the scanner,
background book feed/cache, research/edge pipeline, risk engine, proposal queue,
order execution, exit-rule monitor, FastAPI Control API, PWA, Telegram bot and an
in-process event bus. The book feed refreshes the active venue's candidate books
and exposes freshness/error counters in `/status`.

**Paper by default. No real order is ever sent without an operator-set live flag
and a human action.** The build was completed and tested without live
credentials; public reads for all three venues were verified on 2026-10-30.

---

## Safety model (read before running)

1. **PAPER by default per venue.** A venue trades LIVE only when the operator
   edits `LIVE_POLYMARKET=true`, `LIVE_KALSHI=true` or
   `LIVE_LIMITLESS=true` in `.env`/host env and restarts. No API, chat, UI,
   Telegram or LLM call can change it. Limits can only be lowered at runtime.
2. **Fail closed.** Stale book/research, missing fee info, unit mismatch, feed
   errors, not-ready venue, UNKNOWN order, market closing within 5 minutes,
   synthetic-looking depth, missing research or any risk-cap breach means no
   entry.
3. **UNKNOWN is never resent.** A timeout/5xx becomes `UNKNOWN`, blocks that
   market pending reconciliation and is surfaced in `/status` and Telegram.
4. **Human action for every real order.** Approve a proposal (UI/Telegram) or,
   if `CHAT_AUTOPILOT=true`, issue an explicit chat command. `/panic` always
   needs a second confirmation.
5. **No withdrawals or transfers in v1.** Move funds on the venue's own UI.
6. **Hard caps** (code, not config): $2/trade, 4% of bankroll per event, 15%
   total exposure, 5% daily loss then pause, 8 open positions. Env may lower but
   never raise them.
7. Dedicated trading wallets/accounts with small funds only. Eligibility, KYC
   and geo restrictions are the operator's responsibility; the agent never
   bypasses venue rules.
8. Sizing uses `BANKROLL_USD`, optionally overridden per venue with
   `BANKROLL_POLYMARKET`, `BANKROLL_KALSHI`, `BANKROLL_LIMITLESS` or
   `BANKROLL_FAKE`. Caps remain percentage-based and code-capped.

Full security notes and rotation steps: [SECURITY.md](SECURITY.md). Venue
reconnaissance and current API differences: [DOCS_FINDINGS.md](DOCS_FINDINGS.md).

---

## Architecture

```text
Browser / PWA --HTTPS/WS/SSE--> FastAPI Control API <--> Telegram bot
                                          |
                         AGENT CORE (one asyncio process)
   Orchestrator + event bus + state machines + per-market/venue locks
      |             |               |                 |
 VenueRegistry   Research       Risk / Strategy     Store
 (adapters)      (LLM+search)   Proposals/Rules      SQLAlchemy + Alembic
   |  |  |                         |                 (SQLite dev, Postgres prod)
  PM  KS  LL                    Execution
```

Pipeline: scan → book analysis → sourced research → `p_fair = mid + capped LLM
adjustment (±3pp, only with ≥2 fresh sources)` → edge after fee/slippage → vetoes
→ quarter-Kelly sizing capped → PROPOSAL (5-minute TTL) → human approval →
re-validation (fresh book, edge, caps) → sign → place → persist fill/status →
monitor and exit rules.

Core logic is tested against `FakeVenue`; `PaperExecutor` runs on each venue's
**real** public books. See [PLAN.md](PLAN.md) for phase status and known gaps.

---

## Quick start (PAPER)

Requires Python 3.11+ (3.11/3.12 supported).

```bash
git clone <this-repo> multi-market-agent
cd multi-market-agent
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# Linux/macOS
# source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # Windows: Copy-Item .env.example .env
```

Edit `.env` and set at minimum:

```dotenv
ADMIN_PASSWORD=<a long unique password>
SESSION_SECRET=<a long random stable string>
```

Or generate those two secrets and a local `.env` automatically:

```bash
python scripts/bootstrap_env.py
```

The generated admin password is stored in `.env`; the script refuses to
overwrite an existing file.

Then:

```bash
alembic upgrade head
python scripts/doctor.py --venue all       # public read; no orders
python -m mm_agent.main                    # http://127.0.0.1:8080
```

Sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`. The header always shows
`VENUE | PAPER` (or red `VENUE | LIVE`). Times are Asia/Dhaka. The LLM/research
provider is optional: without it, no proposals are generated and the UI says
research is unavailable. Slash commands still work.

Run the quality gate:

```bash
ruff check src tests scripts
ruff format --check src tests scripts
mypy src
pytest -q
```

---

## Venue setup (read-only / PAPER needs no keys)

Venue credentials are read **only** by the isolated `*_signing.py` modules, but
the adapters need public read access which needs no account. Run the doctor after
each change.

### Polymarket

- Uses the official [`polymarket-client`](https://pypi.org/project/polymarket-client/)
  SDK for private reads/orders and public Gamma/CLOB HTTP for discovery/books.
- Create/find a **dedicated** Polymarket account wallet. Set:
  - `POLYMARKET_WALLET_ADDRESS`
  - `POLYMARKET_PRIVATE_KEY` (signer key; never a main wallet)
  - optional `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`,
    `POLYMARKET_API_PASSPHRASE` if you already derived L2 credentials
- `POLYMARKET_SIGNATURE_TYPE=3` is the default for Deposit Wallets; adjust only
  after reading current wallet-type docs.
- `POLYMARKET_ALLOW_REAL_TEST=true` is only for the explicit doctor order test.
- Current docs show Protocol V2 and pUSD; see [DOCS_FINDINGS.md](DOCS_FINDINGS.md).
  The official SDK handles V2 vs CTF asset ids.

### Kalshi

- Create a dedicated Kalshi API key. Save the private key PEM outside the repo,
  e.g. `C:\secrets\kalshi.key` or `/etc/mm-agent/kalshi.key` (mode 600).
- Set `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`.
- Production base: `https://api.elections.kalshi.com/trade-api/v2`.
- Demo base: `https://demo-api.kalshi.co/trade-api/v2`. Demo keys are separate.
- Doctor: `python scripts/doctor.py --venue kalshi` for public/auth reads;
  `--kalshi-demo` for the demo order roundtrip. Do not point production keys at a
  real-money order test unless you accept the cost.
- Discovery walks events with nested markets because the raw market feed is
  dominated by multivariate combos (excluded in v1).

### Limitless

- Limitless is on Base and uses USDC (6 decimals).
- Derive a **scoped API token** in the Limitless UI (API Tokens → Derive). Set:
  - `LMTS_TOKEN_ID`
  - `LMTS_TOKEN_SECRET` (base64 secret, shown once)
  - `LMTS_OWNER_ID` (profile id; optional, discovered via `/profiles/me` if 0)
  - `LIMITLESS_PRIVATE_KEY` (EOA trading wallet, dedicated, low balance)
  - `BASE_RPC_URL` for the USDC balance read (default public Base RPC)
- The wallet must be in EOA trade-wallet mode; smart-wallet mode rejects
  self-signed API orders. The doctor reports this.
- There is no demo environment. The order roundtrip is opt-in only.
- Fee handling uses a conservative estimate until the venue's fee model is
  verified; see DOCS_FINDINGS.md. Do not go LIVE before checking it.

---

## Doctor

```bash
python scripts/doctor.py --venue polymarket
python scripts/doctor.py --venue kalshi
python scripts/doctor.py --venue kalshi --kalshi-demo
python scripts/doctor.py --venue limitless
python scripts/doctor.py --venue all --json
```

Without `--i-accept-real-money` / `--kalshi-demo`, the doctor never places an
order. It proves public read, book read, and (when credentials exist)
authenticated balance/profile read. A venue whose public read fails is marked
unavailable; the build continues with the others. Exit code is non-zero if any
requested venue is unavailable.

**No real order has been sent by this build.** The order-roundtrip code exists
and is opt-in; authenticated reads remain unverified until the operator supplies
credentials.

---

## Going LIVE per venue (manual checklist)

1. Paper-trade the venue for at least a day and confirm `/status` feed_diag is
   clean. Run the doctor without order tests, then with the demo/opt-in test only
   if you accept the risk.
2. Use a **dedicated trading account/wallet** and a small balance. No withdrawal
   permission is needed by the agent.
3. Confirm the venue's fee model and tick rules against your account. For Kalshi
   sub-cent markets, do not enable LIVE until the tick-grid gap in
   DOCS_FINDINGS.md is fixed.
4. Edit `.env` **by hand**:
   ```dotenv
   LIVE_KALSHI=true
   ```
   Keep all other venues PAPER. To allow chat-issued live orders:
   ```dotenv
   CHAT_AUTOPILOT=true
   ```
   Otherwise live orders require the UI/Telegram Approve button.
5. Restart the process. Confirm the red `VENUE | LIVE` badge and the startup log.
6. Approve a **$1–$2** proposal and watch the order through fill/reconciliation.
7. If anything is unexpected: `/panic` (two confirmations), or the UI PANIC
   button. Positions are not auto-sold; review each venue manually.
8. Rotate keys after any incident. See SECURITY.md.

`LIVE_<VENUE>` cannot be changed by the UI, Telegram, chat or LLM. Restart is
required. There is no endpoint to set it.

---

## Runtime venue switching

- `ENABLED_VENUES=polymarket,kalshi,limitless`
- `ACTIVE_VENUE=polymarket`
- Switch only to a **ready** venue:
  - UI Settings → Venue selector
  - `POST /v1/venues/active` `{"venue":"kalshi"}`
  - chat `/venue kalshi`
  - LLM tool `switch_venue`
- On switch: new proposals pause briefly, in-flight placement finishes, and all
  venues' positions/open orders/rules stay alive. Only the event bus and store
  are shared; every row carries `venue` (and `mode`).
- Paper/live is per venue. The badge always names the active venue and mode.

---

## Chat and slash commands

Slash commands are parsed server-side and never sent to the LLM. Ids may be
shortened to a unique prefix.

```text
/help /status /markets [q] /match <query> /positions /orders /rules /limits
/venue <name> /research <market>
/buy <market> <usd> [yes|no] /sell <market> [usd] /close <market>
/tp <market> <5|20%|70c> /sl <market> <3|30c> /time <market> <2h|30m>
/approve <id> /reject <id> /cancel <id> /cancelall
/pause /resume /panic   (then /panic CONFIRM)
```

The optional LLM assistant has all required tools, is instructed to report
`FILLED/PARTIAL/RESTING/NOT_FILLED/FAILED/UNKNOWN/ERROR` verbatim from tool data,
never sees secrets/raw auth responses, and wraps tool output as DATA. If no LLM
is configured, use slash commands.

---

## UNKNOWN of an order

An UNKNOWN order is never resent. It blocks new entries on that market until
reconciled.

1. `/status` shows `UNKNOWN orders`. `/orders` shows the local order.
2. Check the venue directly (web UI/API) by client id, venue order id, asset and
   timestamp.
3. The reconciler checks open orders; it only resolves when it can prove a
   match. Absence from open orders is not proof of no fill.
4. If the order actually filled, the local position may need manual adjustment;
   do not place a replacement. If it was never accepted, mark/reconcile with the
   operator's evidence.
5. Do not "retry with a new id" on the same market until this is resolved.

---

## Deploy

- **Docker:** `docker build -t mm-agent .` then
  `docker run --env-file .env -p 127.0.0.1:8080:8080 -v mm_data:/app/data mm-agent`.
- **Compose:** `docker compose up -d --build`.
- **Render:** `render.yaml` is a starter always-on blueprint. A free instance
  that sleeps is not acceptable for a trading loop; use an always-on plan and
  keep an external uptime ping on `HEAD /health`.
- **Database:** SQLite is the dev default. Use Postgres for production by setting
  `DATABASE_URL=postgresql+psycopg://...`, installing `.[postgres]`, and running
  `alembic upgrade head`.
- The container runs as a non-root user. `.env` is never copied into the image.
- Put the API behind HTTPS/VPN; it binds `127.0.0.1` by default. Only one
  Telegram poller may run (DB lease); a second instance logs a conflict and does
  not poll.
- Backups: `deploy/backup.sh` makes a consistent SQLite/Postgres dump plus a
  copy of the Alembic version. `deploy/restore.sh` verifies and restores. Test
  restores before trusting them.

---

## Data and migrations

SQLite (`data/mm_agent.db`) is the default; PostgreSQL is supported with psycopg.
Apply migrations before first run and after upgrades:

```bash
alembic upgrade head
```

Normalized `venues`, `events`, `outcomes`, `markets`, research, probabilities,
proposals, orders, fills, positions, PnL, rules, decisions, audit and kv tables
are present. Every trading row carries venue (and mode for money rows). Order/fill
transitions are committed before network side effects; UNKNOWN never transitions
back to SUBMITTING. Reconciliation checks open orders first and then terminal
trade history. See `src/mm_agent/store/repo.py`.

---

## Known gaps / limitations

- **No real order has been attempted.** Live executors are implemented and
  tested against FakeVenue/recorded fixtures, but authenticated venue checks and
  real place/cancel remain unverified without operator credentials.
- **Kalshi sub-cent tick grid:** strategy analysis uses the central-band tick;
  order placement now snaps against the exact per-price `price_ranges` band.
  Validate a tiny sub-cent order before scaling.
- **Limitless fee** now follows the current docs: 5% × (1−price) of USDC value on
  new collateral-fee venues and the documented legacy buy/sell curves as an
  approximation. Always compare the first fill's reported fee before scaling.
- **Polymarket order dedupe:** the SDK does not expose a client id on open
  orders, so a timeout before the response cannot be deduped as strongly as
  Kalshi/Limitless. Reconciliation checks open orders and account trade history,
  but matching is still best-effort by asset/order/time.
- **Resting take-profit exits:** v1 exits are marketable FAK reduce-only orders
  with a slippage cap; a resting limit exit that is cancelled before time-exit is
  not implemented. Time/TP/SL rules do exist and fire at most once.
- **Calibration** computes a Brier score from resolved markets with stored
  probabilities. Coverage depends on the scanner refreshing held markets after
  resolution; otherwise it reports `insufficient_resolved_samples`.
- **Telegram single-poller** uses a DB lease (works on SQLite/Postgres). It is not
  a PostgreSQL session advisory lock, though the effective single-owner guarantee
  is the same for this deployment.
- **Language:** the LLM assistant replies in Bengali/Banglish/English; server-side
  slash replies are English plus a Bengali or Banglish summary when the command
  uses that language. Times are Asia/Dhaka.
- **Venue-native WebSockets:** the book feed currently uses short-interval REST
  refreshes through the adapter `subscribe_books` contract, not each venue's
  native stream. Native WS can replace the feed without changing the pipeline.
- Cross-venue market matching (optional P8) is not built. No automatic arbitrage.

---

## Project layout

```text
src/mm_agent/
  config.py domain.py events.py locks.py books.py scanner.py
  research.py llm.py probability.py strategy.py risk.py agent.py
  execution.py proposals in agent.py rules.py reports.py chat.py
  control_api.py telegram.py doctor.py main.py static/
  venues/{base,registry,fake,polymarket,kalshi,limitless,*_signing}.py
  store/{models,repo}.py
alembic/  tests/  scripts/doctor.py  deploy/  Dockerfile  docker-compose.yml
DOCS_FINDINGS.md  PLAN.md  SECURITY.md  .env.example
```

This is decision-support software, not financial advice. You remain responsible
for every order, every account, and venue compliance.
