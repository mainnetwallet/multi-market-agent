package com.mainnetwallet.mmagent.ui.event

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.unit.dp
import org.json.JSONObject

/**
 * binary and generic_list (and any layout the server invents later): one list that renders any EventView, so an
 * unknown shape still opens. Candidates and Up/Down have their own screens, as on the PWA.
 */
@Composable
fun GenericScreen(view: JSONObject, range: String, onRange: (String) -> Unit, onTrade: OnTrade) {
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        EventChart(view, range, onRange)
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            val sections = filledSections(view)
            sections.forEach { sec ->
                if (sections.size > 1) {
                    Text(sec.text("label"), style = MaterialTheme.typography.titleSmall)
                }
                val rows = sec.optJSONArray("rows")
                for (i in 0 until (rows?.length() ?: 0)) rows?.optJSONObject(i)?.let { MarketRow(it, onTrade) }
            }
        }
        Text(volumeText(view), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}
