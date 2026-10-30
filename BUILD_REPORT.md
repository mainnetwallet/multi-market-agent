# Build report — 2026-10-30

Repository: **private** `mainnetwallet/multi-market-agent`
(https://github.com/mainnetwallet/multi-market-agent)

## What passed

| Check | Result |
|---|---|
| `ruff check src tests scripts` | pass |
| `ruff format --check` | pass |
| `mypy src` (strict) | pass, 38 source files |
| `pytest` | **38 passed** |
| `alembic upgrade head` / `alembic check` | pass, no pending schema changes |
| Wheel build | pass, static PWA assets included |
| `pip-audit` | no known vulnerabilities in runtime deps; local project skipped as not-on-PyPI |
| Docker compose config | valid |
| Docker image build | **not run** — Docker daemon was not running on the build host |
| Git history secret scan | clean after excluding public fixture condition ids and the redaction canary file |

## Venue doctor (public; no orders)

Credentials were not available, so authenticated reads and order roundtrips were
skipped exactly as designed.

| Venue | Status | public | book / market | auth | order |
|---|---|---|---|---|---|
| Polymarket | OK | OK | CLOB book OK | SKIPPED | SKIPPED |
| Kalshi | OK | exchange status OK | nested events + market OK | SKIPPED | SKIPPED |
| Limitless | OK | active markets OK | orderbook OK | SKIPPED | SKIPPED |

No real order was sent during the build.

## Commits

```
94c83e8 phase 7: Docker, Render, CI, backups and operator runbook
c533cb3 phase 6: Control API, PWA, Telegram and runtime venue switching
696d7e2 phase 5: rules engine, autopilot tools and server-side slash commands
c305268 phase 3-4: research, edge, risk, proposals, paper/live execution, reconcile and exits
d79d6ce phase 2: SQLAlchemy store, Alembic schema, books and scanner
1c8f7c2 phase 1: read-only venue adapters, FakeVenue and per-venue doctor
3d71a44 phase 0: scaffold, normalized domain, venue docs recon
```

## Run PAPER now

```bash
cp .env.example .env     # set ADMIN_PASSWORD and SESSION_SECRET
alembic upgrade head
python scripts/doctor.py --venue all
python -m mm_agent.main
```

Open `http://127.0.0.1:8080`. Everything is PAPER until an operator edits
`LIVE_<VENUE>=true` and restarts.

## Go LIVE later

Follow the README checklist. Do not enable LIVE until:

1. Authenticated doctor checks pass for that venue; rerun with the venue's
   demo/opt-in order test only if you accept the risk.
2. Kalshi sub-cent tick snapping is hardened (known gap).
3. Limitless fee estimate is verified against a real fill.
4. A **dedicated** low-balance trading account/wallet is in use.
5. A $1–$2 approval has been watched end to end in PAPER first.

## Known gaps

See README "Known gaps / limitations" and DOCS_FINDINGS.md. The most important:
live place/cancel is unverified without operator credentials; Kalshi sub-cent
tick bands; Limitless fee approximation; Polymarket timeout dedupe limitation;
resting take-profit exits not implemented (FAK reduce-only exits are).

## Credential note

The private repo was published with the operator's pre-existing authenticated
GitHub CLI session (keyring). No new token was requested, displayed, written to
the tree, or embedded in the git remote. If a token is ever pasted into chat,
rotate it immediately after use.
