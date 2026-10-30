package com.mainnetwallet.mmagent.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

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
        .build()

    /** A failure the operator can act on: the server's own message, verbatim. */
    class ApiException(val status: Int, val code: String, message: String) : Exception(message)

    suspend fun get(path: String): JSONObject = request("GET", path, null, null, null)

    suspend fun post(
        path: String,
        body: JSONObject,
        idempotencyKey: String? = null,
        stepupToken: String? = null,
    ): JSONObject = request("POST", path, body, idempotencyKey, stepupToken)

    suspend fun delete(path: String): JSONObject = request("DELETE", path, null, null, null)

    private suspend fun request(
        method: String,
        path: String,
        body: JSONObject?,
        idempotencyKey: String?,
        stepupToken: String?,
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
        val response = http.newCall(builder.build()).execute()
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
                    .ifBlank { json.optString("detail").ifBlank { "Request failed (${it.code})." } }
                throw ApiException(it.code, error?.optString("code") ?: "http_${it.code}", detail)
            }
            json
        }
    }

    companion object {
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
