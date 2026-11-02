package com.mainnetwallet.mmagent.ui.event

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.unit.dp
import com.mainnetwallet.mmagent.data.EventLayout
import org.json.JSONObject

/**
 * multi_candidates: several Yes/No outcomes, highest chance first, each with a crest (initials when there is no logo),
 * under one multi-line chart. Mirrors static/js/views/candidates.js. The server sorts too, but a live patch changes
 * chances in place, so the order is recomputed on every redraw; the long question under each label is left out.
 * An event with a `game` block (Kalshi sports scores) gets a one-line score above the chart.
 */
@Composable
fun CandidatesScreen(view: JSONObject, range: String, onRange: (String) -> Unit, onTrade: OnTrade) {
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        EventLayout.scoreLine(view.optJSONObject("game"))?.let {
            Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        EventChart(view, range, onRange)
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            EventLayout.byChance(allRows(view)).forEach { MarketRow(it, onTrade, crest = true, sub = false) }
        }
        Text(volumeText(view), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}
