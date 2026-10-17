package com.theowlops.boom.ui

import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Image
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.ColorFilter
import androidx.compose.ui.graphics.ColorMatrix
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.theowlops.boom.R

/** Boomi's current status, reflected in how the avatar is presented. */
enum class BoomiStatus {
    IDLE,
    THINKING,
    ERROR,
    SUCCESS,
}

/**
 * Circular avatar of Boomi (the Boom owl mascot).
 *
 * Status variants:
 * - [BoomiStatus.IDLE]: plain avatar.
 * - [BoomiStatus.THINKING]: gentle alpha pulse animation.
 * - [BoomiStatus.ERROR]: desaturated (grayscale).
 * - [BoomiStatus.SUCCESS]: accent-colored ring border.
 */
@Composable
fun BoomiAvatar(
    size: Dp = 40.dp,
    status: BoomiStatus = BoomiStatus.IDLE,
    modifier: Modifier = Modifier,
) {
    // Grayscale the mascot when something went wrong.
    val colorFilter = if (status == BoomiStatus.ERROR) {
        ColorFilter.colorMatrix(ColorMatrix().apply { setToSaturation(0f) })
    } else {
        null
    }

    // Pulse the avatar while Boomi is thinking.
    val alpha = if (status == BoomiStatus.THINKING) {
        val transition = rememberInfiniteTransition(label = "boomi-thinking")
        val pulse by transition.animateFloat(
            initialValue = 0.55f,
            targetValue = 1f,
            animationSpec = infiniteRepeatable(
                animation = tween(durationMillis = 900),
                repeatMode = RepeatMode.Reverse,
            ),
            label = "boomi-pulse",
        )
        pulse
    } else {
        1f
    }

    Image(
        painter = painterResource(R.drawable.boomi_mascot),
        contentDescription = "Boomi",
        modifier = modifier
            .size(size)
            .clip(CircleShape)
            // SUCCESS gets an accent ring ("glow"); other states stay plain.
            .then(
                if (status == BoomiStatus.SUCCESS) {
                    Modifier.border(
                        width = 2.dp,
                        color = MaterialTheme.colorScheme.primary,
                        shape = CircleShape,
                    )
                } else {
                    Modifier
                }
            ),
        alpha = alpha,
        colorFilter = colorFilter,
    )
}
