package com.mainnetwallet.mmagent.data

import android.content.Context
import android.content.SharedPreferences
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
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
    val loading: Boolean = false,
    val error: String? = null,
    val loggedIn: Boolean = false,
)

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

    var baseUrl: String
        get() = prefs.getString(KEY_BASE_URL, "") ?: ""
        set(value) {
            prefs.edit().putString(KEY_BASE_URL, value).apply()
            api.baseUrl = value
        }

    var token: String?
        get() = prefs.getString(KEY_TOKEN, null)
        set(value) {
            prefs.edit().putString(KEY_TOKEN, value).apply()
        }

    fun saveBaseUrl(url: String) {
        baseUrl = url
    }

    fun loadSession() {
        token = prefs.getString(KEY_TOKEN, null)
        _state.value = _state.value.copy(loggedIn = !token.isNullOrBlank())
    }

    fun login(username: String, password: String, onDone: (Boolean) -> Unit) {
        viewModelScope.launch {
            try {
                val response = api.post(
                    "/v1/auth/login",
                    ApiClient.jsonOf("username" to username, "password" to password)
                )
                val newToken = response.optString("token")
                token = newToken
                _state.value = _state.value.copy(loggedIn = true, error = null)
                refresh()
                onDone(true)
            } catch (e: ApiClient.ApiException) {
                _state.value = _state.value.copy(error = e.message)
                onDone(false)
            } catch (e: Exception) {
                _state.value = _state.value.copy(error = e.message)
                onDone(false)
            }
        }
    }

    fun logout() {
        viewModelScope.launch {
            runCatching { api.post("/v1/auth/logout", JSONObject()) }
            token = null
            prefs.edit().remove(KEY_TOKEN).apply()
            _state.value = AppUiState()
        }
    }

    fun refresh() {
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, error = null)
            try {
                val status = api.get("/v1/status")
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

                val capabilities = status.optJSONObject("capabilities")
                val limits = status.optJSONObject("limits")
                val counts = JSONObject()
                status.optJSONObject("counts")?.let { source ->
                    source.keys().forEach { key -> counts.put(key, source.optInt(key)) }
                }
                val balances = ApiClient.arrayOf(balance, "balances").objects()
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
                        counts = counts.keys().asSequence().associateWith { counts.optInt(it) },
                        capabilities = capabilities,
                        heartbeat = capabilities?.optJSONObject("heartbeat")?.optString("reason") ?: "",
                    ),
                    balances = balances,
                    markets = ApiClient.arrayOf(markets, "markets").objects(),
                    positions = ApiClient.arrayOf(positions, "positions").objects(),
                    orders = ApiClient.arrayOf(orders, "orders").objects(),
                    proposals = ApiClient.arrayOf(proposals, "proposals").objects(),
                    rules = ApiClient.arrayOf(rules, "rules").objects(),
                    trades = ApiClient.arrayOf(trades, "trades").objects(),
                    pnlDays = ApiClient.arrayOf(pnl, "days").objects().map { obj ->
                        obj.optString("date") to java.math.BigDecimal(obj.optString("realized_usd", "0"))
                    },
                    allowlist = ApiClient.arrayOf(allowlist, "entries").objects(),
                    approvals = ApiClient.arrayOf(approvals, "approvals").objects(),
                    fundsHistory = ApiClient.arrayOf(fundsHistory, "history").objects(),
                    loading = false,
                    loggedIn = true,
                )
            } catch (e: Exception) {
                _state.value = _state.value.copy(loading = false, error = e.message ?: "Cannot reach the server.")
            }
        }
    }

    /** Pause and cancel-all reduce risk, so they are one tap plus a normal dialog. */
    fun pause() {
        viewModelScope.launch {
            runCatching { api.post("/v1/pause", JSONObject()) }
            refresh()
        }
    }

    fun resume() {
        viewModelScope.launch {
            runCatching { api.post("/v1/resume", JSONObject()) }
            refresh()
        }
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

    companion object {
        const val KEY_BASE_URL = "base_url"
        const val KEY_TOKEN = "session_token"
    }
}
