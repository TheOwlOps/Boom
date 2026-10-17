package com.theowlops.boom.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

// Boom brand palette (dark-first).
private val BoomPrimary = Color(0xFF7C9CFF)
private val BoomOnPrimary = Color(0xFF0B1020)
private val BoomPrimaryContainer = Color(0xFF2B3A67)
private val BoomOnPrimaryContainer = Color(0xFFDCE3FF)
private val BoomBackground = Color(0xFF0D1117)
private val BoomSurface = Color(0xFF161B24)
private val BoomSurfaceVariant = Color(0xFF232A38)
private val BoomOnSurfaceVariant = Color(0xFFC4CBD8)
private val BoomSecondary = Color(0xFF9FB4D8)

private val BoomDarkColorScheme = darkColorScheme(
    primary = BoomPrimary,
    onPrimary = BoomOnPrimary,
    primaryContainer = BoomPrimaryContainer,
    onPrimaryContainer = BoomOnPrimaryContainer,
    background = BoomBackground,
    onBackground = Color(0xFFE8ECF4),
    surface = BoomSurface,
    onSurface = Color(0xFFE8ECF4),
    surfaceVariant = BoomSurfaceVariant,
    onSurfaceVariant = BoomOnSurfaceVariant,
    secondary = BoomSecondary,
)

/** Dark Material3 theme for the Boom Android app. */
@Composable
fun BoomTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = BoomDarkColorScheme,
        content = content,
    )
}
