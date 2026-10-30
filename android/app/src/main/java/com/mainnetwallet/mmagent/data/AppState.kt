package com.mainnetwallet.mmagent.data

import android.content.Context
import android.content.SharedPreferences
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.math.BigDecimal

/** What the app knows about the server right now. */
data class AgentStatus(
    val badge: String = "",
    val venue: String = "",
    val mode: String = "PAPER",
    val paused: Boolean = false,
    val todayPnlUsd: String = "0",
    val availableUsd: String? = null,
    val collateralAsset: String = "USD",
    val unitGuardOk: Boolean = true,
    val unitGuardReason: String = "",
    val hardCeilingMaxTrade: String = "2",
    val maxTradeUsd: String = "2",
    val counts: Map<String, Int> = emptyMap(),
    val capabilities: JSONObject? = null,
    val heartbeat: String = "",
    /** Every venue the server knows: venue, display_name, enabled, ready, reason, live, mode. */
    val venues: List<JSONObject> = emptyList(),
)

/** One line of the chat tab. [role] is "user", "agent" or "error". */
data class ChatMessage(val role: String, val text: String, val at: Long = System.currentTimeMillis())

data class ChatUiState(
    val messages: List<ChatMessage> = emptyList(),
    val busy: Boolean = false,
)

data class AppUiState(
    val status: AgentStatus = AgentStatus(),
    val balances: List<JSONObject> = emptyList(),
    val markets: List<JSONObject> = emptyList(),
    val positions: List<JSONObject> = emptyList(),
    val orders: List<JSONObject> = emptyList(),
    val proposals: List<JSONObject> = emptyList(),
    val rules: List<JSONObject> = emptyList(),
    val trades: List<JSONObject> = emptyList(),
    val pnlDays: List<Pair<String, BigDecimal>> = emptyList(),
    val allowlist: List<JSONObject> = emptyList(),
    val approvals: List<JSONObject> = emptyList(),
    val fundsHistory: List<JSONObject> = emptyList(),
    val withdrawals: List<JSONObject> = emptyList(),
    val loading: Boolean = false,
    val error: String? = null,
    val loggedIn: Boolean = false,
) {
    /**
     * True when the server is reachable and the data is fresh.
     *
     * Money-adding and money-out actions are disabled when this is false: an
     * unreachable server means the price and the balance on screen are unknown.
     * Cancel and Pause stay available, because they reduce risk.
     */
    val online: Boolean get() = error == null && !loading

    /** True when the server could not be reached at the last refresh. */
    val unreachable: Boolean get() = error != null
}

/**
 * The shared app state: one refresh pulls status, balances, markets, positions,
 * orders, proposals, rules and history.
 *
 * Offline or stale data disables the money-adding and money-out actions; cancel
 * and pause stay available whenever the server is reachable.
 */
class AppViewModel(context: Context, private val api: ApiClient) : ViewModel() {

    private val prefs: SharedPreferences =
        context.getSharedPreferences("mm_agent_prefs", Context.MODE_PRIVATE)

    private val _state = MutableStateFlow(AppUiState())
    val state: StateFlow<AppUiState> = _state.asStateFlow()

    private val _chat = MutableStateFlow(ChatUiState())
    val chat: StateFlow<ChatUiState> = _chat.asStateFlow()

    /** One-shot messages for the snackbar: the server's own words, never paraphrased. */
    private val _messages = MutableSharedFlow<String>(extraBufferCapacity = 16)
    val messages: SharedFlow<String> = _messages.asSharedFlow()

    private var refreshJob: Job? = null

    init {
        // A 401 that means "the session is gone" sends the operator back to login.
        api.onUnauthorized = { expireSession() }
        loadSession()
    }

    /** The saved URL, or the one baked into the build when nothing is saved yet. */
    var baseUrl: String
        get() = prefs.getString(KEY_BASE_URL, null)?.takeIf { it.isNotBlank() } ?: api.baseUrl
        set(value) {
            prefs.edit().putString(KEY_BASE_URL, value).apply()
            api.baseUrl = value
        }

    /** The token lives in private prefs; the API client reads it on every request. */
    var token: String?
        get() = prefs.getString(KEY_TOKEN, null)
        set(value) {
            prefs.edit().apply {
                if (value.isNullOrBlank()) remove(KEY_TOKEN) else putString(KEY_TOKEN, value)
            }.apply()
        }

    fun saveBaseUrl(url: String) {
        baseUrl = url.trim()
    }

    /** Restore the saved session after a restart: URL first, then token. */
    fun loadSession() {
        prefs.getString(KEY_BASE_URL, null)?.takeIf { it.isNotBlank() }?.let { api.baseUrl = it }
        val saved = token
        _state.value = _state.value.copy(loggedIn = !saved.isNullOrBlank())
        if (!saved.isNullOrBlank()) refresh()
    }

    fun login(username: String, password: String, onDone: (Boolean) -> Unit) {
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, error = null)
            try {
                val response = api.post(
                    "/v1/auth/login",
                    ApiClient.jsonOf("username" to username, "password" to password)
                )
                val newToken = response.optString("token")
                if (newToken.isBlank()) {
                    _state.value = _state.value.copy(loading = false, error = "The server did not return a session token.")
                    onDone(false)
                    return@launch
                }
                token = newToken
                _state.value = _state.value.copy(loggedIn = true, loading = false, error = null)
                refresh()
                onDone(true)
            } catch (e: Exception) {
                _state.value = _state.value.copy(loading = false, error = e.message ?: "Cannot reach the server.")
                onDone(false)
            }
        }
    }

    fun logout() {
        viewModelScope.launch {
            runCatching { api.post("/v1/auth/logout", JSONObject()) }
            clearSession(null)
        }
    }

    /** The server rejected the session: forget it and ask for the password again. */
    private fun expireSession() {
        if (token.isNullOrBlank()) return
        clearSession("Your session expired. Sign in again.")
    }

    private fun clearSession(error: String?) {
        refreshJob?.cancel()
        token = null
        _chat.value = ChatUiState()
        _state.value = AppUiState(error = error)
    }

    fun notify(message: String) {
        _messages.tryEmit(message)
    }

    /**
     * Pull everything the screens show.
     *
     * [silent] is the 15 s background refresh: it never flips `loading`, so the
     * money-adding buttons do not blink off every cycle, and a failed endpoint
     * keeps the last good data instead of blanking its screen. A manual refresh
     * shows the spinner.
     */
    fun refresh(silent: Boolean = false) {
        if (token.isNullOrBlank()) return
        if (refreshJob?.isActive == true) {
            // A background tick must not pile up; an operator action wins and restarts it
            // so the screen never shows data from before the action.
            if (silent) return
            refreshJob?.cancel()
        }
        refreshJob = viewModelScope.launch {
            if (!silent) _state.value = _state.value.copy(loading = true, error = null)
            try {
                val status = api.get("/v1/status")
                val previous = _state.value
                val balance = runCatching { api.get("/v1/balance") }.getOrNull()
                val markets = runCatching { api.get("/v1/markets?limit=60") }.getOrNull()
                val positions = runCatching { api.get("/v1/positions") }.getOrNull()
                val orders = runCatching { api.get("/v1/orders?status=open&limit=100") }.getOrNull()
                val proposals = runCatching { api.get("/v1/proposals?limit=20") }.getOrNull()
                val rules = runCatching { api.get("/v1/rules") }.getOrNull()
                val trades = runCatching { api.get("/v1/trades?limit=50") }.getOrNull()
                val pnl = runCatching { api.get("/v1/pnl/daily?days=30") }.getOrNull()
                val allowlist = runCatching { api.get("/v1/funds/allowlist") }.getOrNull()
                val approvals = runCatching { api.get("/v1/approvals") }.getOrNull()
                val fundsHistory = runCatching { api.get("/v1/funds/history") }.getOrNull()
                val withdrawals = runCatching { api.get("/v1/funds/withdrawals") }.getOrNull()

                // The session may have been cleared while we were waiting.
                if (token.isNullOrBlank()) return@launch

                val capabilities = status.optJSONObject("capabilities")
                val limits = status.optJSONObject("limits")
                val counts = HashMap<String, Int>()
                status.optJSONObject("counts")?.let { source ->
                    source.keys().forEach { key -> counts[key] = source.optInt(key) }
                }
                val balances = if (balance != null) ApiClient.arrayOf(balance, "balances").objects() else previous.balances
                val firstBalance = balances.firstOrNull()

                _state.value = AppUiState(
                    status = AgentStatus(
                        badge = status.optString("badge"),
                        venue = status.optString("active_venue"),
                        mode = status.optString("badge").substringAfter('|', "PAPER").trim(),
                        paused = status.optBoolean("paused"),
                        todayPnlUsd = status.optString("today_pnl_usd", "0"),
                        availableUsd = firstBalance?.optString("available"),
                        collateralAsset = firstBalance?.optString("collateral_asset") ?: "USD",
                        unitGuardOk = status.optJSONObject("unit_guard")?.optBoolean("ok") ?: true,
                        unitGuardReason = status.optJSONObject("unit_guard")?.optString("reason") ?: "",
                        hardCeilingMaxTrade = limits?.optJSONObject("hard_ceilings")?.optString("max_trade_usd") ?: "2",
                        maxTradeUsd = limits?.optString("max_trade_usd") ?: "2",
                        counts = counts,
                        capabilities = capabilities,
                        heartbeat = capabilities?.optJSONObject("heartbeat")?.optString("reason") ?: "",
                        venues = ApiClient.arrayOf(status, "venues").objects(),
                    ),
                    balances = balances,
                    markets = markets?.let { ApiClient.arrayOf(it, "markets").objects() } ?: previous.markets,
                    positions = positions?.let { ApiClient.arrayOf(it, "positions").objects() } ?: previous.positions,
                    orders = orders?.let { ApiClient.arrayOf(it, "orders").objects() } ?: previous.orders,
                    proposals = proposals?.let { ApiClient.arrayOf(it, "proposals").objects() } ?: previous.proposals,
                    rules = rules?.let { ApiClient.arrayOf(it, "rules").objects() } ?: previous.rules,
                    trades = trades?.let { ApiClient.arrayOf(it, "trades").objects() } ?: previous.trades,
                    pnlDays = pnl?.let { body ->
                        ApiClient.arrayOf(body, "days").objects().map { obj ->
                            obj.optString("date") to (obj.optString("realized_usd", "0").toBigDecimalOrNull() ?: BigDecimal.ZERO)
                        }
                    } ?: previous.pnlDays,
                    allowlist = allowlist?.let { ApiClient.arrayOf(it, "entries").objects() } ?: previous.allowlist,
                    approvals = approvals?.let { ApiClient.arrayOf(it, "approvals").objects() } ?: previous.approvals,
                    fundsHistory = fundsHistory?.let { ApiClient.arrayOf(it, "history").objects() } ?: previous.fundsHistory,
                    withdrawals = withdrawals?.let { ApiClient.arrayOf(it, "withdrawals").objects() } ?: previous.withdrawals,
                    loading = false,
                    loggedIn = true,
                )
            } catch (e: kotlinx.coroutines.CancellationException) {
                throw e
            } catch (e: Exception) {
                if (token.isNullOrBlank()) return@launch
                // Keep the last good data on screen; `error` is what turns the money buttons off.
                _state.value = _state.value.copy(loading = false, error = e.message ?: "Cannot reach the server.")
            }
        }
    }

    /** Pause and cancel-all reduce risk, so they are one tap plus a normal dialog. */
    fun pause() {
        viewModelScope.launch {
            try {
                api.post("/v1/pause", JSONObject())
                notify("Agent paused.")
            } catch (e: Exception) {
                notify(e.message ?: "Pause failed.")
            }
            refresh()
        }
    }

    fun resume() {
        viewModelScope.launch {
            try {
                api.post("/v1/resume", JSONObject())
                notify("Agent resumed.")
            } catch (e: Exception) {
                notify(e.message ?: "Resume failed.")
            }
            refresh()
        }
    }

    /**
     * Switch the active venue. The server only accepts a ready venue and answers
     * 409 with its own reason otherwise; that reason goes to the snackbar verbatim.
     */
    fun switchVenue(venue: String, onDone: (Boolean) -> Unit = {}) {
        viewModelScope.launch {
            try {
                api.post("/v1/venues/active", ApiClient.jsonOf("venue" to venue))
                notify("Active venue: ${venue.replaceFirstChar { it.uppercase() }}.")
                refresh()
                onDone(true)
            } catch (e: Exception) {
                notify(e.message ?: "Venue switch failed.")
                onDone(false)
            }
        }
    }

    /** Rejecting a proposal places no order, so it is one tap. */
    fun rejectProposal(proposalId: String, onDone: (String) -> Unit = {}) {
        viewModelScope.launch {
            val message = try {
                api.post("/v1/proposals/$proposalId/reject", JSONObject())
                "Proposal rejected."
            } catch (e: Exception) {
                e.message ?: "Reject failed."
            }
            notify(message)
            onDone(message)
            refresh()
        }
    }

    /**
     * Raise a hard ceiling. The server wants a single-use step-up token bound to
     * the "ceiling_raise" action; the password is exchanged for one here and is
     * never stored. (The old code sent no token, so every raise was refused.)
     */
    fun raiseCeiling(name: String, value: String, password: String, onDone: (Boolean, String) -> Unit) {
        viewModelScope.launch {
            try {
                val stepup = api.post(
                    "/v1/auth/stepup",
                    ApiClient.jsonOf("password" to password, "action" to "ceiling_raise"),
                )
                val stepupToken = stepup.optString("stepup_token")
                if (stepupToken.isBlank()) {
                    onDone(false, "The server did not issue a step-up token.")
                    return@launch
                }
                api.post(
                    "/v1/limits/hard-ceiling",
                    ApiClient.jsonOf("name" to name, "value" to value),
                    stepupToken = stepupToken,
                )
                onDone(true, "Hard ceiling updated.")
                refresh()
            } catch (e: Exception) {
                onDone(false, e.message ?: "Could not raise the ceiling.")
            }
        }
    }

    /** Send one chat message; slash commands and plain text take the same route. */
    fun sendChat(text: String) {
        val message = text.trim()
        if (message.isEmpty() || _chat.value.busy) return
        _chat.value = ChatUiState(_chat.value.messages + ChatMessage("user", message), busy = true)
        viewModelScope.launch {
            val reply = try {
                // The LLM can take longer than the default 30 s read timeout.
                val response = api.post(
                    "/v1/chat",
                    ApiClient.jsonOf("message" to message),
                    readTimeoutSeconds = 120,
                )
                ChatMessage("agent", response.optString("reply").ifBlank { "(no reply)" })
            } catch (e: Exception) {
                ChatMessage("error", e.message ?: "Chat failed.")
            }
            _chat.value = ChatUiState(_chat.value.messages + reply, busy = false)
            // A slash command may have changed orders, proposals or the venue.
            if (message.startsWith("/")) refresh()
        }
    }

    fun clearChat() {
        _chat.value = ChatUiState()
    }

    fun cancelAll(onDone: (String) -> Unit = {}) {
        viewModelScope.launch {
            try {
                val response = api.post(
                    "/v1/orders/cancel-all",
                    JSONObject(),
                    java.util.UUID.randomUUID().toString(),
                )
                onDone("Cancelled ${response.optInt("cancelled", 0)} order(s).")
                refresh()
            } catch (e: ApiClient.ApiException) {
                onDone(e.message ?: "Cancel-all failed.")
            } catch (e: Exception) {
                onDone(e.message ?: "Cancel-all failed.")
            }
        }
    }

    fun cancelOrder(orderId: String, onDone: (String) -> Unit = {}) {
        viewModelScope.launch {
            try {
                api.post("/v1/orders/$orderId/cancel", JSONObject())
                onDone("Order cancelled.")
                refresh()
            } catch (e: ApiClient.ApiException) {
                onDone(e.message ?: "Cancel failed.")
            } catch (e: Exception) {
                onDone(e.message ?: "Cancel failed.")
            }
        }
    }

    fun approveProposal(proposalId: String, onDone: (String) -> Unit = {}) {
        viewModelScope.launch {
            try {
                val response = api.post("/v1/proposals/$proposalId/approve", JSONObject())
                onDone("Order ${response.optString("status")}.")
                refresh()
            } catch (e: ApiClient.ApiException) {
                onDone(e.message ?: "Approval failed.")
            } catch (e: Exception) {
                onDone(e.message ?: "Approval failed.")
            }
        }
    }

    /** Sell part or all of a position. Reduce-only: the server refuses an open short. */
    fun sellPosition(
        marketId: String,
        outcome: String,
        shares: String,
        venue: String,
        onDone: (String) -> Unit = {},
    ) {
        viewModelScope.launch {
            try {
                api.post(
                    "/v1/positions/sell",
                    ApiClient.jsonOf(
                        "venue" to venue,
                        "market_id" to marketId,
                        "outcome" to outcome,
                        "shares" to shares,
                    ),
                    java.util.UUID.randomUUID().toString(),
                )
                onDone("Sell submitted.")
                refresh()
            } catch (e: ApiClient.ApiException) {
                onDone(e.message ?: "Sell failed.")
            } catch (e: Exception) {
                onDone(e.message ?: "Sell failed.")
            }
        }
    }

    /** Redeem a resolved position, where the venue has an API for it. */
    fun redeemPosition(
        marketId: String,
        outcome: String,
        venue: String,
        onDone: (String) -> Unit = {},
    ) {
        viewModelScope.launch {
            try {
                api.post(
                    "/v1/positions/redeem",
                    ApiClient.jsonOf("venue" to venue, "market_id" to marketId, "outcome" to outcome),
                )
                onDone("Redeem submitted.")
                refresh()
            } catch (e: ApiClient.ApiException) {
                onDone(e.message ?: "Redeem failed.")
            } catch (e: Exception) {
                onDone(e.message ?: "Redeem failed.")
            }
        }
    }

    /** Split a full outcome set or merge it back into collateral (Polymarket only). */
    fun splitOrMerge(
        marketId: String,
        venue: String,
        action: String,
        onDone: (String) -> Unit = {},
    ) {
        viewModelScope.launch {
            try {
                api.post(
                    "/v1/positions/$action",
                    ApiClient.jsonOf("venue" to venue, "market_id" to marketId),
                )
                onDone("${action.replaceFirstChar { it.uppercase() }} submitted.")
                refresh()
            } catch (e: ApiClient.ApiException) {
                onDone(e.message ?: "${action.replaceFirstChar { it.uppercase() }} failed.")
            } catch (e: Exception) {
                onDone(e.message ?: "${action.replaceFirstChar { it.uppercase() }} failed.")
            }
        }
    }

    /** Cancel a withdrawal while it is still in its hold window. */
    fun cancelWithdrawal(withdrawalId: String, onDone: (String) -> Unit = {}) {
        viewModelScope.launch {
            try {
                api.post("/v1/funds/withdrawals/$withdrawalId/cancel", JSONObject())
                onDone("Withdrawal cancelled.")
                refresh()
            } catch (e: ApiClient.ApiException) {
                onDone(e.message ?: "Cancel failed.")
            } catch (e: Exception) {
                onDone(e.message ?: "Cancel failed.")
            }
        }
    }

    /**
     * Amend a resting order. Only Kalshi supports it; elsewhere the server
     * refuses and says so, and the app offers cancel-then-new as two separate
     * audited actions instead.
     */
    fun amendOrder(orderId: String, price: String?, shares: String?, onDone: (String) -> Unit = {}) {
        viewModelScope.launch {
            try {
                api.post(
                    "/v1/orders/$orderId/amend",
                    ApiClient.jsonOf("price" to price, "shares" to shares),
                )
                onDone("Order amended.")
                refresh()
            } catch (e: ApiClient.ApiException) {
                onDone(e.message ?: "Amend failed.")
            } catch (e: Exception) {
                onDone(e.message ?: "Amend failed.")
            }
        }
    }

    companion object {
        const val KEY_BASE_URL = "base_url"
        const val KEY_TOKEN = "session_token"
    }
}
