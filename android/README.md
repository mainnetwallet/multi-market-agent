# Android app — full-control native client

Kotlin · Jetpack Compose · Material 3. It talks to the agent's Control API
directly — no WebView.

## What it does

- **Home** — cash available (tap opens the Wallet), today's PnL, and one-tap
  Pause and Cancel all. Both reduce risk, so neither uses hold-to-confirm.
- **Markets** — the market list; tap a market to open the trade sheet.
- **Trade sheet** — Buy/Sell, Yes/No, then order-type chips driven by the venue's
  capabilities (Limit GTC, Limit with expiry GTD, Market FOK/FAK, Post-only where
  supported). Shows the order book depth and the price history, both drawn with
  Compose Canvas, plus price, size, cost, fee, max loss and payout *before* the
  confirmation. Paper: one tap. Live: press and hold.
- **Portfolio** — tabs for Positions, Orders, Rules and History. Positions offer
  Sell 25/50/100%, Redeem (when the venue supports it) and Split/Merge on
  Polymarket. Open orders can be cancelled one at a time or all at once. History
  shows trade rows and a daily PnL chart.
- **Wallet** — per-venue balances in the venue's own collateral asset (pUSD,
  USD, USDC — never hardcoded), Deposit and Withdraw, Allowlist, Approvals and
  withdrawal/deposit history. Where a venue has no API for something, a card
  explains why and opens the venue in a Custom Tab.
- **Settings** — a Security section with the mode, the per-trade cap, the hard
  ceiling (raisable with a step-up token), the funds capability status and the
  heartbeat/resting-orders state.

## Build

```bash
export ANDROID_HOME=/path/to/android-sdk
export JAVA_HOME=/path/to/jdk-17
gradle -p android -PmmBaseUrl=https://your-service.onrender.com testDebugUnitTest assembleDebug
```

The APK lands in `android/app/build/outputs/apk/debug/app-debug.apk`.
`-PmmBaseUrl` sets the default server URL; the operator can change it on the
login screen.

CI builds the APK on every push to `android/**`:
`.github/workflows/android.yml` → **Actions → Android APK**.

## First run

1. Enter the HTTPS URL of your deployed agent (for example
   `https://multi-market-agent.onrender.com`).
2. Sign in with the same `ADMIN_USERNAME` / `ADMIN_PASSWORD` as the website.
   The token is stored in the app's private storage.
3. The header always shows `VENUE | PAPER` or a red `VENUE | LIVE`.

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
- `FLAG_SECURE` stays on, so a screenshot of the app is not a screenshot of your
  wallet. The copy button covers addresses.
- One idempotency key per user intent, reused if you tap again after a timeout
  and discarded after a definite result.

## Tests

```bash
gradle -p android testDebugUnitTest
```

Covers the money/price/Dhaka-time formatting, the trade sheet's order-type chips
following the venue capabilities, the estimate shown before confirming, and the
withdraw flow's idempotency-key handling.
