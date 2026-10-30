package com.mainnetwallet.mmagent

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import androidx.browser.customtabs.CustomTabsIntent
import com.mainnetwallet.mmagent.data.ApiClient
import com.mainnetwallet.mmagent.data.AppViewModel
import com.mainnetwallet.mmagent.data.TradeSheetViewModel
import com.mainnetwallet.mmagent.data.WithdrawViewModel
import com.mainnetwallet.mmagent.ui.AppScaffold
import com.mainnetwallet.mmagent.ui.AllowlistAddScreen
import com.mainnetwallet.mmagent.ui.DepositScreen
import com.mainnetwallet.mmagent.ui.HomeScreen
import com.mainnetwallet.mmagent.ui.LoginScreen
import com.mainnetwallet.mmagent.ui.MMAgentTheme
import com.mainnetwallet.mmagent.ui.PortfolioScreen
import com.mainnetwallet.mmagent.ui.Tab
import com.mainnetwallet.mmagent.ui.TradeSheetScreen
import com.mainnetwallet.mmagent.ui.WalletScreen
import com.mainnetwallet.mmagent.ui.WithdrawReviewScreen
import kotlinx.coroutines.launch

/**
 * The native shell.
 *
 * FLAG_SECURE stays on: the copy button covers addresses, and a screenshot of a
 * wallet should not be a screenshot of a wallet.
 */
class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.setFlags(WindowManager.LayoutParams.FLAG_SECURE, WindowManager.LayoutParams.FLAG_SECURE)

        val api = ApiClient(BuildConfig.DEFAULT_BASE_URL ?: "") { null }
        val appViewModel: AppViewModel by viewModels {
            object : androidx.lifecycle.ViewModelProvider.Factory {
                @Suppress("UNCHECKED_CAST")
                override fun <T : androidx.lifecycle.ViewModel> create(modelClass: Class<T>): T {
                    return AppViewModel(applicationContext, api) as T
                }
            }
        }

        setContent {
            MMAgentTheme {
                val state by appViewModel.state.collectAsState()
                var tab by remember { mutableStateOf(Tab.HOME) }
                var overlay by remember { mutableStateOf<String?>(null) }
                var selectedVenue by remember { mutableStateOf("") }
                var depositInfo by remember { mutableStateOf<org.json.JSONObject?>(null) }
                var depositError by remember { mutableStateOf<String?>(null) }
                val clipboard = LocalClipboardManager.current

                val tradeVm = remember { TradeSheetViewModel(api) }
                val tradeState by tradeVm.state.collectAsState()
                val withdrawVm = remember { WithdrawViewModel(api) }
                val withdrawState by withdrawVm.state.collectAsState()

                if (!state.loggedIn || appViewModel.baseUrl.isBlank()) {
                    LoginScreen(
                        baseUrl = appViewModel.baseUrl.ifBlank { BuildConfig.DEFAULT_BASE_URL ?: "" },
                        busy = state.loading,
                        error = state.error,
                        onSaveBaseUrl = { appViewModel.saveBaseUrl(it) },
                        onLogin = { user, pass -> appViewModel.login(user, pass) {} },
                    )
                } else {
                    AppScaffold(
                    state = state,
                    tab = tab,
                    onTab = { tab = it },
                    onRefresh = { appViewModel.refresh() },
                ) {
                    when (overlay) {
                        "trade" -> TradeSheetScreen(
                            draft = tradeState.draft,
                            estimate = tradeState.estimate,
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
                            onBack = { overlay = null; withdrawState.error?.let { } },
                        )

                        "withdraw" -> WithdrawReviewScreen(
                            venue = withdrawState.venue,
                            amount = withdrawState.amount,
                            entries = withdrawState.entries,
                            preview = withdrawState.preview,
                            step = withdrawState.step,
                            error = withdrawState.error,
                            submitting = withdrawState.submitting,
                            onAmountChange = { withdrawVm.setAmount(it) },
                            onSelectAddress = { withdrawVm.setAddress(it) },
                            onPreview = { withdrawVm.preview() },
                            onStepUp = { withdrawVm.stepUp(it) },
                            onConfirm = { withdrawVm.confirm() },
                            onCancel = { overlay = null; withdrawVm.reset() },
                        )

                        else -> when (tab) {
                            Tab.HOME -> HomeScreen(
                                state = state,
                                onOpenWallet = { tab = Tab.WALLET },
                                onPauseToggle = { if (state.status.paused) appViewModel.resume() else appViewModel.pause() },
                                onCancelAll = { appViewModel.cancelAll() },
                                onOpenProposals = { tab = Tab.PORTFOLIO },
                            )

                            Tab.MARKETS -> com.mainnetwallet.mmagent.ui.MarketsScreen(
                                markets = state.markets,
                                onOpen = { marketId, title ->
                                    tradeVm.newIntent()
                                    tradeVm.setMarket(marketId, title)
                                    applyCapabilities(tradeVm, state, marketId)
                                    overlay = "trade"
                                },
                            )

                            Tab.PORTFOLIO -> PortfolioScreen(
                                state = state,
                                onCancelOrder = { appViewModel.cancelOrder(it) },
                                onCancelAll = { appViewModel.cancelAll() },
                                onSell = { _, _ -> },
                                onRedeem = { _, _ -> },
                                onAdvanced = { _, _ -> },
                            )

                            Tab.WALLET -> WalletScreen(
                                balances = state.balances,
                                capabilities = state.status.capabilities,
                                allowlist = state.allowlist,
                                approvals = state.approvals,
                                fundsHistory = state.fundsHistory,
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
                            )

                            Tab.SETTINGS -> com.mainnetwallet.mmagent.ui.SettingsScreen(
                                state = state,
                                onRaiseCeiling = { name, value, stepup -> raiseCeiling(api, name, value, stepup) },
                                onLogout = { appViewModel.logout() },
                                onOpenVenue = { url -> openInBrowser(url) },
                            )
                        }
                    }
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

    /** Raising a hard ceiling needs a step-up token; the server enforces it. */
    private fun raiseCeiling(
        api: ApiClient,
        name: String,
        value: String,
        stepupToken: String?,
    ) {
        kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.IO).launch {
            runCatching {
                api.post(
                    "/v1/limits/hard-ceiling",
                    ApiClient.jsonOf("name" to name, "value" to value),
                    stepupToken = stepupToken,
                )
            }
        }
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
}
