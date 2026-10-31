# PLAN — Full-control build (backend + native Android)

Branch: `feature/full-control`. Date: 2026-10-30 (Asia/Dhaka).

Extends the existing agent. Backend changes are **additive only**: existing routes and
response shapes keep working; new fields are optional. The agent's proposal flow stays
one way to trade, not the only one.

Ground rules kept from the base build:

- Every order is real, and a venue trades only after its authenticated doctor check passes.
- Fail closed. Unknown order outcomes are never resent.
- Decimal-as-string JSON everywhere.
- Money-moving POSTs need an `Idempotency-Key`; the server stores key → result for 24 h
  and replays the same result.

---

## (a) Endpoint table

All routes under `/v1`, all authenticated (`require_auth`: session cookie **or**
`Authorization: Bearer`). Decimal values are strings. Every money-moving POST requires
`Idempotency-Key`. `X-Confirm` is not used: step-up tokens replace it.

### Status / capabilities

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/v1/status` | yes | gains `capabilities`, `balance`, `limits.hard_ceilings` (new optional fields) |
| GET | `/v1/balance?venue=` | yes | per-venue `available`, `in_orders`, `positions_value`, `total`, `collateral_asset`, `as_of` |
| GET | `/v1/capabilities` | yes | same `capabilities` block, standalone |

### Manual trading

| Method | Path | Idem | Step-up | Notes |
|---|---|---|---|---|
| POST | `/v1/orders/limit` | yes | no | `{venue?, market_id, outcome, side, price, shares\|amount_usd, tif, expires_at?, post_only?, client_id}` |
| POST | `/v1/orders/market` | yes | no | `{venue?, market_id, outcome, side, shares\|amount_usd, max_slippage_cents}` |
| POST | `/v1/positions/sell` | yes | no | `{venue?, market_id, outcome, shares, price?}`; no price = market sell |
| GET | `/v1/orders?venue=&status=open` | – | – | open orders across venues |
| GET | `/v1/orders/{id}` | – | – | order detail |
| POST | `/v1/orders/{id}/cancel` | yes | no | never blocked by caps |
| POST | `/v1/orders/cancel-all` | yes | no | `{venue?, market_id?}`, idempotent, audited, may set pause |
| POST | `/v1/orders/{id}/amend` | yes | no | `{price?, shares?}`; `supported:false` where the venue lacks amend |

### Market data

| Method | Path | Notes |
|---|---|---|
| GET | `/v1/markets/{id}/book?depth=&outcome=&venue=` | venue public book, Decimal strings |
| GET | `/v1/markets/{id}/history?interval=&venue=` | venue price history; `supported:false` + url when absent |

### Positions

| Method | Path | Notes |
|---|---|---|
| POST | `/v1/positions/redeem` | `{venue, market_id, outcome?}`; `supported:false` when venue settles automatically |
| POST | `/v1/positions/merge` | Polymarket only, advanced, live only |
| POST | `/v1/positions/split` | Polymarket only, advanced, live only |

### History

| Method | Path | Notes |
|---|---|---|
| GET | `/v1/trades?venue=&market_id=&limit=&cursor=` | fills list |
| GET | `/v1/pnl/daily?days=30` | realized + unrealized series |
| GET | `/v1/funds/history?venue=` | deposits + withdrawals (Kalshi read-only) |

### Limits / auth

| Method | Path | Step-up | Notes |
|---|---|---|---|
| POST | `/v1/auth/stepup` | – | `{password}` → `{stepup_token, expires_in:120}`; single-use, session+action bound, login-rate-limited |
| POST | `/v1/limits/hard-ceiling` | raise only | `{name, value}`; raise needs step-up, lowering does not |

### Approvals

| Method | Path | Step-up | Notes |
|---|---|---|---|
| GET | `/v1/approvals?venue=` | – | current allowance state per venue |
| POST | `/v1/approvals` | yes | `{venue, kind}`; exact venue contract only, amount + spender shown first |

### Funds (`src/mm_agent/funds/`, off unless `FUNDS_ENABLED=true`)

| Method | Path | Step-up | Idem | Notes |
|---|---|---|---|---|
| GET | `/v1/funds/{venue}/deposit?network=&asset=` | – | – | `{supported, network, asset, address, memo?, min_amount, warnings[], official_url}` |
| GET/POST/DELETE | `/v1/funds/allowlist` | add | – | `{label, address, network, asset}`; checksum validated; cooling period before first use |
| POST | `/v1/funds/withdraw/preview` | – | – | `{venue, amount, address_id}` → fee, network, ETA, hold window, daily limit left |
| POST | `/v1/funds/withdraw` | yes | yes | hold window, daily limit, allowlist + cooling, Idempotency-Key |
| GET | `/v1/funds/withdrawals` | – | – | lifecycle states |
| POST | `/v1/funds/withdrawals/{id}/cancel` | – | – | during hold only |

### Realtime

`GET /v1/ws` accepts the session cookie **or** `Authorization: Bearer <token>`.
New topics: `balance.updated`, `trade.recorded`, `order.updated` (existing), `withdrawal.updated`.

---

## (b) Data model and Alembic migrations

New tables (one migration, `0004_full_control`):

| Table | Columns | Purpose |
|---|---|---|
| `idempotency_keys` | `key` PK, `scope`, `request_hash`, `response_json`, `created_at` | 24 h replay store |
| `withdrawals` | `id` PK, `venue`, `amount_usd`, `address_id`, `network`, `asset`, `status`, `hold_until`, `bridge_address`, `tx_hash`, `fee_usd`, `eta`, `created_at`, `submitted_at`, `confirmed_at`, `error` | funds lifecycle |
| `withdrawal_allowlist` | `id` PK, `label`, `address`, `network`, `asset`, `added_at`, `usable_at`, `removed_at` | allowlist + cooling |
| `stepup_tokens` | `token_hash` PK, `session_id`, `action`, `expires_at`, `used_at` | single-use step-up |
| `hard_ceilings` | `name` PK, `value`, `updated_at`, `actor` | raised ceiling survives restart |

Existing tables gain optional columns: `orders.order_type`, `orders.tif`, `orders.post_only`,
`orders.client_ref` (the app's intent id), `fills.venue`, `fills.market_title`, `fills.outcome`.

`hard_ceilings` replaces the `$2` constant as the *effective* ceiling:
`effective_max_trade_usd = min(runtime_cap, stored_ceiling)`. Default stored value stays `2`.

---

## (c) Threat model (10 lines)

1. Stolen phone → app lock, step-up for anything money-out, hold-to-confirm in LIVE.
2. Stolen session token → step-up still required for withdrawals, allowlist, ceiling raises.
3. Replayed request → `Idempotency-Key` store returns the first result, never re-executes.
4. Wrong address → allowlist + checksum + cooling period; withdrawal address is the *final recipient*.
5. Double submit → client id + idempotency + venue client order id; unknown orders are never resent.
6. Key custody → trading keys never gain withdrawal permission; funds use a separate signer secret.
7. Backend restart → resting orders are the venue's, not ours; heartbeat decision is explicit and surfaced.
8. Stale data → money-adding and money-out actions disabled offline; cancel/pause stay available.
9. Price/unit drift → one normalizer; the unit guard halts trading on mismatch.
10. Venue silent failure → every order/funds state is reported verbatim, never paraphrased as "sent".

---

## (d) Wireframes

### Wallet

```
┌──────────────────────────────┐
│ ← Wallet            LIVE ▮   │
├──────────────────────────────┤
│ POLYMARKET        pUSD        │
│ Available   12.40            │
│ In orders    1.10            │
│ Positions    2.05            │
│ Total       15.55            │
│ [ Deposit ]  [ Withdraw ]    │
├──────────────────────────────┤
│ KALSHI          USD           │
│ Available    0.00            │
│ ! Deposits/withdrawals not   │
│   available via API          │
│ [ Open Kalshi ]              │
├──────────────────────────────┤
│ LIMITLESS       USDC          │
│ Available    3.20            │
│ [ Deposit ]  [ Withdraw ]    │
├──────────────────────────────┤
│ Allowlist (2)   Approvals    │
│ Withdrawal history            │
└──────────────────────────────┘
```

### Withdraw review

```
┌──────────────────────────────┐
│ ← Withdraw                   │
├──────────────────────────────┤
│ Venue      POLYMARKET        │
│ Amount      10.00 pUSD       │
│ Network     Base (8453)      │
│ Token       USDC             │
│ Fee         0.00             │
│ ETA         ~2 min           │
│ Hold window 10 min           │
│ Daily left  15.00 of 25.00   │
├──────────────────────────────┤
│ Recipient (cold-storage)      │
│ 0x1234 5678 9abc def0 1234   │
│ 5678 9abc def0 1234 5678     │
│ 9abc                           │
├──────────────────────────────┤
│ [ Step-up ] password+biometric│
│      ╭──────────────╮        │
│      │ HOLD TO SEND│        │
│      ╰──────────────╯        │
└──────────────────────────────┘
```

### Trade sheet

```
┌──────────────────────────────┐
│ ← KALSHI  Will X happen?     │
├──────────────────────────────┤
│ [ BUY ] [ SELL ]   [YES][NO] │
│                              │
│ [Limit][Limit+Exp][Market]   │
│ [ ] Post-only                │
│                              │
│ Price      0.5700             │
│ Shares     10.00              │
│                              │
│ Cost       $5.70              │
│ Fee        $0.02              │
│ Max loss   $5.70              │
│ Payout     $10.00             │
│                              │
│      ╭────────────────╮      │
│      │ HOLD TO BUY   │      │
│      ╰────────────────╯      │
└──────────────────────────────┘
```

---

## (e) Per-venue support matrix

Every cell links the doc page verified on 2026-10-30. "deep link" = no API, app opens the venue.

| Capability | Polymarket | Kalshi | Limitless |
|---|---|---|---|
| Limit order GTC | yes — [place-orders](https://docs.polymarket.com/trading/place-orders) | yes — [create-order-v2](https://docs.kalshi.com/api-reference/orders/create-order-v2) | yes — [create-order](https://docs.limitless.exchange/api-reference/trading/create-order) |
| Limit order GTD | yes (expiry at least 3 min out, expires 1 min early) | no (no GTD in the V2 create schema) | no (GTC/FAK/FOK only) |
| Market order | yes FOK/FAK; price bound via signed amounts + `max_price`/`min_price` | yes IOC/FOK via `time_in_force` | yes FAK/FOK |
| Post-only | yes GTC/GTD only, rejected if it would cross | yes `post_only` field | no (not documented) |
| Cancel one | yes `cancel_order` | yes `DELETE /portfolio/events/orders/{id}` (+ `market_ticker` to auto-route) | yes `DELETE /orders/{id}` |
| Cancel many / all | yes `cancel_all`, `cancel_market_orders` | yes `DELETE /portfolio/events/orders`, batch `POST …/batched` | yes `DELETE /orders/all` |
| Amend / decrease | no | yes `POST …/{id}/amend`, `POST …/{id}/decrease` | no |
| Batch orders | yes up to 15 per request | yes `POST /portfolio/events/orders/batched` | no (not documented) |
| Deposit | yes Bridge `POST /deposit` + `GET /supported-assets` | no — history only, deep link | no API — transfer to the wallet on Base |
| Withdraw | yes Bridge `POST /quote` + `POST /withdraw` | no — history only, deep link | partner server-wallet only |
| Redeem | yes Relayer / SDK `redeem_positions` | no — settles automatically, show Settled | on-chain for EOA; partner-only otherwise |
| Merge / split | yes SDK `split_position` / `merge_positions` | no | partner server-wallet only |
| Approvals | yes ERC-20/ERC-1155 via `setup_trading_approvals` | no (not needed, off-exchange) | yes collateral + conditional-token approvals |
| History | yes positions, trades, activity | yes fills, settlements, credit history | yes positions, orders |
| Funds history | yes bridge `GET /status/{address}` | yes `GET /portfolio/deposits` + `/withdrawals` | no (not documented) |
| Price history | yes `GET /prices-history` | yes `GET /markets/candlesticks` | no documented endpoint |
| Heartbeat | `POST /v1/heartbeats` exists but **not used** (see below) | no | no |

### Heartbeat decision

Polymarket's heartbeat is all-or-nothing: the first accepted send enables timeout
cancellation for **all** open orders of the API key, and there is **no disarm** operation
— stopping the sender leaves the deadline active. A backend restart would therefore
silently wipe resting orders.

**Decision: the backend does NOT use the heartbeat.** Resting orders survive a restart, and
the app shows `heartbeat: "disabled"` in Settings so the operator knows the trade-off.
Enabling it would need both a setting and an explicit "I accept that a restart cancels all
resting orders" confirmation; it is not implemented in this build.

---

## (f) Could not verify / not built

| Item | Why | Returned as |
|---|---|---|
| Limitless price history endpoint | no documented endpoint found in the 2026-10-30 docs | `supported:false` + `https://docs.limitless.exchange` |
| Limitless `POST /portfolio/withdraw` | partner server-wallet only, needs `withdrawal` scope | `supported:false` for a normal key |
| Limitless `POST /portfolio/redeem` / `split` / `merge` | same | `supported:false` for a normal key |
| Limitless approvals detail | contract addresses not published on the page read | not built; capabilities say so |
| Kalshi deposit/withdraw initiation | only history endpoints exist in the OpenAPI spec | `supported:false` + deep link |
| Kalshi GTD | not in the V2 create schema | not offered as a chip |
| Polymarket GTD via SDK | SDK `place_limit_order` exposes `expiration` but no documented GTD floor in code | offered only when the venue allows it |
| Kalshi sub-cent tick grid | known gap in the base build | unchanged; flagged |
| Polymarket funder vs signer wallet | the funder/proxy wallet can differ from the signer | operator must confirm the address in the app |
| Push notifications | not requested in scope | not built |

---

## Where the docs disagreed with the brief

1. **Kalshi V2 order types.** The brief says "V2 uses single-book bid/ask sides, fixed-point
   dollar prices, and a required client_order_id". The OpenAPI marks `client_order_id` as
   *optional* in `CreateOrderV2Request`. We still always send one.
2. **Kalshi cancel route.** The brief says "cancel one, many, by market, or all". V2 exposes
   `DELETE /portfolio/events/orders/{id}` (one), `DELETE /portfolio/events/orders` (all) and
   `POST /portfolio/events/orders/batched` (batch). There is no "cancel by market" on V2;
   the closest is `market_ticker` auto-routing plus a market-scoped cancel-all.
3. **Kalshi amend/decrease exist**, which the brief assumed ("Kalshi V2"). Confirmed in the
   OpenAPI spec.
4. **Polymarket collateral is pUSD**, and the Bridge collateral is pUSD on Polygon while
   deposits are listed as USDC/USDC.e per chain. The brief's "never hardcode USDC" is right:
   the asset name comes from `/supported-assets`.
5. **Polymarket has no explicit worst-price field in the raw order body.** The bound is
   expressed through the signed `makerAmount`/`takerAmount` plus the SDK's `max_price` /
   `min_price`. The app still asks for a slippage bound; the backend converts it.
6. **Kalshi candles are public and exist** at `GET /markets/candlesticks` (batch, up to 100
   tickers) — the brief did not mention them.
7. **Limitless has no documented price-history endpoint**, which the brief's "GET
   /v1/markets/{id}/history from each venue's public endpoints" assumed.
8. **Limitless `POST /portfolio/split` and `/merge` exist but are server-wallet only**, so
   the brief's "Merge and Split under Advanced on Polymarket" is right for Polymarket and
   unsupported for a normal Limitless key.

---

## Build order

1. Backend core: idempotency store, step-up, hard ceilings, capabilities, balance.
2. Backend manual trading: limit/market/sell, cancel, cancel-all, amend, order listing.
3. Backend history: trades, pnl daily, market book/history.
4. Backend funds module: deposit, allowlist, withdraw lifecycle, approvals.
5. Backend realtime: WS bearer + new topics.
6. Android native app: design system, 5-tab shell, market detail, trade sheet, portfolio, wallet, withdraw, settings.
7. Tests: pytest (fake venue) + Android unit tests.
8. Docs: SECURITY.md, README.md, android/README.md, DOCS_FINDINGS.md, .env.example, render.yaml.
