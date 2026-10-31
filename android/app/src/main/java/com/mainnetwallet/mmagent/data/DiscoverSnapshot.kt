package com.mainnetwallet.mmagent.data

import android.content.Context
import com.mainnetwallet.mmagent.ui.DiscoverCache
import com.mainnetwallet.mmagent.ui.DiscoverTabsCache
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * The Markets browse lists (tabs and each tab's first page) kept in the app's private cache folder, so the
 * Markets page has something to show the moment the app opens, before the first request has come back.
 * Public market data only. A restored list is shown with its original age, so the screen still refreshes it
 * quietly. Best effort: any read or write error just means the page loads the normal way.
 */
internal class DiscoverSnapshot(context: Context) {
    private val file = File(context.cacheDir, "discover_snapshot.json")

    /** Put the saved lists back into the in-memory caches. Returns the venue they belong to, or null. */
    fun restore(): String? {
        return try {
            if (!file.exists()) return null
            val root = JSONObject(file.readText())
            val venue = root.optString("venue")
            val tabsBody = root.optJSONObject("tabs")
            if (venue.isBlank() || tabsBody == null) return null
            val tabs = DiscoverTabsCache.store(venue, tabsBody, root.optLong("tabs_at", 0L))
            if (tabs.isEmpty()) return null
            val pages = root.optJSONObject("pages")
            if (pages != null) {
                val ids = pages.keys()
                while (ids.hasNext()) {
                    val id = ids.next()
                    val page = pages.optJSONObject(id) ?: continue
                    val body = page.optJSONObject("body") ?: continue
                    DiscoverCache.store(venue, id, "", body, page.optLong("at", 0L))
                }
            }
            venue
        } catch (e: Exception) {
            null
        }
    }

    /** Save the venue's tabs and the first page of its first [tabLimit] tabs, as they are in memory now. */
    fun save(venue: String, tabLimit: Int) {
        try {
            val entry = DiscoverTabsCache.get(venue) ?: return
            val tabsArray = JSONArray()
            for (tab in entry.tabs) tabsArray.put(tabJson(tab))
            val pages = JSONObject()
            for (tab in entry.tabs.take(tabLimit)) {
                val page = DiscoverCache.get(DiscoverCache.key(venue, tab.id, "")) ?: continue
                val items = JSONArray()
                for (item in page.items) items.put(item)
                val body = JSONObject()
                body.put("items", items)
                body.put("next_offset", page.nextOffset ?: JSONObject.NULL)
                body.put("note", page.note)
                val saved = JSONObject()
                saved.put("at", page.at)
                saved.put("body", body)
                pages.put(tab.id, saved)
            }
            val tabsBody = JSONObject()
            tabsBody.put("tabs", tabsArray)
            tabsBody.put("note", entry.note)
            val root = JSONObject()
            root.put("venue", venue)
            root.put("tabs_at", entry.at)
            root.put("tabs", tabsBody)
            root.put("pages", pages)
            file.writeText(root.toString())
        } catch (e: Exception) {
            // Not saving is fine; the next start loads normally.
        }
    }

    fun clear() {
        try {
            file.delete()
        } catch (e: Exception) {
            // Nothing to do.
        }
    }

    private fun tabJson(tab: DiscoverTabsCache.Tab): JSONObject {
        val subtabs = JSONArray()
        for (sub in tab.subtabs) {
            val s = JSONObject()
            s.put("id", sub.id)
            s.put("label", sub.label)
            s.put("separator_before", sub.separatorBefore)
            subtabs.put(s)
        }
        val json = JSONObject()
        json.put("id", tab.id)
        json.put("label", tab.label)
        json.put("highlight", tab.highlight)
        json.put("separator_before", tab.separatorBefore)
        json.put("subtabs", subtabs)
        return json
    }
}
