package com.mainnetwallet.mmagent.ui.event

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import com.mainnetwallet.mmagent.data.ApiClient
import com.mainnetwallet.mmagent.data.Format
import com.mainnetwallet.mmagent.data.LiveIndicator
import com.mainnetwallet.mmagent.data.objects
import com.mainnetwallet.mmagent.ui.ChoiceChip
import com.mainnetwallet.mmagent.ui.Palette
import com.mainnetwallet.mmagent.ui.PriceButton
import org.json.JSONObject

/** Opens the existing trade sheet pre-set to one outcome of one market. No order code lives in these screens. */
typealias OnTrade = (marketId: String, title: String, outcome: String) -> Unit

internal fun JSONObject.text(key: String): String = if (isNull(key)) "" else optString(key)

/** Every row of every section, in order, once per market. */
internal fun allRows(view: JSONObject): List<JSONObject> =
    ApiClient.arrayOf(view, "sections").objects().flatMap { ApiClient.arrayOf(it, "rows").objects() }.distinctBy { it.optString("market_id") }

/** Sections that have at least one row: an empty tab is never shown. */
internal fun filledSections(view: JSONObject): List<JSONObject> =
    ApiClient.arrayOf(view, "sections").objects().filter { ApiClient.arrayOf(it, "rows").length() > 0 }

internal fun JSONObject.side(key: String): JSONObject = optJSONObject(key) ?: JSONObject()

/** LIVE / DELAYED / OFFLINE with the data age and source, on every event view. */
@Composable
fun LiveBadge(indicator: LiveIndicator, modifier: Modifier = Modifier) {
    val (label, color) = when (indicator.state) {
        LiveIndicator.State.LIVE -> "LIVE" to Palette.Long
        LiveIndicator.State.DELAYED -> "DELAYED" to Color(0xFFF59E0B)
        LiveIndicator.State.OFFLINE -> "OFFLINE" to Palette.Short
    }
    Column(modifier) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(8.dp).clip(CircleShape).background(color))
            Spacer(Modifier.width(6.dp))
            Text(label, color = color, style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.Bold)
            val detail = listOfNotNull(
                indicator.source.takeIf { it.isNotBlank() },
                indicator.ageS?.let { "${it}s ago" },
            ).joinToString(" · ")
            if (detail.isNotBlank()) {
                Spacer(Modifier.width(8.dp))
                Text(detail, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        if (indicator.note.isNotBlank()) {
            Text(indicator.note, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

/** A crest or logo. Only https images load; anything else (or no image) shows initials. */
@Composable
fun Crest(name: String, logo: String, size: Int = 40) {
    val initials = name.split(' ', '-').filter { it.isNotBlank() }.take(2).joinToString("") { it.first().uppercase() }.ifBlank { "?" }
    Box(
        Modifier.size(size.dp).clip(CircleShape).background(MaterialTheme.colorScheme.surfaceVariant),
        contentAlignment = Alignment.Center,
    ) {
        Text(initials, style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.onSurfaceVariant)
        if (logo.startsWith("https://")) {
            AsyncImage(model = logo, contentDescription = null, contentScale = ContentScale.Fit, modifier = Modifier.size(size.dp))
        }
    }
}

/** Buttons show the ask (what a buy pays); the chance shown beside them is the midpoint. Disabled once the market is not open. */
@Composable
fun YesNoButtons(row: JSONObject, onTrade: OnTrade, modifier: Modifier = Modifier) {
    val open = row.text("status").let { it.isBlank() || it == "open" }
    val id = row.optString("market_id")
    val title = row.text("label").ifBlank { row.text("sub_label") }
    val yes = row.side("yes")
    val no = row.side("no")
    Row(modifier, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        PriceButton(yes.text("label").ifBlank { "Yes" }, yes.text("ask"), Palette.Long, Modifier.width(88.dp).alpha(if (open) 1f else 0.4f)) {
            if (open) onTrade(id, title, "YES")
        }
        PriceButton(no.text("label").ifBlank { "No" }, no.text("ask"), Palette.Short, Modifier.width(88.dp).alpha(if (open) 1f else 0.4f)) {
            if (open) onTrade(id, title, "NO")
        }
    }
}

/** One market row: label, chance (midpoint), 24h move, Yes/No at the ask. */
@Composable
fun MarketRow(row: JSONObject, onTrade: OnTrade) {
    Column(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(MaterialTheme.colorScheme.surface).padding(12.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            if (row.has("logo") && row.text("logo").isNotBlank()) {
                Crest(row.text("label"), row.text("logo"), 32)
                Spacer(Modifier.width(10.dp))
            }
            Column(Modifier.weight(1f)) {
                Text(row.text("label"), style = MaterialTheme.typography.bodyMedium, maxLines = 2, overflow = TextOverflow.Ellipsis)
                row.text("sub_label").takeIf { it.isNotBlank() }?.let {
                    Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
            Column(horizontalAlignment = Alignment.End) {
                Text(Format.percent(row.text("chance").ifBlank { null }), style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                Format.move(row.text("change_1d").ifBlank { null })?.let {
                    Text(it, style = MaterialTheme.typography.bodySmall, color = if (it.startsWith("-")) Palette.Short else Palette.Long)
                }
            }
        }
        Spacer(Modifier.height(8.dp))
        YesNoButtons(row, onTrade, Modifier.fillMaxWidth())
    }
}

/** Section tabs. Empty sections are filtered out before this is called. */
@Composable
fun SectionTabs(sections: List<JSONObject>, selected: Int, onSelect: (Int) -> Unit) {
    if (sections.size < 2) return
    LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        items(sections.size) { i ->
            ChoiceChip(sections[i].text("label").ifBlank { "Markets" }, selected = i == selected, onClick = { onSelect(i) })
        }
    }
}

/**
 * Multi-line chart of the midpoint history, drawn with Compose Canvas (no chart library). Series are `[ts, "0.35"]`
 * points; at most the first four lines are drawn. No points means no chart and a plain note, never a fake line.
 */
@Composable
fun EventChart(view: JSONObject, range: String, onRange: (String) -> Unit) {
    val chart = view.optJSONObject("chart") ?: return
    val series = ApiClient.arrayOf(chart, "series").objects().take(4)
    val lines = series.map { s ->
        val pts = s.optJSONArray("points")
        (0 until (pts?.length() ?: 0)).mapNotNull { i ->
            val p = pts?.optJSONArray(i) ?: return@mapNotNull null
            val x = p.optDouble(0, Double.NaN)
            val y = p.optString(1).toDoubleOrNull() ?: return@mapNotNull null
            if (x.isNaN()) null else x to y
        }
    }
    val colors = listOf(Palette.Neutral, Palette.Long, Color(0xFFF59E0B), Palette.Short)
    val all = lines.flatten()
    Column(Modifier.fillMaxWidth()) {
        if (all.size < 2) {
            Text(
                "History is not available for this venue.".takeIf { chart.optJSONArray("ranges") == null }
                    ?: "No price history yet.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        } else {
            val minX = all.minOf { it.first }
            val maxX = all.maxOf { it.first }
            val minY = all.minOf { it.second }
            val maxY = all.maxOf { it.second }
            Canvas(Modifier.fillMaxWidth().height(140.dp)) {
                val w = size.width
                val h = size.height
                val dx = (maxX - minX).takeIf { it > 0 } ?: 1.0
                val dy = (maxY - minY).takeIf { it > 0 } ?: 1.0
                lines.forEachIndexed { i, pts ->
                    if (pts.size < 2) return@forEachIndexed
                    val path = Path()
                    pts.forEachIndexed { k, (x, y) ->
                        val px = ((x - minX) / dx * w).toFloat()
                        val py = (h - (y - minY) / dy * h).toFloat()
                        if (k == 0) path.moveTo(px, py) else path.lineTo(px, py)
                    }
                    drawPath(path, colors[i % colors.size], style = Stroke(width = 3f))
                }
                drawLine(Color.Gray.copy(alpha = 0.3f), Offset(0f, h), Offset(w, h))
            }
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.padding(top = 4.dp)) {
                series.forEachIndexed { i, s ->
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Box(Modifier.size(8.dp).clip(CircleShape).background(colors[i % colors.size]))
                        Spacer(Modifier.width(4.dp))
                        Text(s.text("label"), style = MaterialTheme.typography.bodySmall, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    }
                }
            }
        }
        val ranges = chart.optJSONArray("ranges")
        if (ranges != null && ranges.length() > 0) {
            Spacer(Modifier.height(6.dp))
            LazyRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                items((0 until ranges.length()).map { ranges.optString(it) }) { r ->
                    ChoiceChip(r, selected = r == range, onClick = { onRange(r) })
                }
            }
        }
    }
}

/** Volume footer in the venue's own unit (Kalshi counts contracts, never dollars). */
internal fun volumeText(view: JSONObject): String =
    if (view.text("volume_unit") == "contracts") Format.count(view.text("volume")) + " contracts" else Format.volume(view.text("volume")) + " Vol"
