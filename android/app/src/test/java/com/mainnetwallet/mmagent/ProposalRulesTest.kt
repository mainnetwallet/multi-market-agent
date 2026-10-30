package com.mainnetwallet.mmagent

import com.mainnetwallet.mmagent.data.ProposalRules
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Instant

class ProposalRulesTest {

    private val now: Instant = Instant.parse("2026-10-30T12:00:00Z")

    @Test
    fun aPendingProposalInsideItsTtlCanBeActedOn() {
        assertTrue(ProposalRules.actionable("PENDING", "2026-10-30T12:04:00Z", now))
        assertTrue(ProposalRules.actionable("pending", "2026-10-30T12:00:01Z", now))
    }

    @Test
    fun anExpiredProposalCannot() {
        assertFalse(ProposalRules.actionable("PENDING", "2026-10-30T12:00:00Z", now))
        assertFalse(ProposalRules.actionable("PENDING", "2026-10-30T11:55:00Z", now))
    }

    @Test
    fun onlyPendingCountsWhateverTheExpiry() {
        for (status in listOf("APPROVED", "REJECTED", "EXPIRED", "EXECUTING", "EXECUTED", "FAILED")) {
            assertFalse(status, ProposalRules.actionable(status, "2026-10-30T12:04:00Z", now))
        }
    }

    @Test
    fun anUnreadableExpiryIsNotOffered() {
        assertFalse(ProposalRules.actionable("PENDING", null, now))
        assertFalse(ProposalRules.actionable("PENDING", "soon", now))
    }
}
