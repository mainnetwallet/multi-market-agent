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
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import com.mainnetwallet.mmagent.data.AppUiState
import com.mainnetwallet.mmagent.data.Format
import org.json.JSONObject

/** Portfolio: Positions, Orders, Rules, History. */
@Composable
fun PortfolioScreen(
    state: AppUiState,
    onCancelOrder: (String) -> Unit,
    onCancelAll: () -> Unit,
    onSell: (String, String) -> Unit,
    onRedeem: (String, String) -> Unit,
    onAdvanced: (String, String) -> Unit,
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
            1 -> OrdersList(state, onCancelOrder, onCancelAll)
            2 -> RulesList(state)
            else -> HistoryList(state)
        }
    }
}

@Composable
private fun PositionsList(
    state: AppUiState,
    onSell: (String, String) -> Unit,
    onRedeem: (String, String) -> Unit,
    onAdvanced: (String, String) -> Unit,
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

            RowCard(
                title = "${position.optString("outcome")} · ${Format.shares(position.optString("shares"))} shares",
                subtitle = position.optString("market_title").ifBlank { marketId },
                detail = "avg ${Format.cents(position.optString("avg_price"))} · uPnL ${Format.signedMoney(position.optString("unrealized_pnl"))}",
                accent = if (position.optString("outcome") == "YES") Palette.Long else Palette.Short,
                trailing = {
                    Column(horizontalAlignment = Alignment.End) {
                        Text(
                            "Sell",
                            style = MaterialTheme.typography.labelLarge,
                            color = MaterialTheme.colorScheme.secondary,
                            modifier = Modifier
                                .clip(RoundedCornerShape(8.dp))
                                .padding(6.dp),
                        )
                    }
                },
            )
            // Sell 25 / 50 / 100% and the other actions, as separate audited calls.
            Row(
                modifier = Modifier.fillMaxWidth().padding(start = 8.dp),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                listOf("25", "50", "100").forEach { percent ->
                    Text(
                        "Sell $percent%",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.secondary,
                        modifier = Modifier
                            .clip(RoundedCornerShape(999.dp))
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
                            .padding(horizontal = 10.dp, vertical = 8.dp),
                    )
                }
                if (canSplit) {
                    Text(
                        "Split / Merge",
                        style = MaterialTheme.typography.labelMedium,
                        color = Palette.Alert,
                        modifier = Modifier
                            .clip(RoundedCornerShape(999.dp))
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
) {
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
            RowCard(
                title = "${order.optString("side")} ${Format.shares(order.optString("size"))} @ ${Format.cents(order.optString("price"))}",
                subtitle = "${order.optString("venue")} · ${order.optString("market_id")} · ${order.optString("state")}",
                detail = "filled ${Format.shares(order.optString("filled"))} · ${order.optString("order_type")}",
                trailing = {
                    Text(
                        "Cancel",
                        style = MaterialTheme.typography.labelLarge,
                        color = MaterialTheme.colorScheme.secondary,
                        modifier = Modifier
                            .clip(RoundedCornerShape(8.dp))
                            .padding(6.dp),
                    )
                },
            )
        }
        if (state.orders.isEmpty()) {
            item { Text("No open orders.", style = MaterialTheme.typography.bodyMedium) }
        }
    }
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
