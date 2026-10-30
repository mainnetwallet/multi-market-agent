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

private val POLYMARKET_TABS: List<Pair<String, String>> = listOf(
    "trending" to "Trending",
    "combos" to "Combos",
    "perps" to "Perps",
    "breaking" to "Breaking",
    "new" to "New",
    "midterms" to "Midterms",
    "politics" to "Politics",
    "sports" to "Sports",
    "crypto" to "Crypto",
    "esports" to "Esports",
    "iran" to "Iran",
    "finance" to "Finance",
    "geopolitics" to "Geopolitics",
    "tech" to "Tech",
    "economy" to "Economy",
    "elections" to "Elections",
    "culture" to "Culture",
    "weather" to "Weather",
    "mentions" to "Mentions",
)

private val KALSHI_TABS: List<Pair<String, String>> = listOf(
    "trending" to "Trending",
    "new" to "New",
    "politics" to "Politics",
    "elections" to "Elections",
    "sports" to "Sports",
    "crypto" to "Crypto",
    "economics" to "Economics",
    "financials" to "Financials",
    "companies" to "Companies",
    "tech" to "Tech & Science",
    "weather" to "Weather",
    "culture" to "Culture",
    "world" to "World",
    "health" to "Health",
    "mentions" to "Mentions",
)

private val LIMITLESS_TABS: List<Pair<String, String>> = listOf(
    "trending" to "Trending",
    "new" to "New",
    "liquid" to "Liquid",
    "crypto" to "Crypto",
    "sports" to "Sports",
    "politics" to "Politics",
    "economy" to "Economy",
    "tech" to "Tech",
    "culture" to "Culture",
)

/** The server owns what each id means; `/v1/discover/tabs?venue=` lists the same ids. */
private fun tabsFor(venue: String): List<Pair<String, String>> = when (venue) {
    "kalshi" -> KALSHI_TABS
    "limitless" -> LIMITLESS_TABS
    else -> POLYMARKET_TABS
}

/** org.json turns a JSON null into the string "null"; the server sends null for missing values. */
private fun JSONObject.text(key: String): String = if (isNull(key)) "" else optString(key)

/**
 * Pages already fetched per venue, tab and search, so going Trending -> New -> Trending shows the
 * old list at once instead of a spinner. A page older than [FRESH_MS] is shown and then refreshed
 * quietly. Held in memory only; nothing here is private or money data.
 */
private object DiscoverCache {
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
}

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
    onTrade: (marketId: String, title: String, outcome: String) -> Unit,
) {
    val tabs = tabsFor(venue)
    var tab by rememberSaveable { mutableStateOf("trending") }
    var query by rememberSaveable { mutableStateOf("") }
    var items by remember { mutableStateOf<List<JSONObject>>(emptyList()) }
    var loading by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var note by remember { mutableStateOf("") }
    var nextOffset by remember { mutableStateOf<Int?>(null) }
    var generation by remember { mutableIntStateOf(0) }
    var reloadKey by remember { mutableIntStateOf(0) }
    val scope = rememberCoroutineScope()

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
            items(tabs, key = { it.first }) { (id, label) ->
                DiscoverTab(label, selected = tab == id && query.isBlank(), onClick = {
                    if (tab == id) {
                        DiscoverCache.drop(DiscoverCache.key(venue, id, ""))
                        reloadKey += 1
                    } else {
                        tab = id
                    }
                    query = ""
                })
            }
        }
        Box(
            Modifier.fillMaxWidth().height(1.dp).background(MaterialTheme.colorScheme.outline.copy(alpha = 0.4f)),
        )
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
private fun DiscoverTab(label: String, selected: Boolean, onClick: () -> Unit) {
    Column(
        modifier = Modifier.clickable { onClick() }.padding(top = 8.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(
            label,
            style = MaterialTheme.typography.titleSmall,
            fontWeight = if (selected) FontWeight.Bold else FontWeight.Medium,
            color = if (selected) MaterialTheme.colorScheme.onSurface else MaterialTheme.colorScheme.onSurfaceVariant,
        )
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
