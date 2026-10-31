# Security

Report vulnerabilities privately to the operator before opening a public issue.
This is a personal, single-operator trading agent, not a hosted service.

## Threat model

- The Control API is private. Put it behind HTTPS/VPN or an authenticated tunnel.
  It binds to `127.0.0.1` by default.
- Live venue keys are separate, low-balance trading credentials. Never use the
  main wallet or a key with withdrawal permissions.
- A GitHub token used to publish this repo is never written to a file or remote.
- The Telegram bot accepts only `TELEGRAM_ALLOWED_USER_ID`.
- The LLM receives normalized market data and public search results only.

## The full-control build (added 2026-10-30)

The app can now place orders, cancel them and move money. What protects that:

| Risk | Protection |
|---|---|
| Stolen phone | App lock: the app re-locks after a chosen time in the background and needs a biometric to open again. The lock only turns on when the device actually has a biometric enrolled, so it can never strand the operator. Hold-to-confirm for every action that adds risk or moves money out. Cancel and Pause stay one tap. |
| Stolen session token | Sessions last 30 days and renew while the app is used, up to 90 days in total; changing `SESSION_SECRET` signs every device out. Step-up tokens are required for withdrawals, allowlist changes, approval grants and ceiling raises. A step-up token is single-use, bound to the session and to one action, and expires in 120 s. The password is never stored or logged. |
| Replayed request | Every money-moving POST requires an `Idempotency-Key`. The server stores key → result for 24 h and replays the first result, so a retry after a timeout can never double-submit. |
| Wrong withdrawal address | The address format and its EIP-55 checksum are validated, and the chain and token must be on the bridge's own list (EVM chains only). With `FUNDS_REQUIRE_ALLOWLIST=true` only allowlisted addresses may be used, and a new address waits out a cooling period (default 24 h) before first use. With it `false` (the default), the Withdraw page takes a typed-in receiver, chain and token, so a stolen session plus the password could send funds to any address: the password step-up, the hold window (cancellable), the daily limit and the quote shown before confirming are then the only guards. Turn the setting on if that is not acceptable. |
| Double submit | One client id per user intent, one idempotency key per intent, and the venue's own client order id where it has one. UNKNOWN outcomes are never resent. |
| Key custody | The funds module uses its own signer secret (`FUNDS_SIGNER_PRIVATE_KEY`), never a trading key. Trading keys keep no withdrawal permission. |
| Backend restart | Resting orders belong to the venue, not to the backend. The Polymarket order heartbeat is deliberately **not** used, because its first accepted send enables timeout cancellation for every open order with no disarm — a restart would silently cancel resting orders. The app shows the heartbeat state in Settings. |
| Stale data | Money-adding and money-out actions are disabled when the server is unreachable or a refresh is in flight: the price and the balance on screen are then unknown. Cancel and Pause stay available whenever the server is reachable. |
| Price/unit drift | One normalizer for every venue price; the startup unit guard halts all trading on a mismatch. |
| Venue silent failure | Order and withdrawal states are reported exactly as the server reports them. The app never says "sent" or "filled" unless the server did. |

## The funds module

**Off by default.** Every funds route returns `supported: false` with the reason
unless `FUNDS_ENABLED=true`.

What it can do when enabled:

- Show a deposit address per venue, network and asset, taken from the venue's own
  supported-assets list. Polymarket goes through the Bridge API; Limitless shows
  the wallet address on Base because Limitless has no deposit API.
- Keep an allowlist of withdrawal recipients, with a cooling period.
- Preview a withdrawal (fee, network, ETA, hold window, daily limit left) without
  sending anything.
- Create a withdrawal in a hold window, then submit it exactly once when the hold
  ends. `PENDING_HOLD → SUBMITTED → CONFIRMED`, or `FAILED` / `CANCELLED` / `UNKNOWN`.
  The collateral transfer to the bridge address is signed by the funds signer and
  sent once; the bridge's status endpoint moves the withdrawal to `CONFIRMED`.
  UNKNOWN keeps the transaction hash and asks the operator to look; nothing
  resends.

What it deliberately does **not** do:

- It never retries a funds call. A submitted withdrawal is submitted once.
- It never generates a bridge address before the hold ends. Each Polymarket bridge
  address is tied to one destination, so it is requested at execution time and the
  returned address is checked against the withdrawal record before anything is sent.
- It never requests an unlimited token allowance.
- It is off unless `FUNDS_ENABLED=true`; every route refuses with the reason otherwise.

### Key custody: the tradeoff you should read

Polymarket withdrawals and redeems need the *wallet owner's* signature. If the
wallet owner is the same key that trades, then **the trading key must be able to
move money**, which is exactly what the base build promised it could not do
("trading keys have no withdrawal permission").

This build does not silently wire that up. It gives you a separate
`FUNDS_SIGNER_PRIVATE_KEY` and expects you to point it at a dedicated owner key.
If you point it at the trading key, you have made that key a withdrawal key, and
the operator — not the code — has made that choice. The code's job is to make the
choice visible, which is why the funds module is off by default, uses its own
secret, and logs every withdrawal to the audit log.

Limitless's own withdrawal and redeem routes are partner server-wallet features
that need a scoped `withdrawal` token. A normal Limitless API key cannot use them,
so the app shows them as unavailable and deep-links to Limitless instead of
pretending to work.

## Rules that must not be weakened

1. Every order is real. A venue trades only after its
   authenticated doctor check passes, and every order needs a human action
   (or an explicit `CHAT_AUTOPILOT=true` chat command).
2. Unknown order outcomes are `UNKNOWN`, block the market, and are never resent.
3. The per-trade cap is a stored setting, not a constant, but it can only be
   *raised* with a step-up token, and the bankroll and percentage caps can never
   exceed it.
4. Signing keys are read only by `*_signing.py` and `mm_agent.funds`.
5. Logs pass through central redaction. Canary tests are in `tests/test_security.py`.
6. `/panic` requires a second confirmation and cancels open orders and rules.
7. Every money-moving route requires an `Idempotency-Key` and writes an audit row.
8. The Polymarket order heartbeat stays off unless an operator explicitly turns
   it on and accepts that a restart cancels every resting order.

## Rotating credentials

- Polymarket: revoke API/L2 credentials in the Polymarket settings, replace
  `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_API_*`, restart, rerun the doctor.
- Kalshi: delete the API key in Kalshi, create a new key pair, update
  `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`, restart, rerun the doctor.
- Limitless: revoke the scoped token, derive a new one, replace `LMTS_*`, rotate
  the wallet key if compromised, restart.
- Funds signer: replace `FUNDS_SIGNER_PRIVATE_KEY`, restart. Every allowlisted
  address must be re-checked — the allowlist survives a restart by design.
- Control API: change `ADMIN_PASSWORD` and `SESSION_SECRET` (all sessions become
  invalid), restart.
- Telegram: revoke the bot token with BotFather and issue a new one.
- LLM/web search: rotate provider keys, restart.

After any suspected compromise: `/panic`, revoke venue credentials, move funds
manually on each venue, rotate all secrets, and rebuild the host.
