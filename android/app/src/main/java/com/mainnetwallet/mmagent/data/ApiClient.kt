package com.mainnetwallet.mmagent.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import okhttp3.Call
import okhttp3.Callback
import okhttp3.Dispatcher
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/**
 * Small JSON client for the agent's Control API.
 *
 * The server speaks Decimal-as-string JSON, so every money value arrives as a
 * string and is carried as a string all the way to the screen. No float math.
 */
class ApiClient(
    @Volatile var baseUrl: String,
    private val tokenProvider: () -> String?,
) {
    private val http = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        // OkHttp allows only 5 calls per host at once; the refresh sends 12 together.
        .dispatcher(Dispatcher().apply { maxRequests = 32; maxRequestsPerHost = 16 })
        .build()

    /**
     * Called when the server says the session itself is gone (not a wrong
     * step-up password, which also answers 401). The app clears the token and
     * returns to the login screen.
     */
    @Volatile
    var onUnauthorized: (() -> Unit)? = null

    /** A failure the operator can act on: the server's own message, verbatim. */
    class ApiException(val status: Int, val code: String, message: String) : Exception(message)

    suspend fun get(path: String): JSONObject = request("GET", path, null, null, null, null)

    suspend fun post(
        path: String,
        body: JSONObject,
        idempotencyKey: String? = null,
        stepupToken: String? = null,
        readTimeoutSeconds: Long? = null,
    ): JSONObject = request("POST", path, body, idempotencyKey, stepupToken, readTimeoutSeconds)

    suspend fun delete(path: String): JSONObject = request("DELETE", path, null, null, null, null)

    /**
     * Read a Server-Sent Events response. [onEvent] gets each event name with its JSON data, on an
     * OkHttp thread. Cancelling the calling coroutine closes the connection; that is how Stop works.
     */
    suspend fun stream(path: String, body: JSONObject? = null, onEvent: (event: String, data: JSONObject) -> Unit) {
        val builder = Request.Builder()
            .url(baseUrl.trimEnd('/') + path)
            .header("Accept", "text/event-stream")
        if (body != null) builder.post(body.toString().toRequestBody("application/json".toMediaType()))
        val token = tokenProvider()
        if (!token.isNullOrBlank()) {
            builder.header("Authorization", "Bearer $token")
        }
        // The server sends a ping every 10 s, so a long silence means the connection is dead.
        val call = http.newBuilder().readTimeout(60, TimeUnit.SECONDS).build().newCall(builder.build())
        suspendCancellableCoroutine<Unit> { cont ->
            cont.invokeOnCancellation { call.cancel() }
            call.enqueue(object : Callback {
                override fun onFailure(call: Call, e: IOException) {
                    if (cont.isActive) cont.resumeWithException(e)
                }

                override fun onResponse(call: Call, response: Response) {
                    try {
                        response.use { readEvents(it, token, onEvent) }
                        if (cont.isActive) cont.resume(Unit)
                    } catch (e: Exception) {
                        if (cont.isActive) cont.resumeWithException(e)
                    }
                }
            })
        }
    }

    private fun readEvents(response: Response, token: String?, onEvent: (String, JSONObject) -> Unit) {
        if (!response.isSuccessful) {
            val text = response.body?.string().orEmpty()
            val json = try {
                if (text.isBlank()) JSONObject() else JSONObject(text)
            } catch (e: Exception) {
                JSONObject()
            }
            val error = json.optJSONObject("error")
            val detail = error?.optString("message").orEmpty()
                .ifBlank { detailText(json.opt("detail")) }
                .ifBlank { "Request failed (${response.code})." }
            if (response.code == 401 && !token.isNullOrBlank() && isSessionGone(error, detail)) {
                onUnauthorized?.invoke()
            }
            throw ApiException(response.code, error?.optString("code") ?: "http_${response.code}", detail)
        }
        val source = response.body?.source() ?: return
        var event = "message"
        val data = StringBuilder()
        while (true) {
            val line = source.readUtf8Line() ?: break
            when {
                line.isEmpty() -> {
                    if (data.isNotEmpty()) {
                        val payload = try {
                            JSONObject(data.toString())
                        } catch (e: Exception) {
                            null
                        }
                        if (payload != null) onEvent(event, payload)
                    }
                    event = "message"
                    data.setLength(0)
                }
                line.startsWith("event:") -> event = line.substring(6).trim()
                line.startsWith("data:") -> {
                    if (data.isNotEmpty()) data.append('\n')
                    data.append(line.substring(5).trim())
                }
            }
        }
    }

    private suspend fun request(
        method: String,
        path: String,
        body: JSONObject?,
        idempotencyKey: String?,
        stepupToken: String?,
        readTimeoutSeconds: Long?,
    ): JSONObject = withContext(Dispatchers.IO) {
        val builder = Request.Builder().url(baseUrl.trimEnd('/') + path)
        val token = tokenProvider()
        if (!token.isNullOrBlank()) {
            builder.header("Authorization", "Bearer $token")
        }
        if (!idempotencyKey.isNullOrBlank()) {
            builder.header("Idempotency-Key", idempotencyKey)
        }
        if (!stepupToken.isNullOrBlank()) {
            builder.header("X-Stepup-Token", stepupToken)
        }
        when (method) {
            "GET" -> builder.get()
            "DELETE" -> builder.delete()
            else -> builder.post(
                (body?.toString() ?: "{}").toRequestBody("application/json".toMediaType())
            )
        }
        val client = if (readTimeoutSeconds != null) {
            http.newBuilder().readTimeout(readTimeoutSeconds, TimeUnit.SECONDS).build()
        } else {
            http
        }
        val response = client.newCall(builder.build()).execute()
        response.use {
            val text = it.body?.string().orEmpty()
            val json = try {
                if (text.isBlank()) JSONObject() else JSONObject(text)
            } catch (e: Exception) {
                throw ApiException(it.code, "bad_response", "The server sent a response the app could not read.")
            }
            if (!it.isSuccessful) {
                val error = json.optJSONObject("error")
                val detail = error?.optString("message").orEmpty()
                    .ifBlank { detailText(json.opt("detail")) }
                    .ifBlank { "Request failed (${it.code})." }
                if (it.code == 401 && !token.isNullOrBlank() && isSessionGone(error, detail)) {
                    onUnauthorized?.invoke()
                }
                throw ApiException(it.code, error?.optString("code") ?: "http_${it.code}", detail)
            }
            json
        }
    }

    companion object {
        /** FastAPI's own 401 texts: the session cookie/token is missing, bad or expired. */
        private fun isSessionGone(error: JSONObject?, detail: String): Boolean =
            error == null && (detail == "login required" || detail == "invalid or expired session")

        /**
         * FastAPI puts a plain string in `detail`, but a refused approve carries
         * the engine's whole result object there. Show its own message, never raw JSON.
         */
        fun detailText(detail: Any?): String = when (detail) {
            null, JSONObject.NULL -> ""
            is String -> detail
            is JSONObject -> listOf("error", "message", "reason", "detail")
                .map { key -> detail.opt(key) }
                .firstNotNullOfOrNull { value ->
                    when (value) {
                        is String -> value.takeIf { v -> v.isNotBlank() }
                        is JSONObject -> value.optString("message").takeIf { v -> v.isNotBlank() }
                        else -> null
                    }
                } ?: detail.toString()
            is JSONArray -> (0 until detail.length())
                .joinToString("; ") { index -> detailText(detail.opt(index)) }
            else -> detail.toString()
        }

        fun jsonOf(vararg pairs: Pair<String, Any?>): JSONObject {
            val obj = JSONObject()
            for ((key, value) in pairs) {
                when (value) {
                    null -> obj.put(key, JSONObject.NULL)
                    is Number -> obj.put(key, value.toString())
                    is Boolean -> obj.put(key, value)
                    else -> obj.put(key, value.toString())
                }
            }
            return obj
        }

        fun arrayOf(json: JSONObject?, key: String): JSONArray = json?.optJSONArray(key) ?: JSONArray()
    }
}

/** JSONArray is not a Kotlin collection; this makes it iterable. */
fun JSONArray.objects(): List<JSONObject> {
    val out = ArrayList<JSONObject>(length())
    for (index in 0 until length()) {
        optJSONObject(index)?.let { out.add(it) }
    }
    return out
}
