package com.theowlops.boom.data

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

/** Default server URL: Android emulator loopback to the host's Boom backend. */
const val DEFAULT_SERVER_URL = "http://10.0.2.2:18080/"

/**
 * Persistent app settings backed by Jetpack DataStore (Preferences).
 *
 * Requires: androidx.datastore:datastore-preferences (already in app/build.gradle.kts).
 */
class SettingsRepository(private val context: Context) {

    private val Context.dataStore: DataStore<Preferences> by
        preferencesDataStore(name = DATASTORE_NAME)

    companion object {
        val SERVER_URL = stringPreferencesKey("server_url")
        val SETUP_COMPLETE = booleanPreferencesKey("setup_complete")
        val PROVIDER_ID = stringPreferencesKey("provider_id")
    }

    /** Normalized backend base URL (always has scheme + trailing slash). */
    val serverUrl: Flow<String> =
        context.dataStore.data.map { prefs -> prefs[SERVER_URL] ?: DEFAULT_SERVER_URL }

    /** True once the user finished the first-run setup wizard. */
    val setupComplete: Flow<Boolean> =
        context.dataStore.data.map { prefs -> prefs[SETUP_COMPLETE] == true }

    /** Id of the chosen LLM provider (e.g. "openai"), or null if none selected yet. */
    val providerId: Flow<String?> =
        context.dataStore.data.map { prefs -> prefs[PROVIDER_ID] }

    /** Normalizes (trim, scheme, trailing slash) then persists the server URL. */
    suspend fun setServerUrl(url: String) {
        val normalized = url.normalizeBaseUrl()
        context.dataStore.edit { prefs -> prefs[SERVER_URL] = normalized }
    }

    suspend fun setSetupComplete(done: Boolean) {
        context.dataStore.edit { prefs -> prefs[SETUP_COMPLETE] = done }
    }

    suspend fun setProviderId(id: String?) {
        context.dataStore.edit { prefs ->
            if (id == null) prefs.remove(PROVIDER_ID) else prefs[PROVIDER_ID] = id
        }
    }

    // Convenience suspend getters for one-shot reads.

    suspend fun getServerUrl(): String = serverUrl.first()

    suspend fun isSetupComplete(): Boolean = setupComplete.first()

    suspend fun getProviderId(): String? = providerId.first()
}

private const val DATASTORE_NAME = "boom_settings"
