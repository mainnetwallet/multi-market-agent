package com.mainnetwallet.mmagent.data

import android.content.Context
import android.content.SharedPreferences
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import java.math.BigDecimal
import java.net.URLEncoder

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

/** One thing the assistant did while answering. [state] is "running", "done", "error" or "unknown". */
data class ToolStep(val id: String, val name: String, val state: String)

/** One line of the chat tab. [role] is "user", "agent" or "error". */
data class ChatMessage(
    val role: String,
    val text: String,
    val at: Long = System.currentTimeMillis(),
    val tools: List<ToolStep> = emptyList(),
    /** True while the reply is still arriving. */
    val streaming: Boolean = false,
)

/** A saved chat. The server keeps no chat history, so conversations live on this device. */
data class Conversation(val id: String, val title: String, val messages: List<ChatMessage>, val updatedAt: Long)

/** [messages] belong to the open conversation ([activeId]; null = a new chat nothing was sent in yet). */
data class ChatUiState(
    val messages: List<ChatMessage> = emptyList(),
    val busy: Boolean = false,
    val conversations: List<Conversation> = emptyList(),
    val activeId: String? = null,
)

/** Replace the step with the same id (or, with no id, the running step of the same name), else add it. */
internal fun mergeToolStep(steps: List<ToolStep>, step: ToolStep): List<ToolStep> {
    val index = steps.indexOfLast {
        if (step.id.isNotBlank()) it.id == step.id else it.name == step.name && it.state == "running"
    }
    return if (index >= 0) steps.toMutableList().also { it[index] = step } else steps + step
}

/** A chat is titled after its first line, shortened. */
internal fun chatTitle(firstMessage: String): String {
    val line = firstMessage.lineSequence().firstOrNull { it.isNotBlank() }.orEmpty().trim()
    return if (line.length > 40) line.take(40).trimEnd() + "…" else line.ifBlank { "New chat" }
}

/** The two read-only server documents Settings can show. */
data class InfoUiState(
    val report: String? = null,
    val audit: List<JSONObject> = emptyList(),
    val loading: Boolean = false,
    val error: String? = null,
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
    private val _info = MutableStateFlow(InfoUiState())
    val info: StateFlow<InfoUiState> = _info.asStateFlow()

    private val _messages = MutableSharedFlow<String>(extraBufferCapacity = 16)
    val messages: SharedFlow<String> = _messages.asSharedFlow()

    private var refreshJob: Job? = null

    /** The running chat request; cancelling it is "Stop". */
    private var chatJob: Job? = null

    /** When the saved session was last swapped for a fresh one (0 = not yet this run). */
    private var lastRenewAt = 0L

    init {
        // A 401 that means "the session is gone" sends the operator back to login.
        api.onUnauthorized = { expireSession() }
        loadSession()
        if (!token.isNullOrBlank()) loadChats()
        if (token.isNullOrBlank()) warmUp()
    }

    /**
     * Wake the server and open the connection while the operator types, so "Sign in" is not
     * the first request to a server that may be asleep. Failures are ignored.
     */
    private fun warmUp() {
        if (baseUrl.isBlank()) return
        viewModelScope.launch { runCatching { api.get("/health") } }
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

    /** The last username that signed in, so the login screen never starts empty. */
    var savedUsername: String
        get() = prefs.getString(KEY_USERNAME, null)?.takeIf { it.isNotBlank() } ?: DEFAULT_USERNAME
        set(value) {
            prefs.edit().putString(KEY_USERNAME, value.trim().ifBlank { DEFAULT_USERNAME }).apply()
        }

    /**
     * Keep the operator signed in: trade the saved session for a fresh one on every start
     * and every few hours while the app is open. Best effort. Offline, or against a server
     * without the endpoint, the saved session stays as it is; a rejected session still
     * sends the operator to the login screen.
     */
    fun renewSessionIfDue() {
        if (token.isNullOrBlank()) return
        val now = System.currentTimeMillis()
        if (now - lastRenewAt < RENEW_EVERY_MS) return
        lastRenewAt = now
        viewModelScope.launch {
            try {
                val fresh = api.post("/v1/auth/refresh", JSONObject()).optString("token")
                if (fresh.isNotBlank() && !token.isNullOrBlank()) token = fresh
            } catch (e: kotlinx.coroutines.CancellationException) {
                throw e
            } catch (e: Exception) {
                // Try again in a few minutes rather than waiting for the next full interval.
                lastRenewAt = now - RENEW_EVERY_MS + RENEW_RETRY_MS
            }
        }
    }

    fun saveBaseUrl(url: String) {
        baseUrl = url.trim()
    }

    /** Restore the saved session after a restart: URL first, then token. */
    fun loadSession() {
        prefs.getString(KEY_BASE_URL, null)?.takeIf { it.isNotBlank() }?.let { api.baseUrl = it }
        val saved = token
        _state.value = _state.value.copy(loggedIn = !saved.isNullOrBlank())
        if (!saved.isNullOrBlank()) {
            refresh()
            renewSessionIfDue()
        }
    }

    fun login(username: String, password: String, onDone: (Boolean) -> Unit) {
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, error = null)
            try {
                val response = api.post(
                    "/v1/auth/login",
                    ApiClient.jsonOf("username" to username, "password" to password),
                    readTimeoutSeconds = 90,
                )
                val newToken = response.optString("token")
                if (newToken.isBlank()) {
                    _state.value = _state.value.copy(loading = false, error = "The server did not return a session token.")
                    onDone(false)
                    return@launch
                }
                token = newToken
                savedUsername = username
                lastRenewAt = System.currentTimeMillis()
                loadChats()
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
            clearSession(null, wipeChats = true)
        }
    }

    /** The server rejected the session: forget it and ask for the password again. */
    private fun expireSession() {
        if (token.isNullOrBlank()) return
        // Signing in again brings the saved chats back; only an explicit sign-out erases them.
        clearSession("Your session expired. Sign in again.", wipeChats = false)
    }

    private fun clearSession(error: String?, wipeChats: Boolean) {
        refreshJob?.cancel()
        chatJob?.cancel()
        token = null
        if (wipeChats) prefs.edit().remove(KEY_CHATS).apply()
        _chat.value = ChatUiState()
        _state.value = AppUiState(error = error)
    }

    /** One page of browse results for a venue (public data, read-only). */
    suspend fun discover(venue: String, tab: String, query: String, offset: Int): JSONObject {
        val enc = { value: String -> java.net.URLEncoder.encode(value, "UTF-8") }
        val q = if (query.isBlank()) "" else "&q=" + enc(query)
        return api.get("/v1/discover?venue=" + enc(venue) + "&tab=" + enc(tab) + q + "&limit=30&offset=" + offset)
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
                // Everything except /status goes out at once. One request after another meant
                // twelve round trips to a remote server before Home had its data.
                val extras = coroutineScope {
                    listOf(
                        "/v1/balance",
                        "/v1/markets?limit=60",
                        "/v1/positions",
                        "/v1/orders?status=open&limit=100",
                        "/v1/proposals?limit=20",
                        "/v1/rules",
                        "/v1/trades?limit=50",
                        "/v1/pnl/daily?days=30",
                        "/v1/funds/allowlist",
                        "/v1/approvals",
                        "/v1/funds/history",
                        "/v1/funds/withdrawals",
                    ).map { path -> async { runCatching { api.get(path) }.getOrNull() } }.awaitAll()
                }
                val balance = extras[0]
                val markets = extras[1]
                val positions = extras[2]
                val orders = extras[3]
                val proposals = extras[4]
                val rules = extras[5]
                val trades = extras[6]
                val pnl = extras[7]
                val allowlist = extras[8]
                val approvals = extras[9]
                val fundsHistory = extras[10]
                val withdrawals = extras[11]

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

    /**
     * Send one chat message; slash commands and plain text take the same route.
     *
     * The reply streams in over /v1/chat/stream: tool progress and text arrive as they happen. The
     * server answers each message on its own, so earlier messages are not part of the context.
     */
    fun sendChat(text: String) {
        val message = text.trim()
        if (message.isEmpty() || _chat.value.busy) return
        _chat.update { s ->
            val id = s.activeId ?: java.util.UUID.randomUUID().toString()
            val known = s.conversations.any { it.id == id }
            val now = System.currentTimeMillis()
            s.copy(
                activeId = id,
                busy = true,
                messages = s.messages + ChatMessage("user", message) + ChatMessage("agent", "", streaming = true),
                conversations = if (known) s.conversations else listOf(Conversation(id, chatTitle(message), emptyList(), now)) + s.conversations,
            )
        }
        saveActiveChat()
        chatJob = viewModelScope.launch {
            var reply: String? = null
            try {
                api.stream("/v1/chat/stream?message=" + URLEncoder.encode(message, "UTF-8")) { event, data ->
                    when (event) {
                        "delta" -> updateReply { it.copy(text = it.text + data.optString("text")) }
                        "tool" -> updateReply {
                            val step = ToolStep(data.optString("id"), data.optString("name"), data.optString("state"))
                            it.copy(tools = mergeToolStep(it.tools, step))
                        }
                        "message" -> reply = data.optString("reply").ifBlank { "(no reply)" }
                    }
                }
                val finalReply = reply
                if (finalReply != null) {
                    finishTurn(finalReply, null, isError = false)
                } else {
                    finishTurn(null, "The connection closed before the agent replied.", isError = true)
                }
            } catch (e: CancellationException) {
                finishTurn(null, "Stopped.", isError = false)
                throw e
            } catch (e: Exception) {
                finishTurn(null, e.message ?: "Chat failed.", isError = true)
            }
        }
    }

    /**
     * Stop generating. The server never starts a new step after this, but a step that is already
     * running (a trade, say) finishes, so the reply says so and the lists are refreshed.
     */
    fun stopChat() {
        chatJob?.cancel()
    }

    fun newChat() {
        if (_chat.value.busy) return
        _chat.update { it.copy(activeId = null, messages = emptyList()) }
    }

    fun openChat(id: String) {
        if (_chat.value.busy) return
        _chat.update { s ->
            val chat = s.conversations.firstOrNull { it.id == id } ?: return@update s
            s.copy(activeId = chat.id, messages = chat.messages)
        }
    }

    fun renameChat(id: String, title: String) {
        val clean = title.trim().take(60)
        if (clean.isEmpty()) return
        _chat.update { s -> s.copy(conversations = s.conversations.map { if (it.id == id) it.copy(title = clean) else it }) }
        persistChats()
    }

    fun deleteChat(id: String) {
        if (_chat.value.busy && _chat.value.activeId == id) return
        _chat.update { s ->
            val open = s.activeId == id
            s.copy(
                conversations = s.conversations.filterNot { it.id == id },
                activeId = if (open) null else s.activeId,
                messages = if (open) emptyList() else s.messages,
            )
        }
        persistChats()
    }

    /** Change the reply that is still arriving (always the last message). */
    private fun updateReply(change: (ChatMessage) -> ChatMessage) {
        _chat.update { s ->
            val last = s.messages.lastOrNull()
            if (last == null || !last.streaming) s else s.copy(messages = s.messages.dropLast(1) + change(last))
        }
    }

    /**
     * End the current turn. [reply] is the server's final text; [note] is a line added below
     * whatever arrived (a stop or an error). Steps still "running" are shown as unknown, never as done.
     */
    private fun finishTurn(reply: String?, note: String?, isError: Boolean) {
        val last = _chat.value.messages.lastOrNull()?.takeIf { it.streaming }
        val usedTools = last?.tools?.isNotEmpty() == true
        val cutMidStep = last?.tools?.any { it.state == "running" } == true
        _chat.update { s ->
            val open = s.messages.lastOrNull()?.takeIf { it.streaming }
            val before = if (open != null) s.messages.dropLast(1) else s.messages
            val settled = open?.copy(
                text = reply ?: open.text,
                streaming = false,
                tools = open.tools.map {
                    if (it.state != "running") it else it.copy(state = if (reply != null) "done" else "unknown")
                },
            )
            val keep = if (settled != null && (settled.text.isNotBlank() || settled.tools.isNotEmpty())) listOf(settled) else emptyList()
            val warning = if (note != null && cutMidStep) {
                "$note A step was already running and may have finished. Check Orders and Positions."
            } else {
                note
            }
            val tail = if (warning.isNullOrBlank()) emptyList() else listOf(ChatMessage(if (isError) "error" else "agent", warning))
            s.copy(messages = before + keep + tail, busy = false)
        }
        saveActiveChat()
        // A command or a tool may have changed orders, proposals or the venue.
        val lastUser = _chat.value.messages.lastOrNull { it.role == "user" }?.text.orEmpty()
        if (lastUser.startsWith("/") || usedTools) refresh()
    }

    /** Copy the open conversation's messages into the saved list and write it to disk. */
    private fun saveActiveChat() {
        _chat.update { s ->
            val id = s.activeId ?: return@update s
            val now = System.currentTimeMillis()
            val saved = s.messages.filterNot { it.streaming }
            val updated = s.conversations.map { if (it.id == id) it.copy(messages = saved, updatedAt = now) else it }
            s.copy(conversations = updated.sortedByDescending { it.updatedAt }.take(MAX_CHATS))
        }
        persistChats()
    }

    private fun persistChats() {
        val chats = JSONArray()
        for (chat in _chat.value.conversations) {
            val messages = JSONArray()
            for (m in chat.messages.takeLast(MAX_CHAT_MESSAGES)) {
                messages.put(JSONObject().put("role", m.role).put("text", m.text).put("at", m.at))
            }
            chats.put(JSONObject().put("id", chat.id).put("title", chat.title).put("updatedAt", chat.updatedAt).put("messages", messages))
        }
        prefs.edit().putString(KEY_CHATS, chats.toString()).apply()
    }

    private fun loadChats() {
        val raw = prefs.getString(KEY_CHATS, null)?.takeIf { it.isNotBlank() } ?: return
        val chats = try {
            JSONArray(raw).objects().map { o ->
                Conversation(
                    id = o.optString("id"),
                    title = o.optString("title").ifBlank { "New chat" },
                    updatedAt = o.optLong("updatedAt"),
                    messages = ApiClient.arrayOf(o, "messages").objects().map { m ->
                        ChatMessage(m.optString("role"), m.optString("text"), m.optLong("at"))
                    },
                )
            }.filter { it.id.isNotBlank() }
        } catch (e: Exception) {
            emptyList()
        }
        if (chats.isNotEmpty()) {
            _chat.value = ChatUiState(conversations = chats.sortedByDescending { it.updatedAt }.take(MAX_CHATS))
        }
    }

    /** Today's report, rendered by the server as plain text. */
    fun loadReport() {
        viewModelScope.launch {
            _info.value = _info.value.copy(loading = true, error = null)
            try {
                val response = api.get("/v1/reports/daily")
                _info.value = _info.value.copy(report = response.optString("text"), loading = false)
            } catch (e: Exception) {
                _info.value = _info.value.copy(loading = false, error = e.message ?: "Could not load the report.")
            }
        }
    }

    /** The newest audit entries, secrets already redacted by the server. */
    fun loadAudit() {
        viewModelScope.launch {
            _info.value = _info.value.copy(loading = true, error = null)
            try {
                val response = api.get("/v1/audit")
                _info.value = _info.value.copy(
                    audit = ApiClient.arrayOf(response, "entries").objects(),
                    loading = false,
                )
            } catch (e: Exception) {
                _info.value = _info.value.copy(loading = false, error = e.message ?: "Could not load the audit log.")
            }
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
        const val KEY_USERNAME = "username"
        private const val KEY_CHATS = "chat_history"
        private const val MAX_CHATS = 50
        private const val MAX_CHAT_MESSAGES = 200
        const val DEFAULT_USERNAME = "operator"
        private const val RENEW_EVERY_MS = 6L * 60 * 60 * 1000
        private const val RENEW_RETRY_MS = 5L * 60 * 1000
    }
}
