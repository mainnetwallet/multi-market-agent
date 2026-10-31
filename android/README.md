# Android app — full-control native client

Kotlin · Jetpack Compose · Material 3. It talks to the agent's Control API
directly — no WebView.

## What it does

- **Home** — cash available (tap opens the Wallet), today's PnL, and one-tap
  Pause and Cancel all. Both reduce risk, so neither uses hold-to-confirm.
- **Markets** — on Polymarket, a Polymarket-style browser: category tabs (Trending,
  Breaking, New, Midterms, Politics, Sports, Crypto, Esports, Iran, Finance,
  Geopolitics, Tech, Economy, Elections, Culture, Weather, Mentions), search, and
  event cards with the live Yes/No price, chance, 24h move, volume and close time.
  Tap a Yes or No price to open the trade sheet on that side. Combos and Perps tabs
  are listed but say why they are not tradable here. Other venues keep the plain list.
  Data comes from `GET /v1/discover` (public Gamma data, read-only).
- **Trade sheet** — Buy/Sell, Yes/No, then order-type chips driven by the venue's
  capabilities (Limit GTC, Limit with expiry GTD, Market FOK/FAK, Post-only where
  supported). Shows the order book depth and the price history, both drawn with
  Compose Canvas, plus price, size, cost, fee, max loss and payout *before* the
  confirmation. Paper: one tap. Live: press and hold.
- **Portfolio** — tabs for Positions, Orders, Rules and History. Positions offer
  Sell 25/50/100%, Redeem (when the venue supports it) and Split/Merge on
  Polymarket. Open orders can be cancelled one at a time or all at once; the
  order card's Cancel is a single tap plus a normal dialog, because cancelling
  reduces risk. History shows trade rows and a daily PnL chart.
- **Wallet** — per-venue balances in the venue's own collateral asset (pUSD,
  USD, USDC — never hardcoded), Deposit and Withdraw, Allowlist, Approvals and
  withdrawal/deposit history. Where a venue has no API for something, a card
  explains why and opens the venue in a Custom Tab.
- **Chat** — a ChatGPT/Claude-style chat with the agent. Every chat is a server conversation
  (`/v1/conversations`), so the assistant sees that chat's earlier messages and only that chat's.
  A history drawer groups chats by day and offers new chat, rename, delete and search (titles and
  message text, searched on the server). Replies stream in with Markdown, aligned tables and
  syntax-highlighted code blocks; each message and code block has a Copy action. Tool steps show as
  they run, and Stop ends a reply (a step already running, such as a trade, still finishes, so check
  Orders). Web links in replies are tappable, but only after a dialog shows the real address; long chats
  load in pages ("Load earlier messages"). Chats from before this change stay on the device and open as before; sending a message in
  one starts its server copy. Slash commands (`/status`, `/positions`, ...) work as always.
- **Settings** — a Security section with the mode, the app lock (on/off and the
  background timeout), the per-trade cap, the hard ceiling (raisable with a
  step-up token), the funds capability status and the heartbeat/resting-orders
  state.

## App lock

The app re-locks after a chosen time in the background (default 5 minutes) and
needs a biometric to open again. The lock only turns on when the device actually
has a biometric enrolled — a lock that could not be opened would strand the
operator, so without one Settings says "off (no biometric enrolled)" and the app
opens normally.

## Build

```bash
export ANDROID_HOME=/path/to/android-sdk
export JAVA_HOME=/path/to/jdk-17
gradle -p android -PmmBaseUrl=https://your-service.onrender.com testDebugUnitTest assembleDebug
```

The APK lands in `android/app/build/outputs/apk/debug/app-debug.apk`.
`-PmmBaseUrl` sets the default server URL; the operator can change it on the
login screen.

## Install and update

Every push to `main` that touches `android/**` builds a **signed release APK** and
publishes it on the repo's **Releases** page (tag `android-latest`).

- Download the `.apk` from Releases on your phone and open it. Installing a newer
  release over an older one updates the app and keeps your login.
- Updates only work because every release is signed with the same key and has a
  higher `versionCode` (`100 + run number`). The key lives in the repo secrets
  `ANDROID_KEYSTORE_BASE64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS` and
  `ANDROID_KEY_PASSWORD`. If it is ever replaced, uninstall the app once and
  install the new release.
- The first signed release cannot update the older debug builds (different app id
  and key). Uninstall those once.
- Pull-request builds only produce a debug APK (Actions → Android APK → artifact).

CI: `.github/workflows/android.yml`.

## First run

1. Enter the HTTPS URL of your deployed agent (for example
   `https://multi-market-agent.onrender.com`).
2. Sign in with the same `ADMIN_USERNAME` / `ADMIN_PASSWORD` as the website. The
   username starts as `operator` (or the last one you used).
3. You stay signed in. The session token is kept in the app's private storage and
   swapped for a fresh one (`POST /v1/auth/refresh`) every time the app starts and
   every few hours while it is open. Sessions last 30 days and renew while you use
   the app, up to 90 days in total, then you sign in again. Sign out from Settings
   to forget the session. This needs the server to be redeployed with the refresh
   endpoint; against an older server the app still works, but the session ends
   after the old 72 hours.
4. The header always shows `VENUE | PAPER` or a red `VENUE | LIVE`.

## Safety rules the app enforces

- Every confirmation shows venue, market, outcome, side, order type, price, size,
  cost, fee and Paper/Live before anything is sent.
- A withdrawal confirmation shows the full final recipient address in
  4-character groups, the network, the token, the amount, the fee and the hold
  time.
- Buttons are disabled while a request is in flight.
- Order and withdrawal status is shown exactly as the server returns it. The app
  never says "filled" or "sent" unless the server did.
- Live actions that add risk or move money out use press-and-hold. Cancel and
  Pause stay one tap plus a normal dialog.
- When the server is unreachable, every money-adding and money-out action is
  disabled. Cancel and Pause stay available.
- Screenshots work by default. Settings → Security → **Block screenshots** turns on
  `FLAG_SECURE` (no screenshots or screen recording, blank recents thumbnail). The
  copy button covers addresses either way.
- One idempotency key per user intent, reused if you tap again after a timeout
  and discarded after a definite result.

## Tests

```bash
gradle -p android testDebugUnitTest
```

Covers the money/price/Dhaka-time formatting, the trade sheet's order-type chips
following the venue capabilities, the estimate shown before confirming, the
withdraw flow's idempotency-key handling, and the offline gating that disables
money actions when the server cannot be reached.
