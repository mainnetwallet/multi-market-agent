package com.mainnetwallet.mmagent.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import com.mainnetwallet.mmagent.data.ChatMessage
import com.mainnetwallet.mmagent.data.ChatUiState
import com.mainnetwallet.mmagent.data.Format

/**
 * A slash command shortcut. [sendNow] commands take no argument and go straight
 * out; the others only fill the input so the operator can finish them. Nothing
 * that moves money or panics is offered as a one-tap chip.
 */
private data class CommandChip(val text: String, val sendNow: Boolean)

private val COMMANDS = listOf(
    CommandChip("/status", true),
    CommandChip("/markets", true),
    CommandChip("/positions", true),
    CommandChip("/orders", true),
    CommandChip("/limits", true),
    CommandChip("/research ", false),
    CommandChip("/buy ", false),
    CommandChip("/help", true),
)

/** Chat with the agent. Slash commands run on the server and never reach the LLM. */
@Composable
fun ChatScreen(
    chat: ChatUiState,
    onSend: (String) -> Unit,
    onClear: () -> Unit,
) {
    var input by rememberSaveable { mutableStateOf("") }
    val listState = rememberLazyListState()

    // Keep the newest message in view, including the "working" row.
    LaunchedEffect(chat.messages.size, chat.busy) {
        val last = chat.messages.size + (if (chat.busy) 1 else 0) - 1
        if (last >= 0) listState.animateScrollToItem(last)
    }

    fun send() {
        val text = input.trim()
        if (text.isEmpty() || chat.busy) return
        onSend(text)
        input = ""
    }

    Column(modifier = Modifier.fillMaxSize().imePadding()) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(start = 16.dp, end = 8.dp, top = 4.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text("Agent chat", style = MaterialTheme.typography.titleMedium)
            if (chat.messages.isNotEmpty()) {
                TextButton(onClick = onClear, enabled = !chat.busy) { Text("Clear") }
            }
        }

        if (chat.messages.isEmpty()) {
            Box(modifier = Modifier.weight(1f).fillMaxWidth().padding(24.dp), contentAlignment = Alignment.Center) {
                Text(
                    "Ask about a market, or tap a command below. Slash commands answer instantly; " +
                        "plain questions go to the assistant if one is configured.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        } else {
            LazyColumn(
                state = listState,
                modifier = Modifier.weight(1f).fillMaxWidth(),
                contentPadding = PaddingValues(horizontal = 16.dp, vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                items(chat.messages) { message -> Bubble(message) }
                if (chat.busy) {
                    item {
                        Text(
                            "Working…",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
            }
        }

        LazyRow(
            contentPadding = PaddingValues(horizontal = 16.dp, vertical = 6.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            items(COMMANDS) { chip ->
                ChoiceChip(
                    label = chip.text.trim(),
                    selected = false,
                    enabled = !chat.busy,
                    onClick = { if (chip.sendNow) onSend(chip.text) else input = chip.text },
                )
            }
        }

        Row(
            modifier = Modifier.fillMaxWidth().padding(start = 16.dp, end = 8.dp, bottom = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            OutlinedTextField(
                value = input,
                onValueChange = { input = it.take(4000) },
                placeholder = { Text("Message or /command") },
                maxLines = 4,
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                keyboardActions = KeyboardActions(onSend = { send() }),
                modifier = Modifier.weight(1f),
            )
            TextButton(onClick = { send() }, enabled = input.isNotBlank() && !chat.busy) { Text("Send") }
        }
    }
}

@Composable
private fun Bubble(message: ChatMessage) {
    val fromUser = message.role == "user"
    val isError = message.role == "error"
    val background = when {
        fromUser -> MaterialTheme.colorScheme.primary
        isError -> Palette.Danger.copy(alpha = 0.15f)
        else -> MaterialTheme.colorScheme.surface
    }
    val textColor = when {
        fromUser -> androidx.compose.ui.graphics.Color.White
        isError -> Palette.Danger
        else -> MaterialTheme.colorScheme.onSurface
    }
    Column(
        modifier = Modifier.fillMaxWidth(),
        horizontalAlignment = if (fromUser) Alignment.End else Alignment.Start,
    ) {
        Box(
            modifier = Modifier
                .widthIn(max = 320.dp)
                .clip(RoundedCornerShape(14.dp))
                .background(background)
                .padding(horizontal = 12.dp, vertical = 8.dp),
        ) {
            SelectionContainer {
                Text(
                    message.text,
                    color = textColor,
                    style = MaterialTheme.typography.bodyMedium,
                    // Command replies are tables and lists; fixed width keeps them aligned.
                    fontFamily = if (message.role == "agent" && message.text.contains("\n")) FontFamily.Monospace else null,
                )
            }
        }
        Spacer(Modifier.height(2.dp))
        Text(
            Format.clock(message.at),
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}
