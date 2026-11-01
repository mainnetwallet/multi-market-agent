# DOCS_FINDINGS — venue reconnaissance

Date of recon: **2026-10-30** (Asia/Dhaka). Documentation changes constantly. This
file records what the current official docs and live public endpoints actually
said on that date, including differences from the build prompt. **Docs and live
contracts beat the prompt.** No authenticated venue calls or real orders were
made while producing this file.

## Status summary

| Venue | Public read verified live | Authenticated read | Order roundtrip | Sandbox |
|---|---|---|---|---|
| Polymarket | Yes (`gamma-api`, CLOB book) | Not run (no credentials) | Not run — opt-in only | No |
| Kalshi | Yes (exchange status, events/markets, orderbook) | Not run (no credentials) | Not run — demo/opt-in only | Yes |
| Limitless | Yes (active markets, orderbook) | Not run (no credentials) | Not run — no sandbox | No |

`scripts/doctor.py` is the reproducible source for the live row. It never places
an order unless `--i-accept-real-money` (or `--kalshi-demo` for Kalshi) is given.

---

## 1. Polymarket

### Sources (accessed 2026-10-30)

- API overview: https://docs.polymarket.com/getting-started/api
- Python SDK: https://docs.polymarket.com/getting-started/python
- Wallets & authentication: https://docs.polymarket.com/trading/wallets-auth
- Place first order: https://docs.polymarket.com/trading/quickstart
- Place orders: https://docs.polymarket.com/trading/place-orders
- Fees: https://docs.polymarket.com/trading/fees
- Discover markets: https://docs.polymarket.com/market-data/discover-markets
- Prices & order books: https://docs.polymarket.com/concepts/prices-orderbook
- OpenAPI: https://docs.polymarket.com/api-spec/clob-openapi.yaml and
  https://docs.polymarket.com/api-spec/gamma-openapi.yaml
- Live probes: `GET https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=2`,
  `GET https://clob.polymarket.com/book?token_id=<id>`

### What changed vs the prompt

- **Polymarket Protocol V2 is live alongside legacy CTF (v1) markets.** An
  outcome now has both a CTF `tokenId` and a V2 `positionId`; the SDK calls the
  trading identifier `asset_id` and requires the correct one for the market
  version. Gamma responses can have `clobTokenIds` as a JSON-encoded string or
  `positionIds` as a string array (one or the other may be null).
- **Collateral is pUSD**, not plain USDC, in current documentation. The account
  wallet types are EOA, POLY_PROXY, GNOSIS_SAFE and DEPOSIT_WALLET. Wallets
  created on/after 2026-05-04 are Deposit Wallets; they can delegate scoped
  session keys.
- The official Python SDK is now **`polymarket-client`** (package import
  `polymarket`, version 0.15.0 at recon), not `py-clob-client` (which still
  exists on PyPI but is legacy). `AsyncSecureClient` provides
  `get_balance_allowance`, `list_positions`, `place_limit_order`,
  `list_open_orders`, `list_account_trades`, `cancel_order` and
  `cancel_market_orders`.
- Fee formula is `fee = shares × feeRate × p × (1-p)` for takers; makers pay
  zero. Documented category taker rates: Crypto 7%, Sports 5%, Finance /
  Politics / Mentions / Tech 4%, Economics / Culture / Weather / Other 5%,
  Geopolitics 0%, Combos 5%. Fees are rounded to 5 decimals.
- Geopolitical/world-events markets are fee-free.

### Units and wire shapes observed

- Gamma `outcomePrices` are probability strings (`"0.0285"`), not cents.
- CLOB `/book` returns arrays called `bids` and `asks` with **best price at the
  end** in the live probe (bids ascending worst→best, asks descending
  worst→best). The adapter sorts both explicitly; never assumes array order.
- `timestamp` is Unix milliseconds as a string.
- Top-level market ids are Gamma numeric ids; condition id is the 0x hash.
  Namespaced app refs are `polymarket:<gamma-id>`.
- Tick size comes from `orderPriceMinTickSize`; minimum order size from
  `orderMinSize`.

### Auth and orders

- L1: EIP-712 `ClobAuth` over domain `ClobAuthDomain`/version 1/chainId 137,
  fields address, timestamp, nonce, message. This creates or derives L2
  credentials (API key, base64 secret, passphrase).
- L2: HMAC-SHA256 over `timestamp + method + path(without query) + body`,
  URL-safe base64, sent as `POLY_*` headers. Signed orders are EIP-712 and the
  signer/maker depends on wallet type.
- The app uses the official SDK for private reads and signed orders. The private
  key is read only inside `venues/polymarket_signing.py`.
- Unknown outcome handling: the SDK order response has `status` (`live`,
  `matched`, `delayed`) and amounts. Our live gateway never resends on timeout.
- Eligibility: some jurisdictions are restricted. The agent does not bypass
  geo-blocks, KYC or venue rules. Market `restricted` flags are surfaced by the
  venue; eligibility remains the operator's responsibility.

### Open issues / gaps

- No authenticated read or order test was possible without operator credentials.
- V2 vs CTF order signing details are delegated to the official SDK. Do not
  hand-roll order signing without re-reading the V2 migration guide.
- The SDK's `place_limit_order` does not expose a client id; reconciliation for
  a pre-response timeout must match open orders by asset/price/size/time. This is
  inherently weaker than Kalshi/Limitless dedupe ids and is documented as a gap.

---

## 2. Kalshi

### Sources (accessed 2026-10-30)

- Environments: https://docs.kalshi.com/getting_started/api_environments
- Authenticated requests: https://docs.kalshi.com/getting_started/quick_start_authenticated_requests
- Create first order: https://docs.kalshi.com/getting_started/quick_start_create_order
- Create Order V2: https://docs.kalshi.com/api-reference/orders/create-order-v2
- Cancel Order V2: https://docs.kalshi.com/api-reference/orders/cancel-order-v2
- Cancel All: https://docs.kalshi.com/api-reference/orders/cancel-all-orders
- Balance: https://docs.kalshi.com/api-reference/portfolio/get-balance
- Orderbook: https://docs.kalshi.com/getting_started/orderbook_responses
- Fixed point: https://docs.kalshi.com/getting_started/fixed_point_migration
- OpenAPI: https://docs.kalshi.com/openapi.yaml
- Live probes: exchange status, `/events?with_nested_markets=true`, market orderbooks

### What changed vs the prompt

- **Prices are no longer integer cents in the current wire format.** V2 uses
  fixed-point dollar strings (`yes_bid_dollars`, `yes_ask_dollars`,
  `price: "0.5600"`), with up to 4 decimals, plus `price_ranges` per market that
  define the valid tick grid (some markets have sub-cent ticks).
- **Counts are fixed-point strings** (`count_fp`, `fill_count`, `remaining_count`),
  fractional to 0.01 contracts. Requests accept up to 2 decimals.
- Orderbook response is `orderbook_fp` with **bids only**:
  `yes_dollars` and `no_dollars` arrays of `[price_dollars, count_fp]`, sorted
  ascending with the **best bid last**. YES ask = 1 − best NO bid; NO ask =
  1 − best YES bid.
- New event-market orders use **`POST /portfolio/events/orders`** with
  `side: bid|ask` on the single YES book, fixed-point `price` and `count`, and
  required `time_in_force` + `self_trade_prevention_type`. Legacy
  `/portfolio/orders` is being deprecated (no earlier than 2026-05-06).
- Cancel V2 is `DELETE /portfolio/events/orders/{order_id}`. Auto-routing needs
  `market_ticker` as a query parameter; an order id alone cannot identify the
  shard. Cancel-all is `DELETE /portfolio/events/orders`.
- Base URLs: production `https://external-api.kalshi.com/trade-api/v2` or
  `https://api.elections.kalshi.com/trade-api/v2`; demo
  `https://external-api.demo.kalshi.co/trade-api/v2` or
  `https://demo-api.kalshi.co/trade-api/v2`.
- Auth headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (milliseconds),
  `KALSHI-ACCESS-SIGNATURE` (base64). Sign the full path from API root without
  query: `timestamp + METHOD + path`. RSA keys use RSA-PSS/SHA-256; Ed25519 keys
  sign directly.
- Discovery: raw `/markets` returned **200 of 200 multivariate combo markets**
  in the live probe. Walking `/events?with_nested_markets=true` is required to
  reliably find ordinary binary markets. Combos are excluded in v1.
- Balance response includes both cents (`balance`) and `balance_dollars`;
  portfolio value is in cents.
- Fees: the current docs catalog fee schedule separately; this build uses the
  long-standing conservative taker model `ceil_to_cent(0.07 × contracts × p ×
  (1−p))` and records `fee_info.model=kalshi_standard`. **Verify the exact
  series fee before LIVE.** Maker orders are treated as zero fee. A market with
  no fee information is not tradable by policy.

### Units and wire shapes observed

- Market ids are tickers inside event/series tickers. Namespaced app refs are
  `kalshi:<ticker>`.
- Market status values seen: `active`, `closed`, `settled`; event status is
  separate. Only `active` is considered OPEN.
- Orderbook can be empty for a newly created market; a doctor's market check
  accepts a readable, well-formed empty book but a trading unit check needs a
  real book.
- `price_ranges` example from a live market:
  `[{start: "0.0000", end: "0.0100", step: "0.0001"}, {start: "0.0100", end:
  "0.9900", step: "0.0010"}, {start: "0.9900", end: "1.0000", step: "0.0001"}]`.
  The adapter stores the central-band tick for strategy maths and snaps live
  order prices against the exact per-price `price_ranges` band. Strategy edge
  estimates can still be slightly coarse near the edges; confirm with a tiny
  order before scaling on sub-cent markets.

### Eligibility

Kalshi is US-regulated with KYC and state restrictions. The agent marks the
venue unavailable when the account/read fails and never attempts to bypass
eligibility. Demo keys are separate from production keys.

---

## 3. Limitless

### Sources (accessed 2026-10-30)

- API reference: https://docs.limitless.exchange/api-reference/introduction
- Python quick start: https://docs.limitless.exchange/developers/quickstart/python
- Authentication: https://docs.limitless.exchange/developers/authentication
- EIP-712 signing: https://docs.limitless.exchange/developers/eip712-signing
- Create order: https://docs.limitless.exchange/api-reference/trading/create-order
- Orderbook: https://docs.limitless.exchange/api-reference/trading/orderbook
- User orders: https://docs.limitless.exchange/api-reference/trading/user-orders
- Cancel order: https://docs.limitless.exchange/api-reference/trading/cancel-order
- Fees: https://docs.limitless.exchange/user-guide/fees
- Current profile: https://docs.limitless.exchange/api-reference/portfolio/get-current-profile
- Positions: https://docs.limitless.exchange/api-reference/portfolio/positions
- Live probes: `/markets/active`, `/markets/<slug>/orderbook`

### What changed vs the prompt

- Limitless is on **Base** and uses **USDC** (6 decimals).
- Programmatic auth is now **scoped API tokens with HMAC request signing**:
  `lmts-api-key`, `lmts-timestamp` (ISO-8601), `lmts-signature`
  (base64 HMAC-SHA256). Message is
  `timestamp + "\n" + METHOD + "\n" + path_with_query + "\n" + exact_body`.
  Legacy static API keys are deprecated and no longer issued; cookie session
  auth is deprecated.
- Orders need a second auth layer: EIP-712 signature over domain
  `Limitless CTF Exchange`, version 1, chainId 8453, and the market's
  `venue.exchange` as `verifyingContract`. Fields: salt, maker, signer, taker,
  tokenId, makerAmount, takerAmount, expiration (0), nonce (0), feeRateBps, side
  (0 buy / 1 sell), signatureType (0 EOA).
- Order body needs `ownerId` (from `GET /profiles/me`), `orderType` (GTC/FAK/FOK),
  `marketSlug`, and optionally `clientOrderId` for dedupe. A duplicate client id
  returns 409 and is not replayed.
- Amounts are raw 6-decimal integers. Price is a 3-decimal number and
  `price × contracts` must equal the collateral amount exactly, integer raw
  units. FAK/GTC require a price; FOK forbids it.
- On markets using new trading venues (`venue.feeModuleType: "COLLATERAL"`) the
  signed `feeRateBps` must be **0**, regardless of profile rate. A separate USDC
  approval to `venue.feeModule` is required for non-post-only orders.
- The orderbook endpoint exposes only the **YES book**. NO is derived by
  flipping bids/asks and replacing p with 1−p. Sizes are raw 6-decimal position
  units. `minSize` is also raw units.
- Market discovery `/markets/active` returns `{data: [...]}`. `tokens.yes`/`no`
  are position ids; `tradeType` can be `clob` or `amm` (only CLOB supported).
  The active list can include short-duration crypto markets and expired ones;
  the adapter filters by status/`expired`.
- Positions endpoint returns a `clob` array with per-outcome `positions` and
  `tokensBalance`; costs/PnL are raw 6-decimal units.
- There is no generic account-balance endpoint in the current REST reference.
  The adapter reads Base USDC `balanceOf` via JSON-RPC (`BASE_RPC_URL`) after an
  authenticated profile read. If RPC is unavailable, balance is unavailable.
- There is **no demo environment**. The order roundtrip is disabled unless the
  operator passes `--i-accept-real-money`.
- Current fee docs (https://docs.limitless.exchange/user-guide/fees): legacy CLOB
  buys range 0.40–3.00% (paid in outcome tokens), legacy sells 0.42–1.50% (USDC),
  makers free. New trading venues charge **5% × (1 − price) of USDC value on both
  buys and sells**, still takers only. The adapter computes the collateral-venue
  fee exactly and approximates the legacy curve (USD-equivalent for buys) because
  the curve is not published as a closed form. Compare the first fill's
  `effectiveFeeBps`/`usdFee` before scaling.
- Wallet mode matters: if `tradeWalletOption` is `smartWallet`, self-signed API
  orders fail; the doctor reports this as unavailable. The operator must switch
  the profile to EOA mode.

### Live probe observation (2026-10-30)

`/markets/active` included a 5-minute ETH Up/Down market. Its YES orderbook
returned `bids: []` and asks at 0.099/0.100/... plus far-tail 0.969+; the NO book
is derived. This confirms that book shape changes with market phase and that
research/risk must fail closed on one-sided or odd books.

---

## 4. Cross-venue implications recorded in code

- Prices are normalized to `Decimal` probabilities in (0, 1) by ONE function
  (`domain.normalize_probability`). The startup guard halts trading on values
  like `37` (cents) or `37c` rather than guessing.
- Adapters expose a single index price, but order placement uses each venue's
  tick alignment. Kalshi sub-cent edge bands remain a go-live blocker.
- No authenticated or key-bearing call is needed for public reads.
- The build's own public doctor results are in the final report; the exact text
  at recon was: Polymarket OK, Kalshi DEGRADED (credentials absent; public and
  market reads pass), Limitless OK.

---

## 5. Full-control recon (2026-10-30, second pass)

Every endpoint below was read from the venue's own documentation on 2026-10-30.
Where the build prompt and the docs disagreed, the docs win and the difference is
recorded. Nothing here was verified against a live authenticated account: no
credential was available, so no real order and no authenticated read was made.

### Sources read

| Venue | Pages |
|---|---|
| Polymarket | https://docs.polymarket.com/trading/place-orders · https://docs.polymarket.com/trading/bridge/deposit · https://docs.polymarket.com/trading/bridge/withdraw · https://docs.polymarket.com/trading/bridge/quote · https://docs.polymarket.com/trading/bridge/supported-assets · https://docs.polymarket.com/trading/bridge/status · https://docs.polymarket.com/llms.txt · specs: `/api-spec/clob-openapi.yaml`, `/api-spec/bridge-openapi.yaml`, `/api-spec/relayer-openapi.yaml` |
| Kalshi | https://docs.kalshi.com/api-reference/orders/create-order-v2 · https://docs.kalshi.com/api-reference/orders/cancel-order-v2 · https://docs.kalshi.com/api-reference/portfolio/get-deposits · https://docs.kalshi.com/api-reference/portfolio/get-withdrawals · https://docs.kalshi.com/openapi.yaml (fetched and grepped) |
| Limitless | https://docs.limitless.exchange/developers/authentication · https://docs.limitless.exchange/developers/programmatic-api · https://docs.limitless.exchange/user-guide/merge-split · https://docs.limitless.exchange/developers/quickstart/websocket · https://docs.limitless.exchange/llms.txt |

### Polymarket — verified endpoints

| Purpose | Method + path | Notes |
|---|---|---|
| Place order | `POST https://clob.polymarket.com/order` | body `{order:{maker,signer,tokenId,makerAmount,takerAmount,side,expiration,timestamp,metadata,builder,signature,salt,signatureType}, owner, orderType, deferExec, postOnly}`; `orderType` in GTC/GTD/FAK/FOK |
| Batch orders | `POST /orders` | 1 to 15 signed orders per call, each evaluated independently |
| Cancel one | `DELETE /cancel` | body `{order_id, market_id}` |
| Cancel all | `POST /cancel-all` | – |
| Cancel by market | `POST /cancel-market-orders` | – |
| Order book | `GET /book?token_id=` | public |
| Price history | `GET /prices-history?market=<token id>&interval=&fidelity=` (`token_id` is rejected with HTTP 400; verified against docs.polymarket.com 2026-11-01) | – |
| Heartbeat | `POST /v1/heartbeats` | body `{heartbeat_id}`; legacy `POST /heartbeats` |
| Bridge deposit | `POST https://bridge.polymarket.com/deposit` | body `{address}`; returns `{address:{evm,svm,btc,tron}, note}` |
| Bridge supported assets | `GET /supported-assets` | `{supportedAssets:[{chainId,chainName,token:{name,symbol,address,decimals},minCheckoutUsd}]}` |
| Bridge quote | `POST /quote` | body `{fromAmountBaseUnit,fromChainId,fromTokenAddress,recipientAddress,toChainId,toTokenAddress}`; response `{estCheckoutTimeMs,estInputUsd,estOutputUsd,estToTokenBaseUnit,quoteId,estFeeBreakdown{appFeeUsd,gasUsd,minReceived,maxSlippage,...}}` |
| Bridge withdraw | `POST /withdraw` | body `{address,toChainId,toTokenAddress,recipientAddr}`; returns the same `{address:{...}}` shape |
| Bridge status | `GET /status/{address}` | cursor-paginated; `nextCursor` |
| Relayer submit | `POST https://relayer-v2.polymarket.com/submit` | `RELAYER_API_KEY` + `RELAYER_API_KEY_ADDRESS` headers, or builder-key HMAC headers |

**Verified details that changed the build:**

- **Order types.** GTC, GTD, FOK, FAK. FAK is the SDK default for market orders.
- **postOnly** applies to limit orders only (GTC and GTD) and is *rejected* if it
  would match immediately against the book.
- **GTD expiry** must be at least 3 minutes in the future, and the order expires one
  minute *before* its stated expiration. The minimum effective lifetime is about
  two minutes.
- **There is no explicit worst-price field in the raw order body.** The bound is
  expressed through the signed `makerAmount`/`takerAmount`; the SDK exposes it as
  `max_price` (BUY) and `min_price` (SELL), plus a separate fee-inclusive
  `max_spend`. The build converts the app's slippage bound into `max_price`.
- **Heartbeat is all-or-nothing.** The first accepted send enables timeout
  cancellation for **all** open orders of the API key, across markets and processes.
  There is **no disarm operation**: stopping the sender leaves the deadline active,
  and after 10 s without an accepted send the server attempts a cancel-all. A
  heartbeat id mismatch returns HTTP 400 with the expected id.
  **Decision: the backend does not use it.** Resting orders survive a restart, and
  the app shows the heartbeat state so the operator knows the trade-off.
- **Bridge minimums** from the live `/supported-assets` shape: $7 on Ethereum, $2 on
  Polygon/Arbitrum/Base/Optimism/BNB/Solana and most others, $9 on Bitcoin and Tron.
  Deposits below the minimum are not processed.
- **Withdraw addresses are destination-scoped.** The docs say to generate them only
  when ready to execute, so the build requests the bridge address at submission
  time and checks it against the withdrawal record.
- **Collateral is pUSD**, not USDC. The bridge unwraps pUSD on Polygon.

### Kalshi — verified endpoints

| Purpose | Method + path | Notes |
|---|---|---|
| Create order (V2) | `POST /portfolio/events/orders` | body `{ticker, side: bid\|ask, count, price, time_in_force, client_order_id, self_trade_prevention_type, post_only?, reduce_only?, subaccount?, order_group_id?, exchange_index?}` |
| Amend order (V2) | `POST /portfolio/events/orders/{order_id}/amend` | body `{ticker, side, price, count, expiration_time?, client_order_id?, updated_client_order_id?}` |
| Decrease order (V2) | `POST /portfolio/events/orders/{order_id}/decrease` | body `{reduce_by}` or `{reduce_to}` (exactly one) |
| Cancel order (V2) | `DELETE /portfolio/events/orders/{order_id}` | query `market_ticker`, `exchange_index`, `subaccount` |
| Batch create (V2) | `POST /portfolio/events/orders/batched` | body `{orders:[...]}` |
| Batch cancel (V2) | `DELETE /portfolio/events/orders/batched` | body `{orders:[{order_id, subaccount?, exchange_index?, market_ticker?}]}` |
| Cancel all | `DELETE /portfolio/events/orders` | – |
| Open orders | `GET /portfolio/orders` | – |
| Fills | `GET /portfolio/fills` | – |
| Balance | `GET /portfolio/balance` | – |
| Deposits (history) | `GET /portfolio/deposits` | – |
| Withdrawals (history) | `GET /portfolio/withdrawals` | – |
| Candlesticks | `GET /markets/candlesticks` | query `market_tickers` (up to 100), `start_ts`, `end_ts`, `period_interval` (1/60/1440) |
| Subaccounts | `GET/POST /portfolio/subaccounts`, `POST /portfolio/subaccounts/transfer`, `GET /portfolio/subaccounts/balances`, `GET /portfolio/subaccounts/transfers` | – |

**Verified details that changed the build:**

- `client_order_id` is documented as **optional** in `CreateOrderV2Request`, not
  required as the brief said. The build always sends one anyway.
- `time_in_force` is one of `good_till_canceled` / `fill_or_kill` /
  `immediate_or_cancel`. There is **no good-till-date** in the V2 schema, so the
  app does not offer GTD on Kalshi.
- The amend request `count` is the order's **total** max-fillable count (already
  filled plus desired resting), not the new resting size.
- `side` is `bid`/`ask` on the **YES book only**.
- **There is no deposit or withdrawal initiation endpoint in the OpenAPI spec.**
  `GET /portfolio/deposits` and `GET /portfolio/withdrawals` are history only, with
  `type` in ach/wire/crypto/debit/apm and `status` in pending/applied/failed/returned.
  Amounts are integer cents. The app shows this history read-only and deep-links
  to Kalshi for the actual movement of money.
- Kalshi settles automatically, so there is no redeem call; the app shows a Settled
  state instead.
- The legacy `/portfolio/orders` endpoint still exists in the spec.

### Limitless — verified endpoints

| Purpose | Method + path | Notes |
|---|---|---|
| Active markets | `GET /markets/active` | – |
| Order book | `GET /markets/{slug}/orderbook` | YES book only; NO is derived |
| User orders | `GET /markets/{slug}/user-orders?statuses=LIVE` | – |
| Create order | `POST /orders` | body `{order:{salt,maker,signer,taker,tokenId,makerAmount,takerAmount,expiration,nonce,feeRateBps,side,signature,signatureType,price}, ownerId, orderType, marketSlug, clientOrderId, stpPolicy}` |
| Cancel order | `DELETE /orders/{id}` | – |
| Cancel all | `DELETE /orders/all` | – |
| Positions | `GET /portfolio/positions` | – |
| Profile | `GET /profiles/me` | – |
| Redeem (partner) | `POST /portfolio/redeem` | **server wallet only**; scope `trading` |
| Withdraw (partner) | `POST /portfolio/withdraw` | **server wallet only**; scope `withdrawal` |
| Withdraw allowlist (partner) | `POST /portfolio/withdrawal-addresses`, `DELETE /portfolio/withdrawal-addresses/:address` | Privy auth |
| Split (partner) | `POST /portfolio/split` | server wallet only |
| Merge (partner) | `POST /portfolio/merge` | server wallet only |

**Verified details that changed the build:**

- Token scopes are exactly `trading`, `account_creation`, `delegated_signing` and
  `withdrawal`. `account_creation` and `delegated_signing` require an approved
  partner application ("applications from individual traders and personal bot
  runners are not approved").
- `withdrawal` is defined as "Withdraw ERC20 balances from **managed server-wallet
  sub-accounts** to partner-owned addresses." A normal EOA account cannot use it.
  The build reports `supported: false` for a normal key and says why.
- Redeem is blocked until the conditional token payout is settled on-chain
  (`payoutDenominator(conditionId)` still 0); `status: RESOLVED` alone does not
  guarantee redeemability. A split resolution has `winningOutcomeIndex: null` plus
  a `payoutNumerators` array.
- For a normal wallet, redeem is an on-chain transaction. Merge/split for a normal
  wallet means calling `splitPosition`/`mergePositions` on the Conditional Tokens
  Framework contract directly; the API routes sign with a managed server wallet.
- Order types are GTC, FAK and FOK. Post-only is not documented.
- Amounts are raw 6-decimal integers; `price × contracts` must equal the collateral
  amount exactly.
- **No price-history endpoint is documented.** The app shows that capability as
  unavailable rather than drawing an empty chart.
- The docs mention geographic restrictions. The build surfaces the venue's own
  reason string when it reports one.

### Where the docs disagreed with the build prompt

1. Kalshi `client_order_id` is optional, not required.
2. Kalshi has no "cancel by market" on V2; the closest is `market_ticker`
   auto-routing plus a market-scoped cancel.
3. Polymarket has no explicit worst-price field in the raw order body; the bound
   rides in the signed amounts, exposed by the SDK as `max_price`/`min_price`.
4. Kalshi *does* have amend and decrease, which the prompt assumed.
5. Kalshi candles are public (`GET /markets/candlesticks`) — the prompt did not
   mention them.
6. Limitless has **no** documented price-history endpoint, which the prompt's
   "GET /v1/markets/{id}/history from each venue's public endpoints" assumed.
7. Limitless split/merge exist but are server-wallet only, so "Merge and Split
   under Advanced on Polymarket" is right for Polymarket and unsupported for a
   normal Limitless key.
8. Polymarket's heartbeat has no disarm, so it cannot be used "safely" — it
   actively contradicts the requirement that a backend restart never silently
   wipes resting orders.

---

## 6. Polymarket public data recon (2026-11-01)

What the official docs list that the build did not yet use. Read from the venue's own pages; nothing
here was called live (the sandbox that wrote it could not reach `data-api.polymarket.com`).

### Sources

- Data API v2 overview: https://docs.polymarket.com/api-reference/data-api/overview
- Data API v2 contract: https://data-api.polymarket.com/v2/openapi.json (read in full)
- v1 to v2 migration: https://docs.polymarket.com/migrate/data-api-v1-to-v2
- Rate limits: https://docs.polymarket.com/api-reference/rate-limits
- CLOB OpenAPI pages for `/midpoint`, `/midpoints`, `/spread`, `/last-trades-prices`

### What changed vs earlier recon

- **Data API v2 exists** at `https://data-api.polymarket.com/v2`. Public, no key. It replaces the original
  per-route contracts; the old routes keep working. Every response is `{"data": ..., "pagination": ...}`.
- **Pagination is cursor-only.** Sending `offset` is a 400. `pagination.next_cursor` is opaque and signed;
  `has_more` is exact. On `positions` a bare `?cursor=` is a 400: send the same `user` or `condition`
  anchor and filters again. `holders` wants `condition` again, the feeds want identical filters on every
  page, and the boards pin the window and category the cursor was minted on.
- **Units.** Bare `volume`/`size` are shares; `_usdc` fields are dollars; `null` means unavailable.
  `outcome_index: 999` means the outcome could not be labelled.
- **Errors** carry `error`, a stable `code`, `retryable` and `trace_id`; a 429 or 503 has `Retry-After`.
- **Windows.** `/v2/trades?user=` and `/v2/activity` floor an omitted `start` to three years back
  (`start=1` is full history). `/v2/prices-history` rejects a `0` bound.
- **Price history.** Graded store: 1-minute data for at least 7 days, 5-minute for 60, 30-minute for 90,
  3-hour and 12-hour permanently (back to 2022-11-18). Sending `bucket_seconds` is served exactly as asked,
  which can mean an empty page; omitting it lets the server pick a width that covers the window.
- **CLOB `/midpoint` answers `{"mid_price": "0.45"}`** in the official OpenAPI. Some SDKs name the field
  `mid`. The build passes the body through unchanged and does not read either name.
- **Last trade prices** have a batch route, `POST /last-trades-prices`, capped at 500 token ids; `/midpoints`
  takes `token_ids` as a comma list on GET or `[{token_id}]` on POST.
- The Data API's v1 `accounting/snapshot` has no v2 counterpart.

### Routes added to the build

`/v2/positions`, `/v2/value`, `/v2/approvals`, `/v2/user-stats`, `/v2/user-pnl`, `/v2/user-volume`,
`/v2/trades`, `/v2/activity`, `/v2/holders`, `/v2/oi`, `/v2/live-volume`, `/v2/resolutions`, `/v2/tokens`,
`/v2/prices-history`, `/v2/leaderboard`, `/v2/biggest-winners`, `/v2/status`; CLOB `/midpoint`,
`/midpoints`, `/spread`, `/last-trade-price`, `/price`, `/last-trades-prices`.
Served as `/v1/polymarket/data/{name}` and `/v1/polymarket/price/{kind}`; see the README.

### Listed by the venue, not added

Perps API (`api.perpetuals.polymarket.com`, HTTP and WebSocket, a separate product), Data API combo
routes (`/v2/positions/combos`, `/v2/activity/combos`), builder boards (`/v2/builders/leaderboard`,
`/v2/builders/volume`), Gamma series/comments/sports, CLOB batch `/books` and `/spreads`, sports and RFQ
WebSocket channels.
