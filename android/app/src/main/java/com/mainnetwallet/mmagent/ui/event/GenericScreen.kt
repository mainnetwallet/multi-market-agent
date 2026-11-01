package com.mainnetwallet.mmagent.ui.event

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.mainnetwallet.mmagent.data.Format
import org.json.JSONObject

/**
 * multi_candidates, updown_window, binary and generic_list share this one list: it renders any EventView, so an unknown
 * shape still opens. Up/Down also shows the live spot price; the price to beat comes from `raw_extra.crypto` when sent.
 * (The PWA has dedicated candidate and Up/Down layouts; these are a deliberate first cut, logged in WORKLOG.md.)
 */
@Composable
fun GenericScreen(view: JSONObject, range: String, onRange: (String) -> Unit, onTrade: OnTrade) {
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        if (view.text("layout") == "updown_window") SpotCard(view)
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

@Composable
private fun SpotCard(view: JSONObject) {
    val crypto = view.optJSONObject("raw_extra")?.optJSONObject("crypto")
    val spot = view.optJSONObject("spot")?.text("price").orEmpty()
    val beat = crypto?.text("price_to_beat").orEmpty()
    val diff = runCatching { java.math.BigDecimal(spot).subtract(java.math.BigDecimal(beat)) }.getOrNull()
    Row(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(MaterialTheme.colorScheme.surface).padding(14.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Column {
            Text("Price to beat", style = MaterialTheme.typography.bodySmall)
            Text(if (beat.isBlank()) "—" else Format.money(beat), fontWeight = FontWeight.Bold)
        }
        Column {
            Text("Current " + crypto?.text("symbol").orEmpty(), style = MaterialTheme.typography.bodySmall)
            Text(if (spot.isBlank()) "—" else Format.money(spot), fontWeight = FontWeight.Bold)
            if (diff != null) Text((if (diff.signum() >= 0) "▲ " else "▼ ") + Format.money(diff.abs().toPlainString()), style = MaterialTheme.typography.bodySmall)
        }
    }
}
