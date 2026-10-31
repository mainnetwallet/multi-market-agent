package com.mainnetwallet.mmagent.data

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.io.IOException
import java.math.RoundingMode
import java.time.Instant
import java.util.UUID

/** The withdraw page is one form; [DONE] replaces it once the withdrawal has been created. */
enum class WithdrawStep { FORM, DONE }

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

/** A token the bridge can deliver on a chain. [minUsd] is the smallest withdrawal it accepts, when it says so. */
data class WithdrawToken(val symbol: String, val address: String, val minUsd: String?)

/** A chain the bridge can deliver to, with its tokens, exactly as the server read them from the bridge. */
data class WithdrawChain(val id: String, val name: String, val tokens: List<WithdrawToken>)

data class WithdrawPreview(
    val feeUsd: String = "0",
    val receiveUsd: String = "0",
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
    val step: WithdrawStep = WithdrawStep.FORM,
    val venue: String = "",
    val amount: String = "",
    /** The receiver address as typed or pasted (spaces removed). */
    val recipient: String = "",
    val chainId: String = "",
    /** The token symbol on [chainId]. */
    val token: String = "",
    /** The saved (allowlisted) address the receiver was taken from; blank for a typed-in address. */
    val savedId: String = "",
    val chains: List<WithdrawChain> = emptyList(),
    val optionsLoading: Boolean = false,
    val optionsReason: String? = null,
    val requireAllowlist: Boolean = false,
    val entries: List<AllowlistEntry> = emptyList(),
    val preview: WithdrawPreview = WithdrawPreview(),
    /** The [inputKey] the [preview] was fetched for; the page only trusts a fee that matches what is on screen. */
    val quoteKey: String = "",
    val quoting: Boolean = false,
    val quoteError: String? = null,
    val stepUpToken: String? = null,
    val submitting: Boolean = false,
    val error: String? = null,
    val withdrawalId: String? = null,
    val status: String? = null,
    val idempotencyKey: String = UUID.randomUUID().toString(),
) {
    /** Everything the fee depends on. A change in any of it makes the shown fee stale. */
    val inputKey: String
        get() = listOf(venue, amount, recipient.lowercase(), chainId, token.lowercase(), savedId).joinToString("|")

    /** True when the fee on screen was quoted for exactly the inputs on screen. */
    val quoteReady: Boolean
        get() = !quoting && quoteError == null && quoteKey.isNotEmpty() && quoteKey == inputKey

    val selectedChain: WithdrawChain?
        get() = chains.firstOrNull { it.id == chainId }

    val selectedToken: WithdrawToken?
        get() = selectedChain?.tokens?.firstOrNull { it.symbol.equals(token, ignoreCase = true) }
}

/**
 * The withdraw page: amount, receiver address, chain and token on one form, with the fee quoted live for the
 * chain and token picked, then the password and a hold-to-confirm.
 *
 * The idempotency key is generated once per intent (it changes whenever an input changes) and reused if the
 * operator taps again after a timeout, so a retry can never create a second withdrawal.
 */
class WithdrawViewModel(private val api: ApiClient) : ViewModel() {

    private val _state = MutableStateFlow(WithdrawUiState())
    val state: StateFlow<WithdrawUiState> = _state.asStateFlow()

    private var quoteJob: Job? = null

    fun setVenue(venue: String) {
        _state.value = _state.value.copy(venue = venue)
    }

    /** Keeps only digits and a single decimal point, so a typo cannot reach the server as an amount. */
    fun setAmount(amount: String) {
        val cleaned = amount.filter { it.isDigit() || it == '.' }
        val dot = cleaned.indexOf('.')
        val single = if (dot < 0) cleaned else cleaned.substring(0, dot + 1) + cleaned.substring(dot + 1).replace(".", "")
        changeInputs { copy(amount = single.take(18)) }
    }

    /** The whole available balance, rounded down to cents. */
    fun setMax(available: String) {
        val value = available.toBigDecimalOrNull() ?: return
        if (value.signum() <= 0) return
        changeInputs { copy(amount = value.setScale(2, RoundingMode.DOWN).toPlainString()) }
    }

    /** The receiver address as typed or pasted. Whitespace is dropped; the server checks the format and checksum. */
    fun setRecipient(value: String) {
        changeInputs { copy(recipient = value.filter { !it.isWhitespace() }.take(66), savedId = "") }
    }

    /** A new chain keeps the token if the chain offers it, otherwise starts on its first token. */
    fun selectChain(chainId: String) {
        val chain = _state.value.chains.firstOrNull { it.id == chainId } ?: return
        changeInputs {
            val keep = chain.tokens.firstOrNull { it.symbol.equals(token, ignoreCase = true) } ?: chain.tokens.firstOrNull()
            copy(chainId = chain.id, token = keep?.symbol.orEmpty(), savedId = "")
        }
    }

    fun selectToken(symbol: String) {
        changeInputs { copy(token = symbol, savedId = "") }
    }

    /** Fill the receiver from a saved address; its chain and token are picked too when the bridge lists them. */
    fun useSaved(entry: AllowlistEntry) {
        if (!entry.isUsable()) return
        changeInputs {
            val chain = chains.firstOrNull {
                it.id.equals(entry.network, ignoreCase = true) || it.name.equals(entry.network, ignoreCase = true)
            }
            val matched = chain?.tokens?.firstOrNull { it.symbol.equals(entry.asset, ignoreCase = true) }
            copy(
                recipient = entry.address,
                savedId = entry.id,
                chainId = if (chain != null && matched != null) chain.id else chainId,
                token = if (chain != null && matched != null) matched.symbol else token,
            )
        }
    }

    /** Any change to what is on the form: a new intent, a new idempotency key, a fresh fee. */
    private fun changeInputs(transform: WithdrawUiState.() -> WithdrawUiState) {
        val next = _state.value.transform()
        // A step-up token is bound to one confirmation, so it does not outlive a changed form.
        _state.value = next.copy(idempotencyKey = UUID.randomUUID().toString(), stepUpToken = null, error = null)
        refreshQuote()
    }

    /** The chains and tokens the bridge delivers to. The first chain and token are picked until the person picks. */
    fun loadOptions() {
        val venue = _state.value.venue
        if (venue.isBlank()) return
        _state.value = _state.value.copy(optionsLoading = true, optionsReason = null)
        viewModelScope.launch {
            try {
                val response = api.get("/v1/funds/$venue/withdraw/options")
                val chains = ApiClient.arrayOf(response, "chains").objects().map { chain ->
                    WithdrawChain(
                        id = chain.optString("chain_id"),
                        name = chain.optString("chain_name"),
                        tokens = ApiClient.arrayOf(chain, "tokens").objects().map { token ->
                            WithdrawToken(
                                symbol = token.optString("symbol"),
                                address = token.optString("address"),
                                minUsd = token.optString("min_usd").takeIf { it.isNotBlank() && it != "null" },
                            )
                        },
                    )
                }
                val current = _state.value
                val chain = chains.firstOrNull { it.id == current.chainId } ?: chains.firstOrNull()
                val token = chain?.tokens?.firstOrNull { it.symbol.equals(current.token, ignoreCase = true) }
                    ?: chain?.tokens?.firstOrNull()
                _state.value = current.copy(
                    chains = chains,
                    chainId = chain?.id.orEmpty(),
                    token = token?.symbol.orEmpty(),
                    optionsLoading = false,
                    optionsReason = response.optString("reason").takeIf { chains.isEmpty() && it.isNotBlank() && it != "null" },
                    requireAllowlist = response.optBoolean("require_allowlist"),
                )
                refreshQuote()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _state.value = _state.value.copy(optionsLoading = false, optionsReason = e.message ?: "Could not load the chains.")
            }
        }
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
                // A saved address that has since been removed can no longer be the receiver.
                val stillSaved = current.savedId.isBlank() || entries.any { it.id == current.savedId }
                _state.value = current.copy(entries = entries, savedId = if (stillSaved) current.savedId else "")
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

    /** The request body the server previews and creates a withdrawal from. */
    private fun requestBody(s: WithdrawUiState): JSONObject =
        if (s.savedId.isNotBlank()) {
            ApiClient.jsonOf("venue" to s.venue, "amount" to s.amount, "address_id" to s.savedId)
        } else {
            ApiClient.jsonOf(
                "venue" to s.venue,
                "amount" to s.amount,
                "recipient" to s.recipient,
                "network" to s.chainId,
                "asset" to s.token,
            )
        }

    /** Ask the server for the fee once the form is complete. A short pause lets typing finish first. */
    private fun refreshQuote() {
        quoteJob?.cancel()
        val s = _state.value
        val amount = s.amount.toBigDecimalOrNull()
        val complete = amount != null && amount.signum() > 0 && s.venue.isNotBlank() &&
            (s.savedId.isNotBlank() || (isEvmAddress(s.recipient) && s.chainId.isNotBlank() && s.token.isNotBlank()))
        if (!complete) {
            _state.value = s.copy(quoting = false, quoteKey = "", quoteError = null, preview = WithdrawPreview())
            return
        }
        val key = s.inputKey
        _state.value = s.copy(quoting = true, quoteError = null)
        quoteJob = viewModelScope.launch {
            delay(QUOTE_DELAY_MS)
            try {
                val response = api.post("/v1/funds/withdraw/preview", requestBody(s))
                if (_state.value.inputKey != key) return@launch
                _state.value = _state.value.copy(
                    quoting = false,
                    quoteError = null,
                    quoteKey = key,
                    preview = WithdrawPreview(
                        feeUsd = response.optString("fee_usd", "0"),
                        receiveUsd = response.optString("est_output_usd", "0"),
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
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                if (_state.value.inputKey != key) return@launch
                _state.value = _state.value.copy(quoting = false, quoteKey = "", quoteError = e.message ?: "Could not get the fee.")
            }
        }
    }

    /**
     * Withdraw: the password is exchanged for a single-use step-up token, then the withdrawal is created with it
     * in the `X-Stepup-Token` header. Nothing is sent yet: the server holds the withdrawal for a window in which
     * it can still be cancelled.
     *
     * Only a fee quoted for the exact inputs on screen is accepted. A definite answer from the server (a 4xx) may
     * have spent the token, so it is dropped and the password is asked for again. A timeout or a 5xx is uncertain:
     * the token and the idempotency key are kept, and pressing again replays the same request, so it can never
     * create a second withdrawal.
     */
    fun submit(password: String, onDone: (Boolean) -> Unit = {}) {
        val current = _state.value
        if (current.submitting) return
        if (!current.quoteReady) {
            _state.value = current.copy(error = "Wait for the fee to load before withdrawing.")
            onDone(false)
            return
        }
        if (password.isBlank() && current.stepUpToken.isNullOrBlank()) return
        _state.value = current.copy(submitting = true, error = null)
        viewModelScope.launch {
            var token = current.stepUpToken
            if (token.isNullOrBlank()) {
                try {
                    val response = api.post(
                        "/v1/auth/stepup",
                        ApiClient.jsonOf("password" to password, "action" to "withdraw"),
                    )
                    token = response.optString("stepup_token")
                    _state.value = _state.value.copy(stepUpToken = token)
                } catch (e: CancellationException) {
                    throw e
                } catch (e: Exception) {
                    _state.value = _state.value.copy(submitting = false, error = e.message)
                    onDone(false)
                    return@launch
                }
            }
            try {
                val response = api.post(
                    "/v1/funds/withdraw",
                    requestBody(current),
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
            } catch (e: CancellationException) {
                throw e
            } catch (e: ApiClient.ApiException) {
                if (e.status >= 500) {
                    _state.value = _state.value.copy(
                        submitting = false,
                        error = (e.message ?: "Server error.") + " Check Withdrawals before pressing again; " +
                            "pressing again cannot create a second one.",
                    )
                } else {
                    _state.value = _state.value.copy(submitting = false, stepUpToken = null, error = e.message)
                }
                onDone(false)
            } catch (e: IOException) {
                _state.value = _state.value.copy(
                    submitting = false,
                    error = "Could not reach the server, so it is not known whether the withdrawal was created. " +
                        "Check Withdrawals, or press again: the same request cannot create a second one.",
                )
                onDone(false)
            } catch (e: Exception) {
                _state.value = _state.value.copy(submitting = false, error = e.message)
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

    /** A fresh withdrawal needs a fresh idempotency key and an empty form; only the venue is kept. */
    fun reset() {
        quoteJob?.cancel()
        _state.value = WithdrawUiState(venue = _state.value.venue)
    }

    companion object {
        private const val QUOTE_DELAY_MS = 600L
        private val EVM_ADDRESS = Regex("^0x[0-9a-fA-F]{40}$")

        /** The shape of an EVM address. The server also checks the checksum of a mixed-case one. */
        fun isEvmAddress(value: String): Boolean = EVM_ADDRESS.matches(value)
    }
}
