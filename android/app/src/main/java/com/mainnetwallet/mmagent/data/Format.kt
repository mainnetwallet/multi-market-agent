package com.mainnetwallet.mmagent.data

import java.math.BigDecimal
import java.math.RoundingMode
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * Formatting for money, prices and time.
 *
 * The server sends Decimal strings; these helpers never turn them into doubles.
 * Dhaka time (UTC+6) is what the operator reads.
 */
object Format {

    val DHAKA: ZoneId = ZoneId.of("Asia/Dhaka")

    private val dhakaFormat: DateTimeFormatter =
        DateTimeFormatter.ofPattern("dd MMM HH:mm").withZone(DHAKA)

    /** "$12.40" from a Decimal string. Keeps the server's precision. */
    fun money(value: String?): String {
        val amount = value?.toBigDecimalOrNull() ?: return "—"
        return "$" + amount.setScale(2, RoundingMode.HALF_UP).toPlainString()
    }

    /** "12.40" without a currency sign, for rows that carry the asset name. */
    fun amount(value: String?): String {
        val amount = value?.toBigDecimalOrNull() ?: return "—"
        return amount.setScale(2, RoundingMode.HALF_UP).toPlainString()
    }

    /** "+$1.20" / "-$0.40" / "$0.00", always signed so the direction is never ambiguous. */
    fun signedMoney(value: String?): String {
        val amount = value?.toBigDecimalOrNull() ?: return "—"
        val sign = if (amount.signum() > 0) "+" else if (amount.signum() < 0) "-" else ""
        return "$sign$" + amount.abs().setScale(2, RoundingMode.HALF_UP).toPlainString()
    }

    /** "57.0c" from a probability Decimal string in (0, 1). */
    fun cents(probability: String?): String {
        val p = probability?.toBigDecimalOrNull() ?: return "—"
        return p.multiply(BigDecimal(100)).setScale(1, RoundingMode.HALF_UP).toPlainString() + "c"
    }

    /** "12.34" shares, without trailing noise. */
    fun shares(value: String?): String {
        val s = value?.toBigDecimalOrNull() ?: return "—"
        return s.stripTrailingZeros().toPlainString()
    }

    /** "1.2k" / "340" — a large share count stays readable on a phone. */
    fun compact(value: String?): String {
        val s = value?.toBigDecimalOrNull() ?: return "—"
        if (s.abs() >= BigDecimal(1000)) {
            return s.divide(BigDecimal(1000), 1, RoundingMode.HALF_UP).toPlainString() + "k"
        }
        return s.stripTrailingZeros().toPlainString()
    }

    /** "30 Oct 14:05" in Dhaka time. */
    fun dhaka(isoTimestamp: String?): String {
        if (isoTimestamp.isNullOrBlank()) return "—"
        return try {
            dhakaFormat.format(Instant.parse(isoTimestamp))
        } catch (e: Exception) {
            try {
                dhakaFormat.format(Instant.parse(isoTimestamp + "Z"))
            } catch (e2: Exception) {
                "—"
            }
        }
    }

    /**
     * Parse a server timestamp. Accepts "...Z", "...+00:00" and a bare
     * "2026-10-30T12:00:00" (read as UTC, which is what the server stores).
     */
    fun parseInstant(isoTimestamp: String?): Instant? {
        if (isoTimestamp.isNullOrBlank()) return null
        return try {
            Instant.parse(isoTimestamp)
        } catch (e: Exception) {
            try {
                java.time.OffsetDateTime.parse(isoTimestamp).toInstant()
            } catch (e2: Exception) {
                try {
                    Instant.parse(isoTimestamp + "Z")
                } catch (e3: Exception) {
                    null
                }
            }
        }
    }

    /** Whole seconds from [now] until [isoTimestamp]; negative once it has passed, null if unreadable. */
    fun secondsUntil(isoTimestamp: String?, now: Instant = Instant.now()): Long? {
        val instant = parseInstant(isoTimestamp) ?: return null
        return java.time.Duration.between(now, instant).seconds
    }

    /**
     * "Closes in" text for a market: "2d 4h", "3h 20m", "45m", "<1m" or "closed".
     * Only the two largest units are shown so it fits a card.
     */
    fun countdown(isoTimestamp: String?, now: Instant = Instant.now()): String {
        val seconds = secondsUntil(isoTimestamp, now) ?: return "—"
        if (seconds <= 0) return "closed"
        val days = seconds / 86_400
        val hours = (seconds % 86_400) / 3_600
        val minutes = (seconds % 3_600) / 60
        return when {
            days > 0 -> "${days}d ${hours}h"
            hours > 0 -> "${hours}h ${minutes}m"
            minutes > 0 -> "${minutes}m"
            else -> "<1m"
        }
    }

    /** "in 9 min" / "2 min ago" for a hold window or an expiry. */
    fun relative(isoTimestamp: String?): String {
        if (isoTimestamp.isNullOrBlank()) return "—"
        val instant = parseInstant(isoTimestamp) ?: return "—"
        val seconds = java.time.Duration.between(Instant.now(), instant).seconds
        val minutes = seconds / 60
        return when {
            minutes > 0 -> "in $minutes min"
            minutes < 0 -> "${-minutes} min ago"
            else -> "now"
        }
    }

    /** An address in 4-character groups, so it can be read aloud and compared. */
    fun groupAddress(address: String?): String =
        address?.chunked(4)?.joinToString(" ") ?: "—"

    /** An amount with an explicit asset, e.g. "12.40 pUSD". */
    fun withAsset(value: String?, asset: String?): String {
        val name = asset ?: "USD"
        return "${amount(value)} $name"
    }
}
