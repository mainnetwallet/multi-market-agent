# Build report — 2026-10-30

Repository: **private** `mainnetwallet/multi-market-agent`
(https://github.com/mainnetwallet/multi-market-agent)

## What passed

| Check | Result |
|---|---|
| `ruff check src tests scripts` | pass |
| `ruff format --check` | pass |
| `mypy src` (strict) | pass, 38 source files |
| `pytest` | **48 passed** |
| `alembic upgrade head` / `alembic check` | pass, no pending schema changes |
| Wheel build | pass, static PWA assets included |
| `pip-audit` | no known vulnerabilities in runtime deps; local project skipped as not-on-PyPI |
| Docker compose config | valid |
| Docker image build | pass in GitHub Actions (`docker-smoke` job) |
| Docker Render-like smoke | pass: entrypoint drops to UID 10001, migrations apply, `/health` HEAD 200 |
| GitHub CI (Python) | pass after setuptools upgrade |
| Android APK CI | **pass** — unit tests + debug APK built and uploaded |
| Android APK | 22,937 bytes, debug-signed, released as `v1.0.0-android` |
| Git history secret scan | clean after excluding public fixture condition ids and the redaction canary file |

## Venue doctor (public; no orders)

Credentials were not available, so authenticated reads and order roundtrips were
skipped exactly as designed.

Post-audit corrections added normalized `venues`/`events`/`outcomes` tables,
per-venue bankroll overrides, a background book-feed loop, trade-history
reconciliation, approval blocking on UNKNOWN orders, Bengali/Banglish chat
summaries, Brier calibration from resolved markets, exact Kalshi order tick
bands, current Limitless fees, and a `.env` bootstrap script. A local
instance was started and its login, `/health` HEAD, `/status`, markets, and PWA
HTML were verified over HTTP.

| Venue | Status | public | book / market | auth | order |
|---|---|---|---|---|---|
| Polymarket | OK | OK | CLOB book OK | SKIPPED | SKIPPED |
| Kalshi | OK | exchange status OK | nested events + market OK | SKIPPED | SKIPPED |
| Limitless | OK | active markets OK | orderbook OK | SKIPPED | SKIPPED |

No real order was sent during the build.

## Android app

- Native Android WebView shell in `android/`; first launch asks for the Render
  HTTPS URL; credentials are never stored by the app.
- GitHub Actions `Android APK` passed; artifact contains `app-debug.apk`.
- GitHub release: https://github.com/mainnetwallet/multi-market-agent/releases/tag/v1.0.0-android
- SHA256: `80FDF368D6AF7EDE121465539402A5EE5953B899B50C91FA8AF60A4D63964AA4`
- This is a debug-signed sideload APK, not a Play Store build.

## Commits

```
94c83e8 phase 7: Docker, Render, CI, backups and operator runbook
c533cb3 phase 6: Control API, PWA, Telegram and runtime venue switching
696d7e2 phase 5: rules engine, autopilot tools and server-side slash commands
c305268 phase 3-4: research, edge, risk, proposals, execution, reconcile and exits
d79d6ce phase 2: SQLAlchemy store, Alembic schema, books and scanner
1c8f7c2 phase 1: read-only venue adapters, FakeVenue and per-venue doctor
3d71a44 phase 0: scaffold, normalized domain, venue docs recon
```

## Run locally

```bash
cp .env.example .env     # set ADMIN_PASSWORD and SESSION_SECRET
alembic upgrade head
python scripts/doctor.py --venue all
python -m mm_agent.main
```

Open `http://127.0.0.1:8080`. A venue can trade only
after its credentials are set and its authenticated doctor check passes.

## Before the first real order

Follow the README checklist. Do not place a real order until:

1. Authenticated doctor checks pass for that venue; rerun with the venue's
   demo/opt-in order test only if you accept the risk.
2. Kalshi sub-cent tick snapping is hardened (known gap).
3. Limitless fee estimate is verified against a real fill.
4. A **dedicated** low-balance trading account/wallet is in use.
5. The first order is a $1–$2 approval from a dedicated burner account, watched end to end.

## Known gaps

See README "Known gaps / limitations" and DOCS_FINDINGS.md. The most important:
live place/cancel is unverified without operator credentials; Kalshi strategy
edge near sub-cent edge bands; legacy Limitless fee curve approximation;
Polymarket timeout dedupe limitation; resting take-profit exits not implemented
(FAK reduce-only exits are).

## Credential note

The private repo was published with the operator's pre-existing authenticated
GitHub CLI session (keyring). No new token was requested, displayed, written to
the tree, or embedded in the git remote. If a token is ever pasted into chat,
rotate it immediately after use.
