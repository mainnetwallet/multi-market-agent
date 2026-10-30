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
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AccountBalanceWallet
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.ShowChart
import androidx.compose.material.icons.filled.Tune
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
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
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.mainnetwallet.mmagent.data.AgentStatus
import com.mainnetwallet.mmagent.data.AppUiState
import com.mainnetwallet.mmagent.data.Format
import org.json.JSONObject

/** The five tabs: Home, Markets, Trade, Portfolio, Settings. */
enum class Tab(val label: String) {
    HOME("Home"),
    MARKETS("Markets"),
    PORTFOLIO("Portfolio"),
    WALLET("Wallet"),
    SETTINGS("Settings"),
}

@Composable
fun AppScaffold(
    state: AppUiState,
    tab: Tab,
    onTab: (Tab) -> Unit,
    onRefresh: () -> Unit,
    content: @Composable () -> Unit,
) {
    Column(modifier = Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)) {
        ModeFrame(venue = state.status.venue.ifBlank { "no venue" }, mode = state.status.mode, paused = state.status.paused) {
            Column(modifier = Modifier.weight(1f)) { content() }
        }
        NavigationBar(containerColor = MaterialTheme.colorScheme.surface) {
            Tab.entries.forEach { entry ->
                NavigationBarItem(
                    selected = tab == entry,
                    onClick = { onTab(entry) },
                    icon = {
                        Icon(
                            imageVector = when (entry) {
                                Tab.HOME -> Icons.Filled.Home
                                Tab.MARKETS -> Icons.Filled.ShowChart
                                Tab.PORTFOLIO -> Icons.Filled.Warning
                                Tab.WALLET -> Icons.Filled.AccountBalanceWallet
                                Tab.SETTINGS -> Icons.Filled.Tune
                            },
                            contentDescription = entry.label,
                        )
                    },
                    label = { Text(entry.label) },
                )
            }
        }
    }
}

/** Home: cash line, today's PnL, one-tap pause and cancel-all. */
@Composable
fun HomeScreen(
    state: AppUiState,
    onOpenWallet: () -> Unit,
    onPauseToggle: () -> Unit,
    onCancelAll: () -> Unit,
    onOpenProposals: () -> Unit,
) {
    var confirmCancelAll by remember { mutableStateOf(false) }

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item {
            Card(
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Column(Modifier.padding(16.dp)) {
                    Text("Cash available", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Spacer(Modifier.height(4.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            text = Format.withAsset(state.status.availableUsd, state.status.collateralAsset),
                            style = MaterialTheme.typography.headlineSmall,
                        )
                        Spacer(Modifier.width(10.dp))
                        Text(
                            "Open wallet",
                            style = MaterialTheme.typography.labelLarge,
                            color = MaterialTheme.colorScheme.secondary,
                            modifier = Modifier
                                .clip(RoundedCornerShape(8.dp))
                                .clickable { onOpenWallet() }
                                .padding(6.dp),
                        )
                    }
                    Spacer(Modifier.height(6.dp))
                    ValueRow(
                        label = "Today's PnL",
                        value = Format.signedMoney(state.status.todayPnlUsd),
                        valueColor = if (state.status.todayPnlUsd.toBigDecimalOrNull()?.signum() ?: 0 >= 0) Palette.Long else Palette.Short,
                    )
                }
            }
        }
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                // Pause and cancel-all reduce risk: one tap plus a normal dialog.
                ConfirmButton(
                    label = if (state.status.paused) "Resume" else "Pause",
                    enabled = true,
                    onClick = onPauseToggle,
                    modifier = Modifier.weight(1f),
                )
                ConfirmButton(
                    label = "Cancel all",
                    enabled = true,
                    danger = true,
                    onClick = { confirmCancelAll = true },
                    modifier = Modifier.weight(1f),
                )
            }
        }
        if (!state.status.unitGuardOk) {
            item {
                ErrorNote("Trading is halted: ${state.status.unitGuardReason}")
            }
        }
        item {
            Card(
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Column(Modifier.padding(16.dp)) {
                    SectionHeader("Decisions waiting")
                    Text(
                        "Proposals: ${state.status.counts["pending_proposals"] ?: 0} · " +
                            "UNKNOWN orders: ${state.status.counts["unknown_orders"] ?: 0}",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    Spacer(Modifier.height(8.dp))
                    Text(
                        "Open proposals",
                        style = MaterialTheme.typography.labelLarge,
                        color = MaterialTheme.colorScheme.secondary,
                        modifier = Modifier
                            .clip(RoundedCornerShape(8.dp))
                            .clickable { onOpenProposals() }
                            .padding(6.dp),
                    )
                }
            }
        }
        item {
            Card(
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Column(Modifier.padding(16.dp)) {
                    SectionHeader("Limits")
                    ValueRow("Per-trade cap", Format.money(state.status.maxTradeUsd))
                    ValueRow("Hard ceiling", Format.money(state.status.hardCeilingMaxTrade))
                }
            }
        }
    }

    if (confirmCancelAll) {
        AlertDialog(
            onDismissRequest = { confirmCancelAll = false },
            title = { Text("Cancel all orders?") },
            text = { Text("Every open order on every venue is cancelled. This reduces risk and cannot be undone.") },
            confirmButton = {
                TextButton(onClick = {
                    confirmCancelAll = false
                    onCancelAll()
                }) { Text("Cancel all") }
            },
            dismissButton = { TextButton(onClick = { confirmCancelAll = false }) { Text("Keep them") } },
        )
    }
}

/** The lock screen: shown instead of everything else when the app re-locks. */
@Composable
fun LockedScreen(onUnlock: () -> Unit) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .padding(24.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("MM Agent is locked", style = MaterialTheme.typography.displaySmall)
        Spacer(Modifier.height(8.dp))
        Text(
            "Confirm it is you to see your balances and positions.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(24.dp))
        ConfirmButton("Unlock", enabled = true, onClick = onUnlock)
    }
}

/** The login screen: server URL, username, password. */
@Composable
fun LoginScreen(
    baseUrl: String,
    busy: Boolean,
    error: String?,
    onSaveBaseUrl: (String) -> Unit,
    onLogin: (String, String) -> Unit,
) {
    var url by remember { mutableStateOf(baseUrl) }
    var username by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .padding(24.dp),
        verticalArrangement = Arrangement.Center,
    ) {
        Text("Multi-Market Agent", style = MaterialTheme.typography.displaySmall)
        Text(
            "Polymarket · Kalshi · Limitless",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(24.dp))
        OutlinedTextField(
            value = url,
            onValueChange = { url = it },
            label = { Text("Server URL") },
            placeholder = { Text("https://multi-market-agent.onrender.com") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(10.dp))
        OutlinedTextField(
            value = username,
            onValueChange = { username = it },
            label = { Text("Username") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(10.dp))
        OutlinedTextField(
            value = password,
            onValueChange = { password = it },
            label = { Text("Password") },
            visualTransformation = PasswordVisualTransformation(),
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(16.dp))
        ErrorNote(error)
        Spacer(Modifier.height(12.dp))
        ConfirmButton(
            label = "Sign in",
            enabled = url.isNotBlank() && username.isNotBlank() && password.isNotBlank(),
            busy = busy,
            onClick = {
                onSaveBaseUrl(url.trim())
                onLogin(username, password)
            },
        )
    }
}

/** A compact card for one JSON row (a market, an order, a position). */
@Composable
fun RowCard(
    title: String,
    subtitle: String,
    detail: String? = null,
    accent: Color? = null,
    onClick: (() -> Unit)? = null,
    trailing: @Composable (() -> Unit)? = null,
) {
    Card(
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .then(if (onClick != null) Modifier.clickable { onClick() } else Modifier),
    ) {
        Row(
            modifier = Modifier.padding(14.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                Text(title, style = MaterialTheme.typography.titleSmall, color = accent ?: MaterialTheme.colorScheme.onSurface)
                Spacer(Modifier.height(2.dp))
                Text(subtitle, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                if (!detail.isNullOrBlank()) {
                    Spacer(Modifier.height(2.dp))
                    Text(
                        detail,
                        style = MaterialTheme.typography.bodySmall,
                        fontFamily = FontFamily.Monospace,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            if (trailing != null) {
                Spacer(Modifier.width(10.dp))
                trailing()
            }
        }
    }
}
