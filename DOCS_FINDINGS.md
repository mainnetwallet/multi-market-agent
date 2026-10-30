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
- All live placement is behind `LIVE_<VENUE>=true`; no authenticated/key-bearing
  call is needed for PAPER or public reads.
- The build's own public doctor results are in the final report; the exact text
  at recon was: Polymarket OK, Kalshi DEGRADED (credentials absent; public and
  market reads pass), Limitless OK.
