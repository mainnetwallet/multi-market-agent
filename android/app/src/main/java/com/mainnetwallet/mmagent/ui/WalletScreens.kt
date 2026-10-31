package com.mainnetwallet.mmagent.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
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
import androidx.compose.ui.unit.dp
import com.mainnetwallet.mmagent.data.AllowlistEntry
import com.mainnetwallet.mmagent.data.Format
import com.mainnetwallet.mmagent.data.WithdrawPreview
import com.mainnetwallet.mmagent.data.WithdrawStep
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
                        title = "${row.optString("kind")} ${Format.money(row.optString("amount_usd"))}",
                        subtitle = "${row.optString("venue")} · ${row.optString("status")}",
                        detail = "${row.optString("type")} · fee ${Format.money(row.optString("fee_usd"))}",
                    )
                    Spacer(Modifier.height(6.dp))
                }
            }
        }
    }
}

/**
 * The deposit sheet: where to send money, in the asset and network the venue
 * itself lists. The asset name is never hardcoded.
 */
@Composable
fun DepositScreen(
    venue: String,
    info: JSONObject?,
    error: String?,
    onCopy: (String) -> Unit,
    onBack: () -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .padding(16.dp),
    ) {
        SectionHeader("Deposit", venue.uppercase())
        ErrorNote(error)
        Spacer(Modifier.height(10.dp))

        if (info == null || !info.optBoolean("supported")) {
            val reason = info?.optString("reason").orEmpty()
            UnsupportedCard(
                title = "No deposit API",
                reason = reason.ifBlank { "This venue does not accept deposits through the API." },
            )
        } else {
            Card(
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Column(Modifier.padding(16.dp)) {
                    ValueRow("Network", info.optString("network"))
                    ValueRow("Asset", info.optString("asset"), emphasise = true)
                    ValueRow("Minimum", Format.money(info.optString("min_amount")))
                    Spacer(Modifier.height(10.dp))
                    Text(
                        "Send to",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Text(
                        Format.groupAddress(info.optString("address")),
                        style = MaterialTheme.typography.bodyMedium,
                        fontFamily = FontFamily.Monospace,
                    )
                    Spacer(Modifier.height(8.dp))
                    ConfirmButton(
                        "Copy address",
                        enabled = true,
                        onClick = { onCopy(info.optString("address")) },
                    )
                }
            }
            Spacer(Modifier.height(12.dp))
            val warnings = info.optJSONArray("warnings")
            if (warnings != null && warnings.length() > 0) {
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
                        }
                    }
                }
            }
        }
        Spacer(Modifier.height(12.dp))
        ConfirmButton("Back", enabled = true, onClick = onBack)
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

/** The withdraw review sheet: everything the operator must see before sending. */
@Composable
fun WithdrawReviewScreen(
    venue: String,
    amount: String,
    entries: List<AllowlistEntry>,
    preview: WithdrawPreview,
    step: WithdrawStep,
    error: String?,
    online: Boolean,
    submitting: Boolean,
    onAmountChange: (String) -> Unit,
    onSelectAddress: (AllowlistEntry) -> Unit,
    onPreview: () -> Unit,
    onStepUp: (String) -> Unit,
    onConfirm: () -> Unit,
    onCancel: () -> Unit,
) {
    var password by remember { mutableStateOf("") }
    val isLive = venue.isNotBlank()

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .padding(16.dp),
    ) {
        SectionHeader("Withdraw", venue.uppercase())
        ErrorNote(error)
        if (!online) {
            ErrorNote("The server is unreachable. Withdrawing is disabled until it answers again.")
        }

        when (step) {
            WithdrawStep.AMOUNT -> {
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(
                    value = amount,
                    onValueChange = onAmountChange,
                    label = { Text("Amount") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(12.dp))
                SectionHeader("Recipient", "Must already be on the allowlist")
                if (entries.isEmpty()) {
                    Text(
                        "No allowlisted address yet. Add one in Settings first; a new address waits out a cooling period.",
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                } else {
                    entries.forEach { entry ->
                        val selected = entry.id == entries.firstOrNull { it.id == entry.id }?.id
                        RowCard(
                            title = entry.label,
                            subtitle = "${entry.network} · ${entry.asset}",
                            detail = Format.groupAddress(entry.address),
                            accent = if (selected) MaterialTheme.colorScheme.primary else null,
                            onClick = { onSelectAddress(entry) },
                        )
                        Spacer(Modifier.height(6.dp))
                    }
                }
                Spacer(Modifier.height(12.dp))
                ConfirmButton(
                    label = "Preview withdrawal",
                    enabled = amount.isNotBlank() && entries.isNotEmpty() && online,
                    busy = submitting,
                    onClick = onPreview,
                )
            }

            WithdrawStep.PREVIEW -> {
                Spacer(Modifier.height(8.dp))
                Card(
                    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Column(Modifier.padding(16.dp)) {
                        ValueRow("Venue", venue.uppercase())
                        ValueRow("Amount", Format.money(amount), emphasise = true)
                        ValueRow("Network", preview.network)
                        ValueRow("Token", preview.asset)
                        ValueRow("Fee", Format.money(preview.feeUsd))
                        ValueRow("ETA", "${preview.etaSeconds} s")
                        ValueRow("Hold window", "${preview.holdMinutes} min")
                        ValueRow("Daily limit left", Format.money(preview.dailyRemainingUsd))
                        Spacer(Modifier.height(10.dp))
                        Text(
                            "Recipient (${preview.label})",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        Text(
                            Format.groupAddress(preview.recipient),
                            style = MaterialTheme.typography.bodyMedium,
                            fontFamily = FontFamily.Monospace,
                        )
                    }
                }
                Spacer(Modifier.height(12.dp))
                OutlinedTextField(
                    value = password,
                    onValueChange = { password = it },
                    label = { Text("Password (never stored)") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(12.dp))
                ConfirmButton(
                    label = "Step up",
                    enabled = password.isNotBlank(),
                    busy = submitting,
                    onClick = { onStepUp(password) },
                )
                Spacer(Modifier.height(8.dp))
                TextButton(onClick = onCancel) { Text("Back") }
            }

            WithdrawStep.STEP_UP, WithdrawStep.CONFIRMING -> {
                Spacer(Modifier.height(8.dp))
                Card(
                    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Column(Modifier.padding(16.dp)) {
                        ValueRow("Amount", Format.money(amount), emphasise = true)
                        ValueRow("Network", preview.network)
                        ValueRow("Token", preview.asset)
                        ValueRow("Fee", Format.money(preview.feeUsd))
                        ValueRow("Recipient", Format.groupAddress(preview.recipient))
                        ValueRow("Hold window", "${preview.holdMinutes} min")
                    }
                }
                Spacer(Modifier.height(16.dp))
                if (isLive) {
                    HoldToConfirmButton(
                        label = "Hold to send",
                        enabled = !submitting && online,
                        busy = submitting,
                        danger = true,
                        onConfirmed = onConfirm,
                    )
                } else {
                    ConfirmButton(
                        label = "Send",
                        enabled = !submitting && online,
                        busy = submitting,
                        danger = true,
                        onClick = onConfirm,
                    )
                }
                Spacer(Modifier.height(8.dp))
                Text(
                    "Nothing is sent yet: the withdrawal waits out its hold window first.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            WithdrawStep.DONE -> {
                Spacer(Modifier.height(20.dp))
                Card(
                    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Column(Modifier.padding(16.dp)) {
                        Text("Withdrawal created", style = MaterialTheme.typography.titleMedium)
                        Spacer(Modifier.height(8.dp))
                        ValueRow("Status", "PENDING_HOLD")
                        ValueRow("Amount", Format.money(amount))
                        Spacer(Modifier.height(8.dp))
                        Text(
                            "Nothing is sent yet: the withdrawal waits out its hold window first. " +
                                "It can be cancelled until then.",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
                Spacer(Modifier.height(12.dp))
                ConfirmButton("Done", enabled = true, onClick = onCancel)
            }
        }
    }
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
