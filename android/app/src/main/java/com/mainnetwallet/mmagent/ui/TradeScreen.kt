package com.mainnetwallet.mmagent.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.mainnetwallet.mmagent.data.Format
import com.mainnetwallet.mmagent.data.OutcomeBook
import com.mainnetwallet.mmagent.data.TradeDraft
import com.mainnetwallet.mmagent.data.TradeEstimate
import java.math.BigDecimal
import java.math.RoundingMode

/**
 * The trade ticket, laid out like Polymarket's: Buy / Sell, a big Yes and No button that show the live
 * price, one number to type (dollars for a market buy, shares for a sell or a limit order), a "To win"
 * summary, and a 2-second press-and-hold to confirm. Order book, price history and the venue's finer settings sit under
 * "Advanced" so the first screen stays simple.
 *
 * Every order is still real money, so placing one is still a press-and-hold, never a single tap.
 */
@Composable
fun TradeSheetScreen(
    draft: TradeDraft,
    estimate: TradeEstimate,
    books: Map<String, OutcomeBook>,
    bids: List<BookLevel>,
    asks: List<BookLevel>,
    history: List<PricePoint>,
    held: BigDecimal,
    available: String,
    online: Boolean,
    submitting: Boolean,
    error: String?,
    result: String?,
    onSide: (String) -> Unit,
    onOutcome: (String) -> Unit,
    onMarketMode: (Boolean) -> Unit,
    onPostOnly: (Boolean) -> Unit,
    onPrice: (String) -> Unit,
    onStepPrice: (Int) -> Unit,
    onShares: (String) -> Unit,
    onAmount: (String) -> Unit,
    onAddAmount: (Int) -> Unit,
    onPercent: (Int) -> Unit,
    onSlippage: (String) -> Unit,
    onSubmit: () -> Unit,
    onCancel: () -> Unit,
) {
    val buy = draft.side == "BUY"
    val yes = draft.outcome == "YES"
    val outcomeName = if (yes) "Yes" else "No"
    val sideColor = if (yes) Palette.Long else Palette.Short
    var advanced by remember { mutableStateOf(false) }
    val done = result != null
    val canSubmit = estimate.valid && !submitting && online && !done
    val hasInput = draft.amountUsd.isNotBlank() || draft.shares.isNotBlank()

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background),
    ) {
        Column(
            modifier = Modifier
                .weight(1f)
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp, vertical = 12.dp),
        ) {
            // Header: the question, the venue, and a close button.
            Row(verticalAlignment = Alignment.Top) {
                Column(Modifier.weight(1f)) {
                    Text(
                        draft.title,
                        style = MaterialTheme.typography.titleMedium,
                        maxLines = 3,
                        overflow = TextOverflow.Ellipsis,
                    )
                    Spacer(Modifier.height(2.dp))
                    Text(
                        draft.venue.uppercase(),
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                Text(
                    "✕",
                    style = MaterialTheme.typography.titleMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier
                        .clip(CircleShape)
                        .clickable { onCancel() }
                        .padding(horizontal = 12.dp, vertical = 6.dp),
                )
            }

            Spacer(Modifier.height(16.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                TradeTab("Buy", selected = buy) { onSide("BUY") }
                Spacer(Modifier.width(18.dp))
                TradeTab("Sell", selected = !buy) { onSide("SELL") }
                Spacer(Modifier.weight(1f))
                if (draft.supportsMarket && draft.supportsLimit) {
                    ModePill(market = draft.isMarketOrder, onChange = onMarketMode)
                }
            }

            Spacer(Modifier.height(14.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                OutcomeButton(
                    name = "Yes",
                    price = centsText(quote(books["YES"], buy)),
                    selected = yes,
                    color = Palette.Long,
                    modifier = Modifier.weight(1f),
                ) { onOutcome("YES") }
                OutcomeButton(
                    name = "No",
                    price = centsText(quote(books["NO"], buy)),
                    selected = !yes,
                    color = Palette.Short,
                    modifier = Modifier.weight(1f),
                ) { onOutcome("NO") }
            }

            Spacer(Modifier.height(14.dp))
            if (draft.isMarketOrder && buy) {
                // Polymarket's default: one number, in dollars.
                val maxText = available.toBigDecimalOrNull()
                    ?.takeIf { it.signum() > 0 }
                    ?.setScale(2, RoundingMode.DOWN)
                    ?.toPlainString()
                InputCard("Amount", "Cash ${Format.withAsset(available, draft.collateralAsset)}") {
                    BigNumberField(
                        value = draft.amountUsd,
                        prefix = "$",
                        onChange = { onAmount(cleanDecimal(it, 2)) },
                    )
                    Spacer(Modifier.height(10.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        listOf(1, 5, 10, 100).forEach { step ->
                            QuickChip("+$$step") { onAddAmount(step) }
                        }
                        if (maxText != null) QuickChip("Max") { onAmount(maxText) }
                    }
                }
            } else {
                if (!draft.isMarketOrder) {
                    InputCard("Limit price (¢)", "Tick ${tickText(draft.tick)}") {
                        PriceStepper(draft = draft, onPrice = onPrice, onStep = onStepPrice)
                    }
                    Spacer(Modifier.height(10.dp))
                    // A limit price only makes sense next to the book it will rest in, so it is shown right here.
                    InputCard("Order book · $outcomeName", "Tap a price to use it") {
                        OrderBookLadder(
                            bids = bids,
                            asks = asks,
                            onPick = { price -> onPrice(price.stripTrailingZeros().toPlainString()) },
                        )
                    }
                    Spacer(Modifier.height(10.dp))
                }
                InputCard("Shares", if (!buy && held.signum() > 0) "You hold ${held.setScale(2, RoundingMode.DOWN).stripTrailingZeros().toPlainString()}" else null) {
                    BigNumberField(
                        value = draft.shares,
                        prefix = "",
                        onChange = { onShares(cleanDecimal(it, 2)) },
                    )
                    Spacer(Modifier.height(10.dp))
                    if (buy) {
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            listOf(10, 50, 100, 500).forEach { step ->
                                QuickChip("+$step") {
                                    val current = draft.shares.toBigDecimalOrNull() ?: BigDecimal.ZERO
                                    onShares((current + BigDecimal(step)).stripTrailingZeros().toPlainString())
                                }
                            }
                        }
                    } else if (held.signum() > 0) {
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            QuickChip("25%") { onPercent(25) }
                            QuickChip("50%") { onPercent(50) }
                            QuickChip("75%") { onPercent(75) }
                            QuickChip("Max") { onPercent(100) }
                        }
                    } else {
                        Text(
                            "No $outcomeName shares in your positions for this market.",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
            }

            Spacer(Modifier.height(14.dp))
            SummaryCard(
                draft = draft,
                estimate = estimate,
                buy = buy,
                hasInput = hasInput,
            )

            Spacer(Modifier.height(14.dp))
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(10.dp))
                    .clickable { advanced = !advanced }
                    .padding(vertical = 8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    "Advanced",
                    style = MaterialTheme.typography.labelLarge,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.weight(1f),
                )
                Text(if (advanced) "▴" else "▾", color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            if (advanced) {
                if (draft.isMarketOrder) {
                    OutlinedTextField(
                        value = draft.maxSlippageCents,
                        onValueChange = { onSlippage(cleanDecimal(it, 1)) },
                        label = { Text("Max slippage (¢ from the best price)") },
                        singleLine = true,
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                        modifier = Modifier.fillMaxWidth(),
                    )
                } else if (draft.postOnlySupported) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        modifier = Modifier
                            .clip(RoundedCornerShape(8.dp))
                            .clickable { onPostOnly(!draft.postOnly) }
                            .padding(vertical = 6.dp),
                    ) {
                        Text(if (draft.postOnly) "☑" else "☐", style = MaterialTheme.typography.titleMedium)
                        Spacer(Modifier.width(8.dp))
                        Text("Post-only (never takes a price, only rests)", style = MaterialTheme.typography.bodyMedium)
                    }
                }
                if (draft.isMarketOrder) {
                    Spacer(Modifier.height(10.dp))
                    Card(
                        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                        modifier = Modifier.fillMaxWidth(),
                    ) {
                        Column(Modifier.padding(16.dp)) {
                            SectionHeader("Order book", outcomeName)
                            OrderBookLadder(bids = bids, asks = asks, onPick = null)
                        }
                    }
                }
                Spacer(Modifier.height(10.dp))
                Card(
                    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Column(Modifier.padding(16.dp)) {
                        SectionHeader("Price history", "Yes price · 1 day")
                        if (history.isEmpty()) {
                            Text(
                                "No price history for this venue.",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        } else {
                            PriceChart(points = history)
                        }
                    }
                }
            }
            Spacer(Modifier.height(8.dp))
        }

        // The confirm bar stays on screen whatever the scroll position.
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .background(MaterialTheme.colorScheme.surface)
                .padding(horizontal = 16.dp, vertical = 12.dp),
        ) {
            ErrorNote(error)
            if (!online) {
                // The server is unreachable: the price on screen is unknown, so no order.
                ErrorNote("The server is unreachable. Trading is disabled until it answers again.")
            }
            if (result != null) {
                val good = result.startsWith("FILLED") || result.startsWith("PARTIAL") || result.startsWith("RESTING")
                Text(
                    result,
                    style = MaterialTheme.typography.bodyMedium,
                    color = if (good) Palette.Long else Palette.Alert,
                )
                Spacer(Modifier.height(8.dp))
            }
            if (done) {
                ConfirmButton("Done", enabled = true, onClick = onCancel)
            } else {
                HoldToConfirmButton(
                    label = "Hold 2s to ${draft.side.lowercase()} $outcomeName",
                    enabled = canSubmit,
                    busy = submitting,
                    danger = !yes,
                    holdMillis = 2_000L,
                    onConfirmed = onSubmit,
                )
            }
        }
    }
}

/**
 * The order book as a ladder: asks (red) on top with the lowest nearest the middle, the spread, then bids (green).
 * Each row shows price in cents, shares and the dollar total; the bar behind it is the row's size against the
 * biggest row shown. With [onPick] a tap on a row hands that price to the caller (the limit price field).
 */
@Composable
private fun OrderBookLadder(bids: List<BookLevel>, asks: List<BookLevel>, onPick: ((BigDecimal) -> Unit)?) {
    val topAsks = asks.sortedBy { it.price }.take(LADDER_ROWS).reversed()
    val topBids = bids.sortedByDescending { it.price }.take(LADDER_ROWS)
    if (topAsks.isEmpty() && topBids.isEmpty()) {
        Text(
            "No orders in the book yet.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        return
    }
    val biggest = (topAsks + topBids).maxOf { it.shares }.max(BigDecimal.ONE)
    val muted = MaterialTheme.colorScheme.onSurfaceVariant
    Column(Modifier.fillMaxWidth()) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp)) {
            Text("Price", Modifier.weight(1f), style = MaterialTheme.typography.labelSmall, color = muted)
            Text("Shares", Modifier.weight(1f), style = MaterialTheme.typography.labelSmall, color = muted, textAlign = TextAlign.End)
            Text("Total", Modifier.weight(1f), style = MaterialTheme.typography.labelSmall, color = muted, textAlign = TextAlign.End)
        }
        Spacer(Modifier.height(4.dp))
        topAsks.forEach { level -> LadderRow(level, Palette.Short, "Ask", biggest, onPick) }
        val bestAsk = topAsks.lastOrNull()?.price
        val bestBid = topBids.firstOrNull()?.price
        Text(
            if (bestAsk != null && bestBid != null) {
                "Spread ${centsText((bestAsk - bestBid).max(BigDecimal.ZERO))}"
            } else {
                "One-sided book"
            },
            style = MaterialTheme.typography.labelMedium,
            color = muted,
            textAlign = TextAlign.Center,
            modifier = Modifier
                .fillMaxWidth()
                .padding(vertical = 6.dp),
        )
        topBids.forEach { level -> LadderRow(level, Palette.Long, "Bid", biggest, onPick) }
    }
}

private const val LADDER_ROWS = 5

@Composable
private fun LadderRow(
    level: BookLevel,
    color: Color,
    kind: String,
    biggest: BigDecimal,
    onPick: ((BigDecimal) -> Unit)?,
) {
    val fraction = level.shares.divide(biggest, 3, RoundingMode.HALF_UP).toFloat().coerceIn(0.04f, 1f)
    val total = level.price.multiply(level.shares).setScale(2, RoundingMode.HALF_UP)
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .height(32.dp)
            .clip(RoundedCornerShape(6.dp))
            .then(if (onPick != null) Modifier.clickable { onPick(level.price) } else Modifier),
    ) {
        Box(
            Modifier
                .align(Alignment.CenterEnd)
                .fillMaxWidth(fraction)
                .height(32.dp)
                .background(color.copy(alpha = 0.16f)),
        )
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .height(32.dp)
                .padding(horizontal = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                "${centsText(level.price)} $kind",
                Modifier.weight(1f),
                style = MaterialTheme.typography.bodyMedium,
                fontFamily = FontFamily.Monospace,
                fontWeight = FontWeight.SemiBold,
                color = color,
            )
            Text(
                level.shares.setScale(2, RoundingMode.DOWN).stripTrailingZeros().toPlainString(),
                Modifier.weight(1f),
                style = MaterialTheme.typography.bodyMedium,
                fontFamily = FontFamily.Monospace,
                textAlign = TextAlign.End,
            )
            Text(
                "$" + total.toPlainString(),
                Modifier.weight(1f),
                style = MaterialTheme.typography.bodyMedium,
                fontFamily = FontFamily.Monospace,
                textAlign = TextAlign.End,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/** What one tap on this outcome costs (buy) or pays (sell) right now. */
private fun quote(book: OutcomeBook?, buy: Boolean): BigDecimal? = if (buy) book?.bestAsk else book?.bestBid

/** "62¢"; one decimal only when the price really has one. */
private fun centsText(price: BigDecimal?): String {
    if (price == null) return "—"
    val cents = price.multiply(BigDecimal(100))
    val scale = if (cents.stripTrailingZeros().scale() > 0) 1 else 0
    return cents.setScale(scale, RoundingMode.HALF_UP).toPlainString() + "¢"
}

private fun tickText(tick: BigDecimal): String = centsText(tick)

private fun priceToCents(price: String): String {
    val p = price.toBigDecimalOrNull() ?: return ""
    return p.multiply(BigDecimal(100)).stripTrailingZeros().toPlainString()
}

private fun centsToPrice(cents: String): String {
    val c = cents.toBigDecimalOrNull() ?: return ""
    return c.divide(BigDecimal(100)).stripTrailingZeros().toPlainString()
}

/** Digits and one dot only, with a cap on decimals, so a paste or a comma cannot break the number. */
private fun cleanDecimal(raw: String, maxDecimals: Int): String {
    val out = StringBuilder()
    var dot = false
    var decimals = 0
    for (c in raw) {
        if (c in '0'..'9') {
            if (dot) {
                if (decimals >= maxDecimals) continue
                decimals++
            }
            out.append(c)
        } else if ((c == '.' || c == ',') && !dot) {
            dot = true
            out.append('.')
        }
    }
    return out.toString()
}

@Composable
private fun TradeTab(label: String, selected: Boolean, onClick: () -> Unit) {
    Column(
        horizontalAlignment = Alignment.CenterHorizontally,
        modifier = Modifier
            .clip(RoundedCornerShape(6.dp))
            .clickable { onClick() }
            .padding(vertical = 4.dp),
    ) {
        Text(
            label,
            style = MaterialTheme.typography.titleMedium,
            fontWeight = if (selected) FontWeight.Bold else FontWeight.Medium,
            color = if (selected) MaterialTheme.colorScheme.onSurface else MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(4.dp))
        Box(
            Modifier
                .height(2.dp)
                .width(32.dp)
                .background(if (selected) MaterialTheme.colorScheme.onSurface else Color.Transparent),
        )
    }
}

/** Market | Limit, the way Polymarket's order-type dropdown reads, as a two-part pill. */
@Composable
private fun ModePill(market: Boolean, onChange: (Boolean) -> Unit) {
    Row(
        modifier = Modifier
            .clip(RoundedCornerShape(999.dp))
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .padding(3.dp),
    ) {
        listOf(true to "Market", false to "Limit").forEach { (isMarket, label) ->
            val selected = market == isMarket
            Text(
                label,
                style = MaterialTheme.typography.labelMedium,
                color = if (selected) MaterialTheme.colorScheme.onSurface else MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier
                    .clip(RoundedCornerShape(999.dp))
                    .background(if (selected) MaterialTheme.colorScheme.surface else Color.Transparent)
                    .clickable { onChange(isMarket) }
                    .padding(horizontal = 14.dp, vertical = 7.dp),
            )
        }
    }
}

/** A big Yes or No button. The word is always on it, so colour is never the only signal. */
@Composable
private fun OutcomeButton(
    name: String,
    price: String,
    selected: Boolean,
    color: Color,
    modifier: Modifier = Modifier,
    onClick: () -> Unit,
) {
    Box(
        modifier = modifier
            .height(58.dp)
            .clip(RoundedCornerShape(14.dp))
            .background(if (selected) color else MaterialTheme.colorScheme.surfaceVariant)
            .clickable { onClick() },
        contentAlignment = Alignment.Center,
    ) {
        Text(
            "$name $price",
            style = MaterialTheme.typography.titleMedium,
            fontWeight = FontWeight.Bold,
            color = if (selected) Color.White else color,
            maxLines = 1,
        )
    }
}

@Composable
private fun InputCard(
    label: String,
    note: String? = null,
    content: @Composable ColumnScope.() -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(16.dp))
            .background(MaterialTheme.colorScheme.surface)
            .padding(16.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                label,
                style = MaterialTheme.typography.labelLarge,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.weight(1f),
            )
            if (note != null) {
                Text(
                    note,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
        Spacer(Modifier.height(6.dp))
        content()
    }
}

@Composable
private fun RowScope.QuickChip(label: String, onClick: () -> Unit) {
    Text(
        label,
        style = MaterialTheme.typography.labelLarge,
        textAlign = TextAlign.Center,
        color = MaterialTheme.colorScheme.onSurface,
        maxLines = 1,
        modifier = Modifier
            .weight(1f)
            .clip(RoundedCornerShape(999.dp))
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .clickable { onClick() }
            .padding(vertical = 10.dp),
    )
}

/** The one big number: "$ 0", with the number pad in decimal mode. */
@Composable
private fun BigNumberField(value: String, prefix: String, onChange: (String) -> Unit) {
    val strong = MaterialTheme.colorScheme.onSurface
    val faint = MaterialTheme.colorScheme.outline
    val muted = MaterialTheme.colorScheme.onSurfaceVariant
    val cursor = MaterialTheme.colorScheme.primary
    val style = TextStyle(
        fontFamily = FontFamily.Monospace,
        fontWeight = FontWeight.Bold,
        fontSize = 40.sp,
        color = strong,
    )
    Row(verticalAlignment = Alignment.CenterVertically) {
        if (prefix.isNotEmpty()) {
            Text(prefix, style = style.copy(color = muted))
            Spacer(Modifier.width(4.dp))
        }
        BasicTextField(
            value = value,
            onValueChange = onChange,
            singleLine = true,
            textStyle = style,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
            cursorBrush = SolidColor(cursor),
            modifier = Modifier.weight(1f),
            decorationBox = { inner ->
                Box {
                    if (value.isEmpty()) Text("0", style = style.copy(color = faint))
                    inner()
                }
            },
        )
    }
}

/** − 62 + : the limit price in cents, one tick per tap, or typed. */
@Composable
private fun PriceStepper(draft: TradeDraft, onPrice: (String) -> Unit, onStep: (Int) -> Unit) {
    var text by remember(draft.outcome, draft.side) { mutableStateOf(priceToCents(draft.price)) }
    // A stepper tap or a new default changes the draft's price: show it, but never overwrite what is being typed.
    LaunchedEffect(draft.price) {
        val typed = centsToPrice(text).toBigDecimalOrNull()
        val actual = draft.price.toBigDecimalOrNull()
        val same = (typed == null && actual == null) ||
            (typed != null && actual != null && typed.compareTo(actual) == 0)
        if (!same) text = priceToCents(draft.price)
    }
    val strong = MaterialTheme.colorScheme.onSurface
    val faint = MaterialTheme.colorScheme.outline
    val cursor = MaterialTheme.colorScheme.primary
    val style = TextStyle(
        fontFamily = FontFamily.Monospace,
        fontWeight = FontWeight.Bold,
        fontSize = 36.sp,
        color = strong,
        textAlign = TextAlign.Center,
    )
    Row(verticalAlignment = Alignment.CenterVertically) {
        StepButton("−") { onStep(-1) }
        BasicTextField(
            value = text,
            onValueChange = { raw ->
                val cleaned = cleanDecimal(raw, 1)
                text = cleaned
                onPrice(centsToPrice(cleaned))
            },
            singleLine = true,
            textStyle = style,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
            cursorBrush = SolidColor(cursor),
            modifier = Modifier.weight(1f),
            decorationBox = { inner ->
                Box(contentAlignment = Alignment.Center) {
                    if (text.isEmpty()) Text("—", style = style.copy(color = faint))
                    inner()
                }
            },
        )
        StepButton("+") { onStep(1) }
    }
}

@Composable
private fun StepButton(label: String, onClick: () -> Unit) {
    Box(
        modifier = Modifier
            .size(48.dp)
            .clip(CircleShape)
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .clickable { onClick() },
        contentAlignment = Alignment.Center,
    ) {
        Text(label, style = MaterialTheme.typography.titleLarge, color = MaterialTheme.colorScheme.onSurface)
    }
}

/** "To win $8.01 (+60%)" for a buy, "You receive $5.20" for a sell, then the plain numbers. */
@Composable
private fun SummaryCard(draft: TradeDraft, estimate: TradeEstimate, buy: Boolean, hasInput: Boolean) {
    val big = TextStyle(fontFamily = FontFamily.Monospace, fontWeight = FontWeight.Bold, fontSize = 30.sp)
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(16.dp))
            .background(MaterialTheme.colorScheme.surface)
            .padding(16.dp),
    ) {
        Row(verticalAlignment = Alignment.Bottom) {
            Column(Modifier.weight(1f)) {
                Text(
                    if (buy) "To win" else "You receive",
                    style = MaterialTheme.typography.labelLarge,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Text(
                    when {
                        !estimate.valid -> "—"
                        buy -> Format.money(estimate.payoutUsd)
                        else -> Format.money(estimate.costUsd)
                    },
                    style = big,
                    color = if (estimate.valid) Palette.Long else MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            if (buy && estimate.valid && estimate.returnPct != "—") {
                Text(
                    "+${estimate.returnPct}%",
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.Bold,
                    color = Palette.Long,
                )
            }
        }
        Spacer(Modifier.height(10.dp))
        val approx = if (draft.isMarketOrder) " (est.)" else ""
        ValueRow("Avg price$approx", if (estimate.valid) centsText(estimate.avgPrice.toBigDecimalOrNull()) else "—")
        ValueRow("Shares$approx", if (estimate.valid) estimate.shares else "—")
        if (buy) {
            ValueRow("Cost", if (estimate.valid) Format.money(estimate.costUsd) else "—", emphasise = true)
        }
        if (estimate.problem != null) {
            Spacer(Modifier.height(6.dp))
            Text(
                estimate.problem,
                style = MaterialTheme.typography.bodySmall,
                color = if (hasInput) Palette.Alert else MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        if (estimate.note != null) {
            Spacer(Modifier.height(6.dp))
            Text(estimate.note, style = MaterialTheme.typography.bodySmall, color = Palette.Alert)
        }
        Spacer(Modifier.height(6.dp))
        Text(
            "The venue's fee is added when the order fills.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}
