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
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.unit.dp
import com.mainnetwallet.mmagent.data.AppUiState
import com.mainnetwallet.mmagent.data.Format
import org.json.JSONObject

/** Markets: the list the operator picks a market from. */
@Composable
fun MarketsScreen(
    markets: List<JSONObject>,
    onOpen: (marketId: String, title: String) -> Unit,
) {
    var query by remember { mutableStateOf("") }
    val filtered = markets.filter { market ->
        query.isBlank() || market.optString("title").contains(query, ignoreCase = true)
    }

    Column(modifier = Modifier.fillMaxSize()) {
        OutlinedTextField(
            value = query,
            onValueChange = { query = it },
            label = { Text("Search markets") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth().padding(16.dp),
        )
        LazyColumn(
            modifier = Modifier.fillMaxSize(),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(start = 16.dp, end = 16.dp, bottom = 16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            item { SectionHeader("Markets", "${filtered.size}") }
            items(filtered) { market ->
                RowCard(
                    title = market.optString("title"),
                    subtitle = "${market.optString("venue")} · ${market.optString("category")}",
                    detail = "ends ${Format.dhaka(market.optString("end_time"))} · tick ${market.optString("tick_size")}",
                    onClick = { onOpen(market.optString("native_id"), market.optString("title")) },
                )
            }
            if (filtered.isEmpty()) {
                item { Text("No markets yet. The scanner may still be running.", style = MaterialTheme.typography.bodyMedium) }
            }
        }
    }
}

/** Settings, including the Security section. */
@Composable
fun SettingsScreen(
    state: AppUiState,
    appLockEnabled: Boolean,
    appLockAvailable: Boolean,
    appLockTimeoutMinutes: Int,
    onRaiseCeiling: (name: String, value: String, stepupToken: String?) -> Unit,
    onSetAppLock: (enabled: Boolean, timeoutMinutes: Int) -> Unit,
    onLogout: () -> Unit,
    onOpenVenue: (url: String) -> Unit,
) {
    var newCeiling by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }
    var lockTimeout by remember { mutableStateOf(appLockTimeoutMinutes.toString()) }
    val capabilities = state.status.capabilities

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
                    SectionHeader("Security")
                    ValueRow("Mode", state.status.mode, emphasise = true)
                    // The lock is only on when this device can actually open it.
                    ValueRow(
                        label = "App lock",
                        value = when {
                            !appLockAvailable -> "off (no biometric enrolled)"
                            appLockEnabled -> "on · locks after $appLockTimeoutMinutes min"
                            else -> "off"
                        },
                    )
                    if (appLockAvailable) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text(
                                if (appLockEnabled) "☑" else "☐",
                                style = MaterialTheme.typography.titleMedium,
                            )
                            Spacer(Modifier.width(8.dp))
                            Text(
                                "Lock the app",
                                style = MaterialTheme.typography.bodyMedium,
                                modifier = Modifier
                                    .clip(RoundedCornerShape(8.dp))
                                    .clickable { onSetAppLock(!appLockEnabled, lockTimeout.toIntOrNull() ?: 5) }
                                    .padding(6.dp),
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
                            onClick = { onSetAppLock(appLockEnabled, lockTimeout.toIntOrNull() ?: 5) },
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
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Spacer(Modifier.height(10.dp))
                    ErrorNote(error)
                    ConfirmButton(
                        label = "Request step-up and raise",
                        enabled = newCeiling.isNotBlank() && password.isNotBlank(),
                        onClick = {
                            error = null
                            onRaiseCeiling("max_trade_usd", newCeiling, null)
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
