package com.mainnetwallet.mmagent.data

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.net.URLEncoder

/**
 * The store is mutated in place, so [rev] changes on every update: that is what makes Compose redraw.
 * Rows, game and chart are read straight from [view].
 */
data class EventUiState(
    val view: JSONObject? = null,
    val rev: Int = 0,
    val indicator: LiveIndicator = LiveIndicator(LiveIndicator.State.OFFLINE, "", null, ""),
    val error: String? = null,
    val loading: Boolean = true,
    val range: String = "1D",
)

/**
 * One open event: `/v1/stream` pushes snapshot and patch frames into an [EventStore]; while the socket cannot deliver
 * the event is read from `/v1/event/view` (never faster than [LiveIndicator.REST_MIN_MS]). Frames are applied on the main
 * thread, the same one that draws, so the in-place store is never read half-updated.
 * Display data only: a Yes/No tap goes to the existing trade sheet.
 */
class EventViewModel(
    private val api: ApiClient,
    private val baseUrl: () -> String,
    private val token: () -> String?,
    private val now: () -> Long = { System.currentTimeMillis() },
) : ViewModel() {
    private val store = EventStore()
    private val _state = MutableStateFlow(EventUiState())
    val state: StateFlow<EventUiState> = _state.asStateFlow()

    private var target = EventTarget()
    private var canonicalId = ""
    private var conn = "closed"
    private var restOkAt = 0L
    private var restJob: Job? = null
    private var tickJob: Job? = null
    private var stream: LiveStream? = null
    private var rev = 0

    fun open(t: EventTarget) {
        if (t == target && stream != null) return
        close()
        target = t
        canonicalId = ""
        viewModelScope.launch {
            _state.value = EventUiState()
            startTicker()
            if (t.url.isNotBlank()) {
                // A pasted link names a slug, not an event id: read it once, then follow the event the server resolved.
                if (!readRest(first = true)) return@launch
                val v = store.view ?: return@launch
                target = EventTarget(v.optString("venue"), v.optString("event_id"))
            }
            if (target.venue.isBlank() || target.id.isBlank()) {
                _state.update { it.copy(loading = false, error = "This event has no id.") }
                return@launch
            }
            val s = LiveStream(
                baseUrl = baseUrl,
                token = token,
                onFrame = { f -> viewModelScope.launch { onFrame(f) } },
                onConn = { c -> viewModelScope.launch { onConn(c) } },
            )
            stream = s
            conn = "connecting" // the REST fallback waits for the socket's first answer instead of racing it
            s.subscribe(target.venue, target.id, _state.value.range)
            syncRest()
        }
    }

    fun close() {
        restJob?.cancel()
        tickJob?.cancel()
        restJob = null
        tickJob = null
        stream?.stop()
        stream = null
    }

    override fun onCleared() {
        close()
        super.onCleared()
    }

    /** Chart range chips (1H 6H 1D 1W 1M ALL): the server re-sends the snapshot with that history. */
    fun setRange(range: String) {
        _state.update { it.copy(range = range) }
        stream?.requestSnapshot(target.venue, target.id, range)
    }

    private fun onConn(c: String) {
        conn = c
        syncRest()
        publish()
    }

    private fun onFrame(f: JSONObject) {
        if (f.optString("venue") != target.venue) return
        val id = f.optString("id")
        val type = f.optString("type")
        if (type == "snapshot" && f.has("requested")) {
            if (f.optString("requested") != target.id) return
            canonicalId = id
            store.applySnapshot(f, now())
            restJob?.cancel()
            restJob = null
            publish(error = null)
            return
        }
        if (id != target.id && id != canonicalId) return
        when (type) {
            "patch" -> {
                val r = store.applyPatch(f, now())
                if (r.action == EventStore.Action.RESNAPSHOT) {
                    stream?.requestSnapshot(target.venue, target.id)
                } else if (r.action == EventStore.Action.RENDER) {
                    publish()
                }
            }
            "status" -> if (store.applyStatus(f, now())) publish()
            "error" -> {
                publish(error = f.optString("detail").ifBlank { f.optString("code") })
                if (f.optString("code") == "not_found") syncRest(force = true)
            }
        }
    }

    // ------------------------------------------------------------------ REST fallback

    private fun syncRest(force: Boolean = false) {
        val needed = force || conn != "open" || store.view == null
        if (needed && conn != "connecting") startRest() else if (!needed) {
            restJob?.cancel()
            restJob = null
        }
    }

    private fun startRest() {
        if (restJob?.isActive == true) return
        restJob = viewModelScope.launch {
            while (isActive) {
                readRest(first = false)
                delay(LiveIndicator.REST_MIN_MS)
            }
        }
    }

    private suspend fun readRest(first: Boolean): Boolean {
        val q = if (first && target.url.isNotBlank()) {
            "url=" + URLEncoder.encode(target.url, "UTF-8")
        } else {
            "venue=" + URLEncoder.encode(target.venue, "UTF-8") + "&id=" + URLEncoder.encode(target.id, "UTF-8")
        }
        return try {
            val body = api.get("/v1/event/view?$q")
            val view = body.optJSONObject("view") ?: body
            store.applySnapshot(JSONObject().put("view", view).put("source", "rest"), now(), keepChart = true)
            restOkAt = now()
            publish(error = null)
            true
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            // Offline: the indicator turns OFFLINE once the last good read is old. The server's own words are shown.
            _state.update { it.copy(loading = false, error = e.message ?: "Could not load this event.") }
            false
        }
    }

    // ------------------------------------------------------------------ indicator clock

    private fun startTicker() {
        tickJob?.cancel()
        // The only timer on screen: it re-derives the LIVE/DELAYED/OFFLINE age once a second. Nothing is fetched here.
        tickJob = viewModelScope.launch {
            while (isActive) {
                delay(1_000)
                publish()
            }
        }
    }

    private fun publish(error: String? = _state.value.error) {
        rev += 1
        _state.update {
            it.copy(
                view = store.view,
                rev = rev,
                indicator = LiveIndicator.of(conn, store, now(), restOkAt),
                error = if (store.view != null && error == null) null else error,
                loading = store.view == null && error == null,
            )
        }
    }
}
