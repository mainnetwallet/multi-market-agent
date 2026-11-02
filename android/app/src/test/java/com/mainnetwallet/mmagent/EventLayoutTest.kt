package com.mainnetwallet.mmagent

import com.mainnetwallet.mmagent.data.EventLayout
import java.math.BigDecimal
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** The pure helpers behind CandidatesScreen and UpDownScreen. Mirrors the sorting and countdown in static/js/views. */
class EventLayoutTest {
    private fun row(id: String, chance: String?): JSONObject {
        val o = JSONObject().put("market_id", id)
        if (chance != null) o.put("chance", chance)
        return o
    }

    private fun ids(rows: List<JSONObject>) = rows.map { it.getString("market_id") }

    @Test
    fun candidatesAreSortedByChanceHighestFirst() {
        val sorted = EventLayout.byChance(listOf(row("a", "0.12"), row("b", "0.48"), row("c", "0.30")))
        assertEquals(listOf("b", "c", "a"), ids(sorted))
    }

    @Test
    fun tiesKeepTheirOrderAndUnreadableChancesGoLast() {
        val sorted = EventLayout.byChance(
            listOf(row("x", null), row("a", "0.30"), row("junk", "n/a"), row("b", "0.30"), row("z", "0.9")),
        )
        assertEquals(listOf("z", "a", "b", "x", "junk"), ids(sorted))
    }

    @Test
    fun aLivePatchThatChangesChancesChangesTheOrder() {
        val rows = listOf(row("a", "0.48"), row("b", "0.30"))
        assertEquals(listOf("a", "b"), ids(EventLayout.byChance(rows)))
        rows[1].put("chance", "0.55")
        assertEquals(listOf("b", "a"), ids(EventLayout.byChance(rows)))
    }

    @Test
    fun countdownIsMinutesAndSecondsAndNeverNegative() {
        val now = 1_793_000_000_000L
        assertEquals("5:00", EventLayout.countdown("1793000300", now))
        assertEquals("0:07", EventLayout.countdown("1793000007", now))
        assertEquals("0:00", EventLayout.countdown("1792999000", now))
        assertEquals("61:01", EventLayout.countdown(" 1793003661 ", now))
    }

    @Test
    fun countdownIsNullWithoutAReadableEndTime() {
        assertNull(EventLayout.countdown(null, 1L))
        assertNull(EventLayout.countdown("", 1L))
        assertNull(EventLayout.countdown("soon", 1L))
    }

    @Test
    fun spotDiffIsSignedAndExactDecimal() {
        assertEquals(BigDecimal("12.50"), EventLayout.spotDiff("100012.50", "100000"))
        assertEquals(BigDecimal("-0.25"), EventLayout.spotDiff("99.75", "100.00"))
        assertNull(EventLayout.spotDiff("", "100"))
        assertNull(EventLayout.spotDiff("100", null))
        assertNull(EventLayout.spotDiff("x", "100"))
    }

    private fun game(homeScore: Any?, awayScore: Any?, period: String? = "Q3", source: String? = "kalshi_rest_polled"): JSONObject {
        val g = JSONObject()
            .put("home", JSONObject().put("name", "Boston").put("score", homeScore))
            .put("away", JSONObject().put("name", "Los Angeles L").put("score", awayScore))
        if (period != null) g.put("period", period)
        if (source != null) g.put("source", source)
        return g
    }

    @Test
    fun scoreLineNamesTheTeamsTheScoresThePeriodAndTheSource() {
        assertEquals(
            "Boston 91 – 88 Los Angeles L · Q3 · score: kalshi_rest_polled",
            EventLayout.scoreLine(game(91, 88)),
        )
    }

    @Test
    fun scoreLineUsesADashForAMissingScoreAndSkipsEmptyParts() {
        assertEquals("Boston – – 4 Los Angeles L", EventLayout.scoreLine(game(null, 4, period = null, source = null)))
    }

    @Test
    fun scoreLineIsNullWithoutAGameOrWithoutTwoTeams() {
        assertNull(EventLayout.scoreLine(null))
        assertNull(EventLayout.scoreLine(JSONObject().put("home", JSONObject().put("name", "Boston"))))
    }
}
