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
import kotlinx.coroutines.delay
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.LaunchedEffect
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
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.Person
import androidx.compose.material.icons.filled.Visibility
import androidx.compose.material.icons.filled.VisibilityOff
import androidx.compose.ui.focus.FocusDirection
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.compositeOver
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow

/** The five bottom tabs. Settings opens from the gear in the top bar. */
enum class Tab(val label: String) {
    HOME("Home"),
    MARKETS("Markets"),
    CHAT("Chat"),
    PORTFOLIO("Portfolio"),
    WALLET("Wallet"),
}

/**
 * The shell: venue badge, market search (Markets tab) and gear on top, snackbar above the bottom bar, five tabs.
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
    searchQuery: String? = null,
    onSearchQuery: (String) -> Unit = {},
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
                    paused = state.status.paused,
                    onBadgeClick = onVenueClick,
                    searchQuery = searchQuery,
                    onSearchQuery = onSearchQuery,
                    trailing = {
                        IconButton(onClick = onSettings) {
                            Icon(Icons.Filled.Settings, contentDescription = "Settings")
                        }
                    },
                ) {}
                if (state.loading && !state.startupLoad) LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
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
                                    isActive -> "Active"
                                    ready -> "Ready"
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
                    "Positions and orders on the other venues stay alive.",
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
                ProposalCard(proposal, state.online, onApprove, onReject)
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

/**
 * Sign-in. The username starts as the last one used (first run: "operator"), the server
 * sits behind a small "Change" link because it is baked into the build, and the keyboard
 * walks username -> password -> sign in.
 */
@Composable
fun LoginScreen(
    baseUrl: String,
    busy: Boolean,
    error: String?,
    onSaveBaseUrl: (String) -> Unit,
    onLogin: (String, String) -> Unit,
    initialUsername: String = "operator",
) {
    var url by remember { mutableStateOf(baseUrl) }
    var username by remember { mutableStateOf(initialUsername.ifBlank { "operator" }) }
    var password by remember { mutableStateOf("") }
    var showPassword by remember { mutableStateOf(false) }
    var editServer by remember { mutableStateOf(baseUrl.isBlank()) }
    val focus = LocalFocusManager.current

    // A server that was asleep can take a while: count the seconds and say so.
    var waited by remember { mutableIntStateOf(0) }
    LaunchedEffect(busy) {
        waited = 0
        while (busy) {
            delay(1000)
            waited++
        }
    }

    val filled = url.isNotBlank() && username.isNotBlank() && password.isNotBlank()
    val submit: () -> Unit = {
        if (filled && !busy) {
            focus.clearFocus()
            onSaveBaseUrl(url.trim())
            onLogin(username.trim(), password)
        }
    }

    val primary = MaterialTheme.colorScheme.primary
    val background = MaterialTheme.colorScheme.background
    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(
                Brush.verticalGradient(
                    listOf(primary.copy(alpha = 0.20f).compositeOver(background), background, background),
                ),
            ),
    ) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .statusBarsPadding()
                .navigationBarsPadding()
                .imePadding()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 24.dp, vertical = 32.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
        ) {
            Box(
                modifier = Modifier
                    .size(76.dp)
                    .clip(CircleShape)
                    .background(primary.copy(alpha = 0.16f)),
                contentAlignment = Alignment.Center,
            ) {
                Icon(
                    Icons.Filled.Lock,
                    contentDescription = null,
                    tint = primary,
                    modifier = Modifier.size(34.dp),
                )
            }
            Spacer(Modifier.height(20.dp))
            Text(
                "Multi-Market Agent",
                style = MaterialTheme.typography.headlineMedium,
                fontWeight = FontWeight.Bold,
                textAlign = TextAlign.Center,
            )
            Spacer(Modifier.height(4.dp))
            Text(
                "Polymarket · Kalshi · Limitless",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
            )
            Spacer(Modifier.height(28.dp))

            Card(
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                shape = RoundedCornerShape(24.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .widthIn(max = 460.dp),
            ) {
                Column(Modifier.padding(20.dp)) {
                    Text("Sign in", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.SemiBold)
                    Spacer(Modifier.height(16.dp))
                    OutlinedTextField(
                        value = username,
                        onValueChange = { username = it },
                        label = { Text("Username") },
                        leadingIcon = { Icon(Icons.Filled.Person, contentDescription = null) },
                        singleLine = true,
                        shape = RoundedCornerShape(14.dp),
                        keyboardOptions = KeyboardOptions(
                            capitalization = KeyboardCapitalization.None,
                            keyboardType = KeyboardType.Text,
                            imeAction = ImeAction.Next,
                        ),
                        keyboardActions = KeyboardActions(onNext = { focus.moveFocus(FocusDirection.Down) }),
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Spacer(Modifier.height(12.dp))
                    OutlinedTextField(
                        value = password,
                        onValueChange = { password = it },
                        label = { Text("Password") },
                        leadingIcon = { Icon(Icons.Filled.Lock, contentDescription = null) },
                        trailingIcon = {
                            IconButton(onClick = { showPassword = !showPassword }) {
                                Icon(
                                    if (showPassword) Icons.Filled.VisibilityOff else Icons.Filled.Visibility,
                                    contentDescription = if (showPassword) "Hide password" else "Show password",
                                )
                            }
                        },
                        visualTransformation = if (showPassword) VisualTransformation.None else PasswordVisualTransformation(),
                        singleLine = true,
                        shape = RoundedCornerShape(14.dp),
                        keyboardOptions = KeyboardOptions(
                            capitalization = KeyboardCapitalization.None,
                            keyboardType = KeyboardType.Password,
                            imeAction = ImeAction.Done,
                        ),
                        keyboardActions = KeyboardActions(onDone = { submit() }),
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Spacer(Modifier.height(12.dp))

                    if (editServer) {
                        OutlinedTextField(
                            value = url,
                            onValueChange = { url = it },
                            label = { Text("Server URL") },
                            placeholder = { Text("https://multi-market-agent.onrender.com") },
                            singleLine = true,
                            shape = RoundedCornerShape(14.dp),
                            keyboardOptions = KeyboardOptions(
                                capitalization = KeyboardCapitalization.None,
                                keyboardType = KeyboardType.Uri,
                                imeAction = ImeAction.Done,
                            ),
                            keyboardActions = KeyboardActions(onDone = { submit() }),
                            modifier = Modifier.fillMaxWidth(),
                        )
                    } else {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Column(Modifier.weight(1f)) {
                                Text(
                                    "Server",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                                Text(
                                    url,
                                    style = MaterialTheme.typography.bodyMedium,
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis,
                                )
                            }
                            TextButton(onClick = { editServer = true }) { Text("Change") }
                        }
                    }

                    if (!error.isNullOrBlank()) {
                        Spacer(Modifier.height(12.dp))
                        ErrorNote(error)
                    }
                    Spacer(Modifier.height(16.dp))
                    ConfirmButton(
                        label = "Sign in",
                        enabled = filled,
                        busy = busy,
                        onClick = submit,
                    )
                    if (busy && waited >= 4) {
                        Spacer(Modifier.height(10.dp))
                        Text(
                            "Waiting for the server (${waited}s). If it was asleep, the first sign-in can take up to a minute.",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
            }

            Spacer(Modifier.height(16.dp))
            Text(
                "You stay signed in on this device. Sign out any time from Settings.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
            )
        }
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
