package com.mainnetwallet.mmagent.data

import java.time.Instant

/** Which proposals the operator can still act on. Kept free of JSON so it is unit-testable. */
object ProposalRules {

    /**
     * A proposal can be approved or rejected only while the server still has it
     * PENDING and its 5-minute TTL has not run out. An unreadable expiry is treated
     * as expired: the server re-validates anyway, and the app should not offer a
     * button it cannot be sure about.
     */
    fun actionable(status: String, expiresAt: String?, now: Instant = Instant.now()): Boolean {
        if (!status.equals("PENDING", ignoreCase = true)) return false
        val left = Format.secondsUntil(expiresAt, now) ?: return false
        return left > 0
    }
}
