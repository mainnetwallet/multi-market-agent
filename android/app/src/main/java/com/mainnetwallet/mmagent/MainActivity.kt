package com.mainnetwallet.mmagent

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.browser.customtabs.CustomTabsIntent
import androidx.compose.material3.SnackbarHostState
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import androidx.fragment.app.FragmentActivity
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.mainnetwallet.mmagent.data.ApiClient
import com.mainnetwallet.mmagent.data.AppLock
import com.mainnetwallet.mmagent.data.AppViewModel
import com.mainnetwallet.mmagent.data.LiveSocket
import com.mainnetwallet.mmagent.data.EventTarget
import com.mainnetwallet.mmagent.data.EventViewModel
import com.mainnetwallet.mmagent.data.TradeSheetViewModel
import com.mainnetwallet.mmagent.ui.event.EventScreen
import com.mainnetwallet.mmagent.data.heldShares
import com.mainnetwallet.mmagent.data.WithdrawViewModel
import com.mainnetwallet.mmagent.ui.AllowlistAddScreen
import com.mainnetwallet.mmagent.ui.AppScaffold
import com.mainnetwallet.mmagent.ui.BROWSABLE_VENUES
import com.mainnetwallet.mmagent.ui.ChatScreen
import com.mainnetwallet.mmagent.ui.DepositChoice
import com.mainnetwallet.mmagent.ui.DepositScreen
import com.mainnetwallet.mmagent.ui.HomeScreen
import com.mainnetwallet.mmagent.ui.LockedScreen
import com.mainnetwallet.mmagent.ui.LoginScreen
import com.mainnetwallet.mmagent.ui.MMAgentTheme
import com.mainnetwallet.mmagent.ui.PortfolioScreen
import com.mainnetwallet.mmagent.ui.Tab
import com.mainnetwallet.mmagent.ui.TradeSheetScreen
import com.mainnetwallet.mmagent.ui.VenueSwitcherDialog
import com.mainnetwallet.mmagent.ui.WalletScreen
import com.mainnetwallet.mmagent.ui.WithdrawScreen
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch

/**
 * The native shell.
 *
 * Screenshots are allowed by default. Settings has a "Block screenshots" switch that
 * sets FLAG_SECURE (no screenshots, no screen recording, blank recents thumbnail).
 */
class MainActivity : FragmentActivity() {

    private lateinit var appLock: AppLock

    /** FLAG_SECURE on = no screenshots or screen recording; off = they work normally. */
    private fun applyScreenshotPolicy(block: Boolean) {
        if (block) {
            window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_SECURE)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Draw behind the system bars and let Compose pad for them (the bottom nav sits above the
        // phone's own navigation buttons; the venue badge sits below the status bar).
        enableEdgeToEdge()
        appLock = AppLock(this)
        applyScreenshotPolicy(appLock.blockScreenshots)

        // The token provider reads the saved session on every request. It used to be a
        // hardcoded `null`, so nothing after login was ever authenticated and every
        // screen came back empty (401).
        val prefs = getSharedPreferences("mm_agent_prefs", MODE_PRIVATE)
        val savedUrl = prefs.getString(AppViewModel.KEY_BASE_URL, null)?.takeIf { it.isNotBlank() }
        val api = ApiClient(savedUrl ?: BuildConfig.DEFAULT_BASE_URL) {
            prefs.getString(AppViewModel.KEY_TOKEN, null)
        }
        val appViewModel: AppViewModel by viewModels {
            object : androidx.lifecycle.ViewModelProvider.Factory {
                @Suppress("UNCHECKED_CAST")
                override fun <T : androidx.lifecycle.ViewModel> create(modelClass: Class<T>): T {
                    return AppViewModel(applicationContext, api) as T
                }
            }
        }

        // Live updates: the server pushes `account.updated` the moment the venue's own account changes
        // (including a trade made on the venue's website), and the screens re-read at once. Only while the
        // app is on screen; the timer below is the fallback if the socket is down.
        val live = LiveSocket(
            baseUrl = { appViewModel.baseUrl },
            token = { appViewModel.token },
            onTopic = { runOnUiThread { appViewModel.liveRefresh() } },
        )
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                live.start()
                try {
                    kotlinx.coroutines.awaitCancellation()
                } finally {
                    live.stop()
                }
            }
        }

        // 5 s background refresh, only while the app is on screen. Silent: it never
        // flips the loading flag, and a failed tick keeps the last good data.
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                while (true) {
                    delay(AUTO_REFRESH_MS)
                    if (appViewModel.state.value.loggedIn) {
                        appViewModel.refresh(silent = true)
                        appViewModel.renewSessionIfDue()
                    }
                }
            }
        }

        setContent {
            MMAgentTheme {
                val state by appViewModel.state.collectAsState()
                val chatState by appViewModel.chat.collectAsState()
                val infoState by appViewModel.info.collectAsState()
                var tab by rememberSaveable { mutableStateOf(Tab.HOME) }
                var portfolioTab by rememberSaveable { mutableIntStateOf(0) }
                // The market search lives in the top bar on the Markets tab; the browser below reads it.
                var marketQuery by rememberSaveable { mutableStateOf("") }
                var overlay by remember { mutableStateOf<String?>(null) }
                var showVenues by remember { mutableStateOf(false) }
                var selectedVenue by remember { mutableStateOf("") }
                var depositInfo by remember { mutableStateOf<org.json.JSONObject?>(null) }
                var depositOptions by remember { mutableStateOf<org.json.JSONObject?>(null) }
                var depositChoice by remember { mutableStateOf(DepositChoice()) }
                var depositLoading by remember { mutableStateOf(false) }
                var depositError by remember { mutableStateOf<String?>(null) }
                var locked by remember { mutableStateOf(appLock.isLocked()) }
                val snackbar = remember { SnackbarHostState() }
                val clipboard = LocalClipboardManager.current

                val tradeVm = remember { TradeSheetViewModel(api) }
                // One open event view (`/v1/stream` + REST fallback). The trade sheet opened from it returns to it.
                val eventVm = remember { EventViewModel(api, { appViewModel.baseUrl }, { appViewModel.token }) }
                val eventState by eventVm.state.collectAsState()
                var tradeFromEvent by remember { mutableStateOf(false) }
                val tradeState by tradeVm.state.collectAsState()
                val withdrawVm = remember { WithdrawViewModel(api) }
                val withdrawState by withdrawVm.state.collectAsState()

                // Every message the app shows is the server's own words, in one place.
                // collectLatest: a newer message replaces the one on screen, it never queues.
                LaunchedEffect(Unit) {
                    appViewModel.messages.collectLatest { message -> snackbar.showSnackbar(message) }
                }
                LaunchedEffect(overlay) {
                    if (overlay != "event" && !(overlay == "trade" && tradeFromEvent)) eventVm.close()
                }
                // Signing out or an expired session drops every open screen.
                LaunchedEffect(state.loggedIn) {
                    if (!state.loggedIn) {
                        overlay = null
                        showVenues = false
                        tab = Tab.HOME
                        portfolioTab = 0
                    }
                }

                if (locked) {
                    LockedScreen {
                        if (appLock.available(this@MainActivity)) {
                            appLock.prompt(this@MainActivity) { locked = false }
                        } else {
                            // No biometric enrolled: the lock cannot be opened, so do not trap
                            // the operator behind it.
                            appLock.unlock()
                            locked = false
                        }
                    }
                } else if (!state.loggedIn || appViewModel.baseUrl.isBlank()) {
                    LoginScreen(
                        baseUrl = appViewModel.baseUrl,
                        initialUsername = appViewModel.savedUsername,
                        busy = state.loading,
                        error = state.error,
                        onSaveBaseUrl = { appViewModel.saveBaseUrl(it) },
                        onLogin = { user, pass -> appViewModel.login(user, pass) {} },
                    )
                } else {
                    // Back closes the open screen first, then returns to Home, then leaves the app.
                    BackHandler(enabled = overlay != null || showVenues || tab != Tab.HOME) {
                        when {
                            showVenues -> showVenues = false
                            overlay == "trade" && tradeFromEvent -> {
                                tradeFromEvent = false
                                overlay = "event"
                            }
                            overlay != null -> {
                                if (overlay == "withdraw" || overlay == "allowlist") withdrawVm.reset()
                                overlay = null
                            }
                            else -> tab = Tab.HOME
                        }
                    }

                    LaunchedEffect(state.status.venue) { marketQuery = "" }

                    AppScaffold(
                        state = state,
                        tab = if (overlay == "settings") null else tab,
                        onTab = { selected ->
                            if (overlay == "withdraw" || overlay == "allowlist") withdrawVm.reset()
                            overlay = null
                            tab = selected
                        },
                        onVenueClick = { showVenues = true },
                        onSettings = { overlay = "settings" },
                        snackbar = snackbar,
                        searchQuery = if (overlay == null && tab == Tab.MARKETS && state.status.venue in BROWSABLE_VENUES) marketQuery else null,
                        onSearchQuery = { text ->
                            val pasted = EventTarget.fromPaste(text)
                            if (pasted != null) {
                                marketQuery = ""
                                eventVm.open(pasted)
                                overlay = "event"
                            } else {
                                marketQuery = text
                            }
                        },
                    ) {
                        when (overlay) {
                            "trade" -> TradeSheetScreen(
                                draft = tradeState.draft,
                                estimate = tradeState.estimate,
                                books = tradeState.books,
                                bids = tradeState.bids,
                                asks = tradeState.asks,
                                history = tradeState.history,
                                held = tradeState.held,
                                available = state.balances
                                    .firstOrNull { it.optString("venue") == tradeState.draft.venue }
                                    ?.optString("available")
                                    .orEmpty(),
                                online = state.online,
                                submitting = tradeState.submitting,
                                error = tradeState.error,
                                result = tradeState.result,
                                onSide = { tradeVm.setSide(it) },
                                onOutcome = { tradeVm.switchOutcome(it) },
                                onMarketMode = { tradeVm.setMarketMode(it) },
                                onPostOnly = { tradeVm.setPostOnly(it) },
                                onPrice = { tradeVm.setPrice(it) },
                                onStepPrice = { tradeVm.stepPrice(it) },
                                onShares = { tradeVm.setShares(it) },
                                onAmount = { tradeVm.setAmount(it) },
                                onAddAmount = { tradeVm.addAmount(it) },
                                onPercent = { tradeVm.setPercentOfHeld(it) },
                                onSlippage = { tradeVm.setMaxSlippage(it) },
                                onSubmit = { tradeVm.submit() },
                                onCancel = {
                                    overlay = if (tradeFromEvent) "event" else null
                                    tradeFromEvent = false
                                },
                            )

                            "event" -> EventScreen(
                                state = eventState,
                                onRange = { eventVm.setRange(it) },
                                onBack = { overlay = null },
                                onTrade = { marketId, title, outcome ->
                                    tradeVm.newIntent()
                                    tradeVm.setMarket(marketId, title)
                                    applyCapabilities(tradeVm, state, marketId)
                                    tradeVm.startTicket(outcome)
                                    val ticketVenue = tradeVm.state.value.draft.venue
                                    tradeVm.setHeld(
                                        yes = heldShares(state.positions, ticketVenue, marketId, "YES"),
                                        no = heldShares(state.positions, ticketVenue, marketId, "NO"),
                                    )
                                    tradeVm.loadMarketData(marketId, ticketVenue, outcome)
                                    tradeFromEvent = true
                                    overlay = "trade"
                                },
                            )

                            "deposit" -> DepositScreen(
                                venue = selectedVenue,
                                options = depositOptions,
                                info = depositInfo,
                                loading = depositLoading,
                                error = depositError,
                                choice = depositChoice,
                                onChoose = { picked ->
                                    depositChoice = picked
                                    depositLoading = true
                                    depositError = null
                                    loadDeposit(api, selectedVenue, picked, withOptions = false) { _, info, error ->
                                        // A slower answer for an earlier tap must not replace the pair on screen now.
                                        if (depositChoice == picked) {
                                            // Keep the last good card on screen if the new pair could not be read.
                                            if (info != null) depositInfo = info
                                            depositError = error
                                            depositLoading = false
                                        }
                                    }
                                },
                                onCopy = { address -> clipboard.setText(AnnotatedString(address)) },
                                onBack = { overlay = null },
                            )

                            "allowlist" -> AllowlistAddScreen(
                                error = withdrawState.error,
                                busy = withdrawState.submitting,
                                onAdd = { label, address, network, asset, password ->
                                    withdrawVm.stepUpForAllowlist(password) { token ->
                                        if (token != null) {
                                            withdrawVm.addAllowlistEntry(label, address, network, asset, token)
                                        }
                                    }
                                },
                                onBack = { overlay = null },
                            )

                            "withdraw" -> {
                                val availableToWithdraw = state.balances
                                    .firstOrNull { it.optString("venue") == withdrawState.venue }
                                    ?.optString("available")
                                    .orEmpty()
                                WithdrawScreen(
                                    state = withdrawState,
                                    available = availableToWithdraw,
                                    online = state.online,
                                    onAmountChange = { withdrawVm.setAmount(it) },
                                    onMax = { withdrawVm.setMax(availableToWithdraw) },
                                    onRecipientChange = { withdrawVm.setRecipient(it) },
                                    onSelectChain = { withdrawVm.selectChain(it) },
                                    onSelectToken = { withdrawVm.selectToken(it) },
                                    onUseSaved = { withdrawVm.useSaved(it) },
                                    onSubmit = {
                                        withdrawVm.submit { ok -> if (ok) appViewModel.refresh() }
                                    },
                                    onCancelWithdrawal = { withdrawVm.cancelCreated { appViewModel.refresh() } },
                                    onClose = { overlay = null; withdrawVm.reset() },
                                )
                            }

                            "settings" -> com.mainnetwallet.mmagent.ui.SettingsScreen(
                                state = state,
                                info = infoState,
                                appLockEnabled = appLock.enabled,
                                appLockAvailable = appLock.available(this@MainActivity),
                                appLockTimeoutMinutes = appLock.timeoutMinutes,
                                blockScreenshots = appLock.blockScreenshots,
                                onSetBlockScreenshots = { block ->
                                    appLock.blockScreenshots = block
                                    applyScreenshotPolicy(block)
                                },
                                onLoadReport = { appViewModel.loadReport() },
                                onLoadAudit = { appViewModel.loadAudit() },
                                onRaiseCeiling = { name, value, password, onResult ->
                                    appViewModel.raiseCeiling(name, value, password) { ok, message ->
                                        onResult(ok, message)
                                        if (ok) appViewModel.notify(message)
                                    }
                                },
                                onSetAppLock = { enabled, minutes ->
                                    appLock.enabled = enabled
                                    appLock.timeoutMinutes = minutes
                                },
                                onLogout = { appViewModel.logout() },
                                onOpenVenue = { url -> openInBrowser(url) },
                                onAddAllowlist = {
                                    withdrawVm.reset()
                                    overlay = "allowlist"
                                },
                                onRemoveAllowlist = { id -> withdrawVm.removeAllowlistEntry(id) },
                                onCancelWithdrawal = { id ->
                                    appViewModel.cancelWithdrawal(id) { message -> appViewModel.notify(message) }
                                },
                                onBack = { overlay = null },
                            )

                            else -> when (tab) {
                                Tab.HOME -> HomeScreen(
                                    state = state,
                                    onOpenWallet = { tab = Tab.WALLET },
                                    onPauseToggle = { if (state.status.paused) appViewModel.resume() else appViewModel.pause() },
                                    onCancelAll = { appViewModel.cancelAll { appViewModel.notify(it) } },
                                    onOpenMarkets = { tab = Tab.MARKETS },
                                    onOpenChat = { tab = Tab.CHAT },
                                    onOpenProposals = { portfolioTab = 2; tab = Tab.PORTFOLIO },
                                    onOpenPositions = { portfolioTab = 0; tab = Tab.PORTFOLIO },
                                    onApprove = { id, done -> appViewModel.approveProposal(id) { appViewModel.notify(it); done() } },
                                    onReject = { id, done -> appViewModel.rejectProposal(id) { done() } },
                                )

                                Tab.MARKETS -> com.mainnetwallet.mmagent.ui.MarketsScreen(
                                    markets = state.markets,
                                    activeVenue = state.status.venue,
                                    fetchDiscover = { venue, tabId, query, offset -> appViewModel.discover(venue, tabId, query, offset) },
                                    fetchTabs = { venue -> appViewModel.discoverTabs(venue) },
                                    query = marketQuery,
                                    onQueryChange = { marketQuery = it },
                                    onOpenEvent = { venue, eventId ->
                                        eventVm.open(EventTarget(venue, eventId))
                                        overlay = "event"
                                    },
                                    onOpen = { marketId, title, outcome ->
                                        tradeFromEvent = false
                                        tradeVm.newIntent()
                                        tradeVm.setMarket(marketId, title)
                                        applyCapabilities(tradeVm, state, marketId)
                                        // A fresh one-tap ticket: Buy, market order if the venue has one, nothing typed.
                                        tradeVm.startTicket(outcome)
                                        val ticketVenue = tradeVm.state.value.draft.venue
                                        tradeVm.setHeld(
                                            yes = heldShares(state.positions, ticketVenue, marketId, "YES"),
                                            no = heldShares(state.positions, ticketVenue, marketId, "NO"),
                                        )
                                        tradeVm.loadMarketData(marketId, ticketVenue, outcome)
                                        overlay = "trade"
                                    },
                                )

                                Tab.CHAT -> ChatScreen(
                                    chat = chatState,
                                    onSend = { appViewModel.sendChat(it) },
                                    onStop = { appViewModel.stopChat() },
                                    onNewChat = { appViewModel.newChat() },
                                    onOpenChat = { appViewModel.openChat(it) },
                                    onRenameChat = { id, title -> appViewModel.renameChat(id, title) },
                                    onDeleteChat = { appViewModel.deleteChat(it) },
                                    onSearchChats = { appViewModel.searchChats(it) },
                                    onLoadEarlier = { appViewModel.loadEarlier() },
                                )

                                Tab.PORTFOLIO -> PortfolioScreen(
                                    state = state,
                                    onCancelOrder = { appViewModel.cancelOrder(it) { message -> appViewModel.notify(message) } },
                                    onCancelAll = { appViewModel.cancelAll { appViewModel.notify(it) } },
                                    onSell = { marketId, outcome, shares, venue ->
                                        appViewModel.sellPosition(marketId, outcome, shares, venue) { appViewModel.notify(it) }
                                    },
                                    onRedeem = { marketId, outcome, venue ->
                                        appViewModel.redeemPosition(marketId, outcome, venue) { appViewModel.notify(it) }
                                    },
                                    onAdvanced = { marketId, venue, action ->
                                        appViewModel.splitOrMerge(marketId, venue, action) { appViewModel.notify(it) }
                                    },
                                    onAmendOrder = { orderId, price, shares ->
                                        appViewModel.amendOrder(orderId, price, shares) { appViewModel.notify(it) }
                                    },
                                    onApprove = { id, done -> appViewModel.approveProposal(id) { appViewModel.notify(it); done() } },
                                    onReject = { id, done -> appViewModel.rejectProposal(id) { done() } },
                                    selectedTab = portfolioTab,
                                    onSelectTab = { portfolioTab = it },
                                )

                                Tab.WALLET -> WalletScreen(
                                    balances = state.balances,
                                    capabilities = state.status.capabilities,
                                    onDeposit = { venue ->
                                        selectedVenue = venue
                                        depositInfo = null
                                        depositOptions = null
                                        depositChoice = DepositChoice()
                                        depositError = null
                                        depositLoading = true
                                        overlay = "deposit"
                                        loadDeposit(api, venue, DepositChoice(), withOptions = true) { options, info, error ->
                                            depositOptions = options
                                            depositInfo = info
                                            depositError = error
                                            depositLoading = false
                                        }
                                    },
                                    onWithdraw = { venue ->
                                        withdrawVm.reset()
                                        withdrawVm.setVenue(venue)
                                        withdrawVm.loadOptions()
                                        withdrawVm.loadAllowlist()
                                        overlay = "withdraw"
                                    },
                                    onOpenVenue = { _, url -> openInBrowser(url) },
                                )
                            }
                        }
                    }

                    if (showVenues) {
                        VenueSwitcherDialog(
                            venues = state.status.venues,
                            active = state.status.venue,
                            onPick = { venue ->
                                showVenues = false
                                appViewModel.switchVenue(venue)
                            },
                            onDismiss = { showVenues = false },
                        )
                    }
                }
            }
        }
    }

    /** Fill the trade sheet's chips from the venue's capabilities. */
    private fun applyCapabilities(vm: TradeSheetViewModel, state: com.mainnetwallet.mmagent.data.AppUiState, marketId: String) {
        val capabilities = state.status.capabilities ?: return
        val market = state.markets.firstOrNull { it.optString("native_id") == marketId }
        val venue = market?.optString("venue") ?: state.status.venue
        val venueCaps = capabilities.optJSONObject("venues")?.optJSONObject(venue) ?: return
        val types = venueCaps.optJSONArray("order_types")
        val orderTypes = (0 until (types?.length() ?: 0)).map { types!!.getString(it) }
        vm.setCapabilities(
            venue = venue,
            orderTypes = orderTypes,
            postOnly = venueCaps.optBoolean("post_only"),
            tick = java.math.BigDecimal(venueCaps.optString("tick", "0.01")),
            asset = venueCaps.optString("collateral_asset", "USD"),
        )
    }

    /** Unsupported features open the venue in a Custom Tab, never in a WebView. */
    private fun openInBrowser(url: String) {
        try {
            val intent = CustomTabsIntent.Builder().build().apply { intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK) }
            intent.launchUrl(this, Uri.parse(url))
        } catch (e: Exception) {
            runCatching {
                startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
            }
        }
    }

    /**
     * Read the venue's own deposit information: for Polymarket the bridge's full chain and token list
     * (once, [withOptions]) and the address for the chain and token picked. With nothing picked the server
     * chooses its default (Polygon USDC), never just the first asset the bridge happens to list.
     */
    private fun loadDeposit(
        api: ApiClient,
        venue: String,
        choice: DepositChoice,
        withOptions: Boolean,
        onLoaded: (org.json.JSONObject?, org.json.JSONObject?, String?) -> Unit,
    ) {
        kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.IO).launch {
            val enc = { value: String -> java.net.URLEncoder.encode(value, "UTF-8") }
            val options = if (withOptions) {
                runCatching { api.get("/v1/funds/${enc(venue)}/deposit/options") }.getOrNull()
            } else {
                null
            }
            // First load: start on the chain and token the server marks as its default.
            val chain = choice.chainId.ifBlank { options?.optJSONObject("default")?.optString("chain_id").orEmpty() }
            val token = choice.token.ifBlank { options?.optJSONObject("default")?.optString("token").orEmpty() }
            val query = buildList {
                if (chain.isNotBlank()) add("network=" + enc(chain))
                if (token.isNotBlank()) add("token=" + enc(token))
            }.joinToString("&").let { if (it.isEmpty()) "" else "?$it" }
            try {
                val info = api.get("/v1/funds/${enc(venue)}/deposit$query")
                onLoaded(options, info, null)
            } catch (e: Exception) {
                onLoaded(options, null, e.message)
            }
        }
    }

    companion object {
        private const val AUTO_REFRESH_MS = 5_000L
    }

    override fun onStop() {
        super.onStop()
        // Leaving the app starts the lock timer.
        appLock.onBackground()
    }
}
