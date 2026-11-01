package com.mainnetwallet.mmagent.ui.event

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.mainnetwallet.mmagent.data.Format
import com.mainnetwallet.mmagent.ui.PriceButton
import com.mainnetwallet.mmagent.ui.Palette
import org.json.JSONObject

/** The three outcomes of a game's moneyline, found by name (never by order): home, draw, away. */
internal data class Moneyline(val home: JSONObject?, val draw: JSONObject?, val away: JSONObject?) {
    fun hero(): Set<String> = listOfNotNull(home, draw, away).map { it.optString("market_id") }.toSet()
}

internal fun classifyMoneyline(view: JSONObject): Moneyline {
    val game = view.optJSONObject("game") ?: JSONObject()
    val rows = allRows(view).filter { it.text("market_type") == "moneyline" }
    fun norm(s: String) = s.trim().lowercase()
    fun team(key: String): JSONObject? {
        val t = game.optJSONObject(key) ?: return null
        return rows.firstOrNull { norm(it.text("label")) == norm(t.text("name")) || norm(it.text("label")) == norm(t.text("abbr")) }
    }
    val draw = rows.firstOrNull { Regex("draw|tie", RegexOption.IGNORE_CASE).containsMatchIn(it.text("label")) }
    return Moneyline(team("home"), draw, team("away"))
}

/**
 * game_3way (Home / Draw / Away) and game_2way: scoreboard, hero buttons and one tab per non-empty section.
 * The chance is the midpoint, the button shows the ask.
 */
@Composable
fun GameScreen(view: JSONObject, range: String, onRange: (String) -> Unit, onTrade: OnTrade) {
    val game = view.optJSONObject("game")
    val ml = classifyMoneyline(view)
    val sections = filledSections(view)
    var tab by remember { mutableIntStateOf(0) }
    val shown = sections.getOrNull(tab.coerceIn(0, (sections.size - 1).coerceAtLeast(0)))

    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        if (game != null) Scoreboard(view, game)
        if (ml.hero().isNotEmpty()) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                val labelFor = { row: JSONObject?, fallback: String ->
                    row?.text("label")?.ifBlank { null } ?: fallback
                }
                listOf(ml.home to "Home", ml.draw to "Draw", ml.away to "Away").forEach { (row, fallback) ->
                    if (row != null) {
                        val yes = row.side("yes")
                        PriceButton(
                            label = labelFor(row, fallback),
                            price = yes.text("ask"),
                            color = if (fallback == "Draw") Palette.Neutral else Palette.Long,
                            modifier = Modifier.weight(1f),
                        ) { if (row.text("status").let { it.isBlank() || it == "open" }) onTrade(row.optString("market_id"), labelFor(row, fallback), "YES") }
                    }
                }
            }
        }
        SectionTabs(sections, tab) { tab = it }
        // Rows already shown as hero buttons stay in their tab too: the hero is a shortcut, not a move.
        shown?.let { sec ->
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                com.mainnetwallet.mmagent.data.ApiClient.arrayOf(sec, "rows").let { arr ->
                    (0 until arr.length()).mapNotNull { arr.optJSONObject(it) }.forEach { MarketRow(it, onTrade) }
                }
            }
        }
        EventChart(view, range, onRange)
        Text(volumeText(view), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable
private fun Scoreboard(view: JSONObject, game: JSONObject) {
    val home = game.optJSONObject("home") ?: JSONObject()
    val away = game.optJSONObject("away") ?: JSONObject()
    val ended = game.optBoolean("ended", false) || view.text("status") in setOf("ended", "resolved")
    val whenText = when {
        ended -> "Final"
        game.optBoolean("live", false) -> listOfNotNull(
            game.text("period").ifBlank { "LIVE" },
            game.text("elapsed").takeIf { it.isNotBlank() }?.let { "$it'" },
        ).joinToString(" ")
        view.text("start_time").isNotBlank() -> Format.dhaka(view.text("start_time"))
        else -> ""
    }
    Column(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(MaterialTheme.colorScheme.surface).padding(14.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(whenText, style = MaterialTheme.typography.labelLarge, color = if (game.optBoolean("live", false)) Palette.Long else MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(8.dp))
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Team(home, Modifier.weight(1f))
            Text(
                listOf(home, away).joinToString(" – ") { if (it.isNull("score")) "–" else it.optString("score") },
                style = MaterialTheme.typography.headlineMedium,
                fontWeight = FontWeight.Bold,
            )
            Team(away, Modifier.weight(1f))
        }
    }
}

@Composable
private fun Team(team: JSONObject, modifier: Modifier) {
    Column(modifier, horizontalAlignment = Alignment.CenterHorizontally) {
        Crest(team.text("name"), team.text("logo"), 48)
        Spacer(Modifier.height(4.dp))
        Text(team.text("name"), style = MaterialTheme.typography.labelLarge, textAlign = TextAlign.Center, maxLines = 2)
    }
}
