package com.mainnetwallet.mmagent.data

import java.math.BigDecimal
import org.json.JSONObject

/**
 * Pure helpers behind the Candidates and Up/Down screens. No Compose and no Android classes, so they run in plain JVM
 * unit tests. Numbers stay decimal text or [BigDecimal]; nothing here uses a float.
 */
object EventLayout {
    private val UNREADABLE = BigDecimal("-1")

    /** Highest chance first. A row without a readable chance goes last; ties keep the order they arrived in. */
    fun byChance(rows: List<JSONObject>): List<JSONObject> =
        rows.sortedWith(compareByDescending<JSONObject> { chanceOf(it) })

    private fun chanceOf(row: JSONObject): BigDecimal =
        if (row.isNull("chance")) UNREADABLE else (row.optString("chance").toBigDecimalOrNull() ?: UNREADABLE)

    /**
     * "m:ss" left in the window, never negative. [endTs] is the window's end in epoch seconds as text (the server's
     * `raw_extra.crypto.end_ts`); null when it is missing or not a number. Mirrors `countdown()` in views/updown.js.
     */
    fun countdown(endTs: String?, nowMillis: Long): String? {
        val end = endTs?.trim()?.toBigDecimalOrNull() ?: return null
        val left = maxOf(0L, end.toLong() - nowMillis / 1000L)
        return "${left / 60L}:${(left % 60L).toString().padStart(2, '0')}"
    }

    /**
     * One line for an event that carries a `game` block outside the game layouts (Kalshi sports scores): "Boston 91 – 88
     * Los Angeles L · Q3 · score: kalshi_rest_polled". Null when there is no game or no two teams. Mirrors scoreText()
     * in views/candidates.js.
     */
    fun scoreLine(game: JSONObject?): String? {
        if (game == null) return null
        val home = game.optJSONObject("home") ?: return null
        val away = game.optJSONObject("away") ?: return null
        val period = text(game, "period")
        val source = text(game, "source")
        return text(home, "name").ifBlank { "Home" } + " " + score(home) + " – " + score(away) + " " +
            text(away, "name").ifBlank { "Away" } +
            (if (period.isBlank()) "" else " · $period") +
            (if (source.isBlank()) "" else " · score: $source")
    }

    private fun score(team: JSONObject): String = if (team.isNull("score")) "–" else team.optString("score")

    private fun text(o: JSONObject, key: String): String = if (o.isNull(key)) "" else o.optString(key)

    /** Live spot minus the price to beat; null when either is missing or not a number. Positive means "up" is ahead. */
    fun spotDiff(spot: String?, beat: String?): BigDecimal? {
        val s = spot?.trim()?.toBigDecimalOrNull() ?: return null
        val b = beat?.trim()?.toBigDecimalOrNull() ?: return null
        return s.subtract(b)
    }
}
