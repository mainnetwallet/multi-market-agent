package com.mainnetwallet.mmagent.data

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.math.BigDecimal
import java.math.RoundingMode
import java.util.UUID
import com.mainnetwallet.mmagent.ui.BookLevel
import com.mainnetwallet.mmagent.ui.PricePoint

/** What the operator is building, before anything is sent. */
data class TradeDraft(
    val marketId: String = "",
    val venue: String = "",
    val title: String = "",
    val outcome: String = "YES",
    val side: String = "BUY",
    val orderType: String = "GTC",
    val postOnly: Boolean = false,
    val price: String = "",
    val shares: String = "",
    val amountUsd: String = "",
    val maxSlippageCents: String = "2",
    val tick: BigDecimal = BigDecimal("0.01"),
    val availableOrderTypes: List<String> = listOf("GTC"),
    val postOnlySupported: Boolean = false,
    val collateralAsset: String = "USD",
) {
    /** The chips the trade sheet offers, driven by the venue's capabilities. */
    val orderTypeChips: List<String>
        get() = listOf("GTC", "GTD", "FAK", "FOK").filter { it in availableOrderTypes }

    val isMarketOrder: Boolean get() = orderType == "FAK" || orderType == "FOK"

    /** The venue offers a market (fill-now) order. */
    val supportsMarket: Boolean get() = availableOrderTypes.any { it == "FAK" || it == "FOK" }

    /** The venue offers a plain resting limit order. */
    val supportsLimit: Boolean get() = "GTC" in availableOrderTypes
}

/** Best prices of one outcome's order book. */
data class OutcomeBook(
    val bids: List<BookLevel> = emptyList(),
    val asks: List<BookLevel> = emptyList(),
) {
    /** What you pay to buy right now. */
    val bestAsk: BigDecimal? get() = asks.minByOrNull { it.price }?.price

    /** What you get for selling right now. */
    val bestBid: BigDecimal? get() = bids.maxByOrNull { it.price }?.price

    /** The same book seen from the other outcome: a bid on YES at p is an ask on NO at 1 - p. */
    fun inverted(): OutcomeBook = OutcomeBook(
        bids = asks.map { BookLevel(BigDecimal.ONE - it.price, it.shares) },
        asks = bids.map { BookLevel(BigDecimal.ONE - it.price, it.shares) },
    )
}

/** Everything the confirmation sheet has to show, computed before sending. */
data class TradeEstimate(
    val costUsd: String = "—",
    val feeUsd: String = "—",
    val maxLossUsd: String = "—",
    val payoutUsd: String = "—",
    val shares: String = "—",
    val valid: Boolean = false,
    val problem: String? = null,
    /** Average fill price as a probability string, e.g. "0.6234". */
    val avgPrice: String = "—",
    /** Profit if the side wins (payout minus cost), and the same as a percentage of the cost. */
    val returnUsd: String = "—",
    val returnPct: String = "—",
    /** A heads-up that does not block the order, e.g. the book is thinner than the amount. */
    val note: String? = null,
)

data class TradeUiState(
    val draft: TradeDraft = TradeDraft(),
    val estimate: TradeEstimate = TradeEstimate(),
    val bids: List<BookLevel> = emptyList(),
    val asks: List<BookLevel> = emptyList(),
    val history: List<PricePoint> = emptyList(),
    /** Both outcomes' books, so the Yes and No buttons can show their live price. */
    val books: Map<String, OutcomeBook> = emptyMap(),
    val heldYes: BigDecimal = BigDecimal.ZERO,
    val heldNo: BigDecimal = BigDecimal.ZERO,
    val submitting: Boolean = false,
    val result: String? = null,
    val error: String? = null,
    /** One idempotency key per user intent, reused on a retry after a timeout. */
    val idempotencyKey: String = UUID.randomUUID().toString(),
) {
    /** Shares of the selected outcome the operator already holds in this market. */
    val held: BigDecimal get() = if (draft.outcome == "YES") heldYes else heldNo
}

/** Shares held in one market and outcome, summed over the position rows. */
fun heldShares(positions: List<JSONObject>, venue: String, marketId: String, outcome: String): BigDecimal =
    positions
        .filter {
            it.optString("market_id") == marketId &&
                it.optString("outcome").equals(outcome, ignoreCase = true) &&
                (venue.isBlank() || it.optString("venue").isBlank() || it.optString("venue") == venue)
        }
        .fold(BigDecimal.ZERO) { sum, row -> sum + (row.optString("shares").toBigDecimalOrNull() ?: BigDecimal.ZERO) }

private fun String.orNull(): String? = trim().ifBlank { null }

/**
 * The trade sheet: order-type chips follow the venue's capabilities, and the
 * estimate (cost, fee, max loss, payout) is computed before the operator confirms.
 */
class TradeSheetViewModel(private val api: ApiClient) : ViewModel() {

    private val _state = MutableStateFlow(TradeUiState())
    val state: StateFlow<TradeUiState> = _state.asStateFlow()

    fun setCapabilities(venue: String, orderTypes: List<String>, postOnly: Boolean, tick: BigDecimal, asset: String) {
        _state.value = _state.value.copy(
            draft = _state.value.draft.copy(
                venue = venue,
                availableOrderTypes = orderTypes,
                postOnlySupported = postOnly,
                postOnly = _state.value.draft.postOnly && postOnly,
                tick = tick,
                collateralAsset = asset,
                orderType = if (_state.value.draft.orderType in orderTypes) _state.value.draft.orderType else orderTypes.firstOrNull() ?: "GTC",
            )
        )
        recompute()
    }

    fun setMarket(marketId: String, title: String) {
        _state.value = _state.value.copy(draft = _state.value.draft.copy(marketId = marketId, title = title))
    }

    /** Open a fresh one-tap ticket: Buy, market order when the venue has one, nothing typed yet. */
    fun startTicket(outcome: String) {
        val draft = _state.value.draft
        val marketType = listOf("FAK", "FOK").firstOrNull { it in draft.availableOrderTypes }
        _state.value = _state.value.copy(
            draft = draft.copy(
                outcome = outcome,
                side = "BUY",
                orderType = marketType ?: "GTC",
                postOnly = false,
                price = "",
                shares = "",
                amountUsd = "",
            ),
            books = emptyMap(),
            bids = emptyList(),
            asks = emptyList(),
            history = emptyList(),
            heldYes = BigDecimal.ZERO,
            heldNo = BigDecimal.ZERO,
        )
        recompute()
    }

    /** What the operator already holds here, so Sell can offer 25% / 50% / Max. */
    fun setHeld(yes: BigDecimal, no: BigDecimal) {
        _state.value = _state.value.copy(heldYes = yes, heldNo = no)
    }

    /** Load both outcomes' books and the price history the ticket shows. */
    fun loadMarketData(marketId: String, venue: String, outcome: String) {
        viewModelScope.launch {
            val loaded = HashMap<String, OutcomeBook>()
            var failure: String? = null
            for (side in listOf("YES", "NO")) {
                try {
                    val book = api.get("/v1/markets/$marketId/book?venue=$venue&outcome=$side&depth=20")
                    val levels = { key: String ->
                        ApiClient.arrayOf(book, key).objects().map {
                            BookLevel(
                                BigDecimal(it.optString("price", "0")),
                                BigDecimal(it.optString("shares", "0")),
                            )
                        }
                    }
                    loaded[side] = OutcomeBook(bids = levels("bids"), asks = levels("asks"))
                } catch (e: Exception) {
                    if (side == outcome) failure = e.message
                }
            }
            // A venue that did not answer for one outcome still has it: the other side's book, mirrored.
            if ("NO" !in loaded && "YES" in loaded) loaded["NO"] = loaded.getValue("YES").inverted()
            if ("YES" !in loaded && "NO" in loaded) loaded["YES"] = loaded.getValue("NO").inverted()
            setBooks(loaded)
            if (failure != null) _state.value = _state.value.copy(error = failure)
            try {
                val history = api.get("/v1/markets/$marketId/history?venue=$venue&interval=1d")
                val points = ApiClient.arrayOf(history, "points").objects().map {
                    PricePoint(it.optString("at"), BigDecimal(it.optString("close", "0")))
                }
                _state.value = _state.value.copy(history = points)
            } catch (e: Exception) {
                // A venue with no price history is normal; the chart simply stays empty.
            }
        }
    }

    /** Take both outcomes' books (from the server, or from a test) and refresh everything that depends on them. */
    fun setBooks(loaded: Map<String, OutcomeBook>) {
        val selected = loaded[_state.value.draft.outcome]
        _state.value = _state.value.copy(
            books = loaded,
            bids = selected?.bids ?: emptyList(),
            asks = selected?.asks ?: emptyList(),
        )
        applyDefaultPrice()
        recompute()
    }

    /** Tap on Yes / No: change side without a network call, from the books already loaded. */
    fun switchOutcome(outcome: String) {
        val current = _state.value
        if (current.draft.outcome == outcome) return
        val book = current.books[outcome]
        _state.value = current.copy(
            draft = current.draft.copy(outcome = outcome, price = "", shares = if (current.draft.side == "SELL") "" else current.draft.shares),
            bids = book?.bids ?: emptyList(),
            asks = book?.asks ?: emptyList(),
        )
        applyDefaultPrice()
        recompute()
    }

    /** Market (fill now) or Limit (rest at my price). Inputs restart because their meaning changes. */
    fun setMarketMode(market: Boolean) {
        val draft = _state.value.draft
        if (market == draft.isMarketOrder) return
        val wanted: String? = if (market) {
            listOf("FAK", "FOK").firstOrNull { it in draft.availableOrderTypes }
        } else {
            listOf("GTC", "GTD").firstOrNull { it in draft.availableOrderTypes }
        }
        val type = wanted ?: return
        _state.value = _state.value.copy(
            draft = draft.copy(orderType = type, postOnly = false, price = "", shares = "", amountUsd = ""),
        )
        applyDefaultPrice()
        recompute()
    }

    /** A limit order starts at the price you would get right now, so the stepper has somewhere to start. */
    private fun applyDefaultPrice() {
        val current = _state.value
        val draft = current.draft
        if (draft.price.isNotBlank() || draft.isMarketOrder) return
        val book = current.books[draft.outcome] ?: return
        val start = if (draft.side == "BUY") book.bestAsk else book.bestBid
        if (start != null) {
            _state.value = current.copy(draft = draft.copy(price = start.stripTrailingZeros().toPlainString()))
        }
    }

    /** One tick up or down on the limit price. */
    fun stepPrice(direction: Int) {
        val draft = _state.value.draft
        val tick = if (draft.tick.signum() > 0) draft.tick else BigDecimal("0.01")
        val book = _state.value.books[draft.outcome]
        val start = draft.price.toBigDecimalOrNull()
            ?: (if (draft.side == "BUY") book?.bestAsk else book?.bestBid)
            ?: BigDecimal("0.5")
        val low = tick
        val high = BigDecimal.ONE - tick
        val next = (start + tick.multiply(BigDecimal(direction))).max(low).min(high)
        setPrice(next.setScale(tick.scale().coerceAtLeast(0), RoundingMode.HALF_UP).toPlainString())
    }

    /** "+$5" style quick add on the amount. */
    fun addAmount(delta: Int) {
        val current = _state.value.draft.amountUsd.toBigDecimalOrNull() ?: BigDecimal.ZERO
        setAmount((current + BigDecimal(delta)).stripTrailingZeros().toPlainString())
    }

    /** "25% / 50% / Max" of what is held, for selling. */
    fun setPercentOfHeld(percent: Int) {
        val held = _state.value.held
        if (held.signum() <= 0) return
        val size = if (percent >= 100) held else held.multiply(BigDecimal(percent)).divide(BigDecimal(100), 2, RoundingMode.DOWN)
        setShares(size.stripTrailingZeros().toPlainString())
    }

    fun setOutcome(outcome: String) {
        _state.value = _state.value.copy(draft = _state.value.draft.copy(outcome = outcome))
        recompute()
    }

    fun setSide(side: String) {
        val current = _state.value
        if (current.draft.side == side) return
        // Buy is typed in dollars, Sell in shares: start the inputs clean instead of carrying a wrong number across.
        _state.value = current.copy(draft = current.draft.copy(side = side, price = "", shares = "", amountUsd = ""))
        applyDefaultPrice()
        recompute()
    }

    fun setOrderType(orderType: String) {
        val draft = _state.value.draft
        if (orderType !in draft.availableOrderTypes) return
        // post-only only applies to resting orders.
        val postOnly = if (orderType == "FAK" || orderType == "FOK") false else draft.postOnly
        _state.value = _state.value.copy(draft = draft.copy(orderType = orderType, postOnly = postOnly))
        recompute()
    }

    fun setPostOnly(postOnly: Boolean) {
        if (!postOnly || !_state.value.draft.postOnlySupported) return
        val draft = _state.value.draft
        if (draft.orderType == "FAK" || draft.orderType == "FOK") return
        _state.value = _state.value.copy(draft = draft.copy(postOnly = postOnly))
    }

    fun setPrice(price: String) {
        _state.value = _state.value.copy(draft = _state.value.draft.copy(price = price))
        recompute()
    }

    fun setShares(shares: String) {
        _state.value = _state.value.copy(draft = _state.value.draft.copy(shares = shares))
        recompute()
    }

    fun setAmount(amountUsd: String) {
        _state.value = _state.value.copy(draft = _state.value.draft.copy(amountUsd = amountUsd))
        recompute()
    }

    fun setMaxSlippage(cents: String) {
        _state.value = _state.value.copy(draft = _state.value.draft.copy(maxSlippageCents = cents))
    }

    /** A new user intent needs a new idempotency key. */
    fun newIntent() {
        _state.value = _state.value.copy(idempotencyKey = UUID.randomUUID().toString(), result = null, error = null)
    }

    fun recompute() {
        val draft = _state.value.draft
        val price = draft.price.toBigDecimalOrNull()
        val shares = draft.shares.toBigDecimalOrNull()
        val amount = draft.amountUsd.toBigDecimalOrNull()

        if (draft.isMarketOrder) {
            _state.value = _state.value.copy(estimate = estimateMarket(draft, amount, shares))
            return
        }

        if (price == null || price <= BigDecimal.ZERO) {
            _state.value = _state.value.copy(estimate = TradeEstimate(problem = "Enter a price."))
            return
        }
        if (price >= BigDecimal.ONE) {
            _state.value = _state.value.copy(estimate = TradeEstimate(problem = "A price must be between 0 and 1."))
            return
        }
        val size = shares ?: amount?.divide(price, 4, RoundingMode.DOWN)
        if (size == null || size <= BigDecimal.ZERO) {
            _state.value = _state.value.copy(estimate = TradeEstimate(problem = "Enter shares or an amount."))
            return
        }
        val cost = size.multiply(price).setScale(2, RoundingMode.HALF_UP)
        val payout = size.setScale(2, RoundingMode.HALF_UP)
        _state.value = _state.value.copy(
            estimate = TradeEstimate(
                costUsd = cost.toPlainString(),
                feeUsd = "—",
                maxLossUsd = cost.toPlainString(),
                payoutUsd = payout.toPlainString(),
                shares = size.stripTrailingZeros().toPlainString(),
                valid = true,
                avgPrice = price.toPlainString(),
                returnUsd = payout.subtract(cost).toPlainString(),
                returnPct = percentOf(payout.subtract(cost), cost),
            )
        )
    }

    private fun percentOf(part: BigDecimal, whole: BigDecimal): String =
        if (whole.signum() <= 0) "—" else part.multiply(BigDecimal(100)).divide(whole, 0, RoundingMode.HALF_UP).toPlainString()

    /**
     * A market order is priced by walking the live book: a buy spends the dollars up the asks, a sell
     * hands the shares down the bids. The server prices the real order; this is the on-screen estimate.
     */
    private fun estimateMarket(draft: TradeDraft, amount: BigDecimal?, shares: BigDecimal?): TradeEstimate {
        val book = _state.value.books[draft.outcome]
        if (draft.side == "BUY") {
            if (amount == null || amount.signum() <= 0) return TradeEstimate(problem = "Enter an amount.")
            val asks = (book?.asks ?: emptyList()).sortedBy { it.price }
            if (asks.isEmpty()) return TradeEstimate(problem = "No sellers in the book right now.")
            var left: BigDecimal = amount
            var got = BigDecimal.ZERO
            var spent = BigDecimal.ZERO
            for (level in asks) {
                if (left.signum() <= 0 || level.price.signum() <= 0) break
                val levelCost = level.price.multiply(level.shares)
                val take = left.min(levelCost)
                got += take.divide(level.price, 6, RoundingMode.DOWN)
                spent += take
                left -= take
            }
            if (got.signum() <= 0) return TradeEstimate(problem = "No sellers in the book right now.")
            val avg = spent.divide(got, 4, RoundingMode.HALF_UP)
            val payout = got.setScale(2, RoundingMode.HALF_UP)
            val cost = spent.setScale(2, RoundingMode.HALF_UP)
            return TradeEstimate(
                costUsd = cost.toPlainString(),
                feeUsd = "—",
                maxLossUsd = cost.toPlainString(),
                payoutUsd = payout.toPlainString(),
                shares = got.setScale(2, RoundingMode.DOWN).stripTrailingZeros().toPlainString(),
                valid = true,
                avgPrice = avg.toPlainString(),
                returnUsd = payout.subtract(cost).toPlainString(),
                returnPct = percentOf(payout.subtract(cost), cost),
                note = if (left.signum() > 0) "Only about ${Format.money(cost.toPlainString())} can be filled right now." else null,
            )
        }
        if (shares == null || shares.signum() <= 0) return TradeEstimate(problem = "Enter how many shares to sell.")
        val bids = (book?.bids ?: emptyList()).sortedByDescending { it.price }
        if (bids.isEmpty()) return TradeEstimate(problem = "No buyers in the book right now.")
        var left: BigDecimal = shares
        var sold = BigDecimal.ZERO
        var proceeds = BigDecimal.ZERO
        for (level in bids) {
            if (left.signum() <= 0) break
            val take = left.min(level.shares)
            sold += take
            proceeds += take.multiply(level.price)
            left -= take
        }
        if (sold.signum() <= 0) return TradeEstimate(problem = "No buyers in the book right now.")
        val avg = proceeds.divide(sold, 4, RoundingMode.HALF_UP)
        return TradeEstimate(
            costUsd = proceeds.setScale(2, RoundingMode.HALF_UP).toPlainString(),
            feeUsd = "—",
            shares = sold.setScale(2, RoundingMode.DOWN).stripTrailingZeros().toPlainString(),
            valid = true,
            avgPrice = avg.toPlainString(),
            note = if (left.signum() > 0) "Only ${sold.setScale(2, RoundingMode.DOWN).stripTrailingZeros().toPlainString()} shares can be sold right now." else null,
        )
    }

    /** Send the order. The idempotency key is reused if the operator taps again. */
    fun submit(onDone: (Boolean) -> Unit = {}) {
        val current = _state.value
        if (current.submitting || !current.estimate.valid) return
        _state.value = current.copy(submitting = true, error = null, result = null)
        viewModelScope.launch {
            try {
                val draft = current.draft
                val path = if (draft.isMarketOrder) "/v1/orders/market" else "/v1/orders/limit"
                // The server reads a blank size as a bad number, so an unused field goes out as null.
                val body = if (draft.isMarketOrder) {
                    val buy = draft.side == "BUY"
                    ApiClient.jsonOf(
                        "venue" to draft.venue,
                        "market_id" to draft.marketId,
                        "outcome" to draft.outcome,
                        "side" to draft.side,
                        "shares" to (if (buy) null else draft.shares.orNull()),
                        "amount_usd" to (if (buy) draft.amountUsd.orNull() else null),
                        "max_slippage_cents" to draft.maxSlippageCents.orNull(),
                    )
                } else {
                    ApiClient.jsonOf(
                        "venue" to draft.venue,
                        "market_id" to draft.marketId,
                        "outcome" to draft.outcome,
                        "side" to draft.side,
                        "price" to draft.price.orNull(),
                        "shares" to draft.shares.orNull(),
                        "amount_usd" to (if (draft.shares.isBlank()) draft.amountUsd.orNull() else null),
                        "tif" to draft.orderType,
                        "post_only" to draft.postOnly,
                    )
                }
                val response = api.post(path, body, current.idempotencyKey)
                val status = response.optString("status", "UNKNOWN")
                _state.value = _state.value.copy(
                    submitting = false,
                    result = "$status · filled ${response.optString("filled", "0")} · fee ${Format.money(response.optString("fee_usd"))}",
                )
                onDone(true)
            } catch (e: ApiClient.ApiException) {
                _state.value = _state.value.copy(submitting = false, error = e.message)
                onDone(false)
            } catch (e: Exception) {
                _state.value = _state.value.copy(submitting = false, error = e.message ?: "The request failed.")
                onDone(false)
            }
        }
    }
}
