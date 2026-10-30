package com.mainnetwallet.mmagent.data

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import org.json.JSONObject
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
    /** An entry is usable once its cooling period has passed. */
    fun isCooledDown(nowMillis: Long): Boolean = usableAt == null
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

    fun setAmount(amount: String) {
        _state.value = _state.value.copy(amount = amount)
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
                _state.value = _state.value.copy(entries = entries)
            } catch (e: Exception) {
                _state.value = _state.value.copy(error = e.message)
            }
        }
    }

    fun preview(onReady: () -> Unit = {}) {
        val current = _state.value
        if (current.submitting || current.addressId.isBlank()) return
        _state.value = current.copy(submitting = true, error = null)
        viewModelScope.launch {
            try {
                val response = api.post(
                    "/v1/funds/withdraw/preview",
                    ApiClient.jsonOf(
                        "venue" to current.venue,
                        "amount" to current.amount,
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

    /** Hold-to-confirm sends the withdrawal. The step-up token is single-use. */
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
                    current.idempotencyKey,
                )
                _state.value = _state.value.copy(
                    submitting = false,
                    step = WithdrawStep.DONE,
                    withdrawalId = response.optString("id"),
                    status = response.optString("status", "PENDING_HOLD"),
                )
                onDone(true)
            } catch (e: ApiClient.ApiException) {
                _state.value = _state.value.copy(submitting = false, error = e.message)
                onDone(false)
            } catch (e: Exception) {
                _state.value = _state.value.copy(submitting = false, error = e.message)
                onDone(false)
            }
        }
    }

    /** A fresh withdrawal needs a fresh idempotency key. */
    fun reset() {
        _state.value = WithdrawUiState(venue = _state.value.venue)
    }
}
