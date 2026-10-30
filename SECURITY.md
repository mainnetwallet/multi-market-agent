# Security

Report vulnerabilities privately to the operator before opening a public issue.
This is a personal, single-operator trading agent, not a hosted service.

## Threat model (v1)

- The Control API is private. Put it behind HTTPS/VPN or an authenticated tunnel.
  It binds to `127.0.0.1` by default.
- Live venue keys are separate, low-balance trading credentials. Never use the
  main wallet or a key with withdrawal permissions.
- A GitHub token used to publish this repo is never written to a file or remote.
- The Telegram bot accepts only `TELEGRAM_ALLOWED_USER_ID`.
- The LLM receives normalized market data and public search results only.

## Rules that must not be weakened

1. PAPER is the default. LIVE requires a human-edited `LIVE_<VENUE>=true` and a
   restart. No API, chat, UI, Telegram or LLM path may change it.
2. Unknown order outcomes are `UNKNOWN`, block the market, and are never resent.
3. Hard caps live in `config.py`; runtime configuration can only lower them.
4. Signing keys are read only by `*_signing.py` modules.
5. Logs pass through central redaction. Canary tests are in `tests/test_security.py`.
6. `/panic` requires a second confirmation and cancels open orders and rules.

## Rotating credentials

- Polymarket: revoke API/L2 credentials in the Polymarket settings, replace
  `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_API_*`, restart, rerun the doctor.
- Kalshi: delete the API key in Kalshi, create a new key pair, update
  `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`, restart, rerun the doctor.
- Limitless: revoke the scoped token, derive a new one, replace `LMTS_*`, rotate
  the wallet key if compromised, restart.
- Control API: change `ADMIN_PASSWORD` and `SESSION_SECRET` (all sessions become
  invalid), restart.
- Telegram: revoke the bot token with BotFather and issue a new one.
- LLM/web search: rotate provider keys, restart.

After any suspected compromise: `/panic`, revoke venue credentials, move funds
manually on each venue, rotate all secrets, and rebuild the host.
