package com.mainnetwallet.mmagent.data

import android.content.Context
import android.content.SharedPreferences
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.mainnetwallet.mmagent.ui.BROWSABLE_VENUES
import com.mainnetwallet.mmagent.ui.DiscoverCache
import com.mainnetwallet.mmagent.ui.DiscoverFlights
import com.mainnetwallet.mmagent.ui.DiscoverTabsCache
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
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
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.math.BigDecimal

/** What the app knows about the server right now. */
data class AgentStatus(
    val badge: String = "",
    val venue: String = "",
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
    /** Every venue the server knows: venue, display_name, enabled, ready, reason. */
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
    /** The server's position of a stored message in its chat (1, 2, ...); 0 for one not stored yet. */
    val seq: Int = 0,
)

/**
 * A saved chat. A [synced] chat lives on the server (its id is the server's conversation id, and the
 * server gives the assistant that chat's history); [messages] are then a cache that is refreshed when the
 * chat is opened. A chat that is not synced is an older one kept only on this device.
 */
data class Conversation(
    val id: String,
    val title: String,
    val messages: List<ChatMessage>,
    val updatedAt: Long,
    val synced: Boolean = false,
)

/** [messages] belong to the open conversation ([activeId]; null = a new chat nothing was sent in yet). */
data class ChatUiState(
    val messages: List<ChatMessage> = emptyList(),
    val busy: Boolean = false,
    val conversations: List<Conversation> = emptyList(),
    val activeId: String? = null,
    /** True when the server holds older messages of the open chat than the ones shown. */
    val hasEarlier: Boolean = false,
)

/** Replace the step with the same id (or, with no id, the running step of the same name), else add it. */
internal fun mergeToolStep(steps: List<ToolStep>, step: ToolStep): List<ToolStep> {
    val index = steps.indexOfLast {
        if (step.id.isNotBlank()) it.id == step.id else it.name == step.name && it.state == "running"
    }
    return if (index >= 0) steps.toMutableList().also { it[index] = step } else steps + step
}

/**
 * The saved list after asking the server: every server chat (keeping any messages already cached here),
 * plus the chats that exist only on this device. A synced chat the server no longer has was deleted
 * elsewhere and is dropped, unless it is the one open right now ([openId]); that one stays as a
 * device-only chat so its messages are not pulled out from under the reader.
 */
internal fun mergeChatLists(server: List<Conversation>, local: List<Conversation>, openId: String?): List<Conversation> {
    val cached = local.associateBy { it.id }
    val serverIds = server.mapTo(HashSet()) { it.id }
    val fromServer = server.map { chat -> chat.copy(messages = cached[chat.id]?.messages ?: chat.messages) }
    val kept = local.filter { it.id !in serverIds && (!it.synced || it.id == openId) }.map { it.copy(synced = false) }
    return (fromServer + kept).sortedByDescending { it.updatedAt }
}

/** A chat is titled after its first line, shortened. */
internal fun chatTitle(firstMessage: String): String {
    val line = firstMessage.lineSequence().firstOrNull { it.isNotBlank() }.orEmpty().trim()
    return if (line.length > 40) line.take(40).trimEnd() + "…" else line.ifBlank { "New chat" }
}

/** One row of `GET /v1/conversations`; its messages arrive when the chat is opened. */
internal fun conversationFromServer(o: JSONObject): Conversation = Conversation(
    id = o.optString("id"),
    title = o.optString("title").ifBlank { "New chat" },
    messages = emptyList(),
    updatedAt = Format.parseInstant(o.optString("updated_at"))?.toEpochMilli() ?: 0L,
    synced = true,
)

/**
 * One stored message. The server keeps plan notes next to tool steps in `tools`; only entries with a
 * tool name are steps.
 */
internal fun messageFromServer(o: JSONObject): ChatMessage = ChatMessage(
    role = o.optString("role").ifBlank { "agent" },
    text = o.optString("text"),
    at = Format.parseInstant(o.optString("created_at"))?.toEpochMilli() ?: System.currentTimeMillis(),
    seq = o.optInt("seq"),
    tools = ApiClient.arrayOf(o, "tools").objects()
        .filter { it.optString("name").isNotBlank() }
        .map { ToolStep(it.optString("id"), it.optString("name"), it.optString("state").ifBlank { "done" }) },
)

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

    /**
     * Bumped by every rename and delete, so a chat list fetched before one is not applied after it.
     * Until the server has confirmed a change, the pending maps keep a refresh from undoing it.
     */
    private var chatEpoch = 0
    private val pendingRenames = HashMap<String, String>()
    private val pendingDeletes = HashSet<String>()
    private var loadingEarlier = false

    /** When the saved session was last swapped for a fresh one (0 = not yet this run). */
    private var lastRenewAt = 0L

    /** Venues whose Markets lists are being fetched right now. Declared before `init`, which starts a prefetch. */
    private val prefetching = HashSet<String>()

    /** The Markets lists of the last run, so the page has something to show at once. */
    private val discoverSnapshot = DiscoverSnapshot(context.applicationContext)

    /** The last refresh's answers, so every screen has something to show at once. */
    private val appSnapshot = AppSnapshot(context.applicationContext)
    private var lastSnapshotAt = 0L

    /** Save this refresh's answers for the next start (at most every [SNAPSHOT_EVERY_MS]). */
    private fun saveSnapshot(status: JSONObject, extras: List<JSONObject?>) {
        val now = System.currentTimeMillis()
        if (now - lastSnapshotAt < SNAPSHOT_EVERY_MS) return
        lastSnapshotAt = now
        val url = baseUrl
        viewModelScope.launch(Dispatchers.IO) { appSnapshot.save(url, status, extras) }
    }

    init {
        // A 401 that means "the session is gone" sends the operator back to login.
        api.onUnauthorized = { expireSession() }
        if (!token.isNullOrBlank()) {
            // Show the last answers right away. `loading` stays true until the live refresh below finishes
            // (or fails), so the money buttons stay off while the numbers on screen are old.
            appSnapshot.restore(baseUrl, REFRESH_PATHS.size)?.let { saved ->
                _state.value = buildState(saved.status, saved.extras, AppUiState()).copy(loading = true)
            }
        }
        loadSession()
        if (!token.isNullOrBlank()) {
            loadChats()
            refreshChats()
            // The report and audit log Settings shows are fetched now too, not when Settings is tapped.
            loadReport()
            loadAudit()
            // Do not wait for /v1/status to learn the venue: put the last run's Markets lists back and
            // refresh them straight away, in parallel with everything else the app loads.
            viewModelScope.launch(Dispatchers.IO) {
                discoverSnapshot.restore()?.let { prefetchDiscover(it) }
            }
        }
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
                refreshChats()
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
        discoverSnapshot.clear()
        appSnapshot.clear()
        _chat.value = ChatUiState()
        _state.value = AppUiState(error = error)
    }

    /** One page of browse results for a venue (public data, read-only). */
    suspend fun discover(venue: String, tab: String, query: String, offset: Int): JSONObject {
        val enc = { value: String -> java.net.URLEncoder.encode(value, "UTF-8") }
        val q = if (query.isBlank()) "" else "&q=" + enc(query)
        return api.get("/v1/discover?venue=" + enc(venue) + "&tab=" + enc(tab) + q + "&limit=30&offset=" + offset)
    }


    /**
     * Fetch the venue's browse tabs and the first page of every tab and sub-category in the background, so
     * the Markets page opens with all of them already there (no spinner on any tab or chip), and save them for
     * the next start. The first [PREFETCH_TABS] tabs go out first and are kept fresh every minute; every other
     * category and every sub-category follows a few at a time and is refreshed every [PREFETCH_DEEP_MS]. Best
     * effort and public data only: a failure leaves that page to fetch for itself, and nothing is kept unless
     * the server answered with tabs.
     */
    private fun prefetchDiscover(venue: String) {
        if (venue !in BROWSABLE_VENUES) return
        val cached = DiscoverTabsCache.get(venue)
        val tabsFresh = cached != null && System.currentTimeMillis() - cached.at < DiscoverTabsCache.FRESH_MS
        if (tabsFresh && cached != null && stalePrefetchPages(venue, cached.tabs).isEmpty()) return
        if (!synchronized(prefetching) { prefetching.add(venue) }) return
        viewModelScope.launch {
            try {
                val tabs = if (tabsFresh && cached != null) cached.tabs else DiscoverTabsCache.store(venue, DiscoverFlights.run("tabs|$venue") { discoverTabs(venue) })
                if (tabs.isEmpty()) return@launch
                // A few pages at a time, so the server is not asked for a hundred at once. The first batch holds the
                // first tabs, and it is saved right away so a start soon after still finds them.
                stalePrefetchPages(venue, tabs).chunked(PREFETCH_PARALLEL).forEachIndexed { index, batch ->
                    coroutineScope {
                        batch.map { id -> async { prefetchDiscoverPage(venue, id) } }.awaitAll()
                    }
                    if (index == 0) saveDiscoverSnapshot(venue)
                }
                saveDiscoverSnapshot(venue)
            } catch (e: kotlinx.coroutines.CancellationException) {
                throw e
            } catch (e: Exception) {
                // The Markets page will ask again when it opens.
            } finally {
                synchronized(prefetching) { prefetching.remove(venue) }
            }
        }
    }

    /**
     * The tab and sub-category ids whose first page is missing or too old, in the order they are fetched: the
     * first [PREFETCH_TABS] tabs, then the other categories, then every sub-category (Crypto -> 1H, ...).
     */
    private fun stalePrefetchPages(venue: String, tabs: List<DiscoverTabsCache.Tab>): List<String> {
        val now = System.currentTimeMillis()
        fun stale(id: String, maxAgeMs: Long): Boolean {
            val page = DiscoverCache.get(DiscoverCache.key(venue, id, ""))
            return page == null || now - page.at >= maxAgeMs
        }
        val first = tabs.take(PREFETCH_TABS).map { it.id }
        val later = tabs.drop(PREFETCH_TABS).map { it.id } + tabs.flatMap { tab -> tab.subtabs.map { sub -> sub.id } }
        return (first.filter { stale(it, PREFETCH_PAGE_MS) } + later.filter { stale(it, PREFETCH_DEEP_MS) })
            .distinct()
            .take(PREFETCH_MAX_PAGES)
    }

    /** One tab's first page into the cache. A failure is ignored: that tab then loads when it is opened. */
    private suspend fun prefetchDiscoverPage(venue: String, tab: String) {
        try {
            val key = DiscoverCache.key(venue, tab, "")
            DiscoverCache.store(venue, tab, "", DiscoverFlights.run("$key|0") { discover(venue, tab, "", 0) })
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            // Loaded when the tab is opened.
        }
    }

    private suspend fun saveDiscoverSnapshot(venue: String) {
        // Signed out meanwhile: the saved lists were just erased, so they are not written back.
        if (token.isNullOrBlank()) return
        withContext(Dispatchers.IO) { discoverSnapshot.save(venue) }
    }

    /** The browse tabs a venue offers. Polymarket's categories are read from Gamma's own tag list. */
    suspend fun discoverTabs(venue: String): JSONObject =
        api.get("/v1/discover/tabs?venue=" + java.net.URLEncoder.encode(venue, "UTF-8"))

    fun notify(message: String) {
        _messages.tryEmit(message)
    }

    /** Turn one refresh's answers into screen state; an answer that is missing keeps what [previous] had. */
    private fun buildState(status: JSONObject, extras: List<JSONObject?>, previous: AppUiState): AppUiState {
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

        val capabilities = status.optJSONObject("capabilities")
        val limits = status.optJSONObject("limits")
        val counts = HashMap<String, Int>()
        status.optJSONObject("counts")?.let { source ->
            source.keys().forEach { key -> counts[key] = source.optInt(key) }
        }
        val balances = if (balance != null) ApiClient.arrayOf(balance, "balances").objects() else previous.balances
        val firstBalance = balances.firstOrNull()

        return AppUiState(
            status = AgentStatus(
                badge = status.optString("badge"),
                venue = status.optString("active_venue"),
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
                val previous = _state.value
                // /v1/status and everything else go out at once: one round trip to a remote server, not two.
                val (status, extras) = coroutineScope {
                    val statusCall = async { api.get("/v1/status") }
                    val extraCalls = REFRESH_PATHS.map { path -> async { runCatching { api.get(path) }.getOrNull() } }
                    statusCall.await() to extraCalls.awaitAll()
                }
                prefetchDiscover(status.optString("active_venue"))

                // The session may have been cleared while we were waiting.
                if (token.isNullOrBlank()) return@launch

                _state.value = buildState(status, extras, previous)
                saveSnapshot(status, extras)
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
     * The reply streams in over /v1/chat/stream: tool progress and text arrive as they happen. Every
     * chat is a server conversation, so the assistant sees that chat's earlier messages (and only
     * that chat's). A chat that has no server copy yet gets one just before its first message.
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
                val serverId = ensureServerChat()
                api.stream("/v1/chat/stream", ApiClient.jsonOf("message" to message, "conversation_id" to serverId)) { event, data ->
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

    /** The open chat's server id; a new or device-only chat is created on the server first and takes its id. */
    private suspend fun ensureServerChat(): String {
        val id = _chat.value.activeId ?: throw IllegalStateException("No chat is open.")
        val current = _chat.value.conversations.firstOrNull { it.id == id }
        if (current?.synced == true) return id
        val created = api.post("/v1/conversations", ApiClient.jsonOf("title" to (current?.title ?: "")))
        val serverId = created.optJSONObject("conversation")?.optString("id").orEmpty()
        if (serverId.isBlank()) throw IllegalStateException("The server did not create the chat.")
        _chat.update { s ->
            s.copy(
                activeId = serverId,
                conversations = s.conversations.map { if (it.id == id) it.copy(id = serverId, synced = true) else it },
            )
        }
        return serverId
    }

    /** Ask the server for this user's chats and merge them into the saved list; offline, the saved list stays. */
    fun refreshChats() {
        viewModelScope.launch {
            val epoch = chatEpoch
            try {
                val rows = api.get("/v1/conversations?limit=$MAX_CHATS")
                // A rename or delete began (or ended) meanwhile: this answer may be stale, and that change refreshes again.
                if (epoch != chatEpoch) return@launch
                val server = ApiClient.arrayOf(rows, "conversations").objects()
                    .map { conversationFromServer(it) }
                    .filter { it.id.isNotBlank() && it.id !in pendingDeletes }
                    .map { chat -> pendingRenames[chat.id]?.let { chat.copy(title = it) } ?: chat }
                _chat.update { s -> s.copy(conversations = mergeChatLists(server, s.conversations, s.activeId).take(MAX_CHATS)) }
                persistChats()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                // The saved list is still usable; the next refresh tries again.
            }
        }
    }

    /** Ids of the chats whose title or message text match [query], as the server sees them; null if it could not be asked. */
    suspend fun searchChats(query: String): List<String>? {
        val needle = query.trim()
        if (needle.isEmpty()) return emptyList()
        return try {
            val rows = api.get("/v1/conversations?limit=$MAX_CHATS&q=" + java.net.URLEncoder.encode(needle, "UTF-8"))
            ApiClient.arrayOf(rows, "conversations").objects().map { it.optString("id") }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            null
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
        _chat.update { it.copy(activeId = null, messages = emptyList(), hasEarlier = false) }
    }

    fun openChat(id: String) {
        if (_chat.value.busy) return
        val chat = _chat.value.conversations.firstOrNull { it.id == id } ?: return
        _chat.update { s -> s.copy(activeId = chat.id, messages = chat.messages, hasEarlier = false) }
        if (chat.synced) loadChatMessages(chat)
    }

    /** Show the stored messages of a server chat; what was cached stays on screen until they arrive. */
    private fun loadChatMessages(chat: Conversation) {
        viewModelScope.launch {
            try {
                val res = api.get("/v1/conversations/${chat.id}/messages?limit=$MAX_CHAT_MESSAGES")
                val stored = ApiClient.arrayOf(res, "messages").objects().map { messageFromServer(it) }
                // Only if the reader is still on this chat and has not started typing to it.
                val more = res.optBoolean("has_more")
                _chat.update { s -> if (s.activeId == chat.id && !s.busy) s.copy(messages = stored, hasEarlier = more) else s }
                saveActiveChat()
            } catch (e: ApiClient.ApiException) {
                if (e.status == 404) {
                    // Deleted from another device: forget it here too.
                    _chat.update { s ->
                        val open = s.activeId == chat.id
                        s.copy(
                            conversations = s.conversations.filterNot { it.id == chat.id },
                            activeId = if (open) null else s.activeId,
                            messages = if (open) emptyList() else s.messages,
                            hasEarlier = if (open) false else s.hasEarlier,
                        )
                    }
                    persistChats()
                    _messages.tryEmit("That chat no longer exists on the server.")
                } else if (chat.messages.isEmpty()) {
                    _messages.tryEmit("Could not load this chat: ${e.message}")
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                if (chat.messages.isEmpty()) _messages.tryEmit("Could not load this chat: ${e.message}")
            }
        }
    }

    fun renameChat(id: String, title: String) {
        val clean = title.trim().take(60)
        if (clean.isEmpty()) return
        val synced = _chat.value.conversations.firstOrNull { it.id == id }?.synced == true
        _chat.update { s -> s.copy(conversations = s.conversations.map { if (it.id == id) it.copy(title = clean) else it }) }
        persistChats()
        if (synced) {
            chatEpoch++
            pendingRenames[id] = clean
            viewModelScope.launch {
                try {
                    api.post("/v1/conversations/$id/rename", ApiClient.jsonOf("title" to clean))
                } catch (e: CancellationException) {
                    throw e
                } catch (e: Exception) {
                    _messages.tryEmit("Could not rename the chat on the server: ${e.message}")
                } finally {
                    if (pendingRenames[id] == clean) pendingRenames.remove(id)
                    chatEpoch++
                    refreshChats() // confirms the new title, or brings the old one back after a failure
                }
            }
        }
    }

    fun deleteChat(id: String) {
        if (_chat.value.busy && _chat.value.activeId == id) return
        val synced = _chat.value.conversations.firstOrNull { it.id == id }?.synced == true
        _chat.update { s ->
            val open = s.activeId == id
            s.copy(
                conversations = s.conversations.filterNot { it.id == id },
                activeId = if (open) null else s.activeId,
                messages = if (open) emptyList() else s.messages,
                hasEarlier = if (open) false else s.hasEarlier,
            )
        }
        persistChats()
        if (synced) {
            chatEpoch++
            pendingDeletes.add(id)
            viewModelScope.launch {
                try {
                    api.delete("/v1/conversations/$id")
                } catch (e: CancellationException) {
                    throw e
                } catch (e: ApiClient.ApiException) {
                    // 404 means it is already gone, which is what was asked for.
                    if (e.status != 404) _messages.tryEmit("Could not delete the chat on the server: ${e.message}")
                } catch (e: Exception) {
                    _messages.tryEmit("Could not delete the chat on the server: ${e.message}")
                } finally {
                    pendingDeletes.remove(id)
                    chatEpoch++
                    refreshChats() // a chat the server could not delete comes back; a deleted one stays gone
                }
            }
        }
    }

    /** Fetch the page of messages before the oldest one shown and put it in front. */
    fun loadEarlier() {
        val s = _chat.value
        val id = s.activeId ?: return
        val oldest = s.messages.firstOrNull { it.seq > 0 }?.seq ?: return
        if (s.busy || !s.hasEarlier || loadingEarlier) return
        loadingEarlier = true
        viewModelScope.launch {
            try {
                val res = api.get("/v1/conversations/$id/messages?before=$oldest&limit=$MAX_CHAT_MESSAGES")
                val older = ApiClient.arrayOf(res, "messages").objects().map { messageFromServer(it) }
                val more = res.optBoolean("has_more")
                _chat.update { st -> if (st.activeId == id) st.copy(messages = older + st.messages, hasEarlier = more) else st }
                saveActiveChat()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _messages.tryEmit("Could not load earlier messages: ${e.message}")
            } finally {
                loadingEarlier = false
            }
        }
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
        refreshChats()
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
        // Server chats can be fetched again, so only the newest few keep a cache; device-only chats keep everything.
        for ((index, chat) in _chat.value.conversations.withIndex()) {
            val messages = JSONArray()
            if (!chat.synced || index < CACHED_CHATS) {
                for (m in chat.messages.takeLast(MAX_CHAT_MESSAGES)) {
                    messages.put(JSONObject().put("role", m.role).put("text", m.text).put("at", m.at))
                }
            }
            chats.put(
                JSONObject().put("id", chat.id).put("title", chat.title).put("updatedAt", chat.updatedAt)
                    .put("synced", chat.synced).put("messages", messages),
            )
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
                    synced = o.optBoolean("synced", false),
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
            _info.value = _info.value.copy(loading = _info.value.report == null, error = null)
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
            _info.value = _info.value.copy(loading = _info.value.audit.isEmpty(), error = null)
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
        private const val MAX_CHATS = 100
        private const val CACHED_CHATS = 20
        private const val MAX_CHAT_MESSAGES = 200
        const val DEFAULT_USERNAME = "operator"
        private const val RENEW_EVERY_MS = 6L * 60 * 60 * 1000
        private const val RENEW_RETRY_MS = 5L * 60 * 1000

        /** The answers one refresh asks for next to /v1/status, in the order [buildState] reads them. */
        private val REFRESH_PATHS = listOf(
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
        )

        /** The saved answers are rewritten at most this often. */
        private const val SNAPSHOT_EVERY_MS = 30_000L

        /** How many of a venue's first tabs are fetched first and kept fresh every [PREFETCH_PAGE_MS]. */
        private const val PREFETCH_TABS = 4

        /** A page of one of the first tabs younger than this is not fetched again by the background refresh (same as the screen's own freshness). */
        private const val PREFETCH_PAGE_MS = 60_000L

        /** Every other category and sub-category is fetched ahead too; these are refreshed this often. A stale one still shows at once and refreshes quietly. */
        private const val PREFETCH_DEEP_MS = 10 * 60_000L

        /** How many pages the background prefetch fetches at the same time. */
        private const val PREFETCH_PARALLEL = 4

        /** The most pages one background run fetches; the cache holds 400. */
        private const val PREFETCH_MAX_PAGES = 250
    }
}
