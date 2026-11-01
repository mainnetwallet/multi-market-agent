package com.mainnetwallet.mmagent.ui.event

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import org.json.JSONObject

/**
 * bracket_ladder: several markets with ordered thresholds ("$200B or above" ... ). The rows arrive already ordered
 * by the server (ascending thresholds); each shows the midpoint chance, the 24h move and Buy Yes / Buy No at the ask,
 * above one multi-line chart.
 */
@Composable
fun BracketScreen(view: JSONObject, range: String, onRange: (String) -> Unit, onTrade: OnTrade) {
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        EventChart(view, range, onRange)
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            allRows(view).forEach { MarketRow(it, onTrade) }
        }
        Text(volumeText(view), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}
