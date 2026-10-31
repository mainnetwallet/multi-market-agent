package com.mainnetwallet.mmagent

import com.mainnetwallet.mmagent.data.ChatMessage
import com.mainnetwallet.mmagent.data.Conversation
import com.mainnetwallet.mmagent.data.ToolStep
import com.mainnetwallet.mmagent.data.chatTitle
import com.mainnetwallet.mmagent.data.mergeToolStep
import com.mainnetwallet.mmagent.ui.CodeKind
import com.mainnetwallet.mmagent.ui.MdBlock
import com.mainnetwallet.mmagent.ui.filterConversations
import com.mainnetwallet.mmagent.ui.parseMarkdownBlocks
import com.mainnetwallet.mmagent.ui.tokenizeCode
import com.mainnetwallet.mmagent.ui.toolLabel
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ChatTest {

    @Test
    fun markdownBlocksCoverHeadingsListsQuotesRulesAndCode() {
        val text = "# Title\n\nHello **world**\nsecond line\n\n- one\n- two\n1. first\n\n```kotlin\nval x = 1\n```\n> quoted\n---"
        assertEquals(
            listOf(
                MdBlock.Heading(1, "Title"),
                MdBlock.Paragraph("Hello **world**\nsecond line"),
                MdBlock.Bullet("•", "one", 0),
                MdBlock.Bullet("•", "two", 0),
                MdBlock.Bullet("1.", "first", 0),
                MdBlock.Code("kotlin", "val x = 1"),
                MdBlock.Quote("quoted"),
                MdBlock.Rule,
            ),
            parseMarkdownBlocks(text),
        )
    }

    @Test
    fun anUnclosedCodeFenceWhileStreamingIsStillCode() {
        assertEquals(listOf(MdBlock.Code("python", "print(1)")), parseMarkdownBlocks("```python\nprint(1)"))
    }

    @Test
    fun tablesStayTogetherAndBoldIsNotABullet() {
        val blocks = parseMarkdownBlocks("| a | b |\n|---|---|\n| 1 | 2 |\n\n**bold** text\n\n* item")
        assertEquals(
            listOf(
                MdBlock.Table(listOf("| a | b |", "|---|---|", "| 1 | 2 |")),
                MdBlock.Paragraph("**bold** text"),
                MdBlock.Bullet("•", "item", 0),
            ),
            blocks,
        )
    }

    @Test
    fun codeTokensFindCommentsStringsNumbersAndKeywords() {
        val code = "x = 1  # note\nif x: print(\"hi\")"
        val found = tokenizeCode(code, "python").map { code.substring(it.start, it.end) to it.kind }
        assertTrue(found.contains("1" to CodeKind.NUMBER))
        assertTrue(found.contains("# note" to CodeKind.COMMENT))
        assertTrue(found.contains("if" to CodeKind.KEYWORD))
        assertTrue(found.contains("\"hi\"" to CodeKind.STRING))
        assertTrue(found.none { it.first == "print" })
    }

    @Test
    fun aHashInsideAStringIsNotAComment() {
        val code = "s = \"#not\""
        assertEquals(listOf(CodeKind.STRING), tokenizeCode(code, "python").map { it.kind })
    }

    @Test
    fun jsonKeepsStringsButNoKeywords() {
        val code = "{\"a\": true}"
        assertEquals(listOf(CodeKind.STRING), tokenizeCode(code, "json").map { it.kind })
    }

    @Test
    fun aToolStepIsUpdatedInPlaceById() {
        val running = mergeToolStep(emptyList(), ToolStep("c1", "get_status", "running"))
        val done = mergeToolStep(running, ToolStep("c1", "get_status", "done"))
        assertEquals(listOf(ToolStep("c1", "get_status", "done")), done)
        assertEquals(2, mergeToolStep(done, ToolStep("c2", "get_orders", "running")).size)
    }

    @Test
    fun aToolStepWithoutAnIdMatchesTheRunningStepOfTheSameName() {
        val running = mergeToolStep(emptyList(), ToolStep("", "get_status", "running"))
        assertEquals(listOf(ToolStep("", "get_status", "error")), mergeToolStep(running, ToolStep("", "get_status", "error")))
    }

    @Test
    fun titlesUseTheFirstLineShortened() {
        assertEquals("/status", chatTitle("/status"))
        assertEquals("New chat", chatTitle("   "))
        assertEquals("first line", chatTitle("\nfirst line\nsecond"))
        assertEquals("a".repeat(40) + "…", chatTitle("a".repeat(60)))
    }

    @Test
    fun searchMatchesTitlesAndMessagesIgnoringCase() {
        val chats = listOf(
            Conversation("1", "Bitcoin odds", listOf(ChatMessage("user", "hi", 1L)), 1L),
            Conversation("2", "Other", listOf(ChatMessage("agent", "Fed decision looks likely", 1L)), 2L),
        )
        assertEquals(chats, filterConversations(chats, "  "))
        assertEquals(listOf("1"), filterConversations(chats, "BITCOIN").map { it.id })
        assertEquals(listOf("2"), filterConversations(chats, "fed dec").map { it.id })
        assertTrue(filterConversations(chats, "nothing").isEmpty())
    }

    @Test
    fun toolNamesReadAsWords() {
        assertEquals("Placing trade", toolLabel("place_trade"))
        assertEquals("Foo bar", toolLabel("foo_bar"))
    }
}
