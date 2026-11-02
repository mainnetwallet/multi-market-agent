package com.mainnetwallet.mmagent

import com.mainnetwallet.mmagent.data.EventStore
import com.mainnetwallet.mmagent.data.EventStore.Action
import com.mainnetwallet.mmagent.data.EventTarget
import com.mainnetwallet.mmagent.data.LiveIndicator
import com.mainnetwallet.mmagent.data.LiveIndicator.State
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The snapshot/patch reducer behind the Android event views. Mirrors tests/js/live.test.mjs on the PWA side. */
class EventStoreTest {
    private fun view(game: Boolean = true, stale: Boolean = false): JSONObject = JSONObject(
        """
        {"venue":"polymarket","event_id":"E1","layout":"${if (game) "game_3way" else "bracket_ladder"}","status":"live",
         ${if (game) """"game":{"live":true,"period":"1H","elapsed":"12","home":{"name":"Arsenal","score":0},"away":{"name":"Chelsea","score":0}},""" else ""}
         "freshness":{"source":"ws","stale":$stale},
         "sections":[{"id":"ml","label":"Game Lines","rows":[
           {"market_id":"m1","label":"Arsenal","chance":"0.50","yes":{"ask":"0.51","bid":"0.49"},"no":{"ask":"0.51","bid":"0.49"}},
           {"market_id":"m2","label":"Draw","chance":"0.25","yes":{"ask":"0.26","bid":"0.24"},"no":{"ask":"0.76","bid":"0.74"}}]}],
         "chart":{"series":[{"row_id":"m1","label":"Arsenal","points":[[1,"0.40"],[2,"0.45"]]},{"row_id":"m2","label":"Draw","points":[]}]}}
        """.trimIndent(),
    )

    private fun snapshot(seq: Long = 5, v: JSONObject = view()) =
        JSONObject().put("type", "snapshot").put("seq", seq).put("source", "ws").put("view", v)

    private fun patch(seq: Long, body: String) = JSONObject("""{"type":"patch","seq":$seq,"patch":$body}""")

    private fun loaded(): EventStore = EventStore().also { it.applySnapshot(snapshot(), 1_000) }

    @Test
    fun snapshotIndexesRowsAndSetsSeqAndSource() {
        val store = EventStore()
        val c = store.applySnapshot(snapshot(), 1_000)
        assertEquals(setOf("m1", "m2"), c.rows.toSet())
        assertTrue(c.game)
        assertEquals(5L, store.seq)
        assertEquals("ws", store.source)
        assertNotNull(store.row("m1"))
    }

    @Test
    fun snapshotIsACopyOfTheFrame() {
        val frame = snapshot()
        val store = EventStore()
        store.applySnapshot(frame, 1)
        store.applyPatch(patch(6, """{"rows":{"m1":{"chance":"0.60"}}}"""), 2)
        assertEquals("0.50", frame.getJSONObject("view").getJSONArray("sections").getJSONObject(0).getJSONArray("rows").getJSONObject(0).getString("chance"))
    }

    @Test
    fun patchMergesRowFieldsDeepAndKeepsTheRest() {
        val store = loaded()
        val r = store.applyPatch(patch(6, """{"rows":{"m1":{"chance":"0.60","yes":{"ask":"0.61"}}}}"""), 2_000)
        assertEquals(Action.RENDER, r.action)
        assertEquals(listOf("m1"), r.changes?.rows)
        val row = store.row("m1")!!
        assertEquals("0.60", row.getString("chance"))
        assertEquals("0.61", row.getJSONObject("yes").getString("ask"))
        assertEquals("0.49", row.getJSONObject("yes").getString("bid")) // untouched
        assertEquals("Arsenal", row.getString("label"))
        assertEquals(6L, store.seq)
    }

    @Test
    fun gameStatusSpotAndFreshnessPatches() {
        val store = loaded()
        val r = store.applyPatch(
            patch(6, """{"game":{"home":{"score":1},"elapsed":"13"},"status":"ended","spot":{"price":"100.5"},"freshness":{"source":"rest","stale":true}}"""),
            2_000,
        )
        val v = store.view!!
        assertEquals(1, v.getJSONObject("game").getJSONObject("home").getInt("score"))
        assertEquals("Arsenal", v.getJSONObject("game").getJSONObject("home").getString("name"))
        assertEquals("13", v.getJSONObject("game").getString("elapsed"))
        assertEquals("ended", v.getString("status"))
        assertEquals("100.5", v.getJSONObject("spot").getString("price"))
        assertTrue(v.getJSONObject("freshness").getBoolean("stale"))
        assertTrue(r.changes!!.game && r.changes!!.status && r.changes!!.spot)
    }

    @Test
    fun patchForAnUnknownRowIsSkipped() {
        val store = loaded()
        val r = store.applyPatch(patch(6, """{"rows":{"nope":{"chance":"0.9"}}}"""), 2_000)
        assertEquals(Action.RENDER, r.action)
        assertTrue(r.changes!!.rows.isEmpty())
    }

    @Test
    fun chartPointsAreAppendedAndCapped() {
        val store = loaded()
        store.applyPatch(patch(6, """{"chart":{"m1":[[3,"0.50"]]}}"""), 2_000)
        var pts = store.view!!.getJSONObject("chart").getJSONArray("series").getJSONObject(0).getJSONArray("points")
        assertEquals(3, pts.length())
        assertEquals("0.50", pts.getJSONArray(2).getString(1))
        // a long run never grows without bound
        var seq = 7L
        repeat(3) {
            val many = (0 until 1_000).joinToString(",", "[", "]") { "[${100 + it},\"0.5\"]" }
            store.applyPatch(patch(seq++, """{"chart":{"m1":$many}}"""), 3_000)
        }
        pts = store.view!!.getJSONObject("chart").getJSONArray("series").getJSONObject(0).getJSONArray("points")
        assertEquals(EventStore.MAX_POINTS, pts.length())
    }

    @Test
    fun anOldOrRepeatedSeqIsIgnored() {
        val store = loaded()
        assertEquals(Action.IGNORE, store.applyPatch(patch(5, """{"status":"ended"}"""), 2_000).action)
        assertEquals(Action.IGNORE, store.applyPatch(patch(3, """{"status":"ended"}"""), 2_000).action)
        assertEquals("live", store.view!!.getString("status"))
        assertEquals(5L, store.seq)
    }

    @Test
    fun aGapInSeqAsksForASnapshotAndChangesNothing() {
        val store = loaded()
        val r = store.applyPatch(patch(8, """{"rows":{"m1":{"chance":"0.99"}}}"""), 2_000)
        assertEquals(Action.RESNAPSHOT, r.action)
        assertNull(r.changes)
        assertEquals("0.50", store.row("m1")!!.getString("chance"))
        assertEquals(5L, store.seq)
        // the snapshot that follows resyncs, and the next patch applies again
        store.applySnapshot(snapshot(seq = 8), 3_000)
        assertEquals(Action.RENDER, store.applyPatch(patch(9, """{"status":"ended"}"""), 3_100).action)
    }

    @Test
    fun aPatchBeforeAnySnapshotAsksForOne() {
        assertEquals(Action.RESNAPSHOT, EventStore().applyPatch(patch(1, "{}"), 1).action)
    }

    @Test
    fun statusFrameMarksTheEventStaleAndBack() {
        val store = loaded()
        assertTrue(store.applyStatus(JSONObject("""{"type":"status","stale":true,"source":"rest"}"""), 2_000))
        assertTrue(store.view!!.getJSONObject("freshness").getBoolean("stale"))
        assertEquals("rest", store.source)
        store.applyStatus(JSONObject("""{"type":"status","stale":false,"source":"ws"}"""), 3_000)
        assertFalse(store.view!!.getJSONObject("freshness").getBoolean("stale"))
        assertFalse(EventStore().applyStatus(JSONObject("""{"stale":true}"""), 1))
    }

    @Test
    fun aRestSnapshotKeepsTheChartPointsAlreadyHeld() {
        val store = loaded()
        val rest = view()
        rest.getJSONObject("chart").getJSONArray("series").getJSONObject(0).put("points", org.json.JSONArray())
        store.applySnapshot(JSONObject().put("view", rest).put("source", "rest"), 4_000, keepChart = true)
        val pts = store.view!!.getJSONObject("chart").getJSONArray("series").getJSONObject(0).getJSONArray("points")
        assertEquals(2, pts.length())
        assertEquals("rest", store.source)
        // without keepChart the REST view wins as sent
        store.applySnapshot(JSONObject().put("view", rest).put("source", "rest"), 5_000)
        assertEquals(0, store.view!!.getJSONObject("chart").getJSONArray("series").getJSONObject(0).getJSONArray("points").length())
    }

    // ------------------------------------------------------------------ LIVE / DELAYED / OFFLINE

    @Test
    fun indicatorIsLiveWhileTheSocketDelivers() {
        val i = LiveIndicator.of("open", loaded(), now = 4_000)
        assertEquals(State.LIVE, i.state)
        assertEquals("ws", i.source)
        assertEquals(3L, i.ageS)
    }

    @Test
    fun indicatorIsDelayedWhenStaleOrRestSourced() {
        val stale = EventStore().also { it.applySnapshot(snapshot(v = view(stale = true)), 1_000) }
        assertEquals(State.DELAYED, LiveIndicator.of("open", stale, 2_000).state)
        val store = loaded()
        store.applyStatus(JSONObject("""{"stale":false,"source":"rest"}"""), 1_500)
        assertEquals(State.DELAYED, LiveIndicator.of("open", store, 2_000).state)
    }

    @Test
    fun indicatorGoesOfflineWhenTheSocketIsCutAndRecovers() {
        val store = loaded()
        assertEquals(State.OFFLINE, LiveIndicator.of("closed", store, 10_000).state)
        // the REST fallback keeps it DELAYED while its last good read is fresh, then it turns OFFLINE
        assertEquals(State.DELAYED, LiveIndicator.of("closed", store, 10_000, restOkAt = 5_000).state)
        assertEquals(State.OFFLINE, LiveIndicator.of("closed", store, 30_000, restOkAt = 5_000).state)
        assertEquals(State.LIVE, LiveIndicator.of("open", store, 10_000).state)
    }

    @Test
    fun indicatorWithNoViewIsOfflineAndSportsShowTheVendorNote() {
        assertEquals(State.OFFLINE, LiveIndicator.of("open", EventStore(), 1_000).state)
        assertTrue(LiveIndicator.of("open", loaded(), 2_000).note.contains("may be delayed"))
        val bracket = EventStore().also { it.applySnapshot(snapshot(v = view(game = false)), 1_000) }
        assertEquals("", LiveIndicator.of("open", bracket, 2_000).note)
    }

    // ------------------------------------------------------------------ pasted links

    @Test
    fun onlyHttpsLinksToAKnownVenueOpenAnEventView() {
        assertEquals(EventTarget(url = "https://polymarket.com/event/some-game"), EventTarget.fromPaste(" https://polymarket.com/event/some-game "))
        assertNotNull(EventTarget.fromPaste("https://www.kalshi.com/markets/kxfoo/bar"))
        assertNotNull(EventTarget.fromPaste("https://limitless.exchange/markets/abc"))
        assertNull(EventTarget.fromPaste("http://polymarket.com/event/x")) // not https
        assertNull(EventTarget.fromPaste("https://evilpolymarket.com/event/x")) // lookalike host
        assertNull(EventTarget.fromPaste("https://polymarket.com.evil.io/event/x"))
        assertNull(EventTarget.fromPaste("arsenal chelsea")) // an ordinary search
        assertNull(EventTarget.fromPaste("https://polymarket.com/event/x and more"))
    }
}
