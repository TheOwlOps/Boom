package com.theowlops.boom

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Chat
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.theowlops.boom.api.BoomApiClient
import com.theowlops.boom.api.BoomApiService
import com.theowlops.boom.data.DEFAULT_SERVER_URL
import com.theowlops.boom.data.SettingsRepository
import com.theowlops.boom.ui.ChatScreen
import com.theowlops.boom.ui.SettingsScreen
import com.theowlops.boom.ui.SetupWizard
import com.theowlops.boom.ui.theme.BoomTheme

/** Provided by [MainActivity]; gives [BoomApp] access to the backend API. */
val LocalBoomApi = staticCompositionLocalOf<BoomApiService> {
    error("LocalBoomApi not provided — wrap BoomApp in MainActivity's providers")
}

/** Provided by [MainActivity]; gives [BoomApp] access to app settings. */
val LocalSettingsRepository = staticCompositionLocalOf<SettingsRepository> {
    error("LocalSettingsRepository not provided — wrap BoomApp in MainActivity's providers")
}

private enum class Destination(
    val route: String,
    val label: String,
    val icon: ImageVector,
) {
    CHAT("chat", "Chat", Icons.Filled.Chat),
    SETTINGS("settings", "Settings", Icons.Filled.Settings),
}

/**
 * App root: dark Material3 theme + simple state-based navigation between the
 * chat and settings destinations. Dependencies arrive via CompositionLocals
 * provided by [MainActivity].
 */
@Composable
fun BoomApp() {
    val api = LocalBoomApi.current
    val repository = LocalSettingsRepository.current
    var current by remember { mutableStateOf(Destination.CHAT.route) }
    var forceWizard by remember { mutableStateOf(false) }
    val setupComplete by repository.setupComplete.collectAsStateWithLifecycle(
        initialValue = false,
    )

    BoomTheme {
        if (!setupComplete || forceWizard) {
            SetupWizard(
                onComplete = { forceWizard = false },
                apiFactory = { url -> BoomApiClient.create(url) },
                repo = repository,
            )
            return@BoomTheme
        }

        Scaffold(
            bottomBar = {
                NavigationBar {
                    Destination.entries.forEach { destination ->
                        NavigationBarItem(
                            selected = current == destination.route,
                            onClick = { current = destination.route },
                            icon = {
                                Icon(
                                    imageVector = destination.icon,
                                    contentDescription = destination.label,
                                )
                            },
                            label = { Text(destination.label) },
                        )
                    }
                }
            },
        ) { innerPadding ->
            Box(modifier = Modifier.padding(innerPadding)) {
                when (current) {
                    Destination.CHAT.route -> {
                        val serverUrl by repository.serverUrl.collectAsStateWithLifecycle(
                            initialValue = DEFAULT_SERVER_URL,
                        )
                        ChatScreen(api = api, serverUrl = serverUrl)
                    }
                    Destination.SETTINGS.route -> {
                        SettingsScreen(
                            repository = repository,
                            onReshowWizard = { forceWizard = true },
                        )
                    }
                }
            }
        }
    }
}
