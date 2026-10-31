package com.mainnetwallet.mmagent.ui

import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp

/**
 * The top bar: a compact venue badge, then (optionally) the market search, then the extras (PAUSED, settings).
 * Every order is real, so the badge always says LIVE. It is plain text on a neutral pill, never colour-only.
 * [searchQuery] null means no search box in this bar.
 */
@Composable
fun ModeFrame(
    venue: String,
    paused: Boolean,
    modifier: Modifier = Modifier,
    onBadgeClick: (() -> Unit)? = null,
    searchQuery: String? = null,
    onSearchQuery: (String) -> Unit = {},
    trailing: @Composable (() -> Unit)? = null,
    content: @Composable () -> Unit,
) {
    Column(modifier = modifier) {
        Row(
            modifier = Modifier
                .statusBarsPadding()
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 2.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            // Tap the badge to switch venue. The LIVE text stays in the badge, so it is
            // never colour-only and never ambiguous.
            Text(
                text = "${venue.uppercase()} | LIVE" + if (onBadgeClick != null) " ▾" else "",
                color = MaterialTheme.colorScheme.onSurface,
                style = MaterialTheme.typography.labelSmall,
                fontWeight = FontWeight.SemiBold,
                maxLines = 1,
                softWrap = false,
                modifier = Modifier
                    .clip(RoundedCornerShape(999.dp))
                    .background(MaterialTheme.colorScheme.surfaceVariant)
                    .then(if (onBadgeClick != null) Modifier.clickable { onBadgeClick() } else Modifier)
                    .padding(horizontal = 10.dp, vertical = 4.dp),
            )
            if (searchQuery != null) {
                HeaderSearchField(searchQuery, onSearchQuery)
            } else {
                Spacer(Modifier.weight(1f))
            }
            if (paused) {
                Text(
                    text = "PAUSED",
                    color = Palette.Alert,
                    style = MaterialTheme.typography.labelMedium,
                    modifier = Modifier
                        .clip(RoundedCornerShape(999.dp))
                        .border(1.dp, Palette.Alert, RoundedCornerShape(999.dp))
                        .padding(horizontal = 10.dp, vertical = 5.dp),
                )
            }
            trailing?.invoke()
        }
        content()
    }
}

/** A compact search pill for the top bar: icon, hint, clear button. Fills the space between badge and gear. */
@Composable
fun RowScope.HeaderSearchField(query: String, onQueryChange: (String) -> Unit) {
    val shape = RoundedCornerShape(999.dp)
    val hint = MaterialTheme.colorScheme.onSurfaceVariant
    BasicTextField(
        value = query,
        onValueChange = onQueryChange,
        singleLine = true,
        textStyle = MaterialTheme.typography.bodyMedium.copy(color = MaterialTheme.colorScheme.onSurface),
        cursorBrush = SolidColor(MaterialTheme.colorScheme.primary),
        modifier = Modifier
            .weight(1f)
            .height(40.dp)
            .clip(shape)
            .background(MaterialTheme.colorScheme.surface)
            .border(1.dp, MaterialTheme.colorScheme.outline.copy(alpha = 0.5f), shape),
        decorationBox = { inner ->
            Row(
                modifier = Modifier.padding(horizontal = 12.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Icon(Icons.Filled.Search, contentDescription = null, tint = hint, modifier = Modifier.size(18.dp))
                Spacer(Modifier.width(8.dp))
                Box(modifier = Modifier.weight(1f), contentAlignment = Alignment.CenterStart) {
                    if (query.isEmpty()) {
                        Text(
                            "Search markets",
                            style = MaterialTheme.typography.bodyMedium,
                            color = hint,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                    inner()
                }
                if (query.isNotEmpty()) {
                    Icon(
                        Icons.Filled.Close,
                        contentDescription = "Clear search",
                        tint = hint,
                        modifier = Modifier.size(18.dp).clickable { onQueryChange("") },
                    )
                }
            }
        },
    )
}

/**
 * Press-and-hold to confirm. Used for every action that adds risk or moves money
 * out. Risk-reducing actions (cancel, pause) use a normal button instead.
 */
@Composable
fun HoldToConfirmButton(
    label: String,
    enabled: Boolean,
    busy: Boolean = false,
    danger: Boolean = false,
    onConfirmed: () -> Unit,
    modifier: Modifier = Modifier,
) {
    var progress by remember { mutableFloatStateOf(0f) }
    val animated by animateFloatAsState(targetValue = progress, label = "holdProgress")

    Box(
        modifier = modifier
            .fillMaxWidth()
            .height(56.dp)
            .clip(RoundedCornerShape(14.dp))
            .background(
                when {
                    !enabled -> MaterialTheme.colorScheme.surfaceVariant
                    danger -> Palette.Danger
                    else -> MaterialTheme.colorScheme.primary
                }
            )
            .pointerInput(enabled, busy) {
                if (!enabled || busy) return@pointerInput
                val width = size.width.toFloat()
                awaitEachGesture {
                    val down = awaitFirstDown(requireUnconsumed = false)
                    down.consume()
                    while (true) {
                        val event = awaitPointerEvent()
                        val change = event.changes.firstOrNull { it.id == down.id } ?: break
                        if (!change.pressed) break
                        progress = (change.position.x / width).coerceIn(0f, 1f)
                        change.consume()
                        if (progress >= 1f) {
                            progress = 0f
                            onConfirmed()
                            break
                        }
                    }
                    progress = 0f
                }
            }
            .semantics { contentDescription = "$label. Press and hold to confirm." },
        contentAlignment = Alignment.Center,
    ) {
        Box(
            modifier = Modifier
                .fillMaxWidth(animated)
                .height(56.dp)
                .background(Color.White.copy(alpha = 0.14f)),
        )
        Text(
            text = if (busy) "Working…" else label,
            color = if (enabled) Color.White else MaterialTheme.colorScheme.onSurfaceVariant,
            style = MaterialTheme.typography.titleMedium,
            textAlign = TextAlign.Center,
        )
    }
}

/** A plain tap button for actions that place or move nothing. */
@Composable
fun ConfirmButton(
    label: String,
    enabled: Boolean,
    busy: Boolean = false,
    danger: Boolean = false,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Button(
        onClick = onClick,
        enabled = enabled && !busy,
        modifier = modifier.fillMaxWidth().height(52.dp),
        shape = RoundedCornerShape(14.dp),
        colors = ButtonDefaults.buttonColors(
            containerColor = when {
                !enabled -> MaterialTheme.colorScheme.surfaceVariant
                danger -> Palette.Danger
                else -> MaterialTheme.colorScheme.primary
            },
            contentColor = if (enabled) Color.White else MaterialTheme.colorScheme.onSurfaceVariant,
        ),
    ) {
        Text(if (busy) "Working…" else label, style = MaterialTheme.typography.titleMedium)
    }
}

/** A small tappable chip used for order types and outcomes. */
@Composable
fun ChoiceChip(
    label: String,
    selected: Boolean,
    enabled: Boolean = true,
    accent: Color = MaterialTheme.colorScheme.primary,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val background = if (selected) accent else MaterialTheme.colorScheme.surfaceVariant
    val content = if (selected) Color.White else MaterialTheme.colorScheme.onSurfaceVariant
    Text(
        text = label,
        color = content,
        style = MaterialTheme.typography.labelLarge,
        modifier = modifier
            .clip(RoundedCornerShape(999.dp))
            .background(background)
            .clickable(enabled = enabled) { onClick() }
            .padding(horizontal = 14.dp, vertical = 10.dp),
    )
}

/** A label/value row; the value is monospace so columns line up. */
@Composable
fun ValueRow(
    label: String,
    value: String,
    modifier: Modifier = Modifier,
    valueColor: Color = MaterialTheme.colorScheme.onSurface,
    emphasise: Boolean = false,
) {
    Row(
        modifier = modifier.fillMaxWidth().padding(vertical = 3.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = label,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.width(12.dp))
        Text(
            text = value,
            style = if (emphasise) MaterialTheme.typography.titleMedium else MaterialTheme.typography.bodyMedium,
            fontFamily = if (emphasise) androidx.compose.ui.text.font.FontFamily.Monospace else null,
            color = valueColor,
            textAlign = TextAlign.End,
        )
    }
}

/** A section heading with an optional trailing note. */
@Composable
fun SectionHeader(title: String, note: String? = null, modifier: Modifier = Modifier) {
    Row(
        modifier = modifier.fillMaxWidth().padding(vertical = 6.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(title, style = MaterialTheme.typography.titleMedium)
        if (!note.isNullOrBlank()) {
            Text(
                note,
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/** An inline error the operator can act on, distinct per failure kind. */
@Composable
fun ErrorNote(message: String?, modifier: Modifier = Modifier) {
    if (message.isNullOrBlank()) return
    Column(
        modifier = modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .background(Palette.Danger.copy(alpha = 0.12f))
            .border(1.dp, Palette.Danger.copy(alpha = 0.5f), RoundedCornerShape(10.dp))
            .padding(10.dp),
    ) {
        Text(message, color = Palette.Danger, style = MaterialTheme.typography.bodyMedium)
    }
}

/** A card that says a feature is not available here, with the reason. */
@Composable
fun UnsupportedCard(
    title: String,
    reason: String,
    actionLabel: String? = null,
    onAction: (() -> Unit)? = null,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .padding(14.dp),
    ) {
        Text(title, style = MaterialTheme.typography.titleSmall)
        Spacer(Modifier.height(4.dp))
        Text(reason, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        if (actionLabel != null && onAction != null) {
            Spacer(Modifier.height(10.dp))
            Text(
                actionLabel,
                color = MaterialTheme.colorScheme.secondary,
                style = MaterialTheme.typography.labelLarge,
                modifier = Modifier
                    .clip(RoundedCornerShape(10.dp))
                    .clickable { onAction() }
                    .padding(vertical = 10.dp, horizontal = 14.dp),
            )
        }
    }
}
