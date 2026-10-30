package com.mainnetwallet.mmagent

import com.mainnetwallet.mmagent.data.Format
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.math.BigDecimal

class FormatTest {

    @Test
    fun moneyKeepsTheServerPrecision() {
        assertEquals("$12.40", Format.money("12.4"))
        assertEquals("$0.05", Format.money("0.05"))
        assertEquals("—", Format.money(null))
        assertEquals("—", Format.money("not a number"))
    }

    @Test
    fun signedMoneyAlwaysShowsTheDirection() {
        assertEquals("+$1.20", Format.signedMoney("1.2"))
        assertEquals("-$0.40", Format.signedMoney("-0.4"))
        assertEquals("$0.00", Format.signedMoney("0"))
    }

    @Test
    fun centsConvertsProbabilityToCents() {
        assertEquals("57.0c", Format.cents("0.57"))
        assertEquals("5.0c", Format.cents("0.05"))
        assertEquals("—", Format.cents(null))
    }

    @Test
    fun sharesDropTrailingZeros() {
        assertEquals("10", Format.shares("10.00"))
        assertEquals("3.04", Format.shares("3.04"))
    }

    @Test
    fun compactKeepsLargeNumbersReadable() {
        assertEquals("1.2k", Format.compact("1200"))
        assertEquals("340", Format.compact("340"))
    }

    @Test
    fun assetNameComesFromTheVenue() {
        assertEquals("12.40 pUSD", Format.withAsset("12.4", "pUSD"))
        assertEquals("12.40 USDC", Format.withAsset("12.4", "USDC"))
    }

    @Test
    fun addressesAreGroupedForReading() {
        assertEquals(
            "0x12 3456 7890 abcd ef12 3456 7890 abcd ef12 3456 78",
            Format.groupAddress("0x1234567890abcdef1234567890abcdef12345678"),
        )
        assertEquals("—", Format.groupAddress(null))
    }

    @Test
    fun dhakaTimeIsUtcPlusSix() {
        val shown = Format.dhaka("2026-10-30T12:00:00Z")
        // 12:00 UTC is 18:00 in Dhaka.
        assertTrue(shown, shown.contains("18:00"))
    }

    @Test
    fun relativeTimeReadsAsFutureOrPast() {
        assertTrue(Format.relative("2026-10-30T12:00:00Z").contains("ago"))
    }
}
