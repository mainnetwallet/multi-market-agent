package com.mainnetwallet.mmagent.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import com.mainnetwallet.mmagent.data.AppUiState
import com.mainnetwallet.mmagent.data.Format
import com.mainnetwallet.mmagent.data.ProposalRules
import org.json.JSONObject

/** The proposals the operator can still act on, newest first. */
fun pendingProposals(proposals: List<JSONObject>): List<JSONObject> =
    proposals
        .filter { ProposalRules.actionable(it.optString("status"), it.optString("expires_at").ifBlank { null }) }
        .sortedByDescending { it.optString("created_at") }

private fun JSONObject.strings(key: String): List<String> {
    val array = optJSONArray(key) ?: return emptyList()
    return (0 until array.length()).map { array.optString(it) }.filter { it.isNotBlank() }
}

/**
 * One trade the agent wants to make.
 *
 * Approving adds risk, so on a LIVE venue it is press-and-hold. In PAPER it is a
 * normal tap. Rejecting places nothing, so it is always a single tap.
 */
@Composable
fun ProposalCard(
    proposal: JSONObject,
    live: Boolean,
    online: Boolean,
    onApprove: (id: String, done: () -> Unit) -> Unit,
    onReject: (id: String, done: () -> Unit) -> Unit,
) {
    val id = proposal.optString("id")
    var busy by remember(id) { mutableStateOf(false) }
    val outcome = proposal.optString("outcome")
    val reasons = proposal.strings("reasons").take(2)
    val risks = proposal.strings("risks").take(2)

    Card(
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(14.dp)) {
            Text(
                proposal.optString("market_title").ifBlank { proposal.optString("market_id") },
                style = MaterialTheme.typography.titleSmall,
            )
            Spacer(Modifier.height(2.dp))
            Text(
                "${proposal.optString("side", "BUY")} $outcome · ${proposal.optString("venue")} · " +
                    "expires ${Format.relative(proposal.optString("expires_at"))}",
                style = MaterialTheme.typography.bodySmall,
                color = if (outcome == "YES") Palette.Long else Palette.Short,
            )
            Spacer(Modifier.height(8.dp))
            ValueRow("Price", Format.cents(proposal.optString("price")))
            ValueRow("Amount", Format.money(proposal.optString("amount_usd")))
            ValueRow("Fee", Format.money(proposal.optString("fee_usd")))
            ValueRow("Edge", Format.cents(proposal.optString("edge")), valueColor = Palette.Long)
            ValueRow("Fair value", Format.cents(proposal.optString("p_fair")))
            if (reasons.isNotEmpty()) {
                Spacer(Modifier.height(6.dp))
                reasons.forEach {
                    Text("＋ $it", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
            if (risks.isNotEmpty()) {
                risks.forEach {
                    Text("！ $it", style = MaterialTheme.typography.bodySmall, color = Palette.Alert)
                }
            }
            Spacer(Modifier.height(10.dp))
            // An unreachable server means the price on screen is unknown: no approving.
            if (live) {
                HoldToConfirmButton(
                    label = "Hold to approve (LIVE)",
                    enabled = online && !busy,
                    busy = busy,
                    danger = true,
                    onConfirmed = {
                        busy = true
                        onApprove(id) { busy = false }
                    },
                )
            } else {
                ConfirmButton(
                    label = "Approve (paper)",
                    enabled = online,
                    busy = busy,
                    onClick = {
                        busy = true
                        onApprove(id) { busy = false }
                    },
                )
            }
            Spacer(Modifier.height(8.dp))
            OutlinedButton(
                onClick = {
                    busy = true
                    onReject(id) { busy = false }
                },
                enabled = !busy,
                modifier = Modifier.fillMaxWidth(),
            ) { Text("Reject") }
        }
    }
}

/** Portfolio → Proposals tab. */
@Composable
fun ProposalsList(
    state: AppUiState,
    onApprove: (id: String, done: () -> Unit) -> Unit,
    onReject: (id: String, done: () -> Unit) -> Unit,
) {
    val pending = pendingProposals(state.proposals)
    val history = state.proposals.filterNot { p -> pending.any { it.optString("id") == p.optString("id") } }
    val live = state.status.mode.equals("LIVE", ignoreCase = true)

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item { SectionHeader("Needs your decision", "${pending.size}") }
        if (pending.isEmpty()) {
            item {
                Text(
                    if (state.status.paused) "The agent is paused, so no new proposals are made."
                    else "Nothing waiting. A proposal appears here when the agent finds an edge; it expires after 5 minutes.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
        items(pending, key = { it.optString("id") }) { proposal ->
            ProposalCard(proposal, live, state.online, onApprove, onReject)
        }
        if (history.isNotEmpty()) {
            item { SectionHeader("Earlier", "${history.size}") }
            items(history.take(20), key = { "h" + it.optString("id") }) { proposal ->
                RowCard(
                    title = proposal.optString("market_title").ifBlank { proposal.optString("market_id") },
                    subtitle = "${proposal.optString("outcome")} · ${Format.money(proposal.optString("amount_usd"))} · ${proposal.optString("status")}",
                    detail = proposal.optString("error").ifBlank { null },
                )
            }
        }
    }
}
