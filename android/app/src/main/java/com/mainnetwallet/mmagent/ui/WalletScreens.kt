package com.mainnetwallet.mmagent.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.mainnetwallet.mmagent.data.AllowlistEntry
import com.mainnetwallet.mmagent.data.Format
import com.mainnetwallet.mmagent.data.WithdrawPreview
import com.mainnetwallet.mmagent.data.WithdrawStep
import com.mainnetwallet.mmagent.data.WithdrawUiState
import com.mainnetwallet.mmagent.data.WithdrawViewModel
import com.mainnetwallet.mmagent.data.objects
import kotlinx.coroutines.delay
import org.json.JSONObject

/**
 * The Wallet screen: per-venue balances with the venue's own collateral asset
 * name, and the deposit and withdraw entries. Recipients, approvals, withdrawals
 * and history are in Settings (see [fundsSections]).
 */
@Composable
fun WalletScreen(
    balances: List<JSONObject>,
    capabilities: JSONObject?,
    onDeposit: (venue: String) -> Unit,
    onWithdraw: (venue: String) -> Unit,
    onOpenVenue: (venue: String, url: String) -> Unit,
) {
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item { SectionHeader("Balances", "Per venue, in the venue's own asset") }
        items(balances) { balance ->
            WalletCard(balance, capabilities, onDeposit, onWithdraw, onOpenVenue)
        }
        if (balances.isEmpty()) {
            item { Text("No venue balances available.", style = MaterialTheme.typography.bodyMedium) }
        }
    }
}

/**
 * The funds housekeeping that no longer sits on the Wallet page: saved recipients, on-chain approvals,
 * withdrawals (with Cancel while one is still in its hold window) and the venues' own deposit and
 * withdrawal history. It is shown in Settings.
 */
fun LazyListScope.fundsSections(
    allowlist: List<JSONObject>,
    approvals: List<JSONObject>,
    withdrawals: List<JSONObject>,
    fundsHistory: List<JSONObject>,
    onOpenUrl: (url: String) -> Unit,
    onAddAllowlist: () -> Unit,
    onRemoveAllowlist: (String) -> Unit,
    onCancelWithdrawal: (String) -> Unit,
) {
    item {
        SectionHeader("Allowlist", "${allowlist.size} recipient(s)")
        Spacer(Modifier.height(6.dp))
        if (allowlist.isEmpty()) {
            Text(
                "No saved recipients. Saved addresses show up as chips on the Withdraw page; a new one waits out a cooling period before it can be used.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        } else {
            allowlist.forEach { entry ->
                RowCard(
                    title = entry.optString("label"),
                    subtitle = "${entry.optString("network")} · ${entry.optString("asset")}",
                    detail = Format.groupAddress(entry.optString("address")),
                    trailing = {
                        Text(
                            "Remove",
                            style = MaterialTheme.typography.labelLarge,
                            color = MaterialTheme.colorScheme.secondary,
                            modifier = Modifier
                                .clip(RoundedCornerShape(8.dp))
                                .clickable { onRemoveAllowlist(entry.optString("id")) }
                                .padding(6.dp),
                        )
                    },
                )
                Spacer(Modifier.height(6.dp))
            }
        }
        Spacer(Modifier.height(8.dp))
        ConfirmButton("Add recipient", enabled = true, onClick = onAddAllowlist)
    }

    item {
        SectionHeader("Approvals")
        Spacer(Modifier.height(6.dp))
        if (approvals.isEmpty()) {
            Text("No approval data available.", style = MaterialTheme.typography.bodyMedium)
        } else {
            approvals.forEach { approval ->
                val venue = approval.optString("venue")
                val needed = approval.optBoolean("needed")
                RowCard(
                    title = venue.replaceFirstChar { it.uppercase() },
                    subtitle = if (needed) {
                        "Needs an on-chain allowance"
                    } else {
                        "No on-chain approval needed"
                    },
                    detail = approval.optString("reason"),
                    trailing = {
                        if (needed) {
                            Text(
                                "Guide",
                                style = MaterialTheme.typography.labelLarge,
                                color = MaterialTheme.colorScheme.secondary,
                                modifier = Modifier
                                    .clip(RoundedCornerShape(8.dp))
                                    .clickable { onOpenUrl(approval.optString("official_url")) }
                                    .padding(6.dp),
                            )
                        }
                    },
                )
                Spacer(Modifier.height(6.dp))
            }
        }
    }

    item {
        SectionHeader("Withdrawals", "${withdrawals.size}")
        Spacer(Modifier.height(6.dp))
        if (withdrawals.isEmpty()) {
            Text("No withdrawals yet.", style = MaterialTheme.typography.bodyMedium)
        } else {
            withdrawals.forEach { withdrawal ->
                val status = withdrawal.optString("status")
                val txHash = withdrawal.optString("tx_hash").takeIf { it.isNotBlank() && it != "null" }
                RowCard(
                    title = "${Format.money(withdrawal.optString("amount_usd"))} · $status",
                    subtitle = "${withdrawal.optString("venue")} · ${withdrawal.optString("network")} · ${withdrawal.optString("asset")}",
                    detail = "recipient ${Format.groupAddress(withdrawal.optString("recipient"))}" +
                        (txHash?.let { "\ntx $it" } ?: ""),
                    trailing = {
                        // Cancellable only while it is still in the hold window.
                        if (status == "PENDING_HOLD") {
                            Text(
                                "Cancel",
                                style = MaterialTheme.typography.labelLarge,
                                color = MaterialTheme.colorScheme.secondary,
                                modifier = Modifier
                                    .clip(RoundedCornerShape(8.dp))
                                    .clickable { onCancelWithdrawal(withdrawal.optString("id")) }
                                    .padding(6.dp),
                            )
                        }
                    },
                )
                Spacer(Modifier.height(6.dp))
            }
        }
    }

    item {
        SectionHeader("Deposit and withdrawal history")
        Spacer(Modifier.height(6.dp))
        if (fundsHistory.isEmpty()) {
            Text("No history reported by the venues.", style = MaterialTheme.typography.bodyMedium)
        } else {
            fundsHistory.forEach { row ->
                RowCard(
                    title = "${row.optString("kind")} " + if (row.optString("amount_usd").isBlank() && row.optString("amount").isNotBlank()) {
                        "${row.optString("amount")} ${row.optString("asset")}"
                    } else {
                        Format.money(row.optString("amount_usd"))
                    },
                    subtitle = "${row.optString("venue")} · ${row.optString("status")}",
                    detail = "${row.optString("type")} · fee ${Format.money(row.optString("fee_usd"))}",
                )
                Spacer(Modifier.height(6.dp))
            }
        }
    }
}

/** The chain and token the person picked on the deposit sheet; empty means "the venue's default". */
data class DepositChoice(val chainId: String = "", val token: String = "")

/**
 * The deposit page, laid out like an exchange deposit screen: pick a network and a coin from lists, then read the
 * QR code and address for that pair, the minimum and what happens next. For Polymarket the lists are every chain and
 * token the bridge accepts (the server reads them from the bridge's own supported-assets list); other venues have
 * one route and show it directly. Nothing about chains or assets is hardcoded here.
 */
@Composable
fun DepositScreen(
    venue: String,
    options: JSONObject?,
    info: JSONObject?,
    loading: Boolean,
    error: String?,
    choice: DepositChoice,
    onChoose: (DepositChoice) -> Unit,
    onCopy: (String) -> Unit,
    onBack: () -> Unit,
) {
    val chains = options?.optJSONArray("chains")?.objects().orEmpty()
    // The pick the person just made wins at once; before any pick, what the server answered for, then its default.
    val activeChainId = choice.chainId.ifBlank { info?.optString("chain_id").orEmpty() }
        .ifBlank { options?.optJSONObject("default")?.optString("chain_id").orEmpty() }
    val activeChain = chains.firstOrNull { it.optString("chain_id") == activeChainId }
    val tokens = activeChain?.optJSONArray("tokens")?.objects().orEmpty()
    val activeToken = choice.token.ifBlank { info?.optString("token_address").orEmpty() }
    val activeTokenObject = tokens.firstOrNull { it.optString("address").equals(activeToken, ignoreCase = true) }

    // Which list is open: "network", "coin" or none.
    var picker by remember { mutableStateOf<String?>(null) }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 16.dp, vertical = 12.dp),
    ) {
        FundsTopBar("Deposit", venue.uppercase(), onBack)
        if (!error.isNullOrBlank()) {
            ErrorNote(error)
            Spacer(Modifier.height(8.dp))
        }

        if (chains.isNotEmpty()) {
            // Network and coin side by side: one row instead of two stacked fields.
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                SelectorField(
                    label = "Network",
                    value = activeChain?.optString("chain_name").orEmpty(),
                    avatarSymbol = activeChain?.optString("chain_name"),
                    placeholder = "Select",
                    compact = true,
                    onClick = { picker = "network" },
                    modifier = Modifier.weight(1f),
                )
                SelectorField(
                    label = "Coin",
                    value = activeTokenObject?.optString("symbol").orEmpty(),
                    avatarSymbol = activeTokenObject?.optString("symbol"),
                    placeholder = "Select",
                    enabled = tokens.isNotEmpty(),
                    compact = true,
                    onClick = { picker = "coin" },
                    modifier = Modifier.weight(1f),
                )
            }
            Spacer(Modifier.height(12.dp))
        }

        when {
            loading -> Box(Modifier.fillMaxWidth().padding(32.dp), contentAlignment = Alignment.Center) {
                CircularProgressIndicator()
            }

            info == null || !info.optBoolean("supported") -> {
                val reason = info?.optString("reason").orEmpty()
                UnsupportedCard(
                    title = if (chains.isEmpty()) "No deposit API" else "Not available",
                    reason = reason.ifBlank { "This venue does not accept deposits through the API." },
                )
            }

            else -> DepositDetails(info, onCopy)
        }
        Spacer(Modifier.height(8.dp))
    }

    when (picker) {
        "network" -> PickerSheet(
            title = "Select network",
            options = chains.map { chain ->
                val symbols = chain.optJSONArray("tokens")?.objects().orEmpty().map { it.optString("symbol") }
                PickerOption(
                    key = chain.optString("chain_id"),
                    title = chain.optString("chain_name"),
                    subtitle = symbols.distinct().joinToString(", ").ifBlank { null },
                )
            },
            selectedKey = activeChainId,
            onSelect = { id ->
                picker = null
                // A new network starts on its first token, in the order the server sends them.
                val first = chains.firstOrNull { it.optString("chain_id") == id }
                    ?.optJSONArray("tokens")?.objects()?.firstOrNull()
                onChoose(DepositChoice(id, first?.optString("address").orEmpty()))
            },
            onDismiss = { picker = null },
        )

        "coin" -> PickerSheet(
            title = "Select coin",
            options = tokens.map { token ->
                PickerOption(
                    key = token.optString("address"),
                    title = token.optString("symbol"),
                    subtitle = shortMiddle(token.optString("address")),
                    symbol = token.optString("symbol"),
                )
            },
            selectedKey = activeTokenObject?.optString("address").orEmpty(),
            onSelect = { address ->
                picker = null
                onChoose(DepositChoice(activeChainId, address))
            },
            onDismiss = { picker = null },
        )

        else -> Unit
    }
}

/**
 * The deposit card for the pair the server answered for, kept to about one screen: QR code beside the "send only"
 * line and the minimum, the address with a Copy button, the warnings in one banner, and the rest (address type,
 * token contract, credited wallet, the three steps) behind one tap.
 */
@Composable
private fun DepositDetails(info: JSONObject, onCopy: (String) -> Unit) {
    val address = info.optString("address")
    val asset = info.optString("asset")
    val network = info.optString("network")
    val tokenAddress = info.optString("token_address")
    var copied by remember(address) { mutableStateOf(false) }
    var expanded by remember { mutableStateOf(false) }
    LaunchedEffect(copied) {
        if (copied) {
            delay(2_000)
            copied = false
        }
    }

    FundsCard {
        Row(verticalAlignment = Alignment.CenterVertically) {
            if (address.isNotBlank()) {
                QrCode(address, Modifier.size(148.dp))
                Spacer(Modifier.width(14.dp))
            }
            Column(Modifier.weight(1f)) {
                Text(
                    "Send only $asset on $network",
                    style = MaterialTheme.typography.titleSmall,
                    fontWeight = FontWeight.Bold,
                )
                Spacer(Modifier.height(6.dp))
                Text(
                    "Minimum ${Format.money(info.optString("min_amount"))}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
        Spacer(Modifier.height(12.dp))
        FieldLabel("Deposit address")
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                Format.groupAddress(address),
                style = MaterialTheme.typography.bodySmall,
                fontFamily = FontFamily.Monospace,
                modifier = Modifier.weight(1f),
            )
            Spacer(Modifier.width(10.dp))
            OutlinedButton(
                onClick = {
                    onCopy(address)
                    copied = true
                },
                enabled = address.isNotBlank(),
                shape = RoundedCornerShape(12.dp),
                contentPadding = PaddingValues(horizontal = 14.dp, vertical = 0.dp),
                modifier = Modifier.height(40.dp),
            ) {
                Text(if (copied) "Copied" else "Copy")
            }
        }
    }

    // Every warning stays visible, in one banner instead of one banner each.
    val warningList = info.optJSONArray("warnings")
    val warnings = warningList?.let { list -> (0 until list.length()).map { list.optString(it) } }
        .orEmpty()
        .filter { it.isNotBlank() }
    if (warnings.isNotEmpty()) {
        Spacer(Modifier.height(10.dp))
        NoticeBanner(warnings.joinToString("\n"))
    }

    Spacer(Modifier.height(10.dp))
    FundsCard {
        Row(
            modifier = Modifier.fillMaxWidth().clickable { expanded = !expanded },
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                "Details and how it works",
                style = MaterialTheme.typography.titleSmall,
                fontWeight = FontWeight.Bold,
                modifier = Modifier.weight(1f),
            )
            Icon(
                Icons.Filled.KeyboardArrowDown,
                contentDescription = if (expanded) "Hide details" else "Show details",
                tint = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.rotate(if (expanded) 180f else 0f),
            )
        }
        if (expanded) {
            Spacer(Modifier.height(10.dp))
            val kind = info.optString("address_type")
            if (kind.isNotBlank()) SummaryRow("Address type", kind.uppercase())
            if (tokenAddress.isNotBlank()) SummaryRow("Token contract", shortMiddle(tokenAddress), mono = true)
            val wallet = info.optString("wallet")
            if (wallet.isNotBlank() && wallet != "null") SummaryRow("Credits wallet", shortMiddle(wallet), mono = true)
            Spacer(Modifier.height(10.dp))
            StepTimeline(
                listOf(
                    "Send $asset on $network to the address above." to StepState.ACTIVE,
                    "The bridge converts it and credits your Polymarket wallet." to StepState.PENDING,
                    "The balance shows in Wallet after it lands, usually within minutes." to StepState.PENDING,
                ),
            )
        }
    }
}

@Composable
private fun WalletCard(
    balance: JSONObject,
    capabilities: JSONObject?,
    onDeposit: (String) -> Unit,
    onWithdraw: (String) -> Unit,
    onOpenVenue: (String, String) -> Unit,
) {
    val venue = balance.optString("venue")
    val venueCaps = capabilities?.optJSONObject("venues")?.optJSONObject(venue)
    val funds = venueCaps?.optJSONObject("funds")
    val asset = balance.optString("collateral_asset", "USD")
    val unavailable = balance.optString("reason").takeIf { it.isNotBlank() && it != "null" }

    Card(
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(16.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(venue.uppercase(), style = MaterialTheme.typography.titleMedium)
                Text(
                    "LIVE",
                    style = MaterialTheme.typography.labelSmall,
                    color = Palette.Danger,
                )
            }
            Spacer(Modifier.height(10.dp))
            ValueRow("Available", Format.withAsset(balance.optString("available"), asset), emphasise = true)
            ValueRow("In orders", Format.withAsset(balance.optString("in_orders"), asset))
            ValueRow("Positions", Format.withAsset(balance.optString("positions_value"), asset))
            ValueRow("Total", Format.withAsset(balance.optString("total"), asset))
            if (!unavailable.isNullOrBlank()) {
                Spacer(Modifier.height(6.dp))
                Text(unavailable, style = MaterialTheme.typography.bodySmall, color = Palette.Alert)
            }

            Spacer(Modifier.height(12.dp))
            val canDeposit = funds?.optBoolean("deposit") == true
            val canWithdraw = funds?.optBoolean("withdraw") == true
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                if (canDeposit) {
                    ConfirmButton("Deposit", enabled = true, onClick = { onDeposit(venue) }, modifier = Modifier.weight(1f))
                }
                if (canWithdraw) {
                    ConfirmButton("Withdraw", enabled = true, onClick = { onWithdraw(venue) }, modifier = Modifier.weight(1f))
                }
            }
            if (!canDeposit && !canWithdraw) {
                val reason = funds?.optString("reason")?.takeIf { it.isNotBlank() && it != "null" }
                    ?: venueCaps?.optJSONObject("unsupported")?.optString("deposit")
                        ?.takeIf { it.isNotBlank() && it != "null" }
                    ?: "This venue has no deposit or withdrawal API."
                Spacer(Modifier.height(10.dp))
                UnsupportedCard(
                    title = "Not available through the API",
                    reason = reason,
                    actionLabel = "Open ${venue.replaceFirstChar { it.uppercase() }} in browser",
                    onAction = {
                        val url = venueCaps?.optString("official_url") ?: "https://docs.kalshi.com"
                        onOpenVenue(venue, url)
                    },
                )
            }
        }
    }
}

/** How long the finger must stay on the withdraw button. */
private const val WITHDRAW_HOLD_MS = 5_000L

/** What the form lets through right now. The form and its pinned button read the same checks. */
private class WithdrawChecks(state: WithdrawUiState, available: String) {
    val parsed = state.amount.toBigDecimalOrNull()
    val balance = available.toBigDecimalOrNull()
    val tooMuch = parsed != null && balance != null && parsed > balance
    val minimum = state.selectedToken?.minUsd?.toBigDecimalOrNull()
    val belowMinimum = parsed != null && minimum != null && parsed < minimum
    val addressInvalid = state.recipient.isNotEmpty() && state.savedId.isBlank() &&
        !WithdrawViewModel.isEvmAddress(state.recipient)
}

/**
 * The withdraw page, laid out like an exchange withdraw screen: receiver address, network and coin from lists,
 * the amount with Max, a summary card with the fee quoted live for exactly that route, and the hold-to-withdraw
 * button pinned at the bottom.
 *
 * Nothing leaves the account when it is confirmed: the server creates the withdrawal in a short hold window
 * that can still be cancelled, and sends it only when the window ends.
 */
@Composable
fun WithdrawScreen(
    state: WithdrawUiState,
    available: String,
    online: Boolean,
    onAmountChange: (String) -> Unit,
    onMax: () -> Unit,
    onRecipientChange: (String) -> Unit,
    onSelectChain: (String) -> Unit,
    onSelectToken: (String) -> Unit,
    onUseSaved: (AllowlistEntry) -> Unit,
    onSubmit: () -> Unit,
    onCancelWithdrawal: () -> Unit,
    onClose: () -> Unit,
) {
    val checks = WithdrawChecks(state, available)
    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .imePadding()
            .padding(horizontal = 16.dp, vertical = 12.dp),
    ) {
        // The back arrow does nothing while a request is in flight, like the Cancel button it replaces.
        FundsTopBar("Withdraw", state.venue.uppercase(), onBack = { if (!state.submitting) onClose() })
        ErrorNote(state.error)
        if (!online) {
            Spacer(Modifier.height(6.dp))
            ErrorNote("The server is unreachable. Withdrawing is off until it answers again.")
        }

        Column(
            modifier = Modifier
                .weight(1f)
                .fillMaxWidth()
                .verticalScroll(rememberScrollState()),
        ) {
            Spacer(Modifier.height(8.dp))
            if (state.step == WithdrawStep.DONE) {
                WithdrawDone(
                    amount = state.amount,
                    preview = state.preview,
                    status = state.status,
                    withdrawalId = state.withdrawalId,
                    submitting = state.submitting,
                    onCancelWithdrawal = onCancelWithdrawal,
                    onClose = onClose,
                )
            } else {
                WithdrawForm(
                    state = state,
                    available = available,
                    checks = checks,
                    onAmountChange = onAmountChange,
                    onMax = onMax,
                    onRecipientChange = onRecipientChange,
                    onSelectChain = onSelectChain,
                    onSelectToken = onSelectToken,
                    onUseSaved = onUseSaved,
                )
            }
            Spacer(Modifier.height(16.dp))
        }

        if (state.step != WithdrawStep.DONE) {
            val preview = state.preview
            Spacer(Modifier.height(8.dp))
            HoldToConfirmButton(
                label = "Hold 5 seconds to withdraw",
                enabled = online && !state.submitting && state.quoteReady && !checks.tooMuch && !checks.belowMinimum,
                busy = state.submitting,
                danger = true,
                holdMillis = WITHDRAW_HOLD_MS,
                onConfirmed = onSubmit,
            )
            Spacer(Modifier.height(6.dp))
            Text(
                if (preview.holdSeconds > 0) {
                    "Keep your finger down for 5 seconds. The withdrawal then waits ${preview.holdSeconds} sec before it is sent, and you can cancel it until then."
                } else {
                    "Keep your finger down for 5 seconds. The withdrawal then waits briefly before it is sent, and you can cancel it until then."
                },
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
                modifier = Modifier.fillMaxWidth(),
            )
        }
    }
}

@Composable
private fun WithdrawForm(
    state: WithdrawUiState,
    available: String,
    checks: WithdrawChecks,
    onAmountChange: (String) -> Unit,
    onMax: () -> Unit,
    onRecipientChange: (String) -> Unit,
    onSelectChain: (String) -> Unit,
    onSelectToken: (String) -> Unit,
    onUseSaved: (AllowlistEntry) -> Unit,
) {
    val preview = state.preview
    // Which list is open: "network", "coin" or none.
    var picker by remember { mutableStateOf<String?>(null) }

    // 1. Receiver address
    FieldLabel("Receiver address")
    OutlinedTextField(
        value = state.recipient,
        onValueChange = onRecipientChange,
        placeholder = { Text("Paste the 0x… address") },
        singleLine = true,
        enabled = !state.submitting,
        isError = checks.addressInvalid,
        shape = RoundedCornerShape(14.dp),
        supportingText = {
            Text(
                if (checks.addressInvalid) {
                    "Not a valid EVM address: 0x followed by 40 hex characters."
                } else {
                    "The address that receives the money, on the network picked below."
                },
            )
        },
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Ascii),
        textStyle = MaterialTheme.typography.bodyMedium.copy(fontFamily = FontFamily.Monospace),
        modifier = Modifier.fillMaxWidth(),
    )
    if (state.entries.isNotEmpty()) {
        Spacer(Modifier.height(6.dp))
        FieldLabel("Saved addresses")
        LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            items(state.entries, key = { it.id }) { entry ->
                ChoiceChip(
                    label = if (entry.isUsable()) entry.label else "${entry.label} · waiting",
                    selected = entry.id == state.savedId,
                    enabled = entry.isUsable() && !state.submitting,
                    onClick = { onUseSaved(entry) },
                )
            }
        }
    }
    if (state.requireAllowlist) {
        Spacer(Modifier.height(6.dp))
        Text(
            "This server only sends to saved addresses. Pick one above.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }

    // 2. Network and 3. coin, from the bridge's own list
    Spacer(Modifier.height(14.dp))
    when {
        state.optionsLoading -> Box(Modifier.fillMaxWidth().padding(16.dp), contentAlignment = Alignment.Center) {
            CircularProgressIndicator()
        }

        state.chains.isEmpty() -> UnsupportedCard(
            title = "No chains available",
            reason = state.optionsReason ?: "The bridge lists no chain this app can withdraw to.",
        )

        else -> {
            SelectorField(
                label = "Network",
                value = state.selectedChain?.name.orEmpty(),
                avatarSymbol = state.selectedChain?.name,
                placeholder = "Select network",
                enabled = !state.submitting,
                onClick = { picker = "network" },
            )
            Spacer(Modifier.height(14.dp))
            SelectorField(
                label = "Coin",
                value = state.selectedToken?.symbol.orEmpty(),
                avatarSymbol = state.selectedToken?.symbol,
                hint = state.selectedToken?.minUsd?.takeIf { it.isNotBlank() }?.let { "Minimum ${Format.money(it)}" },
                placeholder = "Select coin",
                enabled = !state.submitting && state.selectedChain?.tokens.orEmpty().isNotEmpty(),
                onClick = { picker = "coin" },
            )
        }
    }

    // 4. Amount
    Spacer(Modifier.height(14.dp))
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        FieldLabel("Amount (USD)")
        Text(
            "Available ${if (checks.balance != null) Format.money(available) else "—"}",
            style = MaterialTheme.typography.labelLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(bottom = 6.dp),
        )
    }
    OutlinedTextField(
        value = state.amount,
        onValueChange = onAmountChange,
        placeholder = { Text("0.00") },
        singleLine = true,
        enabled = !state.submitting,
        isError = checks.tooMuch || checks.belowMinimum,
        shape = RoundedCornerShape(14.dp),
        textStyle = MaterialTheme.typography.titleLarge,
        supportingText = {
            if (checks.tooMuch) {
                Text("More than the available balance.")
            } else if (checks.belowMinimum) {
                Text("The minimum for ${state.token} on ${state.selectedChain?.name.orEmpty()} is ${Format.money(state.selectedToken?.minUsd.orEmpty())}.")
            }
        },
        trailingIcon = {
            TextButton(
                onClick = onMax,
                enabled = !state.submitting && checks.balance != null && checks.balance.signum() > 0,
            ) { Text("Max", fontWeight = FontWeight.Bold) }
        },
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
        modifier = Modifier.fillMaxWidth(),
    )

    // 5. The fee for exactly this route, quoted again whenever any input changes.
    Spacer(Modifier.height(14.dp))
    FundsCard {
        when {
            state.quoting -> Row(verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp)
                Spacer(Modifier.width(10.dp))
                Text("Getting the fee…", style = MaterialTheme.typography.bodyMedium)
            }

            state.quoteError != null -> Text(
                state.quoteError.orEmpty(),
                style = MaterialTheme.typography.bodyMedium,
                color = Palette.Alert,
            )

            state.quoteReady -> {
                SummaryRow("Route", "${preview.asset} on ${preview.network}")
                SummaryRow("Network fee", Format.money(preview.feeUsd))
                if ((preview.receiveUsd.toBigDecimalOrNull()?.signum() ?: 0) > 0) {
                    SummaryRow("You receive about", Format.money(preview.receiveUsd))
                }
                if (preview.etaSeconds > 0) {
                    SummaryRow("Arrives in about", "${maxOf(1, preview.etaSeconds / 60)} min")
                }
                SummaryRow("Hold before sending", "${preview.holdSeconds} sec")
                SummaryRow("Daily limit left", Format.money(preview.dailyRemainingUsd))
            }

            else -> Text(
                "Enter the address, network, coin and amount to see the fee.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
    if (state.quoteReady) {
        Spacer(Modifier.height(12.dp))
        NoticeBanner("Check the address against your own record. A transfer to a wrong address cannot be undone.")
    }

    when (picker) {
        "network" -> PickerSheet(
            title = "Select network",
            options = state.chains.map { chain ->
                PickerOption(
                    key = chain.id,
                    title = chain.name,
                    subtitle = chain.tokens.map { it.symbol }.distinct().joinToString(", ").ifBlank { null },
                )
            },
            selectedKey = state.chainId,
            onSelect = { id ->
                picker = null
                onSelectChain(id)
            },
            onDismiss = { picker = null },
        )

        "coin" -> PickerSheet(
            title = "Select coin",
            options = state.selectedChain?.tokens.orEmpty().map { token ->
                PickerOption(
                    key = token.symbol,
                    title = token.symbol,
                    subtitle = token.minUsd?.takeIf { it.isNotBlank() }?.let { "Minimum ${Format.money(it)}" },
                    symbol = token.symbol,
                )
            },
            selectedKey = state.selectedToken?.symbol.orEmpty(),
            onSelect = { symbol ->
                picker = null
                onSelectToken(symbol)
            },
            onDismiss = { picker = null },
        )

        else -> Unit
    }
}

@Composable
private fun WithdrawDone(
    amount: String,
    preview: WithdrawPreview,
    status: String?,
    withdrawalId: String?,
    submitting: Boolean,
    onCancelWithdrawal: () -> Unit,
    onClose: () -> Unit,
) {
    val current = status.orEmpty().ifBlank { "PENDING_HOLD" }
    val cancelled = current == "CANCELLED"

    Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally) {
        Spacer(Modifier.height(8.dp))
        Box(
            modifier = Modifier
                .size(64.dp)
                .clip(CircleShape)
                .background(if (cancelled) MaterialTheme.colorScheme.surfaceVariant else MaterialTheme.colorScheme.primary),
            contentAlignment = Alignment.Center,
        ) {
            Icon(
                if (cancelled) Icons.Filled.Close else Icons.Filled.Check,
                contentDescription = if (cancelled) "Cancelled" else "Created",
                tint = if (cancelled) MaterialTheme.colorScheme.onSurfaceVariant else Color.White,
                modifier = Modifier.size(34.dp),
            )
        }
        Spacer(Modifier.height(12.dp))
        Text(
            if (cancelled) "Withdrawal cancelled" else "Withdrawal created",
            style = MaterialTheme.typography.titleLarge,
            fontWeight = FontWeight.Bold,
        )
        Spacer(Modifier.height(4.dp))
        Text(
            Format.money(amount),
            style = MaterialTheme.typography.headlineSmall,
        )
    }
    Spacer(Modifier.height(18.dp))

    FundsCard {
        SummaryRow("Status", current)
        SummaryRow("To", preview.label)
        if (!withdrawalId.isNullOrBlank()) SummaryRow("Reference", shortMiddle(withdrawalId), mono = true)
    }
    Spacer(Modifier.height(12.dp))

    if (cancelled) {
        Text(
            "Nothing was sent.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    } else {
        FundsCard {
            StepTimeline(
                listOf(
                    "Withdrawal created" to StepState.DONE,
                    "Hold: ${preview.holdSeconds} sec, you can still cancel" to StepState.ACTIVE,
                    "Sent to the bridge" to StepState.PENDING,
                    "Delivered to the receiver" to StepState.PENDING,
                ),
            )
            Spacer(Modifier.height(10.dp))
            Text(
                "Nothing is sent yet. Follow it under Withdrawals in Wallet.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
    Spacer(Modifier.height(12.dp))
    if (current == "PENDING_HOLD") {
        OutlinedButton(
            onClick = onCancelWithdrawal,
            enabled = !submitting,
            modifier = Modifier.fillMaxWidth().height(52.dp),
            shape = RoundedCornerShape(14.dp),
        ) {
            Text(if (submitting) "Cancelling…" else "Cancel withdrawal")
        }
        Spacer(Modifier.height(8.dp))
    }
    ConfirmButton("Done", enabled = true, onClick = onClose)
}
