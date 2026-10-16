package com.theowlops.boom

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.theowlops.boom.api.BoomApiClient
import com.theowlops.boom.data.DEFAULT_SERVER_URL
import com.theowlops.boom.data.SettingsRepository

/**
 * Single-activity host. Reads the backend server URL from [SettingsRepository]
 * and builds the [BoomApiService] once per URL change.
 */
class MainActivity : ComponentActivity() {

    private lateinit var settingsRepository: SettingsRepository

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        settingsRepository = SettingsRepository(applicationContext)

        setContent {
            val serverUrl by settingsRepository.serverUrl
                .collectAsStateWithLifecycle(initialValue = DEFAULT_SERVER_URL)
            val api = remember(serverUrl) { BoomApiClient.create(serverUrl) }

            CompositionLocalProvider(
                LocalBoomApi provides api,
                LocalSettingsRepository provides settingsRepository,
            ) {
                BoomApp()
            }
        }
    }
}
