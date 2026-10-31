package com.mainnetwallet.mmagent.data

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

/**
 * The server's event stream (`/v1/ws`), so the screens redraw the moment something changes instead of
 * at the next poll.
 *
 * The server sends `account.updated` when the venue's own account differs from what the app last saw
 * (including a trade made on the venue's website), and the usual `order.updated`, `proposal.*` and
 * `rule.triggered`. An event carries no state the app trusts: it only means "read the server again", so
 * a dropped or reordered message can never leave a screen wrong. If the socket is down, the periodic
 * refresh in `MainActivity` still runs.
 */
class LiveSocket(
    private val baseUrl: () -> String,
    private val token: () -> String?,
    private val onTopic: (String) -> Unit,
    private val onState: (Boolean) -> Unit = {},
) {
    private val http = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        // The server sends nothing while idle; a ping keeps NATs open and notices a dead link in ~40 s.
        .pingInterval(20, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    private var scope: CoroutineScope? = null
    private var job: Job? = null

    @Volatile
    private var socket: WebSocket? = null

    @Synchronized
    fun start() {
        if (job?.isActive == true) return
        val s = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        scope = s
        job = s.launch { run() }
    }

    @Synchronized
    fun stop() {
        job?.cancel()
        scope?.cancel()
        job = null
        scope = null
        socket?.cancel()
        socket = null
        onState(false)
    }

    private suspend fun run() {
        var backoffMs = 1_000L
        val self = this
        while (currentCoroutineContext().isActive) {
            val bearer = token()
            if (bearer.isNullOrBlank()) {
                delay(2_000)
                continue
            }
            val closed = kotlinx.coroutines.CompletableDeferred<Boolean>()
            val url = baseUrl().trimEnd('/')
                .replaceFirst("https://", "wss://")
                .replaceFirst("http://", "ws://") + "/v1/ws"
            val request = Request.Builder().url(url).header("Authorization", "Bearer $bearer").build()
            socket = http.newWebSocket(
                request,
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        backoffMs = 1_000L
                        self.onState(true)
                        // We may have missed events while disconnected; read the server once now.
                        self.onTopic("reconnected")
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        val topic = try {
                            JSONObject(text).optString("topic")
                        } catch (e: Exception) {
                            ""
                        }
                        if (topic in LIVE_TOPICS) self.onTopic(topic)
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        // The server closed first; answer the close frame so the socket really ends.
                        webSocket.close(1000, null)
                        closed.complete(code != 4401)
                    }

                    override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                        closed.complete(code != 4401)
                    }

                    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                        closed.complete(response?.code != 401)
                    }
                },
            )
            val retry = closed.await()
            onState(false)
            socket = null
            // 4401 / 401: the session is gone; the REST refresh will send the user to login, so wait.
            delay(if (retry) backoffMs else 10_000L)
            backoffMs = (backoffMs * 2).coerceAtMost(15_000L)
        }
    }

    companion object {
        /** Topics that mean "the account or the queue changed; read the server again". */
        val LIVE_TOPICS = setOf(
            "account.updated",
            "order.updated",
            "proposal.created",
            "proposal.executed",
            "rule.triggered",
            "agent.panic",
        )
    }
}
