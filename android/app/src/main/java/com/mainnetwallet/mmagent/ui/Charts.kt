package com.mainnetwallet.mmagent.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.dp
import com.mainnetwallet.mmagent.data.Format
import java.math.BigDecimal

/** One level of an order book. */
data class BookLevel(val price: BigDecimal, val shares: BigDecimal)

/**
 * Order-book depth, drawn with Compose Canvas. No chart library.
 *
 * Bids are green, asks are red, and both carry a text label so the direction is
 * never colour-only.
 */
@Composable
fun DepthView(
    bids: List<BookLevel>,
    asks: List<BookLevel>,
    modifier: Modifier = Modifier,
) {
    // Colours are read outside the Canvas: the draw scope is not a composable scope.
    val outline = MaterialTheme.colorScheme.outline
    val panel = MaterialTheme.colorScheme.surfaceVariant
    Column(modifier = modifier.fillMaxWidth()) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            Text("Bids ▲", style = MaterialTheme.typography.labelSmall, color = Palette.Long)
            Text("Depth", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Text("▼ Asks", style = MaterialTheme.typography.labelSmall, color = Palette.Short)
        }
        Canvas(
            modifier = Modifier
                .fillMaxWidth()
                .height(140.dp)
                .padding(vertical = 6.dp)
                .clip(RoundedCornerShape(8.dp))
                .background(panel),
        ) {
            val maxSize = (bids + asks).maxOfOrNull { it.shares.toDouble() } ?: 1.0
            if (maxSize <= 0.0) return@Canvas
            val mid = size.width / 2f
            val barHeight = size.height / (maxOf(bids.size, asks.size, 1) + 1)
            fun barHeightFor(index: Int): Float = barHeight * (index + 1)

            bids.asReversed().take(10).forEachIndexed { index, level ->
                val y = size.height - barHeightFor(index)
                val width = (level.shares.toDouble() / maxSize * (mid - 2f)).toFloat()
                drawRect(
                    color = Palette.Long.copy(alpha = 0.55f),
                    topLeft = Offset(mid - width, y),
                    size = Size(width, barHeight - 2f),
                )
            }
            asks.take(10).forEachIndexed { index, level ->
                val y = size.height - barHeightFor(index)
                val width = (level.shares.toDouble() / maxSize * (mid - 2f)).toFloat()
                drawRect(
                    color = Palette.Short.copy(alpha = 0.55f),
                    topLeft = Offset(mid, y),
                    size = Size(width, barHeight - 2f),
                )
            }
            drawLine(
                color = outline,
                start = Offset(mid, 0f),
                end = Offset(mid, size.height),
                strokeWidth = 1f,
            )
        }
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(
                bids.firstOrNull()?.let { "${Format.cents(it.price.toPlainString())} × ${Format.compact(it.shares.toPlainString())}" } ?: "—",
                style = MaterialTheme.typography.bodySmall,
                color = Palette.Long,
            )
            Text(
                asks.firstOrNull()?.let { "${Format.cents(it.price.toPlainString())} × ${Format.compact(it.shares.toPlainString())}" } ?: "—",
                style = MaterialTheme.typography.bodySmall,
                color = Palette.Short,
            )
        }
    }
}

/** One point of a price series. */
data class PricePoint(val at: String, val close: BigDecimal)

/**
 * A price-history line, drawn with Compose Canvas. No chart library.
 */
@Composable
fun PriceChart(
    points: List<PricePoint>,
    modifier: Modifier = Modifier,
    lineColor: Color = Palette.Neutral,
) {
    val panel = MaterialTheme.colorScheme.surfaceVariant
    Column(modifier = modifier.fillMaxWidth()) {
        Canvas(
            modifier = Modifier
                .fillMaxWidth()
                .height(120.dp)
                .clip(RoundedCornerShape(8.dp))
                .background(panel),
        ) {
            if (points.size < 2) return@Canvas
            val values = points.map { it.close.toDouble() }
            val min = values.min()
            val max = values.max()
            val span = (max - min).takeIf { it > 1e-9 } ?: 1.0
            val stepX = size.width / (points.size - 1)
            val path = Path()
            values.forEachIndexed { index, value ->
                val x = index * stepX
                val y = size.height - ((value - min) / span * size.height).toFloat()
                if (index == 0) path.moveTo(x, y) else path.lineTo(x, y)
            }
            drawPath(path, color = lineColor, style = Stroke(width = 3f))
            val last = values.last()
            val lastY = size.height - ((last - min) / span * size.height).toFloat()
            drawCircle(color = lineColor, radius = 5f, center = Offset(size.width - stepX / 2, lastY))
        }
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(
                points.firstOrNull()?.let { Format.dhaka(it.at) } ?: "—",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Text(
                points.lastOrNull()?.let { Format.cents(it.close.toPlainString()) } ?: "—",
                style = MaterialTheme.typography.titleSmall,
                color = lineColor,
            )
            Text(
                points.lastOrNull()?.let { Format.dhaka(it.at) } ?: "—",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/** A daily PnL series: sign, arrow and colour together. */
@Composable
fun PnlChart(
    days: List<Pair<String, BigDecimal>>,
    modifier: Modifier = Modifier,
) {
    val panel = MaterialTheme.colorScheme.surfaceVariant
    val outline = MaterialTheme.colorScheme.outline
    Column(modifier = modifier.fillMaxWidth()) {
        Canvas(
            modifier = Modifier
                .fillMaxWidth()
                .height(100.dp)
                .clip(RoundedCornerShape(8.dp))
                .background(panel),
        ) {
            if (days.isEmpty()) return@Canvas
            val values = days.map { it.second.toDouble() }
            val max = values.max().coerceAtLeast(0.0)
            val min = values.min().coerceAtMost(0.0)
            val span = (max - min).takeIf { it > 1e-9 } ?: 1.0
            val stepX = size.width / days.size
            val zeroY = size.height - ((0.0 - min) / span * size.height).toFloat()
            drawLine(
                color = outline,
                start = Offset(0f, zeroY),
                end = Offset(size.width, zeroY),
                strokeWidth = 2f,
            )
            val path = Path()
            values.forEachIndexed { index, value ->
                val x = index * stepX + stepX / 2
                val y = size.height - ((value - min) / span * size.height).toFloat()
                if (index == 0) path.moveTo(x, y) else path.lineTo(x, y)
            }
            drawPath(path, color = Palette.Neutral, style = Stroke(width = 3f))
        }
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            val last = days.lastOrNull()?.second
            val positive = (last ?: BigDecimal.ZERO).signum() > 0
            Text(
                text = if (positive) "▲ ${Format.signedMoney(last?.toPlainString())}" else "▼ ${Format.signedMoney(last?.toPlainString())}",
                style = MaterialTheme.typography.titleSmall,
                color = if (positive) Palette.Long else Palette.Short,
            )
            Text(
                text = "${days.size} days",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}
