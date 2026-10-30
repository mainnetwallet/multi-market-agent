package com.mainnetwallet.mmagent.ui

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
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import org.json.JSONObject
import java.math.BigDecimal
import java.math.RoundingMode
import com.mainnetwallet.mmagent.data.AppUiState
import com.mainnetwallet.mmagent.data.Format

/** Portfolio: Positions, Orders, Rules, History. */
@Composable
fun PortfolioScreen(
    state: AppUiState,
    onCancelOrder: (String) -> Unit,
    onCancelAll: () -> Unit,
    onSell: (marketId: String, outcome: String, shares: String, venue: String) -> Unit,
    onRedeem: (marketId: String, outcome: String, venue: String) -> Unit,
    onAdvanced: (marketId: String, venue: String, action: String) -> Unit,
    onAmendOrder: (orderId: String, price: String?, shares: String?) -> Unit,
) {
    var tab by remember { mutableIntStateOf(0) }
    val titles = listOf("Positions", "Orders", "Rules", "History")

    Column(modifier = Modifier.fillMaxSize()) {
        TabRow(selectedTabIndex = tab) {
            titles.forEachIndexed { index, title ->
                Tab(
                    selected = tab == index,
                    onClick = { tab = index },
                    text = { Text(title) },
                )
            }
        }
        when (tab) {
            0 -> PositionsList(state, onSell, onRedeem, onAdvanced)
            1 -> OrdersList(state, onCancelOrder, onCancelAll, onAmendOrder)
            2 -> RulesList(state)
            else -> HistoryList(state)
        }
    }
}

@Composable
private fun PositionsList(
    state: AppUiState,
    onSell: (marketId: String, outcome: String, shares: String, venue: String) -> Unit,
    onRedeem: (marketId: String, outcome: String, venue: String) -> Unit,
    onAdvanced: (marketId: String, venue: String, action: String) -> Unit,
) {
    val capabilities = state.status.capabilities
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item { SectionHeader("Positions", "${state.positions.size} open") }
        items(state.positions) { position ->
            val venue = position.optString("venue")
            val marketId = position.optString("market_id")
            val outcome = position.optString("outcome")
            val venueCaps = capabilities?.optJSONObject("venues")?.optJSONObject(venue)
            val canRedeem = venueCaps?.optJSONObject("funds")?.optBoolean("redeem") == true
            val canSplit = venue == "polymarket"
            val shares = position.optString("shares")

            RowCard(
                title = "${position.optString("outcome")} · ${Format.shares(shares)} shares",
                subtitle = position.optString("market_title").ifBlank { marketId },
                detail = "avg ${Format.cents(position.optString("avg_price"))} · uPnL ${Format.signedMoney(position.optString("unrealized_pnl"))}",
                accent = if (position.optString("outcome") == "YES") Palette.Long else Palette.Short,
            )
            // Sell 25 / 50 / 100% and the other actions, as separate audited calls.
            Row(
                modifier = Modifier.fillMaxWidth().padding(start = 8.dp),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                listOf("25", "50", "100").forEach { percent ->
                    val portion = BigDecimal(shares)
                        .multiply(BigDecimal(percent))
                        .divide(BigDecimal(100), 4, RoundingMode.DOWN)
                    Text(
                        "Sell $percent%",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.secondary,
                        modifier = Modifier
                            .clip(RoundedCornerShape(999.dp))
                            .clickable { onSell(marketId, outcome, portion.toPlainString(), venue) }
                            .padding(horizontal = 10.dp, vertical = 8.dp),
                    )
                }
            }
            Row(
                modifier = Modifier.fillMaxWidth().padding(start = 8.dp),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                if (canRedeem) {
                    Text(
                        "Redeem",
                        style = MaterialTheme.typography.labelMedium,
                        color = Palette.Long,
                        modifier = Modifier
                            .clip(RoundedCornerShape(999.dp))
                            .clickable { onRedeem(marketId, outcome, venue) }
                            .padding(horizontal = 10.dp, vertical = 8.dp),
                    )
                }
                if (canSplit) {
                    // Advanced: these move collateral, not just position size.
                    Text(
                        "Split",
                        style = MaterialTheme.typography.labelMedium,
                        color = Palette.Alert,
                        modifier = Modifier
                            .clip(RoundedCornerShape(999.dp))
                            .clickable { onAdvanced(marketId, venue, "split") }
                            .padding(horizontal = 10.dp, vertical = 8.dp),
                    )
                    Text(
                        "Merge",
                        style = MaterialTheme.typography.labelMedium,
                        color = Palette.Alert,
                        modifier = Modifier
                            .clip(RoundedCornerShape(999.dp))
                            .clickable { onAdvanced(marketId, venue, "merge") }
                            .padding(horizontal = 10.dp, vertical = 8.dp),
                    )
                }
            }
        }
        if (state.positions.isEmpty()) {
            item { Text("No open positions.", style = MaterialTheme.typography.bodyMedium) }
        }
    }
}

@Composable
private fun OrdersList(
    state: AppUiState,
    onCancelOrder: (String) -> Unit,
    onCancelAll: () -> Unit,
    onAmendOrder: (orderId: String, price: String?, shares: String?) -> Unit,
) {
    val capabilities = state.status.capabilities
    var amending by remember { mutableStateOf<JSONObject?>(null) }
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                ConfirmButton(
                    label = "Cancel all",
                    enabled = state.orders.isNotEmpty(),
                    danger = true,
                    onClick = onCancelAll,
                    modifier = Modifier.weight(1f),
                )
            }
        }
        item { SectionHeader("Open orders", "${state.orders.size}") }
        items(state.orders) { order ->
            val venue = order.optString("venue")
            val canAmend = capabilities?.optJSONObject("venues")?.optJSONObject(venue)?.optBoolean("amend") == true
            RowCard(
                title = "${order.optString("side")} ${Format.shares(order.optString("size"))} @ ${Format.cents(order.optString("price"))}",
                subtitle = "${order.optString("venue")} · ${order.optString("market_id")} · ${order.optString("state")}",
                detail = "filled ${Format.shares(order.optString("filled"))} · ${order.optString("order_type")}",
                trailing = {
                    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                        if (canAmend) {
                            Text(
                                "Amend",
                                style = MaterialTheme.typography.labelLarge,
                                color = MaterialTheme.colorScheme.secondary,
                                modifier = Modifier
                                    .clip(RoundedCornerShape(8.dp))
                                    .clickable { amending = order }
                                    .padding(6.dp),
                            )
                        }
                        Text(
                            "Cancel",
                            style = MaterialTheme.typography.labelLarge,
                            color = MaterialTheme.colorScheme.secondary,
                            modifier = Modifier
                                .clip(RoundedCornerShape(8.dp))
                                .clickable { onCancelOrder(order.optString("id")) }
                                .padding(6.dp),
                        )
                    }
                },
            )
        }
        if (state.orders.isEmpty()) {
            item { Text("No open orders.", style = MaterialTheme.typography.bodyMedium) }
        }
    }

    amending?.let { order ->
        AmendDialog(
            order = order,
            onDismiss = { amending = null },
            onConfirm = { price, shares ->
                amending = null
                onAmendOrder(order.optString("id"), price, shares)
            },
        )
    }
}

/**
 * Amend a resting order: a new price, a new size, or both. Kalshi's amend count
 * is the total fillable amount, so the field is labelled as such.
 */
@Composable
private fun AmendDialog(
    order: JSONObject,
    onDismiss: () -> Unit,
    onConfirm: (price: String?, shares: String?) -> Unit,
) {
    var price by remember { mutableStateOf(order.optString("price")) }
    var shares by remember { mutableStateOf(order.optString("size")) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Amend order") },
        text = {
            Column {
                Text(
                    "${order.optString("side")} on ${order.optString("market_id")}",
                    style = MaterialTheme.typography.bodyMedium,
                )
                Spacer(Modifier.height(10.dp))
                OutlinedTextField(
                    value = price,
                    onValueChange = { price = it },
                    label = { Text("New price") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(
                    value = shares,
                    onValueChange = { shares = it },
                    label = { Text("New total size") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
            }
        },
        confirmButton = {
            TextButton(onClick = {
                // Blank means "leave it alone"; the server validates the rest.
                onConfirm(price.trim().ifBlank { null }, shares.trim().ifBlank { null })
            }) { Text("Amend") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

/** The amend sheet: a new price and/or size for a resting order. */
@Composable
fun AmendSheet(
    orderId: String,
    currentPrice: String,
    currentSize: String,
    onAmend: (orderId: String, price: String?, shares: String?) -> Unit,
    onDismiss: () -> Unit,
) {
    var price by remember { mutableStateOf("") }
    var shares by remember { mutableStateOf("") }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Amend order") },
        text = {
            Column {
                Text(
                    "Currently ${Format.shares(currentSize)} @ ${Format.cents(currentPrice)}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.height(10.dp))
                OutlinedTextField(
                    value = price,
                    onValueChange = { price = it },
                    label = { Text("New price (0–1)") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(
                    value = shares,
                    onValueChange = { shares = it },
                    label = { Text("New size") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(8.dp))
                Text(
                    "Leave a field empty to keep it. The venue decides whether the change is allowed.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        },
        confirmButton = {
            TextButton(
                onClick = {
                    onAmend(orderId, price.ifBlank { null }, shares.ifBlank { null })
                },
                enabled = price.isNotBlank() || shares.isNotBlank(),
            ) { Text("Amend") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

@Composable
private fun RulesList(state: AppUiState) {
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item { SectionHeader("Exit rules", "${state.rules.size}") }
        items(state.rules) { rule ->
            RowCard(
                title = rule.optString("kind"),
                subtitle = "${rule.optString("venue")} · ${rule.optString("market_id")} · ${rule.optString("status")}",
                detail = "trigger ${rule.optString("trigger_price").ifBlank { rule.optString("trigger_at") }}",
            )
        }
        if (state.rules.isEmpty()) {
            item { Text("No exit rules armed.", style = MaterialTheme.typography.bodyMedium) }
        }
    }
}

@Composable
private fun HistoryList(state: AppUiState) {
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        if (state.pnlDays.isNotEmpty()) {
            item {
                Card(
                    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Column(Modifier.padding(16.dp)) {
                        SectionHeader("Daily PnL")
                        PnlChart(state.pnlDays)
                    }
                }
            }
        }
        item { SectionHeader("Trades", "${state.trades.size}") }
        items(state.trades) { trade ->
            val side = trade.optString("side")
            RowCard(
                title = "$side ${Format.shares(trade.optString("shares"))} @ ${Format.cents(trade.optString("price"))}",
                subtitle = "${trade.optString("venue")} · ${trade.optString("market_title").ifBlank { trade.optString("market_id") }}",
                detail = "fee ${Format.money(trade.optString("fee_usd"))} · ${Format.dhaka(trade.optString("at"))}",
                accent = if (side == "BUY") Palette.Long else Palette.Short,
            )
        }
        if (state.trades.isEmpty()) {
            item { Text("No trades yet.", style = MaterialTheme.typography.bodyMedium) }
        }
    }
}
