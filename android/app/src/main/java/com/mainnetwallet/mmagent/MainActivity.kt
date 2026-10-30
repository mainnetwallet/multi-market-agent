package com.mainnetwallet.mmagent

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
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
import com.mainnetwallet.mmagent.data.TradeSheetViewModel
import com.mainnetwallet.mmagent.data.WithdrawViewModel
import com.mainnetwallet.mmagent.ui.AllowlistAddScreen
import com.mainnetwallet.mmagent.ui.AppScaffold
import com.mainnetwallet.mmagent.ui.ChatScreen
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
import com.mainnetwallet.mmagent.ui.WithdrawReviewScreen
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

        // 15 s background refresh, only while the app is on screen. Silent: it never
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
                var overlay by remember { mutableStateOf<String?>(null) }
                var showVenues by remember { mutableStateOf(false) }
                var selectedVenue by remember { mutableStateOf("") }
                var depositInfo by remember { mutableStateOf<org.json.JSONObject?>(null) }
                var depositError by remember { mutableStateOf<String?>(null) }
                var locked by remember { mutableStateOf(appLock.isLocked()) }
                val snackbar = remember { SnackbarHostState() }
                val clipboard = LocalClipboardManager.current

                val tradeVm = remember { TradeSheetViewModel(api) }
                val tradeState by tradeVm.state.collectAsState()
                val withdrawVm = remember { WithdrawViewModel(api) }
                val withdrawState by withdrawVm.state.collectAsState()

                // Every message the app shows is the server's own words, in one place.
                // collectLatest: a newer message replaces the one on screen, it never queues.
                LaunchedEffect(Unit) {
                    appViewModel.messages.collectLatest { message -> snackbar.showSnackbar(message) }
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
                            overlay != null -> {
                                if (overlay == "withdraw" || overlay == "allowlist") withdrawVm.reset()
                                overlay = null
                            }
                            else -> tab = Tab.HOME
                        }
                    }

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
                    ) {
                        when (overlay) {
                            "trade" -> TradeSheetScreen(
                                draft = tradeState.draft,
                                estimate = tradeState.estimate,
                                bids = tradeState.bids,
                                asks = tradeState.asks,
                                history = tradeState.history,
                                online = state.online,
                                submitting = tradeState.submitting,
                                error = tradeState.error,
                                result = tradeState.result,
                                onSide = { tradeVm.setSide(it) },
                                onOutcome = { tradeVm.setOutcome(it) },
                                onOrderType = { tradeVm.setOrderType(it) },
                                onPostOnly = { tradeVm.setPostOnly(it) },
                                onPrice = { tradeVm.setPrice(it) },
                                onShares = { tradeVm.setShares(it) },
                                onAmount = { tradeVm.setAmount(it) },
                                onSubmit = { tradeVm.submit() },
                                onCancel = { overlay = null },
                            )

                            "deposit" -> DepositScreen(
                                venue = selectedVenue,
                                info = depositInfo,
                                error = depositError,
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

                            "withdraw" -> WithdrawReviewScreen(
                                venue = withdrawState.venue,
                                amount = withdrawState.amount,
                                entries = withdrawState.entries,
                                preview = withdrawState.preview,
                                step = withdrawState.step,
                                error = withdrawState.error,
                                online = state.online,
                                submitting = withdrawState.submitting,
                                onAmountChange = { withdrawVm.setAmount(it) },
                                onSelectAddress = { withdrawVm.setAddress(it) },
                                onPreview = { withdrawVm.preview() },
                                onStepUp = { withdrawVm.stepUp(it) },
                                onConfirm = { withdrawVm.confirm() },
                                onCancel = { overlay = null; withdrawVm.reset() },
                            )

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
                                    onOpen = { marketId, title ->
                                        tradeVm.newIntent()
                                        tradeVm.setMarket(marketId, title)
                                        applyCapabilities(tradeVm, state, marketId)
                                        tradeVm.loadMarketData(marketId, tradeVm.state.value.draft.venue, "YES")
                                        overlay = "trade"
                                    },
                                )

                                Tab.CHAT -> ChatScreen(
                                    chat = chatState,
                                    onSend = { appViewModel.sendChat(it) },
                                    onClear = { appViewModel.clearChat() },
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
                                    allowlist = state.allowlist,
                                    approvals = state.approvals,
                                    fundsHistory = state.fundsHistory,
                                    withdrawals = state.withdrawals,
                                    onDeposit = { venue ->
                                        selectedVenue = venue
                                        depositInfo = null
                                        depositError = null
                                        overlay = "deposit"
                                        loadDepositInfo(api, venue) { info, error ->
                                            depositInfo = info
                                            depositError = error
                                        }
                                    },
                                    onWithdraw = { venue ->
                                        withdrawVm.reset()
                                        withdrawVm.setVenue(venue)
                                        withdrawVm.loadAllowlist()
                                        overlay = "withdraw"
                                    },
                                    onOpenVenue = { _, url -> openInBrowser(url) },
                                    onAddAllowlist = {
                                        withdrawVm.reset()
                                        overlay = "allowlist"
                                    },
                                    onRemoveAllowlist = { id -> withdrawVm.removeAllowlistEntry(id) },
                                    onCancelWithdrawal = { id ->
                                        appViewModel.cancelWithdrawal(id) { message -> appViewModel.notify(message) }
                                    },
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
            mode = venueCaps.optString("mode", "PAPER"),
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

    /** Read the venue's own deposit information: chain, asset, address and minimum. */
    private fun loadDepositInfo(
        api: ApiClient,
        venue: String,
        onLoaded: (org.json.JSONObject?, String?) -> Unit,
    ) {
        kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.IO).launch {
            try {
                val response = api.get("/v1/funds/$venue/deposit")
                onLoaded(response, null)
            } catch (e: ApiClient.ApiException) {
                onLoaded(null, e.message)
            } catch (e: Exception) {
                onLoaded(null, e.message)
            }
        }
    }

    companion object {
        private const val AUTO_REFRESH_MS = 15_000L
    }

    override fun onStop() {
        super.onStop()
        // Leaving the app starts the lock timer.
        appLock.onBackground()
    }
}
