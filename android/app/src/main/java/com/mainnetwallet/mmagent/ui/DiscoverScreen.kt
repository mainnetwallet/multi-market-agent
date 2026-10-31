package com.mainnetwallet.mmagent.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import com.mainnetwallet.mmagent.data.ApiClient
import com.mainnetwallet.mmagent.data.Format
import com.mainnetwallet.mmagent.data.objects
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONObject

/** Venues that have a browse feed on the server (`/v1/discover`). */
val BROWSABLE_VENUES: Set<String> = setOf("polymarket", "kalshi", "limitless")

/** org.json turns a JSON null into the string "null"; the server sends null for missing values. */
private fun JSONObject.text(key: String): String = if (isNull(key)) "" else optString(key)

/**
 * Pages already fetched per venue, tab and search, so going Trending -> New -> Trending shows the
 * old list at once instead of a spinner. A page older than [FRESH_MS] is shown and then refreshed
 * quietly. Held in memory only; nothing here is private or money data.
 */
internal object DiscoverCache {
    class Entry(val items: List<JSONObject>, val nextOffset: Int?, val note: String, val at: Long)

    const val FRESH_MS = 60_000L
    private const val MAX_ENTRIES = 40
    private val map = LinkedHashMap<String, Entry>()

    fun key(venue: String, tab: String, query: String): String = "$venue|$tab|$query"

    @Synchronized
    fun get(key: String): Entry? = map[key]

    @Synchronized
    fun put(key: String, entry: Entry) {
        map.remove(key)
        map[key] = entry
        while (map.size > MAX_ENTRIES) map.remove(map.keys.first())
    }

    @Synchronized
    fun drop(key: String) {
        map.remove(key)
    }

    /** Keep a page the server just answered, so the screen can show it the moment it opens. */
    fun store(venue: String, tab: String, query: String, body: JSONObject) {
        val next = if (body.isNull("next_offset")) null else body.optInt("next_offset")
        put(
            key(venue, tab, query),
            Entry(ApiClient.arrayOf(body, "items").objects(), next, body.optString("note"), System.currentTimeMillis()),
        )
    }
}

/**
 * The tab list each venue's server last returned, fetched in the background as soon as the app knows the
 * active venue, so the Markets page opens with its tabs already there. Memory only, and only ever what the
 * server sent: an empty or failed answer is never stored.
 */
internal object DiscoverTabsCache {
    /** One tab as the server sent it. [highlight] draws it in the accent gradient; [separatorBefore] adds a divider. */
    class Tab(
        val id: String,
        val label: String,
        val highlight: Boolean = false,
        val separatorBefore: Boolean = false,
        val subtabs: List<Tab> = emptyList(),
    )

    class Entry(val tabs: List<Tab>, val note: String, val at: Long)

    const val FRESH_MS = 5 * 60_000L
    private val map = HashMap<String, Entry>()

    @Synchronized
    fun get(venue: String): Entry? = map[venue]

    private fun tabOf(it: JSONObject): Tab = Tab(
        id = it.optString("id"),
        label = it.optString("label"),
        highlight = it.optBoolean("highlight", false),
        separatorBefore = it.optBoolean("separator_before", false),
        // Sub-categories of a category, as the server read them from Gamma (e.g. Crypto -> 1H).
        subtabs = ApiClient.arrayOf(it, "subtabs").objects()
            .map { sub -> Tab(sub.optString("id"), sub.optString("label"), separatorBefore = sub.optBoolean("separator_before", false)) }
            .filter { sub -> sub.id.isNotBlank() && sub.label.isNotBlank() },
    )

    fun parse(body: JSONObject): List<Tab> =
        ApiClient.arrayOf(body, "tabs").objects()
            .map { tabOf(it) }
            .filter { it.id.isNotBlank() && it.label.isNotBlank() }

    /** Store a server answer; returns the tabs, or an empty list (and stores nothing) when it had none. */
    @Synchronized
    fun store(venue: String, body: JSONObject): List<Tab> {
        val tabs = parse(body)
        if (tabs.isNotEmpty()) map[venue] = Entry(tabs, body.text("note"), System.currentTimeMillis())
        return tabs
    }
}

/** Accent for a highlighted tab (Midterms on polymarket.com): periwinkle to red. */
private val HIGHLIGHT_GRADIENT = listOf(Color(0xFF8E9BFF), Color(0xFFFF5C5C))

/** A card shows at most this many outcomes of a multi-market event. */
private const val CARD_ROWS = 3

/**
 * Polymarket-style market browser (Polymarket, Kalshi, Limitless): category tabs, search, event cards with the live
 * Yes/No prices, volume and close time. Tapping a price opens the trade sheet on that side.
 * Read-only: the app only fetches public data here; every order still goes through the trade sheet.
 */
@Composable
fun DiscoverScreen(
    venue: String,
    fetch: suspend (venue: String, tab: String, query: String, offset: Int) -> JSONObject,
    fetchTabs: suspend (venue: String) -> JSONObject,
    onTrade: (marketId: String, title: String, outcome: String) -> Unit,
) {
    // No tab is built into the app: every tab (sort modes and categories) is whatever the server returns.
    // When the background prefetch already got them, the page opens with its tabs and does not wait.
    val seeded = remember(venue) { DiscoverTabsCache.get(venue) }
    var tabs by remember(venue) { mutableStateOf(seeded?.tabs ?: emptyList()) }
    var tabsNote by remember(venue) { mutableStateOf(seeded?.note ?: "") }
    var tabsLoading by remember(venue) { mutableStateOf(seeded == null) }
    var tabsReloadKey by remember { mutableIntStateOf(0) }
    var tab by rememberSaveable(venue) { mutableStateOf(seeded?.tabs?.firstOrNull()?.id ?: "") }
    var query by rememberSaveable { mutableStateOf("") }
    var items by remember { mutableStateOf<List<JSONObject>>(emptyList()) }
    var loading by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var note by remember { mutableStateOf("") }
    var nextOffset by remember { mutableStateOf<Int?>(null) }
    var generation by remember { mutableIntStateOf(0) }
    var reloadKey by remember { mutableIntStateOf(0) }
    val scope = rememberCoroutineScope()

    // The tab list comes from the server (for Polymarket, from the tags of live Gamma events).
    LaunchedEffect(venue, tabsReloadKey) {
        val cached = DiscoverTabsCache.get(venue)
        if (cached != null && tabsReloadKey == 0 && System.currentTimeMillis() - cached.at < DiscoverTabsCache.FRESH_MS) {
            tabs = cached.tabs
            tabsNote = cached.note
            if (tabs.none { it.id == tab.substringBefore(':') }) tab = tabs.first().id
            tabsLoading = false
            return@LaunchedEffect
        }
        tabsLoading = true
        try {
            val body = fetchTabs(venue)
            val loaded = DiscoverTabsCache.store(venue, body)
            tabsNote = body.text("note")
            if (loaded.isEmpty()) {
                if (tabsNote.isBlank()) tabsNote = "The server returned no categories."
            } else {
                tabs = loaded
                if (tabs.none { it.id == tab.substringBefore(':') }) tab = tabs.first().id
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            tabsNote = e.message ?: "Could not load categories."
        } finally {
            tabsLoading = false
        }
    }

    // A response that arrives after the tab or search changed is dropped, never shown.
    suspend fun load(offset: Int, gen: Int, tabId: String, text: String) {
        loading = true
        try {
            val body = fetch(venue, tabId, text, offset)
            if (gen != generation) return
            val page = ApiClient.arrayOf(body, "items").objects()
            items = if (offset == 0) {
                page
            } else {
                val have = items.map { it.optString("event_id") }.toSet()
                items + page.filter { it.optString("event_id") !in have }
            }
            note = body.optString("note")
            nextOffset = if (body.isNull("next_offset")) null else body.optInt("next_offset")
            error = null
            val cacheKey = DiscoverCache.key(venue, tabId, text)
            val firstFetched = if (offset == 0) System.currentTimeMillis() else DiscoverCache.get(cacheKey)?.at
            DiscoverCache.put(
                cacheKey,
                DiscoverCache.Entry(items, nextOffset, note, firstFetched ?: System.currentTimeMillis()),
            )
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            if (gen == generation) error = e.message ?: "Could not load markets."
        } finally {
            if (gen == generation) loading = false
        }
    }

    LaunchedEffect(tab, query, reloadKey) {
        generation += 1
        val gen = generation
        if (tab.isBlank()) {
            // No tab yet: nothing to show until the server has said which tabs exist.
            items = emptyList()
            nextOffset = null
            note = ""
            error = null
            loading = false
            return@LaunchedEffect
        }
        val cached = DiscoverCache.get(DiscoverCache.key(venue, tab, query.trim()))
        error = null
        if (cached != null) {
            items = cached.items
            nextOffset = cached.nextOffset
            note = cached.note
            loading = false
            if (System.currentTimeMillis() - cached.at < DiscoverCache.FRESH_MS) return@LaunchedEffect
            // Older than a minute: keep showing it and replace it when the fresh page arrives.
        } else {
            items = emptyList()
            nextOffset = null
            note = ""
            if (query.isNotBlank()) delay(400)
        }
        load(0, gen, tab, query.trim())
    }

    // A different tab or search starts at the top of its own list.
    val listState = rememberLazyListState()
    LaunchedEffect(tab, query) { listState.scrollToItem(0) }

    Column(modifier = Modifier.fillMaxSize()) {
        OutlinedTextField(
            value = query,
            onValueChange = { query = it },
            label = { Text("Search markets") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth().padding(start = 16.dp, end = 16.dp, top = 8.dp),
        )
        LazyRow(
            contentPadding = PaddingValues(horizontal = 16.dp, vertical = 4.dp),
            horizontalArrangement = Arrangement.spacedBy(20.dp),
        ) {
            items(tabs, key = { it.id }) { item ->
                Row(verticalAlignment = Alignment.CenterVertically) {
                    if (item.separatorBefore) {
                        // The thin divider polymarket.com shows between its featured tabs and the categories.
                        Box(
                            Modifier
                                .padding(end = 20.dp, top = 8.dp)
                                .width(1.dp)
                                .height(16.dp)
                                .background(MaterialTheme.colorScheme.outline.copy(alpha = 0.6f)),
                        )
                    }
                    DiscoverTab(item.label, selected = tab.substringBefore(':') == item.id && query.isBlank(), highlight = item.highlight, onClick = {
                        if (tab == item.id) {
                            DiscoverCache.drop(DiscoverCache.key(venue, item.id, ""))
                            reloadKey += 1
                        } else {
                            tab = item.id
                        }
                        query = ""
                    })
                }
            }
        }
        Box(
            Modifier.fillMaxWidth().height(1.dp).background(MaterialTheme.colorScheme.outline.copy(alpha = 0.4f)),
        )
        // Sub-categories of the open category, whatever the server lists: All, then each one.
        val parentId = tab.substringBefore(':')
        val subtabs = tabs.firstOrNull { it.id == parentId }?.subtabs.orEmpty()
        if (subtabs.isNotEmpty()) {
            val subChips = listOf(DiscoverTabsCache.Tab(parentId, "All")) + subtabs
            LazyRow(
                contentPadding = PaddingValues(horizontal = 16.dp, vertical = 8.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                items(subChips, key = { it.id }) { sub ->
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        if (sub.separatorBefore) {
                            Box(
                                Modifier
                                    .padding(end = 8.dp)
                                    .width(1.dp)
                                    .height(18.dp)
                                    .background(MaterialTheme.colorScheme.outline.copy(alpha = 0.6f)),
                            )
                        }
                        SubTabChip(sub.label, selected = tab == sub.id && query.isBlank(), onClick = {
                            if (tab == sub.id) {
                                DiscoverCache.drop(DiscoverCache.key(venue, sub.id, ""))
                                reloadKey += 1
                            } else {
                                tab = sub.id
                            }
                            query = ""
                        })
                    }
                }
            }
        }
        if (tabsNote.isNotBlank() && tabs.isNotEmpty()) {
            Text(
                tabsNote,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 16.dp, end = 16.dp, top = 6.dp),
            )
        }
        LazyColumn(
            state = listState,
            modifier = Modifier.fillMaxSize(),
            contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 12.dp, bottom = 16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            items(items, key = { it.optString("event_id") }) { event ->
                EventCard(event, onTrade)
            }
            item {
                when {
                    tab.isBlank() -> if (tabsLoading) {
                        Box(Modifier.fillMaxWidth().padding(24.dp), contentAlignment = Alignment.Center) {
                            CircularProgressIndicator()
                        }
                    } else {
                        Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally) {
                            Text(
                                tabsNote.ifBlank { "Could not load categories." },
                                style = MaterialTheme.typography.bodyMedium,
                                color = Palette.Alert,
                            )
                            Spacer(Modifier.height(8.dp))
                            LinkText("Retry", onClick = { tabsReloadKey += 1 })
                        }
                    }
                    error != null -> Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally) {
                        Text(error ?: "", style = MaterialTheme.typography.bodyMedium, color = Palette.Alert)
                        Spacer(Modifier.height(8.dp))
                        LinkText("Retry", onClick = {
                            DiscoverCache.drop(DiscoverCache.key(venue, tab, query.trim()))
                            reloadKey += 1
                        })
                    }
                    loading && items.isEmpty() -> Box(Modifier.fillMaxWidth().padding(24.dp), contentAlignment = Alignment.Center) {
                        CircularProgressIndicator()
                    }
                    note.isNotBlank() -> Text(note, style = MaterialTheme.typography.bodyMedium)
                    items.isEmpty() && !loading -> Text(
                        if (query.isBlank()) "No open markets in this tab." else "No market matches this search.",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                }
            }
            val more = nextOffset
            if (more != null && error == null && items.isNotEmpty()) {
                item(key = "more:$more") {
                    // Composing this row means the list reached its end: fetch the next page once.
                    LaunchedEffect(more) {
                        if (!loading) scope.launch { load(more, generation, tab, query.trim()) }
                    }
                    Box(Modifier.fillMaxWidth().padding(12.dp), contentAlignment = Alignment.Center) {
                        CircularProgressIndicator(modifier = Modifier.size(24.dp))
                    }
                }
            }
        }
    }
}

@Composable
private fun DiscoverTab(label: String, selected: Boolean, highlight: Boolean = false, onClick: () -> Unit) {
    Column(
        modifier = Modifier.clickable { onClick() }.padding(top = 8.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        if (highlight) {
            // Midterms on polymarket.com: blue to red gradient text instead of the plain tab colour.
            Text(
                label,
                style = MaterialTheme.typography.titleSmall.copy(brush = Brush.horizontalGradient(HIGHLIGHT_GRADIENT)),
                fontWeight = if (selected) FontWeight.Bold else FontWeight.Medium,
            )
        } else {
            Text(
                label,
                style = MaterialTheme.typography.titleSmall,
                fontWeight = if (selected) FontWeight.Bold else FontWeight.Medium,
                color = if (selected) MaterialTheme.colorScheme.onSurface else MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Spacer(Modifier.height(6.dp))
        Box(
            Modifier
                .height(2.dp)
                .width(if (selected) 28.dp else 0.dp)
                .background(MaterialTheme.colorScheme.onSurface),
        )
    }
}

@Composable
private fun SubTabChip(label: String, selected: Boolean, onClick: () -> Unit) {
    Box(
        Modifier
            .clip(RoundedCornerShape(16.dp))
            .background(
                if (selected) MaterialTheme.colorScheme.surfaceVariant else Color.Transparent,
            )
            .clickable { onClick() }
            .padding(horizontal = 12.dp, vertical = 6.dp),
    ) {
        Text(
            label,
            style = MaterialTheme.typography.labelLarge,
            fontWeight = if (selected) FontWeight.Bold else FontWeight.Medium,
            color = if (selected) MaterialTheme.colorScheme.onSurface else MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

@Composable
private fun EventCard(event: JSONObject, onTrade: (String, String, String) -> Unit) {
    val markets = ApiClient.arrayOf(event, "markets").objects()
    if (markets.isEmpty()) return
    val single = markets.size == 1 && event.optInt("market_count", 1) == 1
    val image = event.text("image")
    val first = markets.first()
    val footer = buildList {
        if (event.text("volume_unit") == "contracts") {
            add(Format.count(event.optString("volume")) + " contracts")
        } else {
            add(Format.volume(event.optString("volume")) + " Vol")
        }
        if (single) Format.move(first.text("change_1d").ifBlank { null })?.let { add(it + " 24h") }
        val end = event.text("end_time")
        if (end.isNotBlank()) add("closes in " + Format.countdown(end))
    }.joinToString(" · ")

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(MaterialTheme.colorScheme.surface)
            .padding(14.dp),
    ) {
        Row(verticalAlignment = Alignment.Top) {
            if (image.isNotBlank()) {
                AsyncImage(
                    model = image,
                    contentDescription = null,
                    contentScale = ContentScale.Crop,
                    modifier = Modifier.size(44.dp).clip(RoundedCornerShape(8.dp))
                        .background(MaterialTheme.colorScheme.surfaceVariant),
                )
                Spacer(Modifier.width(12.dp))
            }
            Text(
                event.optString("title"),
                style = MaterialTheme.typography.titleSmall,
                maxLines = 3,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
            if (single) {
                Spacer(Modifier.width(10.dp))
                Column(horizontalAlignment = Alignment.End) {
                    Text(
                        Format.percent(first.text("chance").ifBlank { first.optString("yes_price") }),
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold,
                    )
                    Text("chance", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
        Spacer(Modifier.height(10.dp))
        if (single) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                PriceButton(
                    label = first.optString("yes_label", "Yes"),
                    price = first.optString("yes_price"),
                    color = Palette.Long,
                    modifier = Modifier.weight(1f),
                ) { onTrade(first.optString("market_id"), first.optString("question"), "YES") }
                PriceButton(
                    label = first.optString("no_label", "No"),
                    price = first.optString("no_price"),
                    color = Palette.Short,
                    modifier = Modifier.weight(1f),
                ) { onTrade(first.optString("market_id"), first.optString("question"), "NO") }
            }
        } else {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                markets.take(CARD_ROWS).forEach { market ->
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            market.optString("label"),
                            style = MaterialTheme.typography.bodyMedium,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                            modifier = Modifier.weight(1f),
                        )
                        Text(
                            Format.percent(market.text("chance").ifBlank { market.optString("yes_price") }),
                            style = MaterialTheme.typography.titleSmall,
                            fontWeight = FontWeight.Bold,
                            modifier = Modifier.padding(horizontal = 8.dp),
                        )
                        PriceButton("Yes", market.optString("yes_price"), Palette.Long, Modifier.width(76.dp)) {
                            onTrade(market.optString("market_id"), market.optString("question"), "YES")
                        }
                        Spacer(Modifier.width(6.dp))
                        PriceButton("No", market.optString("no_price"), Palette.Short, Modifier.width(76.dp)) {
                            onTrade(market.optString("market_id"), market.optString("question"), "NO")
                        }
                    }
                }
                val extra = event.optInt("market_count", markets.size) - CARD_ROWS
                if (extra > 0) {
                    Text(
                        "+$extra more outcomes",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }
        Spacer(Modifier.height(10.dp))
        Text(footer, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

/** A tinted "Yes 57¢" button. The label always names the side, so colour is never the only signal. */
@Composable
private fun PriceButton(
    label: String,
    price: String,
    color: Color,
    modifier: Modifier = Modifier,
    onClick: () -> Unit,
) {
    Box(
        modifier = modifier
            .clip(RoundedCornerShape(8.dp))
            .background(color.copy(alpha = 0.16f))
            .clickable { onClick() }
            .padding(vertical = 10.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            label + " " + Format.cent(price),
            style = MaterialTheme.typography.labelLarge,
            fontWeight = FontWeight.SemiBold,
            color = color,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
    }
}
