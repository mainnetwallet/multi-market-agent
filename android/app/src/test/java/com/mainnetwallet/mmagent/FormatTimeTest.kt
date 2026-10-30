package com.mainnetwallet.mmagent

import com.mainnetwallet.mmagent.data.Format
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test
import java.time.Instant

class FormatTimeTest {

    private val now: Instant = Instant.parse("2026-10-30T12:00:00Z")

    @Test
    fun countdownShowsTheTwoLargestUnits() {
        assertEquals("2d 4h", Format.countdown("2026-11-01T16:30:00Z", now))
        assertEquals("3h 20m", Format.countdown("2026-10-30T15:20:30Z", now))
        assertEquals("45m", Format.countdown("2026-10-30T12:45:10Z", now))
    }

    @Test
    fun countdownUnderAMinuteAndAfterClose() {
        assertEquals("<1m", Format.countdown("2026-10-30T12:00:30Z", now))
        assertEquals("closed", Format.countdown("2026-10-30T12:00:00Z", now))
        assertEquals("closed", Format.countdown("2026-10-29T12:00:00Z", now))
    }

    @Test
    fun countdownIsADashWhenTheTimeIsMissingOrUnreadable() {
        assertEquals("—", Format.countdown(null, now))
        assertEquals("—", Format.countdown("", now))
        assertEquals("—", Format.countdown("tomorrow", now))
    }

    @Test
    fun timestampsWithAnOffsetOrNoZoneAreBothRead() {
        // The server stores UTC; a bare timestamp is read as UTC, not as local time.
        assertEquals("1h 0m", Format.countdown("2026-10-30T13:00:00", now))
        assertEquals("1h 0m", Format.countdown("2026-10-30T13:00:00+00:00", now))
        // 19:00 in Dhaka (+06:00) is 13:00 UTC.
        assertEquals("1h 0m", Format.countdown("2026-10-30T19:00:00+06:00", now))
    }

    @Test
    fun secondsUntilIsNegativeOncePassed() {
        assertEquals(90L, Format.secondsUntil("2026-10-30T12:01:30Z", now))
        assertEquals(-60L, Format.secondsUntil("2026-10-30T11:59:00Z", now))
        assertNull(Format.secondsUntil("not a time", now))
        assertNotNull(Format.parseInstant("2026-10-30T12:00:00Z"))
    }

    @Test
    fun clockIsDhakaTime() {
        // 12:00 UTC is 18:00 in Dhaka.
        assertEquals("18:00", Format.clock(Instant.parse("2026-10-30T12:00:00Z").toEpochMilli()))
    }
}
