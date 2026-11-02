package com.mainnetwallet.mmagent.data

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import kotlin.random.Random

/**
 * Client for the server's event stream (`/v1/stream`, Bearer auth), separate from `LiveSocket` (`/v1/ws`, account topics).
 *
 * It only moves frames: the owner of a subscription ([EventViewModel]) applies them to an [EventStore]. After every
 * (re)connect it sends the subscribe frames again, and the server answers each with a full snapshot, so a dropped or
 * reordered message can never leave a screen wrong. A quiet socket is probed with a `snapshot` request; no answer
 * within [PING_REPLY_MS] means the link is dead and it reconnects. Close 4401 = signed out, 4404 = live views disabled:
 * neither is retried.
 *
 * Connection states: `connecting`, `open`, `closed`, `auth`, `disabled`.
 */
class LiveStream(
    private val baseUrl: () -> String,
    private val token: () -> String?,
    private val onFrame: (JSONObject) -> Unit,
    private val onConn: (String) -> Unit,
    private val random: () -> Double = { Random.nextDouble() },
) {
    private data class Sub(val venue: String, val id: String, var range: String)

    private val http = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    private val subs = LinkedHashMap<String, Sub>()
    private var scope: CoroutineScope? = null
    private var job: Job? = null
    private var idleJob: Job? = null

    @Volatile
    private var socket: WebSocket? = null

    @Volatile
    private var lastMessageAt = 0L

    @Volatile
    var conn: String = "closed"
        private set

    private fun setConn(c: String) {
        if (conn == c) return
        conn = c
        onConn(c)
    }

    @Synchronized
    fun subscribe(venue: String, id: String, range: String = "1D") {
        val key = "$venue|$id"
        val fresh = subs[key] == null
        subs[key] = Sub(venue, id, range)
        start()
        if (fresh && conn == "open") send(frame("subscribe", venue, id, range))
    }

    @Synchronized
    fun unsubscribe(venue: String, id: String) {
        if (subs.remove("$venue|$id") != null && conn == "open") send(frame("unsubscribe", venue, id, null))
        if (subs.isEmpty()) stop()
    }

    /** Ask for a fresh snapshot: after a seq gap, or when the chart range changes. */
    @Synchronized
    fun requestSnapshot(venue: String, id: String, range: String? = null) {
        val sub = subs["$venue|$id"] ?: return
        if (range != null) sub.range = range
        if (conn == "open") send(frame("snapshot", venue, id, sub.range))
    }

    @Synchronized
    private fun start() {
        if (job?.isActive == true || conn == "auth" || conn == "disabled") return
        val s = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        scope = s
        job = s.launch { run() }
    }

    @Synchronized
    fun stop() {
        job?.cancel()
        idleJob?.cancel()
        scope?.cancel()
        job = null
        idleJob = null
        scope = null
        socket?.cancel()
        socket = null
        subs.clear()
        conn = "closed"
    }

    private fun send(obj: JSONObject) {
        socket?.send(obj.toString())
    }

    private fun frame(op: String, venue: String, id: String, range: String?): JSONObject =
        JSONObject().put("op", op).put("topic", "event").put("venue", venue).put("id", id).apply {
            if (range != null) put("range", range)
        }

    private suspend fun run() {
        var attempt = 0
        while (currentCoroutineContext().isActive) {
            val bearer = token()
            if (bearer.isNullOrBlank()) {
                setConn("auth")
                return
            }
            setConn("connecting")
            val closed = CompletableDeferred<String>()
            val url = baseUrl().trimEnd('/')
                .replaceFirst("https://", "wss://")
                .replaceFirst("http://", "ws://") + "/v1/stream"
            val request = Request.Builder().url(url).header("Authorization", "Bearer $bearer").build()
            val self = this
            socket = http.newWebSocket(
                request,
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        attempt = 0
                        lastMessageAt = System.currentTimeMillis()
                        self.setConn("open")
                        synchronized(self) {
                            for (s in subs.values) webSocket.send(frame("subscribe", s.venue, s.id, s.range).toString())
                        }
                        self.armIdle(webSocket)
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        lastMessageAt = System.currentTimeMillis()
                        val f = try {
                            JSONObject(text)
                        } catch (e: Exception) {
                            return // a malformed frame is dropped, never fatal
                        }
                        self.onFrame(f)
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(1000, null)
                        closed.complete(verdict(code))
                    }

                    override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                        closed.complete(verdict(code))
                    }

                    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                        closed.complete(if (response?.code == 401) "auth" else "closed")
                    }
                },
            )
            val result = closed.await()
            idleJob?.cancel()
            socket = null
            if (result == "auth" || result == "disabled") {
                setConn(result)
                return
            }
            setConn("closed")
            if (subs.isEmpty()) return
            val base = minOf(15_000L, 1_000L shl minOf(attempt, 4))
            attempt += 1
            delay((base * (0.5 + random() * 0.5)).toLong())
        }
    }

    private fun verdict(code: Int): String = when (code) {
        4401 -> "auth"
        4404 -> "disabled"
        else -> "closed"
    }

    /** Quiet socket: ask for a snapshot as a liveness probe; no reply in [PING_REPLY_MS] -> drop and reconnect. */
    private fun armIdle(webSocket: WebSocket) {
        idleJob?.cancel()
        idleJob = scope?.launch {
            while (isActive) {
                delay(IDLE_PING_MS)
                if (System.currentTimeMillis() - lastMessageAt < IDLE_PING_MS) continue
                val first = synchronized(this@LiveStream) { subs.values.firstOrNull() } ?: continue
                val probeAt = System.currentTimeMillis()
                webSocket.send(frame("snapshot", first.venue, first.id, first.range).toString())
                delay(PING_REPLY_MS)
                if (lastMessageAt < probeAt) {
                    webSocket.cancel()
                    return@launch
                }
            }
        }
    }

    companion object {
        const val IDLE_PING_MS = 45_000L
        const val PING_REPLY_MS = 10_000L
    }
}
