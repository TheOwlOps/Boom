package com.theowlops.boom.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.theowlops.boom.data.DEFAULT_SERVER_URL
import com.theowlops.boom.data.PROVIDER_CATALOG
import com.theowlops.boom.data.SettingsRepository
import kotlinx.coroutines.launch

private const val APP_VERSION = "0.1.0"

/**
 * Post-setup settings: view/edit the server URL, see the current provider,
 * about info, and a reset action that clears the setup flag — [BoomApp]
 * observes the flag and shows the setup wizard again automatically.
 *
 * [onReshowWizard] is an optional hook for callers that host the wizard
 * themselves; it defaults to a no-op.
 */
@Composable
fun SettingsScreen(
    repository: SettingsRepository,
    onReshowWizard: () -> Unit = {},
) {
    val scope = rememberCoroutineScope()
    val serverUrl by repository.serverUrl.collectAsStateWithLifecycle(
        initialValue = DEFAULT_SERVER_URL,
    )
    val providerId by repository.providerId.collectAsStateWithLifecycle(initialValue = null)

    var showServerDialog by remember { mutableStateOf(false) }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(24.dp),
    ) {
        Text("Settings", style = MaterialTheme.typography.headlineMedium)
        Spacer(Modifier.height(16.dp))

        // --- Server section -------------------------------------------------
        SectionCard(title = "Server") {
            Text(
                serverUrl,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(8.dp))
            Button(onClick = { showServerDialog = true }) {
                Text("Edit")
            }
        }
        Spacer(Modifier.height(12.dp))

        // --- Provider section -----------------------------------------------
        SectionCard(title = "Provider") {
            val providerName = PROVIDER_CATALOG.firstOrNull { it.id == providerId }?.name
                ?: providerId
                ?: "Not set"
            Text(
                providerName,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(4.dp))
            Text(
                "To change the provider or its API key, re-run the setup wizard.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Spacer(Modifier.height(12.dp))

        // --- About section ---------------------------------------------------
        SectionCard(title = "About") {
            Text("Boom — AI agent harness", style = MaterialTheme.typography.bodyMedium)
            Text(
                "Version $APP_VERSION",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Spacer(Modifier.height(24.dp))

        // --- Danger zone ------------------------------------------------------
        TextButton(
            onClick = {
                scope.launch {
                    repository.setSetupComplete(false)
                    onReshowWizard()
                }
            },
        ) {
            Text("Reset setup", color = MaterialTheme.colorScheme.error)
        }
    }

    if (showServerDialog) {
        ServerUrlDialog(
            currentUrl = serverUrl,
            onDismiss = { showServerDialog = false },
            onSave = { newUrl ->
                scope.launch {
                    repository.setServerUrl(newUrl)
                    showServerDialog = false
                }
            },
        )
    }
}

/** Labeled card used for each settings section. */
@Composable
private fun SectionCard(
    title: String,
    content: @Composable () -> Unit,
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceVariant,
        ),
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(8.dp))
            content()
        }
    }
}

/** Dialog to edit the backend server URL. */
@Composable
private fun ServerUrlDialog(
    currentUrl: String,
    onDismiss: () -> Unit,
    onSave: (String) -> Unit,
) {
    var url by remember(currentUrl) { mutableStateOf(currentUrl) }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Server URL") },
        text = {
            OutlinedTextField(
                value = url,
                onValueChange = { url = it },
                label = { Text("Server URL") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
        },
        confirmButton = {
            TextButton(onClick = { onSave(url) }, enabled = url.isNotBlank()) {
                Text("Save")
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("Cancel")
            }
        },
    )
}
