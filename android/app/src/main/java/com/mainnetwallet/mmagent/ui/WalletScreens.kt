package com.mainnetwallet.mmagent.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.mainnetwallet.mmagent.data.AllowlistEntry
import com.mainnetwallet.mmagent.data.Format
import com.mainnetwallet.mmagent.data.WithdrawPreview
import com.mainnetwallet.mmagent.data.WithdrawStep
import com.mainnetwallet.mmagent.data.objects
import kotlinx.coroutines.delay
import org.json.JSONObject

/**
 * The Wallet screen: per-venue balances with the venue's own collateral asset
 * name, deposit and withdraw entries, the allowlist, approvals and history.
 */
@Composable
fun WalletScreen(
    balances: List<JSONObject>,
    capabilities: JSONObject?,
    allowlist: List<JSONObject>,
    approvals: List<JSONObject>,
    fundsHistory: List<JSONObject>,
    withdrawals: List<JSONObject>,
    onDeposit: (venue: String) -> Unit,
    onWithdraw: (venue: String) -> Unit,
    onOpenVenue: (venue: String, url: String) -> Unit,
    onAddAllowlist: () -> Unit,
    onRemoveAllowlist: (String) -> Unit,
    onCancelWithdrawal: (String) -> Unit,
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

        item {
            SectionHeader("Allowlist", "${allowlist.size} recipient(s)")
            Spacer(Modifier.height(6.dp))
            if (allowlist.isEmpty()) {
                Text(
                    "No withdrawal recipients yet. Add one before withdrawing; a new address waits out a cooling period.",
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
                                        .clickable { onOpenVenue(venue, approval.optString("official_url")) }
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
}

/** The chain and token the person picked on the deposit sheet; empty means "the venue's default". */
data class DepositChoice(val chainId: String = "", val token: String = "")

/**
 * The deposit sheet. For Polymarket it lists every chain and token the bridge accepts (the server reads them
 * from the bridge's own supported-assets list) and shows the address for the pair picked; other venues have one
 * route and show it directly. Nothing about chains or assets is hardcoded here.
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
    // The chip the person just tapped wins at once; before any tap, what the server answered for, then its default.
    val activeChainId = choice.chainId.ifBlank { info?.optString("chain_id").orEmpty() }
        .ifBlank { options?.optJSONObject("default")?.optString("chain_id").orEmpty() }
    val activeChain = chains.firstOrNull { it.optString("chain_id") == activeChainId }
    val tokens = activeChain?.optJSONArray("tokens")?.objects().orEmpty()
    val activeToken = choice.token.ifBlank { info?.optString("token_address").orEmpty() }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
    ) {
        SectionHeader("Deposit", venue.uppercase())
        ErrorNote(error)
        Spacer(Modifier.height(10.dp))

        if (chains.isNotEmpty()) {
            Text("Network", style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(6.dp))
            LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                items(chains, key = { it.optString("chain_id") }) { chain ->
                    val id = chain.optString("chain_id")
                    ChoiceChip(
                        label = chain.optString("chain_name"),
                        selected = id == activeChainId,
                        onClick = {
                            // A new network starts on its first token, in the order the server sends them.
                            val first = chain.optJSONArray("tokens")?.objects()?.firstOrNull()
                            onChoose(DepositChoice(id, first?.optString("address").orEmpty()))
                        },
                    )
                }
            }
            if (tokens.isNotEmpty()) {
                Spacer(Modifier.height(14.dp))
                Text("Asset", style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.height(6.dp))
                val duplicated = tokens.groupingBy { it.optString("symbol") }.eachCount().filterValues { it > 1 }.keys
                LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(tokens, key = { it.optString("address") + it.optString("symbol") }) { token ->
                        val symbol = token.optString("symbol")
                        val address = token.optString("address")
                        ChoiceChip(
                            label = if (symbol in duplicated) "$symbol ${shortMiddle(address)}" else symbol,
                            selected = address.equals(activeToken, ignoreCase = true),
                            onClick = { onChoose(DepositChoice(activeChainId, address)) },
                        )
                    }
                }
            }
            Spacer(Modifier.height(14.dp))
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
    }
}

/** The address card, the three steps and the warnings for the pair the server answered for. */
@Composable
private fun DepositDetails(info: JSONObject, onCopy: (String) -> Unit) {
    val address = info.optString("address")
    val asset = info.optString("asset")
    val network = info.optString("network")
    val tokenAddress = info.optString("token_address")
    var copied by remember(address) { mutableStateOf(false) }
    LaunchedEffect(copied) {
        if (copied) {
            delay(2_000)
            copied = false
        }
    }

    Card(
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(16.dp)) {
            Text(
                "Send $asset on $network",
                style = MaterialTheme.typography.titleMedium,
            )
            Spacer(Modifier.height(10.dp))
            ValueRow("Minimum", Format.money(info.optString("min_amount")), emphasise = true)
            val kind = info.optString("address_type")
            if (kind.isNotBlank()) ValueRow("Address type", kind.uppercase())
            if (tokenAddress.isNotBlank()) ValueRow("Token contract", shortMiddle(tokenAddress))
            val wallet = info.optString("wallet")
            if (wallet.isNotBlank() && wallet != "null") ValueRow("Credits wallet", shortMiddle(wallet))
            Spacer(Modifier.height(12.dp))
            Text(
                "Send to",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(4.dp))
            Text(
                Format.groupAddress(address),
                style = MaterialTheme.typography.bodyMedium,
                fontFamily = FontFamily.Monospace,
            )
            Spacer(Modifier.height(12.dp))
            ConfirmButton(
                if (copied) "Copied" else "Copy address",
                enabled = address.isNotBlank(),
                onClick = {
                    onCopy(address)
                    copied = true
                },
            )
        }
    }

    Spacer(Modifier.height(12.dp))
    Card(
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(16.dp)) {
            Text("How it works", style = MaterialTheme.typography.titleSmall)
            Spacer(Modifier.height(6.dp))
            listOf(
                "1  Send $asset from $network to the address above.",
                "2  The bridge converts it and credits your Polymarket wallet.",
                "3  The balance shows in Wallet after it lands, usually within minutes.",
            ).forEach { step ->
                Text(step, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.height(2.dp))
            }
        }
    }

    val warnings = info.optJSONArray("warnings")
    if (warnings != null && warnings.length() > 0) {
        Spacer(Modifier.height(12.dp))
        Card(
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Column(Modifier.padding(16.dp)) {
                Text("Before you send", style = MaterialTheme.typography.titleSmall)
                Spacer(Modifier.height(6.dp))
                for (index in 0 until warnings.length()) {
                    Text(
                        "• ${warnings.optString(index)}",
                        style = MaterialTheme.typography.bodySmall,
                        color = Palette.Alert,
                    )
                    Spacer(Modifier.height(2.dp))
                }
            }
        }
    }
}

/** "0xA0b8…eB48": enough to tell two contracts apart without filling the row. */
private fun shortMiddle(value: String): String =
    if (value.length <= 14) value else value.take(6) + "…" + value.takeLast(4)

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

/**
 * The withdraw flow, three steps: amount and recipient, review with the password, then hold to confirm.
 *
 * Nothing leaves the account when it is confirmed: the server creates the withdrawal in a hold window
 * that can still be cancelled, and sends it only when the window ends.
 */
@Composable
fun WithdrawReviewScreen(
    venue: String,
    amount: String,
    available: String,
    selectedId: String,
    entries: List<AllowlistEntry>,
    preview: WithdrawPreview,
    step: WithdrawStep,
    error: String?,
    online: Boolean,
    submitting: Boolean,
    status: String?,
    withdrawalId: String?,
    onAmountChange: (String) -> Unit,
    onMax: () -> Unit,
    onSelectAddress: (AllowlistEntry) -> Unit,
    onPreview: () -> Unit,
    onStepUp: (String) -> Unit,
    onConfirm: () -> Unit,
    onBack: () -> Unit,
    onCancelWithdrawal: () -> Unit,
    onClose: () -> Unit,
) {
    var password by remember { mutableStateOf("") }
    // The password lives only in this field. It is cleared once the step-up has been asked for.
    LaunchedEffect(step) {
        if (step != WithdrawStep.PREVIEW) password = ""
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .padding(horizontal = 16.dp, vertical = 12.dp),
    ) {
        SectionHeader("Withdraw", venue.uppercase())
        if (step != WithdrawStep.DONE) {
            WithdrawProgress(
                when (step) {
                    WithdrawStep.AMOUNT -> 0
                    WithdrawStep.PREVIEW -> 1
                    else -> 2
                },
            )
            Spacer(Modifier.height(10.dp))
        }
        ErrorNote(error)
        if (!online) {
            Spacer(Modifier.height(6.dp))
            ErrorNote("The server is unreachable. Withdrawing is off until it answers again.")
        }

        Column(
            modifier = Modifier
                .weight(1f)
                .fillMaxWidth()
                .imePadding()
                .verticalScroll(rememberScrollState()),
        ) {
            Spacer(Modifier.height(8.dp))
            when (step) {
                WithdrawStep.AMOUNT -> WithdrawAmountStep(
                    amount = amount,
                    available = available,
                    selectedId = selectedId,
                    entries = entries,
                    online = online,
                    submitting = submitting,
                    onAmountChange = onAmountChange,
                    onMax = onMax,
                    onSelectAddress = onSelectAddress,
                    onPreview = onPreview,
                    onClose = onClose,
                )

                WithdrawStep.PREVIEW -> {
                    WithdrawSummary(venue, amount, preview, full = true)
                    Spacer(Modifier.height(14.dp))
                    OutlinedTextField(
                        value = password,
                        onValueChange = { password = it },
                        label = { Text("Your password") },
                        supportingText = { Text("Asked again for every withdrawal. Never stored.") },
                        singleLine = true,
                        visualTransformation = PasswordVisualTransformation(),
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Spacer(Modifier.height(8.dp))
                    ConfirmButton(
                        label = "Continue",
                        enabled = password.isNotBlank() && online,
                        busy = submitting,
                        onClick = { onStepUp(password) },
                    )
                    TextButton(onClick = onBack, enabled = !submitting, modifier = Modifier.fillMaxWidth()) {
                        Text("Back")
                    }
                }

                WithdrawStep.STEP_UP, WithdrawStep.CONFIRMING -> {
                    WithdrawSummary(venue, amount, preview, full = false)
                    Spacer(Modifier.height(16.dp))
                    HoldToConfirmButton(
                        label = "Hold to withdraw",
                        enabled = !submitting && online,
                        busy = submitting,
                        danger = true,
                        onConfirmed = onConfirm,
                    )
                    Spacer(Modifier.height(8.dp))
                    Text(
                        "This creates the withdrawal. It waits ${preview.holdMinutes} min before it is sent, " +
                            "and you can cancel it until then.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    TextButton(onClick = onBack, enabled = !submitting, modifier = Modifier.fillMaxWidth()) {
                        Text("Back")
                    }
                }

                WithdrawStep.DONE -> WithdrawDone(
                    amount = amount,
                    preview = preview,
                    status = status,
                    withdrawalId = withdrawalId,
                    submitting = submitting,
                    onCancelWithdrawal = onCancelWithdrawal,
                    onClose = onClose,
                )
            }
            Spacer(Modifier.height(16.dp))
        }
    }
}

/** Three labelled stages; the ones reached so far are tinted. */
@Composable
private fun WithdrawProgress(current: Int) {
    val labels = listOf("1  Amount", "2  Review", "3  Confirm")
    Row(horizontalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.fillMaxWidth()) {
        labels.forEachIndexed { index, label ->
            val reached = index <= current
            Text(
                label,
                style = MaterialTheme.typography.labelMedium,
                textAlign = TextAlign.Center,
                color = if (reached) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier
                    .weight(1f)
                    .clip(RoundedCornerShape(10.dp))
                    .background(
                        if (reached) {
                            MaterialTheme.colorScheme.primary.copy(alpha = 0.12f)
                        } else {
                            MaterialTheme.colorScheme.surface
                        },
                    )
                    .padding(vertical = 8.dp),
            )
        }
    }
}

@Composable
private fun WithdrawAmountStep(
    amount: String,
    available: String,
    selectedId: String,
    entries: List<AllowlistEntry>,
    online: Boolean,
    submitting: Boolean,
    onAmountChange: (String) -> Unit,
    onMax: () -> Unit,
    onSelectAddress: (AllowlistEntry) -> Unit,
    onPreview: () -> Unit,
    onClose: () -> Unit,
) {
    val parsed = amount.toBigDecimalOrNull()
    val balance = available.toBigDecimalOrNull()
    val tooMuch = parsed != null && balance != null && parsed > balance
    val selected = entries.firstOrNull { it.id == selectedId }

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
                Text("Available", style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Text(
                    if (balance != null) Format.money(available) else "—",
                    style = MaterialTheme.typography.titleMedium,
                )
            }
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = amount,
                onValueChange = onAmountChange,
                label = { Text("Amount (USD)") },
                singleLine = true,
                isError = tooMuch,
                supportingText = { if (tooMuch) Text("More than the available balance.") },
                trailingIcon = {
                    TextButton(onClick = onMax, enabled = balance != null && balance.signum() > 0) { Text("Max") }
                },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                modifier = Modifier.fillMaxWidth(),
            )
        }
    }

    Spacer(Modifier.height(12.dp))
    SectionHeader("Send to", "Allowlisted addresses only")
    if (entries.isEmpty()) {
        UnsupportedCard(
            title = "No recipient yet",
            reason = "Add an address under Wallet first. A new address waits out a cooling period before it can be used.",
        )
    } else {
        entries.forEach { entry ->
            val usable = entry.isUsable()
            val isSelected = entry.id == selectedId
            RowCard(
                title = entry.label,
                subtitle = "${entry.network} · ${entry.asset}",
                detail = Format.groupAddress(entry.address),
                accent = if (isSelected) MaterialTheme.colorScheme.primary else null,
                onClick = if (usable) ({ onSelectAddress(entry) }) else null,
                trailing = {
                    when {
                        isSelected -> Text(
                            "Selected",
                            style = MaterialTheme.typography.labelLarge,
                            color = MaterialTheme.colorScheme.primary,
                        )

                        !usable -> Text(
                            "Ready in ${Format.countdown(entry.usableAt)}",
                            style = MaterialTheme.typography.labelSmall,
                            color = Palette.Alert,
                        )

                        else -> Unit
                    }
                },
            )
            Spacer(Modifier.height(6.dp))
        }
    }

    Spacer(Modifier.height(12.dp))
    ConfirmButton(
        label = "Review withdrawal",
        enabled = parsed != null && parsed.signum() > 0 && !tooMuch && selected != null && selected.isUsable() && online,
        busy = submitting,
        onClick = onPreview,
    )
    TextButton(onClick = onClose, modifier = Modifier.fillMaxWidth()) { Text("Cancel") }
}

/** The amount, the recipient and what the server said about fee and timing. [full] adds the limits. */
@Composable
private fun WithdrawSummary(venue: String, amount: String, preview: WithdrawPreview, full: Boolean) {
    Card(
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(16.dp)) {
            Text(
                Format.money(amount),
                style = MaterialTheme.typography.headlineMedium,
            )
            Text(
                "from ${venue.uppercase()}",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(12.dp))
            Text(
                "To ${preview.label}",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Text(
                Format.groupAddress(preview.recipient),
                style = MaterialTheme.typography.bodyMedium,
                fontFamily = FontFamily.Monospace,
            )
            Spacer(Modifier.height(10.dp))
            ValueRow("Network", preview.network)
            ValueRow("Token", preview.asset)
            ValueRow("Fee", Format.money(preview.feeUsd))
            if (preview.etaSeconds > 0) {
                ValueRow("Arrives in about", "${maxOf(1, preview.etaSeconds / 60)} min")
            }
            ValueRow("Hold before sending", "${preview.holdMinutes} min")
            if (full) {
                ValueRow("Daily limit left", Format.money(preview.dailyRemainingUsd))
            }
        }
    }
    if (full) {
        Spacer(Modifier.height(8.dp))
        Text(
            "Check the address against your own record. A transfer to a wrong address cannot be undone.",
            style = MaterialTheme.typography.bodySmall,
            color = Palette.Alert,
        )
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
    Card(
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(16.dp)) {
            Text(
                if (cancelled) "Withdrawal cancelled" else "Withdrawal created",
                style = MaterialTheme.typography.titleMedium,
            )
            Spacer(Modifier.height(8.dp))
            ValueRow("Status", current)
            ValueRow("Amount", Format.money(amount), emphasise = true)
            ValueRow("To", preview.label)
            if (!withdrawalId.isNullOrBlank()) ValueRow("Reference", shortMiddle(withdrawalId))
            Spacer(Modifier.height(8.dp))
            Text(
                if (cancelled) {
                    "Nothing was sent."
                } else {
                    "Nothing is sent yet. It goes out after ${preview.holdMinutes} min unless you cancel it. " +
                        "Follow it under Withdrawals in Wallet."
                },
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

/** The trade sheet: side, outcome, order-type chips, price, size, and the estimate. */
@Composable
fun TradeSheetScreen(
    draft: com.mainnetwallet.mmagent.data.TradeDraft,
    estimate: com.mainnetwallet.mmagent.data.TradeEstimate,
    bids: List<BookLevel>,
    asks: List<BookLevel>,
    history: List<PricePoint>,
    online: Boolean,
    submitting: Boolean,
    error: String?,
    result: String?,
    onSide: (String) -> Unit,
    onOutcome: (String) -> Unit,
    onOrderType: (String) -> Unit,
    onPostOnly: (Boolean) -> Unit,
    onPrice: (String) -> Unit,
    onShares: (String) -> Unit,
    onAmount: (String) -> Unit,
    onSubmit: () -> Unit,
    onCancel: () -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .padding(16.dp),
    ) {
        SectionHeader("Trade", draft.venue.uppercase())
        Text(
            draft.title,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(12.dp))

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            ChoiceChip(
                label = "BUY",
                selected = draft.side == "BUY",
                accent = Palette.Long,
                onClick = { onSide("BUY") },
            )
            ChoiceChip(
                label = "SELL",
                selected = draft.side == "SELL",
                accent = Palette.Short,
                onClick = { onSide("SELL") },
            )
            Spacer(Modifier.width(4.dp))
            ChoiceChip("YES", selected = draft.outcome == "YES", onClick = { onOutcome("YES") })
            ChoiceChip("NO", selected = draft.outcome == "NO", onClick = { onOutcome("NO") })
        }

        Spacer(Modifier.height(10.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            draft.orderTypeChips.forEach { chip ->
                ChoiceChip(chip, selected = draft.orderType == chip, onClick = { onOrderType(chip) })
            }
        }
        if (draft.postOnlySupported) {
            Spacer(Modifier.height(8.dp))
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier
                    .clip(RoundedCornerShape(8.dp))
                    .clickable { onPostOnly(!draft.postOnly) }
                    .padding(vertical = 6.dp),
            ) {
                Text(
                    if (draft.postOnly) "☑" else "☐",
                    style = MaterialTheme.typography.titleMedium,
                )
                Spacer(Modifier.width(8.dp))
                Text("Post-only", style = MaterialTheme.typography.bodyMedium)
            }
        }

        Spacer(Modifier.height(10.dp))
        OutlinedTextField(
            value = draft.price,
            onValueChange = onPrice,
            label = { Text(if (draft.isMarketOrder) "Worst price" else "Price (0–1)") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = draft.shares,
            onValueChange = onShares,
            label = { Text("Shares") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = draft.amountUsd,
            onValueChange = onAmount,
            label = { Text("Or amount (${draft.collateralAsset})") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )

        Spacer(Modifier.height(14.dp))
        Card(
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Column(Modifier.padding(16.dp)) {
                SectionHeader("Order book")
                DepthView(bids = bids, asks = asks)
            }
        }
        Spacer(Modifier.height(12.dp))
        Card(
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Column(Modifier.padding(16.dp)) {
                SectionHeader("Price history", "1 day")
                if (history.isEmpty()) {
                    Text(
                        "No price history for this venue.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                } else {
                    PriceChart(points = history)
                }
            }
        }
        Spacer(Modifier.height(14.dp))
        Card(
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Column(Modifier.padding(16.dp)) {
                ValueRow("Shares", estimate.shares)
                ValueRow("Cost", Format.money(estimate.costUsd), emphasise = true)
                ValueRow("Fee", Format.money(estimate.feeUsd))
                ValueRow("Max loss", Format.money(estimate.maxLossUsd))
                ValueRow("Payout if right", Format.money(estimate.payoutUsd))
                if (estimate.problem != null) {
                    Spacer(Modifier.height(6.dp))
                    Text(estimate.problem, style = MaterialTheme.typography.bodySmall, color = Palette.Alert)
                }
            }
        }

        Spacer(Modifier.height(10.dp))
        ErrorNote(error)
        if (!online) {
            // The server is unreachable: the price on screen is unknown, so no order.
            ErrorNote("The server is unreachable. Trading is disabled until it answers again.")
        }
        if (result != null) {
            Spacer(Modifier.height(8.dp))
            Text(result, style = MaterialTheme.typography.bodyMedium, color = Palette.Long)
        }
        Spacer(Modifier.height(12.dp))

        val canSubmit = estimate.valid && !submitting && online
        HoldToConfirmButton(
            label = "Hold to ${draft.side.lowercase()}",
            enabled = canSubmit,
            busy = submitting,
            onConfirmed = onSubmit,
        )
        Spacer(Modifier.height(6.dp))
        TextButton(onClick = onCancel) { Text("Cancel") }
    }
}
