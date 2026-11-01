package com.mainnetwallet.mmagent.data

import org.json.JSONArray
import org.json.JSONObject

/**
 * Live event store: one EventView plus the snapshot/patch reducer the server's `/v1/stream` speaks.
 * Display data only. Trading always goes through the existing trade sheet and `/v1/orders`.
 *
 * Pure (no Android, no coroutines) so it is unit-tested on the JVM. It mirrors the PWA's `static/js/live.js`:
 * a patch is applied only when its `seq` is exactly the next one; an older one is ignored and a gap asks for a snapshot.
 */
class EventStore {
    var view: JSONObject? = null
        private set
    var seq: Long = -1
        private set
    var source: String = ""
        private set
    var receivedAt: Long = 0
        private set

    private var rows: MutableMap<String, JSONObject> = mutableMapOf()

    fun row(marketId: String): JSONObject? = rows[marketId]

    /** What a screen needs to redraw. Rows are market ids; the booleans say which parts changed. */
    data class Changes(
        val kind: String,
        val rows: List<String> = emptyList(),
        val game: Boolean = false,
        val chart: Boolean = false,
        val status: Boolean = false,
        val spot: Boolean = false,
    )

    enum class Action { RENDER, IGNORE, RESNAPSHOT }

    data class PatchResult(val action: Action, val changes: Changes?)

    private fun indexRows() {
        rows = mutableMapOf()
        val sections = view?.optJSONArray("sections") ?: return
        for (i in 0 until sections.length()) {
            val list = sections.optJSONObject(i)?.optJSONArray("rows") ?: continue
            for (j in 0 until list.length()) {
                val row = list.optJSONObject(j) ?: continue
                rows[row.optString("market_id")] = row
            }
        }
    }

    /**
     * Replace the whole view. REST snapshots carry no chart history, so with [keepChart] the points already held are kept.
     */
    fun applySnapshot(frame: JSONObject, now: Long, keepChart: Boolean = false): Changes {
        val fresh = frame.optJSONObject("view")?.let { JSONObject(it.toString()) } ?: return Changes("snapshot")
        val old = view
        if (keepChart && old != null) {
            val oldChart = old.optJSONObject("chart")
            val newChart = fresh.optJSONObject("chart")
            if (oldChart != null && newChart != null) {
                val held = HashMap<String, JSONArray>()
                val oldSeries = oldChart.optJSONArray("series") ?: JSONArray()
                for (i in 0 until oldSeries.length()) {
                    val s = oldSeries.optJSONObject(i) ?: continue
                    s.optJSONArray("points")?.let { held[s.optString("row_id")] = it }
                }
                val newSeries = newChart.optJSONArray("series") ?: JSONArray()
                for (i in 0 until newSeries.length()) {
                    val s = newSeries.optJSONObject(i) ?: continue
                    val pts = s.optJSONArray("points")
                    if ((pts == null || pts.length() == 0) && held.containsKey(s.optString("row_id"))) {
                        s.put("points", held[s.optString("row_id")])
                    }
                }
            }
        }
        view = fresh
        if (frame.has("seq")) seq = frame.optLong("seq", seq)
        source = frame.optString("source").ifBlank { fresh.optJSONObject("freshness")?.optString("source").orEmpty() }
        receivedAt = now
        indexRows()
        return Changes("snapshot", rows.keys.toList(), game = fresh.has("game"), chart = true, status = true, spot = true)
    }

    /** Apply a patch frame. A gap in `seq` asks for a snapshot, an old or repeated one is ignored. */
    fun applyPatch(frame: JSONObject, now: Long): PatchResult {
        val v = view ?: return PatchResult(Action.RESNAPSHOT, null)
        if (frame.has("seq")) {
            val s = frame.optLong("seq", Long.MIN_VALUE)
            if (s <= seq) return PatchResult(Action.IGNORE, null)
            if (s > seq + 1) return PatchResult(Action.RESNAPSHOT, null)
        }
        val patch = frame.optJSONObject("patch") ?: JSONObject()
        val changedRows = mutableListOf<String>()
        var game = false
        var chart = false
        var status = false
        var spot = false

        patch.optJSONObject("rows")?.let { byId ->
            for (id in byId.keys()) {
                val row = rows[id] ?: continue
                val rp = byId.optJSONObject(id) ?: continue
                mergeDeep(row, rp)
                changedRows += id
            }
        }
        patch.optJSONObject("game")?.let { g ->
            v.optJSONObject("game")?.let {
                mergeDeep(it, g)
                game = true
            }
        }
        if (patch.has("status") && patch.opt("status") is String) {
            v.put("status", patch.getString("status"))
            status = true
        }
        patch.optJSONObject("spot")?.let {
            v.put("spot", JSONObject(it.toString()))
            spot = true
        }
        patch.optJSONObject("freshness")?.let {
            v.put("freshness", JSONObject(it.toString()))
            status = true
        }
        patch.optJSONObject("chart")?.let { byRow ->
            val series = v.optJSONObject("chart")?.optJSONArray("series")
            if (series != null) {
                for (id in byRow.keys()) {
                    val add = byRow.optJSONArray(id) ?: continue
                    for (i in 0 until series.length()) {
                        val line = series.optJSONObject(i) ?: continue
                        if (line.optString("row_id") != id) continue
                        val pts = line.optJSONArray("points") ?: JSONArray()
                        for (k in 0 until add.length()) pts.put(add.opt(k))
                        line.put("points", tail(pts, MAX_POINTS))
                        chart = true
                    }
                }
            }
        }
        if (frame.has("seq")) seq = frame.optLong("seq", seq)
        frame.optString("source").takeIf { it.isNotBlank() }?.let { source = it }
        receivedAt = now
        return PatchResult(Action.RENDER, Changes("patch", changedRows, game, chart, status, spot))
    }

    /** `status` frame: the hub switched between ws and rest, or marked the event stale. */
    fun applyStatus(frame: JSONObject, now: Long): Boolean {
        val v = view ?: return false
        val fresh = v.optJSONObject("freshness") ?: JSONObject().also { v.put("freshness", it) }
        val src = frame.optString("source").ifBlank { source }
        fresh.put("stale", frame.optBoolean("stale", false))
        fresh.put("source", src)
        source = src
        receivedAt = now
        return true
    }

    private fun mergeDeep(target: JSONObject, patch: JSONObject) {
        for (k in patch.keys()) {
            val v = patch.opt(k)
            val existing = target.opt(k)
            if (v is JSONObject && existing is JSONObject) {
                mergeDeep(existing, v)
            } else {
                target.put(k, if (v is JSONObject) JSONObject(v.toString()) else if (v is JSONArray) JSONArray(v.toString()) else v)
            }
        }
    }

    private fun tail(a: JSONArray, keep: Int): JSONArray {
        if (a.length() <= keep) return a
        val out = JSONArray()
        for (i in a.length() - keep until a.length()) out.put(a.opt(i))
        return out
    }

    companion object {
        const val MAX_POINTS = 2000
    }
}

/** LIVE / DELAYED / OFFLINE, with the data age and source shown on every event view. */
data class LiveIndicator(val state: State, val source: String, val ageS: Long?, val note: String) {
    enum class State { LIVE, DELAYED, OFFLINE }

    companion object {
        /** Do not fetch faster than this when the socket cannot deliver (the venue limits are shared). */
        const val REST_MIN_MS = 5_000L

        /** Pure. [conn]: open | connecting | closed | disabled | auth. [restOkAt]: epoch ms of the last good REST read (0 = never). */
        fun of(conn: String, store: EventStore, now: Long, restOkAt: Long = 0): LiveIndicator {
            val v = store.view ?: return LiveIndicator(State.OFFLINE, "", null, "")
            val age = if (store.receivedAt > 0) maxOf(0L, (now - store.receivedAt) / 1000) else null
            val fresh = v.optJSONObject("freshness")
            val note = if (v.has("game")) "Vendor score data may be delayed." else ""
            if (conn == "open") {
                return if (fresh?.optBoolean("stale", false) == true || store.source == "rest") {
                    LiveIndicator(State.DELAYED, "rest", age, note)
                } else {
                    LiveIndicator(State.LIVE, store.source.ifBlank { "ws" }, age, note)
                }
            }
            if (restOkAt > 0 && now - restOkAt < 3 * REST_MIN_MS) return LiveIndicator(State.DELAYED, "rest", age, note)
            return LiveIndicator(State.OFFLINE, store.source, age, note)
        }
    }
}
