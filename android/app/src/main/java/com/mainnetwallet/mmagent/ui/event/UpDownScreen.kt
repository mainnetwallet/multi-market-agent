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
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.mainnetwallet.mmagent.data.EventLayout
import com.mainnetwallet.mmagent.data.Format
import kotlinx.coroutines.delay
import org.json.JSONObject

/**
 * updown_window: time left, the price to beat against the live spot, then Up / Down at the ask, then the chart.
 * Mirrors static/js/views/updown.js. The price to beat and the window end come from `raw_extra.crypto`, the spot from
 * `view.spot` (the server labels its source). The only timer here is a one-second UI clock for the countdown: nothing
 * is fetched in it. The "next window" chips of the PWA are not built on Android (logged in WORKLOG.md).
 */
@Composable
fun UpDownScreen(view: JSONObject, range: String, onRange: (String) -> Unit, onTrade: OnTrade) {
    val crypto = view.optJSONObject("raw_extra")?.optJSONObject("crypto")
    val spot = view.optJSONObject("spot")?.text("price").orEmpty()
    val beat = crypto?.text("price_to_beat").orEmpty()
    val endTs = crypto?.text("end_ts").orEmpty()
    val diff = EventLayout.spotDiff(spot, beat)
    var nowMillis by remember { mutableStateOf(System.currentTimeMillis()) }
    LaunchedEffect(endTs) {
        while (endTs.isNotBlank()) {
            nowMillis = System.currentTimeMillis()
            delay(1_000)
        }
    }
    val row = allRows(view).firstOrNull()
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Row(
            Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(MaterialTheme.colorScheme.surface).padding(14.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            Column {
                Text("Time left", style = MaterialTheme.typography.bodySmall)
                Text(EventLayout.countdown(endTs, nowMillis) ?: "—", fontWeight = FontWeight.Bold)
            }
            Column {
                Text("Price to beat", style = MaterialTheme.typography.bodySmall)
                Text(if (beat.isBlank()) "—" else Format.money(beat), fontWeight = FontWeight.Bold)
            }
            Column {
                Text("Current " + crypto?.text("symbol").orEmpty(), style = MaterialTheme.typography.bodySmall)
                Text(if (spot.isBlank()) "—" else Format.money(spot), fontWeight = FontWeight.Bold)
                if (diff != null) {
                    Text(
                        (if (diff.signum() >= 0) "▲ " else "▼ ") + Format.money(diff.abs().toPlainString()),
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
        }
        if (row != null) {
            Text(
                Format.percent(row.text("chance").ifBlank { null }),
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold,
            )
            YesNoButtons(row, onTrade, Modifier.fillMaxWidth())
        }
        EventChart(view, range, onRange)
        val source = view.optJSONObject("spot")?.text("source").orEmpty()
        Text(
            volumeText(view) + if (source.isBlank()) "" else " · spot: $source",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}
