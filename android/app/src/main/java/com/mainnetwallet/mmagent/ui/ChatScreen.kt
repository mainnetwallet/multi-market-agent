package com.mainnetwallet.mmagent.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.ArrowUpward
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.ContentCopy
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.ErrorOutline
import androidx.compose.material.icons.filled.Menu
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DrawerValue
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalDrawerSheet
import androidx.compose.material3.ModalNavigationDrawer
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TextField
import androidx.compose.material3.TextFieldDefaults
import androidx.compose.material3.rememberDrawerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.luminance
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.mainnetwallet.mmagent.data.ChatMessage
import com.mainnetwallet.mmagent.data.ChatUiState
import com.mainnetwallet.mmagent.data.Conversation
import com.mainnetwallet.mmagent.data.Format
import com.mainnetwallet.mmagent.data.ToolStep
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

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

/**
 * Chat with the agent. Slash commands run on the server and never reach the LLM.
 * The history drawer, search, rename and delete work on chats saved on this device.
 */
@Composable
fun ChatScreen(
    chat: ChatUiState,
    onSend: (String) -> Unit,
    onStop: () -> Unit,
    onNewChat: () -> Unit,
    onOpenChat: (String) -> Unit,
    onRenameChat: (String, String) -> Unit,
    onDeleteChat: (String) -> Unit,
) {
    var input by rememberSaveable { mutableStateOf("") }
    val listState = rememberLazyListState()
    val drawerState = rememberDrawerState(DrawerValue.Closed)
    val scope = rememberCoroutineScope()
    val clipboard = LocalClipboardManager.current

    BackHandler(enabled = drawerState.isOpen) { scope.launch { drawerState.close() } }

    // A new message scrolls into view; the spacer after the last message makes "the end" reachable.
    LaunchedEffect(chat.messages.size, chat.activeId) {
        if (chat.messages.isNotEmpty()) listState.animateScrollToItem(chat.messages.size)
    }
    // While a reply streams in, follow it, but only if the reader has not scrolled away.
    val replyLength = chat.messages.lastOrNull()?.text?.length ?: 0
    LaunchedEffect(replyLength) {
        val lastVisible = listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0
        if (chat.busy && chat.messages.isNotEmpty() && lastVisible >= chat.messages.lastIndex) {
            listState.scrollToItem(chat.messages.size)
        }
    }

    fun send() {
        val text = input.trim()
        if (text.isEmpty() || chat.busy) return
        onSend(text)
        input = ""
    }

    ModalNavigationDrawer(
        drawerState = drawerState,
        drawerContent = {
            ChatHistoryDrawer(
                chat = chat,
                onNewChat = {
                    onNewChat()
                    scope.launch { drawerState.close() }
                },
                onOpen = {
                    onOpenChat(it)
                    scope.launch { drawerState.close() }
                },
                onRename = onRenameChat,
                onDelete = onDeleteChat,
            )
        },
    ) {
        Column(modifier = Modifier.fillMaxSize().imePadding()) {
            Row(
                modifier = Modifier.fillMaxWidth().padding(horizontal = 4.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                IconButton(onClick = { scope.launch { drawerState.open() } }) {
                    Icon(Icons.Filled.Menu, contentDescription = "Chat history")
                }
                Text(
                    chat.conversations.firstOrNull { it.id == chat.activeId }?.title ?: "New chat",
                    style = MaterialTheme.typography.titleMedium,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f),
                )
                IconButton(onClick = onNewChat, enabled = !chat.busy) {
                    Icon(Icons.Filled.Add, contentDescription = "New chat")
                }
            }

            if (chat.messages.isEmpty()) {
                Box(modifier = Modifier.weight(1f).fillMaxWidth().padding(24.dp), contentAlignment = Alignment.Center) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text("How can I help?", style = MaterialTheme.typography.titleMedium)
                        Text(
                            "Ask about a market, or tap a command below. Slash commands answer instantly; " +
                                "plain questions go to the assistant if one is configured.",
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
            } else {
                LazyColumn(
                    state = listState,
                    modifier = Modifier.weight(1f).fillMaxWidth(),
                    contentPadding = PaddingValues(horizontal = 16.dp, vertical = 8.dp),
                    verticalArrangement = Arrangement.spacedBy(16.dp),
                ) {
                    itemsIndexed(chat.messages, key = { index, m -> "$index-${m.role}-${m.at}" }) { index, message ->
                        val previous = chat.messages.getOrNull(index - 1)
                        // Command replies are tables and lists, not markdown: keep them aligned.
                        val fromCommand = previous != null && previous.role == "user" && previous.text.startsWith("/")
                        MessageRow(message, plain = fromCommand, onCopy = { clipboard.setText(AnnotatedString(it)) })
                    }
                    item(key = "end") { Spacer(Modifier.height(1.dp)) }
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

            Composer(
                input = input,
                onInput = { input = it.take(4000) },
                busy = chat.busy,
                onSend = { send() },
                onStop = onStop,
            )
        }
    }
}

// ------------------------------------------------------------------ composer

@Composable
private fun Composer(input: String, onInput: (String) -> Unit, busy: Boolean, onSend: () -> Unit, onStop: () -> Unit) {
    val shape = RoundedCornerShape(26.dp)
    val canAct = busy || input.isNotBlank()
    Row(
        modifier = Modifier.fillMaxWidth().padding(start = 12.dp, end = 12.dp, bottom = 8.dp),
        verticalAlignment = Alignment.Bottom,
    ) {
        Box(
            modifier = Modifier
                .weight(1f)
                .clip(shape)
                .background(MaterialTheme.colorScheme.surface)
                .border(1.dp, MaterialTheme.colorScheme.outline, shape),
        ) {
            TextField(
                value = input,
                onValueChange = onInput,
                placeholder = { Text("Message or /command") },
                maxLines = 6,
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                keyboardActions = KeyboardActions(onSend = { onSend() }),
                colors = TextFieldDefaults.colors(
                    focusedContainerColor = Color.Transparent,
                    unfocusedContainerColor = Color.Transparent,
                    disabledContainerColor = Color.Transparent,
                    focusedIndicatorColor = Color.Transparent,
                    unfocusedIndicatorColor = Color.Transparent,
                    disabledIndicatorColor = Color.Transparent,
                ),
                modifier = Modifier.fillMaxWidth(),
            )
        }
        Spacer(Modifier.width(8.dp))
        Box(
            modifier = Modifier
                .padding(bottom = 4.dp)
                .size(48.dp)
                .clip(CircleShape)
                .background(if (canAct) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.surfaceVariant)
                .clickable(enabled = canAct, role = Role.Button) { if (busy) onStop() else onSend() },
            contentAlignment = Alignment.Center,
        ) {
            Icon(
                imageVector = if (busy) Icons.Filled.Stop else Icons.Filled.ArrowUpward,
                contentDescription = if (busy) "Stop generating" else "Send",
                tint = if (canAct) MaterialTheme.colorScheme.onPrimary else MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

// ------------------------------------------------------------------ messages

@Composable
private fun MessageRow(message: ChatMessage, plain: Boolean, onCopy: (String) -> Unit) {
    when (message.role) {
        "user" -> UserMessage(message, onCopy)
        "error" -> ErrorMessage(message)
        else -> AgentMessage(message, plain, onCopy)
    }
}

@Composable
private fun UserMessage(message: ChatMessage, onCopy: (String) -> Unit) {
    Column(modifier = Modifier.fillMaxWidth(), horizontalAlignment = Alignment.End) {
        Box(
            modifier = Modifier
                .widthIn(max = 320.dp)
                .clip(RoundedCornerShape(20.dp, 20.dp, 4.dp, 20.dp))
                .background(MaterialTheme.colorScheme.primary.copy(alpha = 0.16f))
                .padding(horizontal = 14.dp, vertical = 10.dp),
        ) {
            SelectionContainer {
                Text(message.text, color = MaterialTheme.colorScheme.onSurface, style = MaterialTheme.typography.bodyLarge)
            }
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(Format.clock(message.at), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            CopyAction(label = "Copy message", onClick = { onCopy(message.text) })
        }
    }
}

@Composable
private fun ErrorMessage(message: ChatMessage) {
    Text(
        message.text,
        color = Palette.Danger,
        style = MaterialTheme.typography.bodyMedium,
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(Palette.Danger.copy(alpha = 0.15f))
            .padding(12.dp),
    )
}

@Composable
private fun AgentMessage(message: ChatMessage, plain: Boolean, onCopy: (String) -> Unit) {
    Column(modifier = Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        if (message.tools.isNotEmpty()) {
            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                message.tools.forEach { ToolStepRow(it) }
            }
        }
        when {
            message.text.isBlank() && message.streaming && message.tools.none { it.state == "running" } -> {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    CircularProgressIndicator(modifier = Modifier.size(14.dp), strokeWidth = 2.dp)
                    Text("Thinking…", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
            message.text.isBlank() -> Unit
            plain -> SelectionContainer {
                Text(
                    message.text,
                    color = MaterialTheme.colorScheme.onSurface,
                    style = MaterialTheme.typography.bodyMedium,
                    fontFamily = if (message.text.contains("\n")) FontFamily.Monospace else null,
                )
            }
            else -> MarkdownText(message.text, onCopy)
        }
        if (!message.streaming && message.text.isNotBlank()) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                CopyAction(label = "Copy message", onClick = { onCopy(message.text) })
                Text(Format.clock(message.at), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

/** What the assistant is doing, in words. Colour is never the only signal. */
@Composable
private fun ToolStepRow(step: ToolStep) {
    val label = toolLabel(step.name) + when (step.state) {
        "error" -> " failed"
        "unknown" -> " (may have run)"
        else -> ""
    }
    Row(
        modifier = Modifier
            .clip(RoundedCornerShape(10.dp))
            .background(MaterialTheme.colorScheme.surface)
            .padding(horizontal = 10.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        when (step.state) {
            "running" -> CircularProgressIndicator(modifier = Modifier.size(14.dp), strokeWidth = 2.dp)
            "error" -> Icon(Icons.Filled.ErrorOutline, contentDescription = null, tint = Palette.Danger, modifier = Modifier.size(16.dp))
            "unknown" -> Icon(Icons.Filled.Warning, contentDescription = null, tint = Palette.Alert, modifier = Modifier.size(16.dp))
            else -> Icon(Icons.Filled.Check, contentDescription = null, tint = Palette.Long, modifier = Modifier.size(16.dp))
        }
        Text(label, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurface)
    }
}

@Composable
private fun CopyAction(label: String, onClick: () -> Unit) {
    var copied by remember { mutableStateOf(false) }
    LaunchedEffect(copied) {
        if (copied) {
            delay(1500)
            copied = false
        }
    }
    Row(
        modifier = Modifier
            .clip(RoundedCornerShape(8.dp))
            .clickable(role = Role.Button) {
                onClick()
                copied = true
            }
            .padding(horizontal = 8.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        Icon(
            imageVector = if (copied) Icons.Filled.Check else Icons.Filled.ContentCopy,
            contentDescription = label,
            tint = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.size(16.dp),
        )
        Text(if (copied) "Copied" else "Copy", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

// ------------------------------------------------------------------ markdown

@Composable
private fun MarkdownText(text: String, onCopy: (String) -> Unit) {
    val blocks = remember(text) { parseMarkdownBlocks(text) }
    val dark = MaterialTheme.colorScheme.background.luminance() < 0.5f
    val codeBackground = MaterialTheme.colorScheme.surfaceVariant
    val linkColor = MaterialTheme.colorScheme.secondary
    val onSurface = MaterialTheme.colorScheme.onSurface
    SelectionContainer {
        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            blocks.forEach { block ->
                when (block) {
                    is MdBlock.Heading -> Text(
                        inlineMarkdown(block.text, codeBackground, linkColor),
                        style = if (block.level <= 2) MaterialTheme.typography.titleMedium else MaterialTheme.typography.titleSmall,
                        color = onSurface,
                    )
                    is MdBlock.Paragraph -> Text(
                        inlineMarkdown(block.text, codeBackground, linkColor),
                        style = MaterialTheme.typography.bodyLarge,
                        color = onSurface,
                    )
                    is MdBlock.Bullet -> Row(modifier = Modifier.padding(start = (block.indent * 16).dp)) {
                        Text(block.marker, style = MaterialTheme.typography.bodyLarge, color = onSurface, modifier = Modifier.width(24.dp))
                        Text(
                            inlineMarkdown(block.text, codeBackground, linkColor),
                            style = MaterialTheme.typography.bodyLarge,
                            color = onSurface,
                            modifier = Modifier.weight(1f),
                        )
                    }
                    is MdBlock.Quote -> Row(modifier = Modifier.height(IntrinsicSize.Min)) {
                        Box(modifier = Modifier.width(3.dp).fillMaxHeight().background(MaterialTheme.colorScheme.outline))
                        Text(
                            inlineMarkdown(block.text, codeBackground, linkColor),
                            style = MaterialTheme.typography.bodyLarge,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(start = 10.dp),
                        )
                    }
                    is MdBlock.Table -> Text(
                        block.rows.joinToString("\n"),
                        fontFamily = FontFamily.Monospace,
                        fontSize = 12.sp,
                        softWrap = false,
                        color = onSurface,
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(10.dp))
                            .background(MaterialTheme.colorScheme.surface)
                            .horizontalScroll(rememberScrollState())
                            .padding(10.dp),
                    )
                    is MdBlock.Code -> CodeBlock(block.lang, block.code, dark, onCopy)
                    MdBlock.Rule -> Box(modifier = Modifier.fillMaxWidth().height(1.dp).background(MaterialTheme.colorScheme.outline))
                }
            }
        }
    }
}

@Composable
private fun CodeBlock(lang: String, code: String, dark: Boolean, onCopy: (String) -> Unit) {
    val shape = RoundedCornerShape(12.dp)
    val highlighted = remember(code, lang, dark) { highlightCode(code, lang, dark) }
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(shape)
            .background(MaterialTheme.colorScheme.surface)
            .border(1.dp, MaterialTheme.colorScheme.outline, shape),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.surfaceVariant).padding(start = 12.dp, end = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            Text(lang.ifBlank { "code" }, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            CopyAction(label = "Copy code", onClick = { onCopy(code) })
        }
        Text(
            highlighted,
            fontFamily = FontFamily.Monospace,
            fontSize = 13.sp,
            lineHeight = 18.sp,
            softWrap = false,
            color = MaterialTheme.colorScheme.onSurface,
            modifier = Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()).padding(12.dp),
        )
    }
}

// MARKDOWN-PURE-BEGIN (no Compose runtime needed below; unit-tested in ChatTest)

internal sealed interface MdBlock {
    data class Heading(val level: Int, val text: String) : MdBlock
    data class Paragraph(val text: String) : MdBlock
    data class Bullet(val marker: String, val text: String, val indent: Int) : MdBlock
    data class Quote(val text: String) : MdBlock
    data class Table(val rows: List<String>) : MdBlock
    data class Code(val lang: String, val code: String) : MdBlock
    object Rule : MdBlock
}

private val HEADING = Regex("^(#{1,6})\\s+(.*)$")
private val BULLET = Regex("^(\\s*)([-*+]|\\d+[.)])\\s+(.*)$")
private val RULE = Regex("^([-*_])\\1{2,}$")

/** Block-level markdown: headings, lists, quotes, tables, rules and fenced code. An unclosed fence (a reply still streaming) is kept as code. */
internal fun parseMarkdownBlocks(text: String): List<MdBlock> {
    val blocks = mutableListOf<MdBlock>()
    val paragraph = mutableListOf<String>()
    fun flush() {
        if (paragraph.isNotEmpty()) blocks.add(MdBlock.Paragraph(paragraph.joinToString("\n")))
        paragraph.clear()
    }
    val lines = text.replace("\r\n", "\n").split("\n")
    var i = 0
    while (i < lines.size) {
        val line = lines[i]
        val trimmed = line.trim()
        when {
            trimmed.startsWith("```") -> {
                flush()
                val lang = trimmed.removePrefix("```").trim()
                val code = mutableListOf<String>()
                i++
                while (i < lines.size && !lines[i].trim().startsWith("```")) {
                    code.add(lines[i])
                    i++
                }
                blocks.add(MdBlock.Code(lang, code.joinToString("\n")))
            }
            trimmed.isEmpty() -> flush()
            HEADING.matches(trimmed) -> {
                flush()
                val match = HEADING.find(trimmed)!!
                blocks.add(MdBlock.Heading(match.groupValues[1].length, match.groupValues[2].trim()))
            }
            RULE.matches(trimmed) -> {
                flush()
                blocks.add(MdBlock.Rule)
            }
            trimmed.startsWith(">") -> {
                flush()
                blocks.add(MdBlock.Quote(trimmed.removePrefix(">").trim()))
            }
            trimmed.startsWith("|") && trimmed.indexOf('|', 1) > 0 -> {
                flush()
                val rows = mutableListOf<String>()
                while (i < lines.size && lines[i].trim().startsWith("|")) {
                    rows.add(lines[i].trim())
                    i++
                }
                i--
                blocks.add(MdBlock.Table(rows))
            }
            BULLET.matches(line) -> {
                flush()
                val match = BULLET.find(line)!!
                val marker = match.groupValues[2]
                blocks.add(MdBlock.Bullet(if (marker[0].isDigit()) marker else "•", match.groupValues[3], match.groupValues[1].length / 2))
            }
            else -> paragraph.add(line.trim())
        }
        i++
    }
    flush()
    return blocks
}

internal enum class CodeKind { COMMENT, STRING, NUMBER, KEYWORD }

internal data class CodeToken(val start: Int, val end: Int, val kind: CodeKind)

private val KEYWORDS = setOf(
    "if", "else", "elif", "for", "while", "do", "return", "def", "fun", "fn", "function", "class", "object", "interface",
    "import", "from", "as", "package", "use", "val", "var", "let", "const", "in", "is", "not", "and", "or", "null", "None",
    "nil", "true", "false", "True", "False", "new", "this", "self", "try", "catch", "except", "finally", "throw", "raise",
    "async", "await", "yield", "when", "match", "switch", "case", "break", "continue", "public", "private", "static",
    "void", "int", "string", "bool", "struct", "enum", "type", "impl", "pub", "with", "lambda", "echo", "export",
    "select", "where", "insert", "update", "delete", "create", "table", "order", "group", "by", "limit", "join",
    "on", "set", "values", "into",
)
private val HASH_COMMENT_LANGS = setOf("python", "py", "sh", "bash", "shell", "zsh", "yaml", "yml", "toml", "ruby", "rb", "dockerfile", "ini", "env", "")
private val DASH_COMMENT_LANGS = setOf("sql", "lua")
private val NO_KEYWORD_LANGS = setOf("json", "jsonc", "text", "txt", "log", "output")

private fun codeRegex(comment: String) = Regex(
    "($comment|/\\*[\\s\\S]*?\\*/)" +
        "|(\"(?:\\\\.|[^\"\\\\\\n])*\"|'(?:\\\\.|[^'\\\\\\n])*'|`(?:\\\\.|[^`\\\\])*`)" +
        "|(\\b\\d+(?:\\.\\d+)?\\b)" +
        "|([A-Za-z_][A-Za-z0-9_]*)",
)

private val SLASH_CODE = codeRegex("//[^\\n]*")
private val HASH_CODE = codeRegex("#[^\\n]*")
private val DASH_CODE = codeRegex("--[^\\n]*")

/** Find comments, strings, numbers and keywords. One pass, no dependency; good enough to read, not a full lexer. */
internal fun tokenizeCode(code: String, lang: String): List<CodeToken> {
    val name = lang.trim().lowercase()
    val regex = when {
        name in DASH_COMMENT_LANGS -> DASH_CODE
        name in HASH_COMMENT_LANGS -> HASH_CODE
        else -> SLASH_CODE
    }
    val tokens = mutableListOf<CodeToken>()
    for (match in regex.findAll(code)) {
        val range = match.range
        val word = match.value
        val kind = when {
            match.groups[1] != null -> CodeKind.COMMENT
            match.groups[2] != null -> CodeKind.STRING
            match.groups[3] != null -> CodeKind.NUMBER
            name in NO_KEYWORD_LANGS -> null
            word in KEYWORDS || (word == word.uppercase() && word.lowercase() in KEYWORDS) -> CodeKind.KEYWORD
            else -> null
        }
        if (kind != null) tokens.add(CodeToken(range.first, range.last + 1, kind))
    }
    return tokens
}

/** Chats whose title or any message contains [query] (case-insensitive); everything when it is blank. */
internal fun filterConversations(chats: List<Conversation>, query: String): List<Conversation> {
    val needle = query.trim().lowercase()
    if (needle.isEmpty()) return chats
    return chats.filter { chat -> chat.title.lowercase().contains(needle) || chat.messages.any { it.text.lowercase().contains(needle) } }
}

private val TOOL_LABELS = mapOf(
    "get_status" to "Checking status",
    "list_markets" to "Searching markets",
    "get_market" to "Reading market",
    "get_book" to "Reading order book",
    "research_market" to "Researching market",
    "get_positions" to "Reading positions",
    "get_orders" to "Reading orders",
    "get_rules" to "Reading exit rules",
    "get_limits" to "Reading limits",
    "switch_venue" to "Switching venue",
    "place_trade" to "Placing trade",
    "sell_position" to "Selling position",
    "cancel_order" to "Cancelling order",
    "cancel_all_orders" to "Cancelling all orders",
    "set_exit_rules" to "Setting exit rules",
    "cancel_rule" to "Cancelling rule",
    "pause_agent" to "Pausing agent",
    "resume_agent" to "Resuming agent",
    "panic_stop" to "Panic stop",
    "set_limit" to "Changing limit",
)

/** A tool's friendly name for the status line; an unknown tool still reads as words. */
internal fun toolLabel(name: String): String = TOOL_LABELS[name] ?: name.replace('_', ' ').replaceFirstChar { it.uppercase() }

// MARKDOWN-PURE-END

/** `code`, **bold**, *italic* and [label](url). Links are underlined; the address itself is not shown. */
internal fun inlineMarkdown(text: String, codeBackground: Color, linkColor: Color): AnnotatedString = buildAnnotatedString {
    var i = 0
    while (i < text.length) {
        val c = text[i]
        if (c == '`') {
            val end = text.indexOf('`', i + 1)
            if (end > i + 1) {
                withStyle(SpanStyle(fontFamily = FontFamily.Monospace, background = codeBackground)) { append(text.substring(i + 1, end)) }
                i = end + 1
                continue
            }
        } else if (c == '*' && text.startsWith("**", i)) {
            val end = text.indexOf("**", i + 2)
            if (end > i + 2) {
                withStyle(SpanStyle(fontWeight = FontWeight.Bold)) {
                    append(inlineMarkdown(text.substring(i + 2, end), codeBackground, linkColor))
                }
                i = end + 2
                continue
            }
        } else if (c == '*') {
            val end = text.indexOf('*', i + 1)
            if (end > i + 1 && !text[i + 1].isWhitespace() && !text[end - 1].isWhitespace()) {
                withStyle(SpanStyle(fontStyle = FontStyle.Italic)) { append(text.substring(i + 1, end)) }
                i = end + 1
                continue
            }
        } else if (c == '[') {
            val mid = text.indexOf("](", i + 1)
            val end = if (mid > i) text.indexOf(')', mid + 2) else -1
            if (mid > i + 1 && end > mid && !text.substring(i + 1, mid).contains('\n')) {
                withStyle(SpanStyle(color = linkColor, textDecoration = TextDecoration.Underline)) { append(text.substring(i + 1, mid)) }
                i = end + 1
                continue
            }
        }
        append(c)
        i++
    }
}

private fun highlightCode(code: String, lang: String, dark: Boolean): AnnotatedString {
    val keyword = if (dark) Color(0xFFC792EA) else Color(0xFF8250DF)
    val string = if (dark) Color(0xFF9ECE6A) else Color(0xFF0A7D3E)
    val number = if (dark) Color(0xFFFF9E64) else Color(0xFFB45309)
    val comment = if (dark) Color(0xFF7C8DA3) else Color(0xFF6E7781)
    return buildAnnotatedString {
        append(code)
        for (token in tokenizeCode(code, lang)) {
            val color = when (token.kind) {
                CodeKind.KEYWORD -> keyword
                CodeKind.STRING -> string
                CodeKind.NUMBER -> number
                CodeKind.COMMENT -> comment
            }
            addStyle(SpanStyle(color = color), token.start, token.end)
        }
    }
}

// ------------------------------------------------------------------ history

@Composable
private fun ChatHistoryDrawer(
    chat: ChatUiState,
    onNewChat: () -> Unit,
    onOpen: (String) -> Unit,
    onRename: (String, String) -> Unit,
    onDelete: (String) -> Unit,
) {
    var query by rememberSaveable { mutableStateOf("") }
    var renaming by remember { mutableStateOf<Conversation?>(null) }
    var deleting by remember { mutableStateOf<Conversation?>(null) }
    val shown = remember(chat.conversations, query) { filterConversations(chat.conversations, query) }

    ModalDrawerSheet {
        Text("Chats", style = MaterialTheme.typography.titleMedium, modifier = Modifier.padding(start = 16.dp, top = 16.dp, end = 16.dp))
        Button(
            onClick = onNewChat,
            enabled = !chat.busy,
            modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
        ) {
            Icon(Icons.Filled.Add, contentDescription = null, modifier = Modifier.size(18.dp))
            Spacer(Modifier.width(8.dp))
            Text("New chat")
        }
        OutlinedTextField(
            value = query,
            onValueChange = { query = it },
            placeholder = { Text("Search chats") },
            leadingIcon = { Icon(Icons.Filled.Search, contentDescription = null) },
            trailingIcon = {
                if (query.isNotEmpty()) {
                    IconButton(onClick = { query = "" }) { Icon(Icons.Filled.Close, contentDescription = "Clear search") }
                }
            },
            singleLine = true,
            shape = RoundedCornerShape(24.dp),
            modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp),
        )
        LazyColumn(modifier = Modifier.weight(1f).fillMaxWidth(), contentPadding = PaddingValues(vertical = 8.dp)) {
            if (shown.isEmpty()) {
                item(key = "empty") {
                    Text(
                        if (chat.conversations.isEmpty()) "No chats yet." else "No chats match.",
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(16.dp),
                    )
                }
            }
            items(shown, key = { it.id }) { conversation ->
                val active = conversation.id == chat.activeId
                var menu by remember { mutableStateOf(false) }
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .background(if (active) MaterialTheme.colorScheme.primary.copy(alpha = 0.12f) else Color.Transparent)
                        .clickable(enabled = !chat.busy || active) { onOpen(conversation.id) }
                        .padding(start = 16.dp, end = 4.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        conversation.title,
                        style = MaterialTheme.typography.bodyMedium,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.weight(1f).padding(vertical = 12.dp),
                    )
                    Box {
                        IconButton(onClick = { menu = true }) { Icon(Icons.Filled.MoreVert, contentDescription = "Chat options") }
                        DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                            DropdownMenuItem(
                                text = { Text("Rename") },
                                leadingIcon = { Icon(Icons.Filled.Edit, contentDescription = null) },
                                onClick = {
                                    menu = false
                                    renaming = conversation
                                },
                            )
                            DropdownMenuItem(
                                text = { Text("Delete") },
                                leadingIcon = { Icon(Icons.Filled.Delete, contentDescription = null) },
                                enabled = !(chat.busy && active),
                                onClick = {
                                    menu = false
                                    deleting = conversation
                                },
                            )
                        }
                    }
                }
            }
        }
    }

    renaming?.let { target ->
        var title by remember(target.id) { mutableStateOf(target.title) }
        AlertDialog(
            onDismissRequest = { renaming = null },
            title = { Text("Rename chat") },
            text = { OutlinedTextField(value = title, onValueChange = { title = it.take(60) }, singleLine = true) },
            confirmButton = {
                TextButton(
                    enabled = title.isNotBlank(),
                    onClick = {
                        onRename(target.id, title)
                        renaming = null
                    },
                ) { Text("Save") }
            },
            dismissButton = { TextButton(onClick = { renaming = null }) { Text("Cancel") } },
        )
    }
    deleting?.let { target ->
        AlertDialog(
            onDismissRequest = { deleting = null },
            title = { Text("Delete this chat?") },
            text = { Text("\"${target.title}\" will be removed from this device.") },
            confirmButton = {
                TextButton(
                    onClick = {
                        onDelete(target.id)
                        deleting = null
                    },
                ) { Text("Delete", color = Palette.Danger) }
            },
            dismissButton = { TextButton(onClick = { deleting = null }) { Text("Cancel") } },
        )
    }
}
