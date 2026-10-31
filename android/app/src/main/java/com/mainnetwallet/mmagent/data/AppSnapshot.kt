package com.mainnetwallet.mmagent.data

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * The server's last answers (status, balance, markets, positions, orders and the rest of one refresh), kept in the
 * app's private cache folder so every screen has something to show the moment the app opens. They are the raw
 * answers, parsed by the same code as a live refresh, and shown only until the live refresh arrives: while it
 * runs the app counts as not online, so nothing that adds money works on old numbers. Removed on sign-out.
 */
internal class AppSnapshot(context: Context) {
    private val file = File(context.cacheDir, "app_snapshot.json")

    class Saved(val status: JSONObject, val extras: List<JSONObject?>)

    fun save(baseUrl: String, status: JSONObject, extras: List<JSONObject?>) {
        try {
            val list = JSONArray()
            for (extra in extras) list.put(extra ?: JSONObject.NULL)
            val root = JSONObject()
            root.put("base_url", baseUrl)
            root.put("status", status)
            root.put("extras", list)
            file.writeText(root.toString())
        } catch (e: Exception) {
            // Not saving is fine; the next start loads normally.
        }
    }

    /** The saved answers, or null when there are none, they came from another server, or they do not fit this build. */
    fun restore(baseUrl: String, expectedExtras: Int): Saved? {
        return try {
            if (!file.exists()) return null
            val root = JSONObject(file.readText())
            if (root.optString("base_url") != baseUrl) return null
            val status = root.optJSONObject("status") ?: return null
            val array = root.optJSONArray("extras") ?: return null
            if (array.length() != expectedExtras) return null
            val extras: List<JSONObject?> = List(array.length()) { index -> array.optJSONObject(index) }
            Saved(status, extras)
        } catch (e: Exception) {
            null
        }
    }

    fun clear() {
        try {
            file.delete()
        } catch (e: Exception) {
            // Nothing to do.
        }
    }
}
