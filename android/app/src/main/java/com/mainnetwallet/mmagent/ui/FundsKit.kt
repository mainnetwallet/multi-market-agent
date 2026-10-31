package com.mainnetwallet.mmagent.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.google.zxing.BarcodeFormat
import com.google.zxing.EncodeHintType
import com.google.zxing.qrcode.QRCodeWriter
import com.google.zxing.qrcode.decoder.ErrorCorrectionLevel

/*
 * Building blocks shared by the Deposit and Withdraw screens: a top bar, rounded cards, selector fields that open
 * a searchable bottom-sheet list, a coin avatar, a QR code, notices and a status timeline. Nothing here knows
 * about venues or chains; the screens feed it whatever the server listed.
 */

/** "0xA0b8…eB48": enough to tell two contracts apart without filling the row. */
internal fun shortMiddle(value: String): String =
    if (value.length <= 14) value else value.take(6) + "…" + value.takeLast(4)

private val AvatarColors = listOf(
    Color(0xFF2775CA),
    Color(0xFF26A17B),
    Color(0xFF8247E5),
    Color(0xFFE08A1E),
    Color(0xFF4F6BED),
    Color(0xFFD9485B),
    Color(0xFF0E9FBC),
    Color(0xFF3B9C74),
)

/** A round letter badge for a coin or a network. The colour comes from the name, so it is stable. */
@Composable
fun CoinAvatar(symbol: String, size: Dp = 36.dp) {
    val letters = symbol.filter { it.isLetterOrDigit() }.take(2).uppercase().ifBlank { "?" }
    val color = AvatarColors[(symbol.lowercase().hashCode() and 0x7fffffff) % AvatarColors.size]
    Box(
        modifier = Modifier.size(size).clip(CircleShape).background(color),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            letters,
            color = Color.White,
            fontWeight = FontWeight.Bold,
            fontSize = (size.value * 0.38f).sp,
        )
    }
}

/** The top bar of a full-screen flow: a back arrow, the title and a small tag (the venue). */
@Composable
fun FundsTopBar(title: String, tag: String, onBack: () -> Unit) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(bottom = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            modifier = Modifier
                .size(40.dp)
                .clip(CircleShape)
                .clickable { onBack() },
            contentAlignment = Alignment.Center,
        ) {
            Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
        }
        Spacer(Modifier.width(8.dp))
        Text(title, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
        Spacer(Modifier.weight(1f))
        if (tag.isNotBlank()) {
            Text(
                tag,
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier
                    .clip(RoundedCornerShape(999.dp))
                    .background(MaterialTheme.colorScheme.surface)
                    .padding(horizontal = 12.dp, vertical = 6.dp),
            )
        }
    }
}

/** The rounded card every block of these screens sits in. */
@Composable
fun FundsCard(modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(16.dp))
            .background(MaterialTheme.colorScheme.surface)
            .border(1.dp, MaterialTheme.colorScheme.outline.copy(alpha = 0.5f), RoundedCornerShape(16.dp))
            .padding(16.dp),
        content = content,
    )
}

/** A small grey caption above a field. */
@Composable
fun FieldLabel(text: String, modifier: Modifier = Modifier) {
    Text(
        text,
        style = MaterialTheme.typography.labelLarge,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = modifier.padding(bottom = 6.dp),
    )
}

/** One entry of a [PickerSheet]. [symbol] drives the avatar; [subtitle] is the small second line. */
data class PickerOption(
    val key: String,
    val title: String,
    val subtitle: String? = null,
    val symbol: String? = null,
    val enabled: Boolean = true,
)

/**
 * A tappable field that shows the current choice (with an avatar) and a down arrow. It only reports the tap; the
 * screen decides what opens, normally a [PickerSheet]. [compact] makes it one line high so two fit side by side.
 */
@Composable
fun SelectorField(
    label: String,
    value: String,
    modifier: Modifier = Modifier,
    placeholder: String = "Select",
    hint: String? = null,
    avatarSymbol: String? = null,
    enabled: Boolean = true,
    compact: Boolean = false,
    onClick: () -> Unit,
) {
    Column(modifier.fillMaxWidth()) {
        FieldLabel(label)
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(14.dp))
                .background(MaterialTheme.colorScheme.surface)
                .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(14.dp))
                .clickable(enabled = enabled) { onClick() }
                .padding(horizontal = if (compact) 10.dp else 14.dp, vertical = if (compact) 9.dp else 12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            if (avatarSymbol != null && value.isNotBlank()) {
                CoinAvatar(avatarSymbol, size = if (compact) 26.dp else 32.dp)
                Spacer(Modifier.width(if (compact) 8.dp else 12.dp))
            }
            Column(Modifier.weight(1f)) {
                Text(
                    value.ifBlank { placeholder },
                    style = if (compact) MaterialTheme.typography.titleSmall else MaterialTheme.typography.titleMedium,
                    color = if (value.isBlank()) MaterialTheme.colorScheme.onSurfaceVariant else MaterialTheme.colorScheme.onSurface,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                // The compact form is one line high: the contract hint lives in the coin list instead.
                if (!compact && !hint.isNullOrBlank()) {
                    Text(
                        hint,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
            }
            Icon(
                Icons.Filled.KeyboardArrowDown,
                contentDescription = null,
                tint = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/** A bottom sheet with a list to pick one entry from; a search box appears when the list is long. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PickerSheet(
    title: String,
    options: List<PickerOption>,
    selectedKey: String,
    onSelect: (String) -> Unit,
    onDismiss: () -> Unit,
) {
    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    var query by remember { mutableStateOf("") }
    val shown = if (query.isBlank()) {
        options
    } else {
        options.filter {
            it.title.contains(query, ignoreCase = true) || it.subtitle.orEmpty().contains(query, ignoreCase = true)
        }
    }
    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = sheetState,
        containerColor = MaterialTheme.colorScheme.surface,
    ) {
        Text(
            title,
            style = MaterialTheme.typography.titleMedium,
            modifier = Modifier.padding(horizontal = 20.dp, vertical = 4.dp),
        )
        if (options.size > 8) {
            OutlinedTextField(
                value = query,
                onValueChange = { query = it },
                placeholder = { Text("Search") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
            )
        }
        LazyColumn(
            modifier = Modifier.fillMaxWidth().heightIn(max = 480.dp),
            contentPadding = PaddingValues(bottom = 24.dp),
        ) {
            items(shown) { option ->
                val selected = option.key == selectedKey
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .clickable(enabled = option.enabled) { onSelect(option.key) }
                        .padding(horizontal = 20.dp, vertical = 12.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    CoinAvatar(option.symbol ?: option.title, size = 34.dp)
                    Spacer(Modifier.width(14.dp))
                    Column(Modifier.weight(1f)) {
                        Text(
                            option.title,
                            style = MaterialTheme.typography.titleMedium,
                            color = if (option.enabled) MaterialTheme.colorScheme.onSurface else MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        if (!option.subtitle.isNullOrBlank()) {
                            Text(
                                option.subtitle,
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    }
                    if (selected) {
                        Icon(Icons.Filled.Check, contentDescription = "Selected", tint = MaterialTheme.colorScheme.primary)
                    }
                }
            }
        }
    }
}

/** A QR code for [content], drawn on a white tile so any scanner can read it in dark mode too. */
@Composable
fun QrCode(content: String, modifier: Modifier = Modifier) {
    val matrix = remember(content) {
        runCatching {
            QRCodeWriter().encode(
                content,
                BarcodeFormat.QR_CODE,
                0,
                0,
                mapOf(EncodeHintType.MARGIN to 0, EncodeHintType.ERROR_CORRECTION to ErrorCorrectionLevel.M),
            )
        }.getOrNull()
    }
    Box(
        modifier = modifier
            .clip(RoundedCornerShape(16.dp))
            .background(Color.White)
            .padding(14.dp),
        contentAlignment = Alignment.Center,
    ) {
        matrix?.let { m ->
            Canvas(Modifier.fillMaxSize()) {
                val cell = size.minDimension / m.width
                for (x in 0 until m.width) {
                    for (y in 0 until m.height) {
                        if (m.get(x, y)) {
                            drawRect(
                                color = Color.Black,
                                topLeft = Offset(x * cell, y * cell),
                                size = Size(cell + 0.5f, cell + 0.5f),
                            )
                        }
                    }
                }
            }
        }
    }
}

/** A tinted notice with a warning mark; the mark means the colour is never the only signal. */
@Composable
fun NoticeBanner(text: String, modifier: Modifier = Modifier, color: Color = Palette.Alert) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(color.copy(alpha = 0.12f))
            .border(1.dp, color.copy(alpha = 0.4f), RoundedCornerShape(12.dp))
            .padding(12.dp),
        verticalAlignment = Alignment.Top,
    ) {
        Icon(Icons.Filled.Warning, contentDescription = null, tint = color, modifier = Modifier.size(18.dp))
        Spacer(Modifier.width(10.dp))
        Text(text, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurface)
    }
}

/** A key/value row for a summary card: grey label left, value right (monospace when it is a number). */
@Composable
fun SummaryRow(label: String, value: String, modifier: Modifier = Modifier, mono: Boolean = false) {
    Row(
        modifier = modifier.fillMaxWidth().padding(vertical = 5.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.Top,
    ) {
        Text(
            label,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.width(16.dp))
        Text(
            value,
            style = MaterialTheme.typography.bodyMedium,
            fontWeight = FontWeight.Medium,
            fontFamily = if (mono) androidx.compose.ui.text.font.FontFamily.Monospace else null,
            textAlign = TextAlign.End,
            modifier = Modifier.weight(1f, fill = false),
        )
    }
}

/** How far a step of a [StepTimeline] has got. */
enum class StepState { DONE, ACTIVE, PENDING }

/** A vertical list of steps with a numbered or ticked dot, used for "how it works" and withdrawal progress. */
@Composable
fun StepTimeline(steps: List<Pair<String, StepState>>, modifier: Modifier = Modifier) {
    Column(modifier) {
        steps.forEachIndexed { index, (label, state) ->
            Row(verticalAlignment = Alignment.Top) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    val fill = when (state) {
                        StepState.DONE -> MaterialTheme.colorScheme.primary
                        StepState.ACTIVE -> Palette.Alert
                        StepState.PENDING -> MaterialTheme.colorScheme.surfaceVariant
                    }
                    Box(
                        modifier = Modifier.size(24.dp).clip(CircleShape).background(fill),
                        contentAlignment = Alignment.Center,
                    ) {
                        if (state == StepState.DONE) {
                            Icon(Icons.Filled.Check, contentDescription = "Done", tint = Color.White, modifier = Modifier.size(15.dp))
                        } else {
                            Text(
                                "${index + 1}",
                                style = MaterialTheme.typography.labelSmall,
                                color = if (state == StepState.ACTIVE) Color.Black else MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    }
                    if (index < steps.lastIndex) {
                        Box(
                            Modifier
                                .width(2.dp)
                                .height(18.dp)
                                .background(MaterialTheme.colorScheme.outline.copy(alpha = 0.6f)),
                        )
                    }
                }
                Spacer(Modifier.width(12.dp))
                Text(
                    label,
                    style = MaterialTheme.typography.bodyMedium,
                    color = if (state == StepState.PENDING) MaterialTheme.colorScheme.onSurfaceVariant else MaterialTheme.colorScheme.onSurface,
                    modifier = Modifier.padding(top = 2.dp, bottom = if (index < steps.lastIndex) 12.dp else 0.dp),
                )
            }
        }
    }
}
