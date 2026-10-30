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
    val mode: String = "PAPER",
    val collateralAsset: String = "USD",
) {
    /** The chips the trade sheet offers, driven by the venue's capabilities. */
    val orderTypeChips: List<String>
        get() = listOf("GTC", "GTD", "FAK", "FOK").filter { it in availableOrderTypes }

    val isMarketOrder: Boolean get() = orderType == "FAK" || orderType == "FOK"
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
)

data class TradeUiState(
    val draft: TradeDraft = TradeDraft(),
    val estimate: TradeEstimate = TradeEstimate(),
    val bids: List<BookLevel> = emptyList(),
    val asks: List<BookLevel> = emptyList(),
    val history: List<PricePoint> = emptyList(),
    val submitting: Boolean = false,
    val result: String? = null,
    val error: String? = null,
    /** One idempotency key per user intent, reused on a retry after a timeout. */
    val idempotencyKey: String = UUID.randomUUID().toString(),
)

/**
 * The trade sheet: order-type chips follow the venue's capabilities, and the
 * estimate (cost, fee, max loss, payout) is computed before the operator confirms.
 */
class TradeSheetViewModel(private val api: ApiClient) : ViewModel() {

    private val _state = MutableStateFlow(TradeUiState())
    val state: StateFlow<TradeUiState> = _state.asStateFlow()

    fun setCapabilities(venue: String, orderTypes: List<String>, postOnly: Boolean, tick: BigDecimal, mode: String, asset: String) {
        _state.value = _state.value.copy(
            draft = _state.value.draft.copy(
                venue = venue,
                availableOrderTypes = orderTypes,
                postOnlySupported = postOnly,
                postOnly = _state.value.draft.postOnly && postOnly,
                tick = tick,
                mode = mode,
                collateralAsset = asset,
                orderType = if (_state.value.draft.orderType in orderTypes) _state.value.draft.orderType else orderTypes.firstOrNull() ?: "GTC",
            )
        )
        recompute()
    }

    fun setMarket(marketId: String, title: String) {
        _state.value = _state.value.copy(draft = _state.value.draft.copy(marketId = marketId, title = title))
    }

    /** Load the book and the price history the market detail shows. */
    fun loadMarketData(marketId: String, venue: String, outcome: String) {
        viewModelScope.launch {
            try {
                val book = api.get("/v1/markets/$marketId/book?venue=$venue&outcome=$outcome&depth=20")
                val levels = { key: String ->
                    ApiClient.arrayOf(book, key).objects().map {
                        BookLevel(
                            BigDecimal(it.optString("price", "0")),
                            BigDecimal(it.optString("shares", "0")),
                        )
                    }
                }
                _state.value = _state.value.copy(bids = levels("bids"), asks = levels("asks"))
            } catch (e: Exception) {
                _state.value = _state.value.copy(error = e.message)
            }
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

    fun setOutcome(outcome: String) {
        _state.value = _state.value.copy(draft = _state.value.draft.copy(outcome = outcome))
        recompute()
    }

    fun setSide(side: String) {
        _state.value = _state.value.copy(draft = _state.value.draft.copy(side = side))
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
            // A market order has no price of its own; the worst price is the limit.
            if (shares == null && amount == null) {
                _state.value = _state.value.copy(estimate = TradeEstimate(problem = "Enter shares or an amount."))
                return
            }
            val size = shares ?: (amount!! / (price ?: BigDecimal("0.5")))
            val worst = price ?: BigDecimal("0.5")
            val cost = size.multiply(worst).setScale(2, RoundingMode.HALF_UP)
            _state.value = _state.value.copy(
                estimate = TradeEstimate(
                    costUsd = cost.toPlainString(),
                    feeUsd = "0",
                    maxLossUsd = cost.toPlainString(),
                    payoutUsd = size.setScale(2, RoundingMode.HALF_UP).toPlainString(),
                    shares = size.stripTrailingZeros().toPlainString(),
                    valid = true,
                )
            )
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
            )
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
                val body = ApiClient.jsonOf(
                    "venue" to draft.venue,
                    "market_id" to draft.marketId,
                    "outcome" to draft.outcome,
                    "side" to draft.side,
                    "price" to draft.price,
                    "shares" to draft.shares,
                    "amount_usd" to draft.amountUsd,
                    "tif" to draft.orderType,
                    "post_only" to draft.postOnly,
                )
                val path = if (draft.isMarketOrder) "/v1/orders/market" else "/v1/orders/limit"
                val marketBody = if (draft.isMarketOrder) {
                    ApiClient.jsonOf(
                        "venue" to draft.venue,
                        "market_id" to draft.marketId,
                        "outcome" to draft.outcome,
                        "side" to draft.side,
                        "shares" to draft.shares,
                        "amount_usd" to draft.amountUsd,
                        "max_slippage_cents" to draft.maxSlippageCents,
                    )
                } else {
                    body
                }
                val response = api.post(path, marketBody, current.idempotencyKey)
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
