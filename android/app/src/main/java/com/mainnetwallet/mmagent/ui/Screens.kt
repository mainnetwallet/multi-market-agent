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
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.material.icons.filled.ChatBubble
import androidx.compose.material.icons.filled.PieChart
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import java.math.BigDecimal
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

/** The five bottom tabs. Settings opens from the gear in the top bar. */
enum class Tab(val label: String) {
    HOME("Home"),
    MARKETS("Markets"),
    CHAT("Chat"),
    PORTFOLIO("Portfolio"),
    WALLET("Wallet"),
}

/**
 * The shell: venue badge and gear on top, snackbar above the bottom bar, five tabs.
 * The badge opens the venue switcher.
 */
@Composable
fun AppScaffold(
    state: AppUiState,
    tab: Tab?,
    onTab: (Tab) -> Unit,
    onVenueClick: () -> Unit,
    onSettings: () -> Unit,
    snackbar: SnackbarHostState,
    content: @Composable () -> Unit,
) {
    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
        contentWindowInsets = WindowInsets(0, 0, 0, 0),
        snackbarHost = { SnackbarHost(snackbar) },
        topBar = {
            Column {
                ModeFrame(
                    venue = state.status.venue.ifBlank { "no venue" },
                    mode = state.status.mode,
                    paused = state.status.paused,
                    onBadgeClick = onVenueClick,
                    trailing = {
                        IconButton(onClick = onSettings) {
                            Icon(Icons.Filled.Settings, contentDescription = "Settings")
                        }
                    },
                ) {}
                if (state.loading) LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
                if (state.unreachable) {
                    // Last data stays on screen; the money buttons are off until it is fresh.
                    AlertCard(
                        text = "Server unreachable: showing the last data. Buying and withdrawing are off. ${state.error.orEmpty()}",
                        color = Palette.Alert,
                        marker = "⚠",
                    )
                }
            }
        },
        bottomBar = {
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
                                    Tab.CHAT -> Icons.Filled.ChatBubble
                                    Tab.PORTFOLIO -> Icons.Filled.PieChart
                                    Tab.WALLET -> Icons.Filled.AccountBalanceWallet
                                },
                                contentDescription = entry.label,
                            )
                        },
                        label = { Text(entry.label) },
                    )
                }
            }
        },
    ) { padding ->
        Box(modifier = Modifier.padding(padding).fillMaxSize()) { content() }
    }
}

/**
 * Pick the active venue. A venue that is not ready is shown with the server's
 * reason and cannot be picked: the server would refuse it anyway (409).
 */
@Composable
fun VenueSwitcherDialog(
    venues: List<JSONObject>,
    active: String,
    onPick: (venue: String) -> Unit,
    onDismiss: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Switch venue") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (venues.isEmpty()) {
                    Text("No venue information yet. Pull to refresh from Home.", style = MaterialTheme.typography.bodyMedium)
                }
                venues.filter { it.optBoolean("enabled", true) }.forEach { venue ->
                    val id = venue.optString("venue")
                    val isActive = id == active
                    val ready = venue.optBoolean("ready")
                    val mode = venue.optString("mode", "PAPER")
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(10.dp))
                            .background(MaterialTheme.colorScheme.surfaceVariant)
                            .clickable(enabled = ready && !isActive) { onPick(id) }
                            .padding(12.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Column(Modifier.weight(1f)) {
                            Text(
                                venue.optString("display_name").ifBlank { id.replaceFirstChar { it.uppercase() } },
                                style = MaterialTheme.typography.titleSmall,
                            )
                            Text(
                                when {
                                    isActive -> "Active · $mode"
                                    ready -> "Ready · $mode"
                                    else -> "Not ready: ${venue.optString("reason").ifBlank { "the server gave no reason" }}"
                                },
                                style = MaterialTheme.typography.bodySmall,
                                color = if (ready || isActive) MaterialTheme.colorScheme.onSurfaceVariant else Palette.Alert,
                            )
                        }
                        if (isActive) Text("✓", style = MaterialTheme.typography.titleMedium, color = Palette.Long)
                    }
                }
                Text(
                    "Positions and orders on the other venues stay alive. LIVE is set on the server, not here.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        },
        confirmButton = { TextButton(onClick = onDismiss) { Text("Close") } },
    )
}

/**
 * Home: money, risk and decisions at a glance. Pause and cancel-all reduce risk,
 * so they are a tap plus a normal dialog, never hold-to-confirm.
 */
@Composable
fun HomeScreen(
    state: AppUiState,
    onOpenWallet: () -> Unit,
    onPauseToggle: () -> Unit,
    onCancelAll: () -> Unit,
    onOpenMarkets: () -> Unit,
    onOpenChat: () -> Unit,
    onOpenProposals: () -> Unit,
    onOpenPositions: () -> Unit,
    onApprove: (id: String, done: () -> Unit) -> Unit,
    onReject: (id: String, done: () -> Unit) -> Unit,
) {
    var confirmCancelAll by remember { mutableStateOf(false) }
    val pending = pendingProposals(state.proposals)
    val live = state.status.mode.equals("LIVE", ignoreCase = true)
    val pnl = state.status.todayPnlUsd.toBigDecimalOrNull() ?: BigDecimal.ZERO
    val pnlColor = when {
        pnl.signum() > 0 -> Palette.Long
        pnl.signum() < 0 -> Palette.Short
        else -> MaterialTheme.colorScheme.onSurface
    }
    val pnlArrow = when {
        pnl.signum() > 0 -> "▲"
        pnl.signum() < 0 -> "▼"
        else -> "–"
    }
    val unknown = state.status.counts["unknown_orders"] ?: 0
    val topPositions = state.positions
        .sortedByDescending { it.optString("unrealized_pnl").toBigDecimalOrNull()?.abs() ?: BigDecimal.ZERO }
        .take(3)

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
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
                            style = MaterialTheme.typography.headlineMedium,
                        )
                        Spacer(Modifier.weight(1f))
                        LinkText("Wallet", onOpenWallet)
                    }
                    Spacer(Modifier.height(6.dp))
                    // Sign, arrow and colour together: never colour alone.
                    ValueRow(
                        label = "Today's PnL",
                        value = "$pnlArrow ${Format.signedMoney(state.status.todayPnlUsd)}",
                        valueColor = pnlColor,
                        emphasise = true,
                    )
                }
            }
        }
        item {
            EvenRow {
                StatTile("Positions", "${state.positions.size}", onClick = onOpenPositions)
                StatTile("Open orders", "${state.orders.size}", onClick = onOpenPositions)
                StatTile(
                    "To decide",
                    "${pending.size}",
                    valueColor = if (pending.isNotEmpty()) Palette.Alert else MaterialTheme.colorScheme.onSurface,
                    onClick = onOpenProposals,
                )
                StatTile(
                    "UNKNOWN",
                    "$unknown",
                    valueColor = if (unknown > 0) Palette.Danger else MaterialTheme.colorScheme.onSurface,
                )
            }
        }
        if (!state.status.unitGuardOk) {
            item {
                AlertCard("Trading is halted: ${state.status.unitGuardReason}", Palette.Danger, "⛔")
            }
        }
        if (unknown > 0) {
            item {
                AlertCard(
                    "$unknown order(s) are UNKNOWN. They are never resent and block their market until reconciled; check the venue directly.",
                    Palette.Danger,
                )
            }
        }
        if (state.status.paused) {
            item { AlertCard("The agent is paused: no new entries until you resume.", Palette.Alert, "⏸") }
        }
        if (pending.isNotEmpty()) {
            item { SectionHeader("Needs your decision", "${pending.size}") }
            items(pending.take(2), key = { "p" + it.optString("id") }) { proposal ->
                ProposalCard(proposal, live, state.online, onApprove, onReject)
            }
            if (pending.size > 2) {
                item { LinkText("See all ${pending.size} proposals", onOpenProposals) }
            }
        }
        item {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                SectionHeader("Quick actions")
                EvenRow {
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
                EvenRow {
                    OutlinedButton(onClick = onOpenMarkets, modifier = Modifier.weight(1f)) { Text("Browse markets") }
                    OutlinedButton(onClick = onOpenChat, modifier = Modifier.weight(1f)) { Text("Ask the agent") }
                }
            }
        }
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
        if (topPositions.isNotEmpty()) {
            item { SectionHeader("Top positions", "by open PnL") }
            items(topPositions, key = { "t" + it.optString("venue") + it.optString("market_id") + it.optString("outcome") }) { position ->
                val upnl = position.optString("unrealized_pnl")
                RowCard(
                    title = "${position.optString("outcome")} · ${Format.shares(position.optString("shares"))} shares",
                    subtitle = position.optString("market_title").ifBlank { position.optString("market_id") },
                    detail = "avg ${Format.cents(position.optString("avg_price"))} · open PnL ${Format.signedMoney(upnl)}",
                    accent = if (position.optString("outcome") == "YES") Palette.Long else Palette.Short,
                    onClick = onOpenPositions,
                )
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
