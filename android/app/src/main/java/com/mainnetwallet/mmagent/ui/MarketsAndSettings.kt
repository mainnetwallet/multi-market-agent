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
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import com.mainnetwallet.mmagent.data.InfoUiState
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.Switch
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.mainnetwallet.mmagent.data.AppUiState
import com.mainnetwallet.mmagent.data.Format
import org.json.JSONObject

/** Markets: filter by venue, sort by what closes first, see the countdown. */
@Composable
fun MarketsScreen(
    markets: List<JSONObject>,
    activeVenue: String,
    onOpen: (marketId: String, title: String) -> Unit,
) {
    var query by rememberSaveable { mutableStateOf("") }
    var venueFilter by rememberSaveable { mutableStateOf("") }
    var soonest by rememberSaveable { mutableStateOf(false) }

    val venues = markets.map { it.optString("venue") }.filter { it.isNotBlank() }.distinct()
    val filtered = markets
        .filter { market ->
            (venueFilter.isBlank() || market.optString("venue") == venueFilter) &&
                (query.isBlank() || market.optString("title").contains(query, ignoreCase = true))
        }
        .let { list ->
            if (!soonest) list
            else list.sortedBy { market ->
                // Closed or unreadable go last so "soonest" means soonest still open.
                val left = Format.secondsUntil(market.optString("end_time"))
                if (left == null || left <= 0) Long.MAX_VALUE else left
            }
        }

    Column(modifier = Modifier.fillMaxSize()) {
        OutlinedTextField(
            value = query,
            onValueChange = { query = it },
            label = { Text("Search markets") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth().padding(start = 16.dp, end = 16.dp, top = 8.dp),
        )
        LazyRow(
            contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = 16.dp, vertical = 8.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            item { ChoiceChip("All venues", selected = venueFilter.isBlank(), onClick = { venueFilter = "" }) }
            items(venues) { venue ->
                ChoiceChip(
                    label = venue.replaceFirstChar { it.uppercase() } + if (venue == activeVenue) " ·active" else "",
                    selected = venueFilter == venue,
                    onClick = { venueFilter = venue },
                )
            }
            item { ChoiceChip("Closes first", selected = soonest, onClick = { soonest = !soonest }) }
        }
        LazyColumn(
            modifier = Modifier.fillMaxSize(),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(start = 16.dp, end = 16.dp, bottom = 16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            item { SectionHeader("Markets", "${filtered.size}") }
            items(filtered, key = { it.optString("venue") + ":" + it.optString("native_id") }) { market ->
                val end = market.optString("end_time")
                val left = Format.secondsUntil(end)
                // The agent never enters inside the last 5 minutes; say so on the card.
                val closingSoon = left != null && left in 1L..300L
                val closed = left != null && left <= 0
                RowCard(
                    title = market.optString("title"),
                    subtitle = "${market.optString("venue")} · ${market.optString("category")}",
                    detail = "closes in ${Format.countdown(end)} (${Format.dhaka(end)}) · tick ${market.optString("tick_size")}" +
                        if (closingSoon) " · no new entries" else "",
                    onClick = if (closed) null else ({ onOpen(market.optString("native_id"), market.optString("title")) }),
                    trailing = {
                        Text(
                            Format.countdown(end),
                            style = MaterialTheme.typography.labelLarge,
                            color = when {
                                closed -> MaterialTheme.colorScheme.onSurfaceVariant
                                left != null && left <= 3600 -> Palette.Alert
                                else -> MaterialTheme.colorScheme.onSurface
                            },
                        )
                    },
                )
            }
            if (filtered.isEmpty()) {
                item {
                    Text(
                        if (markets.isEmpty()) "No markets yet. The scanner may still be running."
                        else "No market matches this filter.",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                }
            }
        }
    }
}

/** Settings, including the Security section. */
@Composable
fun SettingsScreen(
    state: AppUiState,
    info: InfoUiState,
    appLockEnabled: Boolean,
    appLockAvailable: Boolean,
    appLockTimeoutMinutes: Int,
    onLoadReport: () -> Unit,
    onLoadAudit: () -> Unit,
    onRaiseCeiling: (name: String, value: String, password: String, onResult: (Boolean, String) -> Unit) -> Unit,
    onSetAppLock: (enabled: Boolean, timeoutMinutes: Int) -> Unit,
    onLogout: () -> Unit,
    onOpenVenue: (url: String) -> Unit,
    onBack: () -> Unit,
) {
    var newCeiling by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }
    var ceilingNote by remember { mutableStateOf<String?>(null) }
    var ceilingBusy by remember { mutableStateOf(false) }
    var lockTimeout by remember { mutableStateOf(appLockTimeoutMinutes.toString()) }
    // AppLock is not observable, so the switch keeps its own state and writes through.
    var lockOn by remember(appLockEnabled) { mutableStateOf(appLockEnabled) }
    val capabilities = state.status.capabilities

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item { LinkText("← Back", onBack) }
        item {
            Card(
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Column(Modifier.padding(16.dp)) {
                    SectionHeader("Security")
                    ValueRow("Mode", state.status.mode, emphasise = true)
                    // The lock is only on when this device can actually open it.
                    ValueRow(
                        label = "App lock",
                        value = when {
                            !appLockAvailable -> "off (no biometric enrolled)"
                            lockOn -> "on · locks after $appLockTimeoutMinutes min"
                            else -> "off"
                        },
                    )
                    if (appLockAvailable) {
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.SpaceBetween,
                        ) {
                            Text("Lock the app", style = MaterialTheme.typography.bodyMedium)
                            Switch(
                                checked = lockOn,
                                onCheckedChange = { on ->
                                    lockOn = on
                                    onSetAppLock(on, lockTimeout.toIntOrNull()?.coerceIn(0, 60) ?: 5)
                                },
                            )
                        }
                        Spacer(Modifier.height(6.dp))
                        OutlinedTextField(
                            value = lockTimeout,
                            onValueChange = { lockTimeout = it },
                            label = { Text("Lock after (minutes, 0-60)") },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth(),
                        )
                        Spacer(Modifier.height(6.dp))
                        ConfirmButton(
                            "Save lock timeout",
                            enabled = lockTimeout.toIntOrNull() in 0..60,
                            onClick = { onSetAppLock(lockOn, lockTimeout.toIntOrNull() ?: 5) },
                        )
                    }
                    ValueRow("Per-trade cap", Format.money(state.status.maxTradeUsd))
                    ValueRow("Hard ceiling", Format.money(state.status.hardCeilingMaxTrade))
                    ValueRow("Withdrawal daily limit", "set on the server")
                    ValueRow("Hold window", "set on the server")
                    Spacer(Modifier.height(6.dp))
                    Text(
                        state.status.heartbeat.ifBlank {
                            "The Polymarket order heartbeat is off: enabling it would let a backend restart cancel every resting order."
                        },
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
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
                    SectionHeader("Venues", "${state.status.venues.size}")
                    if (state.status.venues.isEmpty()) {
                        Text("No venue data yet.", style = MaterialTheme.typography.bodyMedium)
                    }
                    state.status.venues.forEach { venue ->
                        val id = venue.optString("venue")
                        val ready = venue.optBoolean("ready")
                        ValueRow(
                            label = venue.optString("display_name").ifBlank { id.replaceFirstChar { it.uppercase() } } +
                                if (id == state.status.venue) " (active)" else "",
                            value = (if (ready) "ready" else "not ready") + " · " + venue.optString("mode", "PAPER"),
                            valueColor = if (ready) Palette.Long else Palette.Alert,
                        )
                        Text(
                            "doctor ${venue.optString("doctor_status")} · keys ${if (venue.optBoolean("credentialed")) "set" else "not set"}" +
                                if (!ready && venue.optString("reason").isNotBlank()) " · ${venue.optString("reason")}" else "",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        Spacer(Modifier.height(4.dp))
                    }
                }
            }
        }
        item {
            Card(
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Column(Modifier.padding(16.dp)) {
                    SectionHeader("Daily report")
                    ErrorNote(info.error)
                    info.report?.let {
                        SelectionContainer {
                            Text(
                                it,
                                style = MaterialTheme.typography.bodySmall,
                                fontFamily = FontFamily.Monospace,
                            )
                        }
                        Spacer(Modifier.height(8.dp))
                    }
                    ConfirmButton(
                        label = if (info.report == null) "Load today's report" else "Refresh report",
                        enabled = !info.loading,
                        busy = info.loading && info.report == null,
                        onClick = onLoadReport,
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
                    SectionHeader("Audit log", if (info.audit.isEmpty()) null else "newest ${minOf(info.audit.size, 30)}")
                    info.audit.take(30).forEach { entry ->
                        Text(
                            "${Format.dhaka(entry.optString("created_at"))} · ${entry.optString("actor")}",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        Text(entry.optString("action"), style = MaterialTheme.typography.bodyMedium, fontFamily = FontFamily.Monospace)
                        Spacer(Modifier.height(6.dp))
                    }
                    ConfirmButton(
                        label = if (info.audit.isEmpty()) "Load audit log" else "Refresh audit log",
                        enabled = !info.loading,
                        onClick = onLoadAudit,
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
                    SectionHeader("Raise the hard ceiling", "Step-up required")
                    OutlinedTextField(
                        value = newCeiling,
                        onValueChange = { newCeiling = it },
                        label = { Text("New per-trade cap (USD)") },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Spacer(Modifier.height(8.dp))
                    OutlinedTextField(
                        value = password,
                        onValueChange = { password = it },
                        label = { Text("Password for step-up (never stored)") },
                        singleLine = true,
                        visualTransformation = PasswordVisualTransformation(),
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Spacer(Modifier.height(10.dp))
                    ErrorNote(error)
                    ceilingNote?.let {
                        Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        Spacer(Modifier.height(6.dp))
                    }
                    ConfirmButton(
                        label = "Request step-up and raise",
                        enabled = newCeiling.isNotBlank() && password.isNotBlank() && !ceilingBusy,
                        onClick = {
                            error = null
                            ceilingNote = null
                            ceilingBusy = true
                            onRaiseCeiling("max_trade_usd", newCeiling.trim(), password) { ok, message ->
                                ceilingBusy = false
                                if (ok) {
                                    ceilingNote = message
                                    password = ""
                                    newCeiling = ""
                                } else {
                                    error = message
                                }
                            }
                        },
                    )
                    Spacer(Modifier.height(6.dp))
                    Text(
                        "Lowering a cap never needs a step-up.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
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
                    SectionHeader("Funds capability")
                    val venues = capabilities?.optJSONObject("venues")
                    if (venues != null) {
                        venues.keys().forEach { key ->
                            val caps = venues.optJSONObject(key) ?: return@forEach
                            val funds = caps.optJSONObject("funds")
                            ValueRow(
                                label = key.replaceFirstChar { it.uppercase() },
                                value = "deposit ${funds?.optBoolean("deposit")} · withdraw ${funds?.optBoolean("withdraw")}",
                            )
                        }
                    } else {
                        Text("No capability data yet.", style = MaterialTheme.typography.bodyMedium)
                    }
                }
            }
        }
        item {
            Card(
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Column(Modifier.padding(16.dp)) {
                    SectionHeader("Session")
                    Text(
                        "PAPER by default. LIVE is enabled only by editing LIVE_<VENUE> in the server environment.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Spacer(Modifier.height(10.dp))
                    ConfirmButton("Sign out", enabled = true, onClick = onLogout)
                }
            }
        }
    }
}
