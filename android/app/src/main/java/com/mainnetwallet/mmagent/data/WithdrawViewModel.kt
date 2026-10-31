package com.mainnetwallet.mmagent.data

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.io.IOException
import java.math.RoundingMode
import java.time.Instant
import java.util.UUID

/** Where the withdrawal flow currently is. */
enum class WithdrawStep { AMOUNT, PREVIEW, STEP_UP, CONFIRMING, DONE }

data class AllowlistEntry(
    val id: String,
    val label: String,
    val address: String,
    val network: String,
    val asset: String,
    val usableAt: String?,
) {
    /** False while the address is still in its cooling period. An unreadable time counts as usable: the server decides. */
    fun isUsable(now: Instant = Instant.now()): Boolean {
        val at = Format.parseInstant(usableAt) ?: return true
        return !at.isAfter(now)
    }
}

data class WithdrawPreview(
    val feeUsd: String = "0",
    val network: String = "",
    val asset: String = "",
    val recipient: String = "",
    val label: String = "",
    val etaSeconds: Int = 0,
    val holdMinutes: Int = 0,
    val dailyLimitUsd: String = "0",
    val dailyRemainingUsd: String = "0",
)

data class WithdrawUiState(
    val step: WithdrawStep = WithdrawStep.AMOUNT,
    val venue: String = "",
    val amount: String = "",
    val addressId: String = "",
    val entries: List<AllowlistEntry> = emptyList(),
    val preview: WithdrawPreview = WithdrawPreview(),
    val stepUpToken: String? = null,
    val submitting: Boolean = false,
    val error: String? = null,
    val withdrawalId: String? = null,
    val status: String? = null,
    val idempotencyKey: String = UUID.randomUUID().toString(),
)

/**
 * The withdraw flow: amount → preview → step-up → hold-to-confirm → status.
 *
 * The idempotency key is generated once per intent and reused if the operator taps
 * again after a timeout, so a retry can never create a second withdrawal.
 */
class WithdrawViewModel(private val api: ApiClient) : ViewModel() {

    private val _state = MutableStateFlow(WithdrawUiState())
    val state: StateFlow<WithdrawUiState> = _state.asStateFlow()

    fun setVenue(venue: String) {
        _state.value = _state.value.copy(venue = venue)
    }

    /** Keeps only digits and a single decimal point, so a typo cannot reach the server as an amount. */
    fun setAmount(amount: String) {
        val cleaned = amount.filter { it.isDigit() || it == '.' }
        val dot = cleaned.indexOf('.')
        val single = if (dot < 0) cleaned else cleaned.substring(0, dot + 1) + cleaned.substring(dot + 1).replace(".", "")
        _state.value = _state.value.copy(amount = single.take(18))
    }

    /** The whole available balance, rounded down to cents. */
    fun setMax(available: String) {
        val value = available.toBigDecimalOrNull() ?: return
        if (value.signum() <= 0) return
        _state.value = _state.value.copy(amount = value.setScale(2, RoundingMode.DOWN).toPlainString())
    }

    fun setAddress(entry: AllowlistEntry) {
        _state.value = _state.value.copy(addressId = entry.id)
    }

    fun loadAllowlist() {
        viewModelScope.launch {
            try {
                val response = api.get("/v1/funds/allowlist")
                val entries = ApiClient.arrayOf(response, "entries").objects().map { obj ->
                    AllowlistEntry(
                        id = obj.optString("id"),
                        label = obj.optString("label"),
                        address = obj.optString("address"),
                        network = obj.optString("network"),
                        asset = obj.optString("asset"),
                        usableAt = obj.optString("usable_at").takeIf { it.isNotBlank() && it != "null" },
                    )
                }
                val current = _state.value
                // Keep the pick if it is still usable; otherwise the only usable address is picked for the operator.
                val chosen = entries.firstOrNull { it.id == current.addressId && it.isUsable() }
                    ?: entries.singleOrNull { it.isUsable() }
                _state.value = current.copy(entries = entries, addressId = chosen?.id.orEmpty())
            } catch (e: Exception) {
                _state.value = _state.value.copy(error = e.message)
            }
        }
    }

    /** Add a recipient. Needs a step-up token; the server enforces the cooling period. */
    fun addAllowlistEntry(label: String, address: String, network: String, asset: String, stepupToken: String) {
        viewModelScope.launch {
            try {
                api.post(
                    "/v1/funds/allowlist",
                    ApiClient.jsonOf(
                        "label" to label,
                        "address" to address,
                        "network" to network,
                        "asset" to asset,
                    ),
                    stepupToken = stepupToken,
                )
                _state.value = _state.value.copy(error = null)
                loadAllowlist()
            } catch (e: ApiClient.ApiException) {
                _state.value = _state.value.copy(error = e.message)
            } catch (e: Exception) {
                _state.value = _state.value.copy(error = e.message)
            }
        }
    }

    /** Removal is immediate; it never needs a step-up token. */
    fun removeAllowlistEntry(entryId: String) {
        viewModelScope.launch {
            try {
                api.delete("/v1/funds/allowlist/$entryId")
                _state.value = _state.value.copy(error = null)
                loadAllowlist()
            } catch (e: ApiClient.ApiException) {
                _state.value = _state.value.copy(error = e.message)
            } catch (e: Exception) {
                _state.value = _state.value.copy(error = e.message)
            }
        }
    }

    /** A step-up token for the allowlist action. */
    fun stepUpForAllowlist(password: String, onToken: (String?) -> Unit) {
        viewModelScope.launch {
            try {
                val response = api.post(
                    "/v1/auth/stepup",
                    ApiClient.jsonOf("password" to password, "action" to "allowlist_add"),
                )
                onToken(response.optString("stepup_token"))
            } catch (e: ApiClient.ApiException) {
                _state.value = _state.value.copy(error = e.message)
                onToken(null)
            } catch (e: Exception) {
                _state.value = _state.value.copy(error = e.message)
                onToken(null)
            }
        }
    }

    fun preview(onReady: () -> Unit = {}) {
        val current = _state.value
        if (current.submitting || current.addressId.isBlank()) return
        val amount = current.amount.trim()
        val parsed = amount.toBigDecimalOrNull()
        if (parsed == null || parsed.signum() <= 0) {
            _state.value = current.copy(error = "Enter an amount greater than zero.")
            return
        }
        // A preview starts a new intent, so it gets a new idempotency key: a changed amount or recipient
        // must never be answered with the stored result of an earlier attempt.
        _state.value = current.copy(
            submitting = true,
            error = null,
            amount = amount,
            idempotencyKey = UUID.randomUUID().toString(),
        )
        viewModelScope.launch {
            try {
                val response = api.post(
                    "/v1/funds/withdraw/preview",
                    ApiClient.jsonOf(
                        "venue" to current.venue,
                        "amount" to amount,
                        "address_id" to current.addressId,
                    )
                )
                _state.value = _state.value.copy(
                    submitting = false,
                    step = WithdrawStep.PREVIEW,
                    preview = WithdrawPreview(
                        feeUsd = response.optString("fee_usd", "0"),
                        network = response.optString("network"),
                        asset = response.optString("asset"),
                        recipient = response.optString("recipient"),
                        label = response.optString("label"),
                        etaSeconds = response.optInt("eta_seconds"),
                        holdMinutes = response.optInt("hold_minutes"),
                        dailyLimitUsd = response.optString("daily_limit_usd", "0"),
                        dailyRemainingUsd = response.optString("daily_remaining_usd", "0"),
                    ),
                )
                onReady()
            } catch (e: ApiClient.ApiException) {
                _state.value = _state.value.copy(submitting = false, error = e.message)
            } catch (e: Exception) {
                _state.value = _state.value.copy(submitting = false, error = e.message)
            }
        }
    }

    /** Password re-entry. The password is never stored; only the token is kept. */
    fun stepUp(password: String, onReady: () -> Unit = {}) {
        val current = _state.value
        if (current.submitting) return
        _state.value = current.copy(submitting = true, error = null)
        viewModelScope.launch {
            try {
                val response = api.post(
                    "/v1/auth/stepup",
                    ApiClient.jsonOf("password" to password, "action" to "withdraw"),
                )
                _state.value = _state.value.copy(
                    submitting = false,
                    step = WithdrawStep.STEP_UP,
                    stepUpToken = response.optString("stepup_token"),
                )
                onReady()
            } catch (e: ApiClient.ApiException) {
                _state.value = _state.value.copy(submitting = false, error = e.message)
            } catch (e: Exception) {
                _state.value = _state.value.copy(submitting = false, error = e.message)
            }
        }
    }

    /** Back one step. A step-up token is dropped on the way: it is bound to one confirmation. */
    fun back() {
        val current = _state.value
        if (current.submitting) return
        _state.value = when (current.step) {
            WithdrawStep.PREVIEW -> current.copy(step = WithdrawStep.AMOUNT, error = null)
            WithdrawStep.STEP_UP, WithdrawStep.CONFIRMING ->
                current.copy(step = WithdrawStep.PREVIEW, stepUpToken = null, error = null)
            else -> current
        }
    }

    /**
     * Hold-to-confirm creates the withdrawal. The step-up token is single-use and goes out in the
     * `X-Stepup-Token` header; without it the server refuses every withdrawal.
     *
     * A definite answer from the server (a 4xx) may have spent the token, so the flow returns to the review
     * step and asks for the password again. A timeout or a 5xx is uncertain: the token and the idempotency
     * key are kept, and pressing again replays the same request, so it can never create a second withdrawal.
     */
    fun confirm(onDone: (Boolean) -> Unit = {}) {
        val current = _state.value
        val token = current.stepUpToken
        if (current.submitting || token.isNullOrBlank()) return
        _state.value = current.copy(submitting = true, error = null, step = WithdrawStep.CONFIRMING)
        viewModelScope.launch {
            try {
                val response = api.post(
                    "/v1/funds/withdraw",
                    ApiClient.jsonOf(
                        "venue" to current.venue,
                        "amount" to current.amount,
                        "address_id" to current.addressId,
                    ),
                    idempotencyKey = current.idempotencyKey,
                    stepupToken = token,
                )
                _state.value = _state.value.copy(
                    submitting = false,
                    step = WithdrawStep.DONE,
                    stepUpToken = null,
                    withdrawalId = response.optString("id"),
                    status = response.optString("status", "PENDING_HOLD"),
                )
                onDone(true)
            } catch (e: ApiClient.ApiException) {
                if (e.status >= 500) {
                    _state.value = _state.value.copy(
                        submitting = false,
                        step = WithdrawStep.STEP_UP,
                        error = (e.message ?: "Server error.") + " Check Withdrawals before pressing again; " +
                            "pressing again cannot create a second one.",
                    )
                } else {
                    _state.value = _state.value.copy(
                        submitting = false,
                        step = WithdrawStep.PREVIEW,
                        stepUpToken = null,
                        error = e.message,
                    )
                }
                onDone(false)
            } catch (e: IOException) {
                _state.value = _state.value.copy(
                    submitting = false,
                    step = WithdrawStep.STEP_UP,
                    error = "Could not reach the server, so it is not known whether the withdrawal was created. " +
                        "Check Withdrawals, or press again: the same request cannot create a second one.",
                )
                onDone(false)
            } catch (e: Exception) {
                _state.value = _state.value.copy(submitting = false, step = WithdrawStep.STEP_UP, error = e.message)
                onDone(false)
            }
        }
    }

    /** Cancel the withdrawal just created, while it is still in its hold window. */
    fun cancelCreated(onDone: (Boolean) -> Unit = {}) {
        val current = _state.value
        val id = current.withdrawalId
        if (current.submitting || id.isNullOrBlank()) return
        _state.value = current.copy(submitting = true, error = null)
        viewModelScope.launch {
            try {
                api.post("/v1/funds/withdrawals/$id/cancel", JSONObject())
                _state.value = _state.value.copy(submitting = false, status = "CANCELLED")
                onDone(true)
            } catch (e: Exception) {
                _state.value = _state.value.copy(submitting = false, error = e.message ?: "Cancel failed.")
                onDone(false)
            }
        }
    }

    /** A fresh withdrawal needs a fresh idempotency key. */
    fun reset() {
        _state.value = WithdrawUiState(venue = _state.value.venue)
    }
}
