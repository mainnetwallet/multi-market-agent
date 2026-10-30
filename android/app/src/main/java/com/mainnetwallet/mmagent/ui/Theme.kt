package com.mainnetwallet.mmagent.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

/**
 * A calm, high-contrast trading palette. Colour is never the only signal: every
 * LONG/SHORT indicator also carries a label and an arrow.
 */
object Palette {
    val Background = Color(0xFF0B0F14)
    val Surface = Color(0xFF141B23)
    val SurfaceAlt = Color(0xFF0E141C)
    val Line = Color(0xFF223040)
    val TextPrimary = Color(0xFFE8ECF1)
    val TextSecondary = Color(0xFF8FA3B8)

    val Long = Color(0xFF22C55E)
    val LongDark = Color(0xFF15803D)
    val Short = Color(0xFFEF4444)
    val ShortDark = Color(0xFFB91C1C)
    val Neutral = Color(0xFF60A5FA)
    val NeutralDark = Color(0xFF2563EB)
    val Alert = Color(0xFFF59E0B)
    val AlertDark = Color(0xFFD97706)
    val Danger = Color(0xFFDC2626)
    val DangerDark = Color(0xFFB91C1C)
}

private val DarkColors = darkColorScheme(
    primary = Palette.Long,
    onPrimary = Color(0xFF062016),
    secondary = Palette.Neutral,
    background = Palette.Background,
    onBackground = Palette.TextPrimary,
    surface = Palette.Surface,
    onSurface = Palette.TextPrimary,
    surfaceVariant = Palette.SurfaceAlt,
    onSurfaceVariant = Palette.TextSecondary,
    outline = Palette.Line,
    error = Palette.Danger,
)

private val LightColors = lightColorScheme(
    primary = Palette.LongDark,
    onPrimary = Color.White,
    secondary = Palette.NeutralDark,
    background = Color(0xFFF4F6F8),
    onBackground = Color(0xFF1A1F26),
    surface = Color(0xFFEFF2F5),
    onSurface = Color(0xFF1A1F26),
    surfaceVariant = Color(0xFFE6EAEF),
    onSurfaceVariant = Color(0xFF5B6B7A),
    outline = Color(0xFFC9D2DB),
    error = Palette.DangerDark,
)

@Composable
fun MMAgentTheme(darkTheme: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = if (darkTheme) DarkColors else LightColors,
        typography = MMAgentTypography,
        content = content,
    )
}
