package com.theowlops.boom.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Error
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.theowlops.boom.api.ApiResult
import com.theowlops.boom.api.BoomApiService
import com.theowlops.boom.api.ProviderConfigRequest
import com.theowlops.boom.api.safeCall
import com.theowlops.boom.data.DEFAULT_SERVER_URL
import com.theowlops.boom.data.PROVIDER_CATALOG
import com.theowlops.boom.data.SettingsRepository
import com.theowlops.boom.data.UiProvider
import com.theowlops.boom.data.normalizeBaseUrl
import kotlinx.coroutines.launch

private const val STEP_WELCOME = 0
private const val STEP_SERVER = 1
private const val STEP_PROVIDER = 2
private const val TOTAL_STEPS = 3

/** Outcome of the "Test connection" check in step 1. */
private sealed interface ConnCheck {
    data object Idle : ConnCheck
    data object Checking : ConnCheck
    data class Done(val ok: Boolean, val message: String, val testedUrl: String) : ConnCheck
}

/**
 * First-run wizard: welcome -> backend server URL -> LLM provider setup.
 *
 * The API client is rebuilt from the server URL via [apiFactory], because
 * Retrofit's base URL is fixed at client creation time.
 */
@Composable
fun SetupWizard(
    onComplete: () -> Unit,
    apiFactory: (String) -> BoomApiService,
    repo: SettingsRepository,
) {
    var step by remember { mutableStateOf(STEP_WELCOME) }
    val scope = rememberCoroutineScope()

    Column(modifier = Modifier.fillMaxSize().padding(24.dp)) {
        // Progress bar + optional Back button.
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            if (step > STEP_WELCOME) {
                IconButton(onClick = { step-- }) {
                    Icon(Icons.Default.ArrowBack, contentDescription = "Back")
                }
            }
            LinearProgressIndicator(
                progress = { (step + 1) / TOTAL_STEPS.toFloat() },
                modifier = Modifier
                    .weight(1f)
                    .padding(start = if (step > STEP_WELCOME) 8.dp else 0.dp),
            )
            Spacer(Modifier.width(48.dp)) // visual balance against the back button
        }

        Spacer(Modifier.height(16.dp))

        when (step) {
            STEP_WELCOME -> WelcomeStep(onNext = { step = STEP_SERVER })
            STEP_SERVER -> ServerStep(
                repo = repo,
                apiFactory = apiFactory,
                onNext = { verifiedUrl ->
                    scope.launch {
                        // Persist the verified URL before moving on.
                        repo.setServerUrl(verifiedUrl)
                        step = STEP_PROVIDER
                    }
                },
            )
            STEP_PROVIDER -> ProviderStep(
                repo = repo,
                apiFactory = apiFactory,
                onComplete = {
                    scope.launch {
                        repo.setSetupComplete(true)
                        onComplete()
                    }
                },
            )
        }
    }
}

/** Step 0: title, short description, single CTA. */
@Composable
private fun WelcomeStep(onNext: () -> Unit) {
    Column(
        modifier = Modifier.fillMaxSize(),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("Boom", style = MaterialTheme.typography.displayMedium)
        Spacer(Modifier.height(12.dp))
        Text(
            "Your AI agent harness — chat, tools, memory and automations, " +
                "powered by your own LLM provider.",
            style = MaterialTheme.typography.bodyLarge,
        )
        Spacer(Modifier.height(32.dp))
        Button(onClick = onNext, modifier = Modifier.fillMaxWidth()) {
            Text("Get Started")
        }
    }
}

/** Step 1: enter the backend URL, test the connection, continue once it passes. */
@Composable
private fun ServerStep(
    repo: SettingsRepository,
    apiFactory: (String) -> BoomApiService,
    onNext: (verifiedUrl: String) -> Unit,
) {
    val scope = rememberCoroutineScope()
    val storedUrl by repo.serverUrl.collectAsStateWithLifecycle(initialValue = DEFAULT_SERVER_URL)
    var url by remember(storedUrl) { mutableStateOf(storedUrl) }
    var check by remember { mutableStateOf<ConnCheck>(ConnCheck.Idle) }

    Column(modifier = Modifier.fillMaxSize()) {
        Text("Connect to your backend", style = MaterialTheme.typography.headlineSmall)
        Spacer(Modifier.height(8.dp))
        Text(
            "Enter the address of the machine running the Boom backend " +
                "(gateway on port 18080).",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(16.dp))

        OutlinedTextField(
            value = url,
            onValueChange = { url = it },
            label = { Text("Server URL") },
            placeholder = { Text(DEFAULT_SERVER_URL) },
            singleLine = true,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(12.dp))

        Button(
            onClick = {
                val normalized = url.normalizeBaseUrl()
                check = ConnCheck.Checking
                scope.launch {
                    val api = apiFactory(normalized)
                    check = when (val result = safeCall { api.health() }) {
                        is ApiResult.Success -> {
                            val health = result.value
                            if (health.status == "ok") {
                                ConnCheck.Done(
                                    ok = true,
                                    message = "Connected — backend v${health.version ?: "?"}; " +
                                        "model: ${health.model ?: "unconfigured"}",
                                    testedUrl = normalized,
                                )
                            } else {
                                ConnCheck.Done(
                                    ok = false,
                                    message = "Unexpected status: ${health.status}",
                                    testedUrl = normalized,
                                )
                            }
                        }
                        is ApiResult.Failure ->
                            ConnCheck.Done(false, result.message, normalized)
                    }
                }
            },
            enabled = check !is ConnCheck.Checking,
        ) {
            if (check is ConnCheck.Checking) {
                CircularProgressIndicator(
                    modifier = Modifier.padding(end = 8.dp),
                    strokeWidth = 2.dp,
                )
            }
            Text("Test connection")
        }

        Spacer(Modifier.height(8.dp))
        when (val c = check) {
            is ConnCheck.Idle -> Unit
            is ConnCheck.Checking -> Text("Testing…", style = MaterialTheme.typography.bodyMedium)
            is ConnCheck.Done -> Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(
                    imageVector = if (c.ok) Icons.Default.CheckCircle else Icons.Default.Error,
                    contentDescription = null,
                    tint = if (c.ok) MaterialTheme.colorScheme.primary
                    else MaterialTheme.colorScheme.error,
                )
                Spacer(Modifier.width(8.dp))
                Text(
                    c.message,
                    style = MaterialTheme.typography.bodyMedium,
                    color = if (c.ok) MaterialTheme.colorScheme.onSurface
                    else MaterialTheme.colorScheme.error,
                )
            }
        }

        Spacer(Modifier.weight(1f))

        // Enabled only after a successful test against the current (normalized) URL.
        val done = check as? ConnCheck.Done
        val canContinue = done != null && done.ok && done.testedUrl == url.normalizeBaseUrl()
        Button(
            onClick = { onNext(url.normalizeBaseUrl()) },
            enabled = canContinue,
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text("Next")
        }
    }
}

/**
 * Step 2: pick a provider, then "Save & Test" posts the API key to
 * POST /api/providers/config and re-reads GET /api/providers to confirm the
 * backend stored it. Ollama needs no key, so it just records the selection.
 */
@Composable
private fun ProviderStep(
    repo: SettingsRepository,
    apiFactory: (String) -> BoomApiService,
    onComplete: () -> Unit,
) {
    val scope = rememberCoroutineScope()
    val baseUrl by repo.serverUrl.collectAsStateWithLifecycle(initialValue = DEFAULT_SERVER_URL)
    // Rebuild the client if the stored URL changes (should not mid-wizard, but safe).
    val api = remember(baseUrl) { apiFactory(baseUrl) }

    var providers by remember { mutableStateOf<List<UiProvider>>(PROVIDER_CATALOG) }
    var loading by remember { mutableStateOf(true) }
    var fetchError by remember { mutableStateOf<String?>(null) }
    var selected by remember { mutableStateOf<String?>(null) }
    var apiKey by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var statusMessage by remember { mutableStateOf<String?>(null) }
    var providerReady by remember { mutableStateOf(false) }

    /** Merges backend state (has_key / active provider) into the static catalog. */
    suspend fun refreshProviders() {
        when (val result = safeCall { api.providers() }) {
            is ApiResult.Success -> {
                val response = result.value
                val infoById = response.providers.associateBy { it.id }
                providers = PROVIDER_CATALOG.map { ui ->
                    val info = infoById[ui.id]
                    ui.copy(
                        hasKey = info?.hasKey ?: false,
                        isActive = info?.isActive(response.activeId) ?: false,
                    )
                }
                fetchError = null
                response.activeId?.let { activeId ->
                    if (PROVIDER_CATALOG.any { it.id == activeId }) selected = activeId
                }
            }
            is ApiResult.Failure -> fetchError = result.message
        }
    }

    LaunchedEffect(Unit) {
        refreshProviders()
        loading = false
    }

    Column(modifier = Modifier.fillMaxSize()) {
        Text("Choose your AI provider", style = MaterialTheme.typography.headlineSmall)
        Spacer(Modifier.height(8.dp))
        Text(
            "Pick the LLM provider Boom will use. Keys are stored on your backend only.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(12.dp))

        if (loading) {
            Column(
                modifier = Modifier.weight(1f).fillMaxWidth(),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.Center,
            ) {
                CircularProgressIndicator()
            }
        } else {
            if (fetchError != null) {
                Text(
                    "Could not load provider status from the backend: $fetchError. " +
                        "You can still pick a provider below.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                )
                Spacer(Modifier.height(8.dp))
            }
            LazyColumn(
                modifier = Modifier.weight(1f).fillMaxWidth(),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                items(providers, key = { it.id }) { provider ->
                    ProviderCard(
                        provider = provider,
                        isSelected = selected == provider.id,
                        onSelect = {
                            selected = provider.id
                            apiKey = ""
                            statusMessage = null
                            providerReady = provider.id == "ollama"
                        },
                    )
                }
            }
        }

        val selectedProvider = providers.firstOrNull { it.id == selected }
        val needsKey = selectedProvider != null && selectedProvider.id != "ollama"

        if (needsKey) {
            Spacer(Modifier.height(12.dp))
            OutlinedTextField(
                value = apiKey,
                onValueChange = { apiKey = it },
                label = { Text("API key") },
                placeholder = { Text(selectedProvider?.hint ?: "") },
                singleLine = true,
                visualTransformation = PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(8.dp))
            Button(
                onClick = {
                    val providerId = selected ?: return@Button
                    busy = true
                    statusMessage = null
                    providerReady = false
                    scope.launch {
                        val saveResult = safeCall {
                            api.saveProvider(
                                ProviderConfigRequest(
                                    providerId = providerId,
                                    apiKey = apiKey.trim(),
                                ),
                            )
                        }
                        when (saveResult) {
                            is ApiResult.Success -> {
                                // Persist the choice locally, then re-read the
                                // backend to confirm the key landed.
                                repo.setProviderId(providerId)
                                refreshProviders()
                                val stored = providers.firstOrNull { it.id == providerId }
                                providerReady = stored?.hasKey == true
                                statusMessage = if (providerReady) {
                                    "Saved. The backend reports an API key on file " +
                                        "for ${stored?.name ?: providerId}."
                                } else {
                                    "Saved, but the backend does not report a key " +
                                        "for ${stored?.name ?: providerId} — try again."
                                }
                            }
                            is ApiResult.Failure ->
                                statusMessage = "Could not save the key: ${saveResult.message}"
                        }
                        busy = false
                    }
                },
                enabled = !busy && apiKey.isNotBlank(),
            ) {
                if (busy) {
                    CircularProgressIndicator(
                        modifier = Modifier.padding(end = 8.dp),
                        strokeWidth = 2.dp,
                    )
                }
                Text("Save & Test")
            }
        }

        statusMessage?.let {
            Spacer(Modifier.height(8.dp))
            Text(
                it,
                style = MaterialTheme.typography.bodySmall,
                color = if (providerReady) MaterialTheme.colorScheme.primary
                else MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        Spacer(Modifier.height(12.dp))
        Button(
            onClick = {
                val providerId = selected ?: return@Button
                scope.launch {
                    // Ollama needs no key: just persist the selection here.
                    if (providerId == "ollama") repo.setProviderId(providerId)
                    onComplete()
                }
            },
            enabled = selected != null && (selected == "ollama" || providerReady),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text("Finish")
        }
    }
}

/** Single selectable provider row with key/active badges. */
@Composable
private fun ProviderCard(
    provider: UiProvider,
    isSelected: Boolean,
    onSelect: () -> Unit,
) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onSelect),
        colors = CardDefaults.cardColors(
            containerColor = if (isSelected) MaterialTheme.colorScheme.primaryContainer
            else MaterialTheme.colorScheme.surfaceVariant,
        ),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            RadioButton(selected = isSelected, onClick = onSelect)
            Spacer(Modifier.width(8.dp))
            Column(modifier = Modifier.weight(1f)) {
                Text(provider.name, style = MaterialTheme.typography.titleMedium)
                Text(
                    provider.hint,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            if (provider.isActive) {
                Text(
                    "active",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.primary,
                )
            } else if (provider.hasKey) {
                Text(
                    "key saved",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}
