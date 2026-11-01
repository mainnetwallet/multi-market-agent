package com.mainnetwallet.mmagent.data

/** What an event screen follows: a venue event id, or a pasted polymarket.com / kalshi.com / limitless.exchange link. */
data class EventTarget(val venue: String = "", val id: String = "", val url: String = "") {
    companion object {
        private val HOSTS = listOf("polymarket.com", "kalshi.com", "limitless.exchange")

        /** A pasted venue link (https only, a known host) becomes a target; anything else is a normal search. */
        fun fromPaste(text: String): EventTarget? {
            val t = text.trim()
            if (!t.startsWith("https://") || t.any { it.isWhitespace() } || t.length > 300) return null
            val host = t.removePrefix("https://").substringBefore('/').substringBefore('?').lowercase()
            return if (HOSTS.any { host == it || host.endsWith(".$it") }) EventTarget(url = t) else null
        }
    }
}
