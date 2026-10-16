package com.theowlops.boom.data

/**
 * UI-facing provider entry. [hasKey] and [isActive] are filled in at runtime
 * from the backend's GET /api/providers response; the catalog below only
 * carries static metadata.
 */
data class UiProvider(
    val id: String,
    val name: String,
    val hasKey: Boolean,
    val isActive: Boolean,
    val hint: String,
)

/** Static provider catalog shown in the setup wizard. Order = display order. */
val PROVIDER_CATALOG: List<UiProvider> = listOf(
    UiProvider(id = "openai", name = "OpenAI", hasKey = false, isActive = false, hint = "sk-..."),
    UiProvider(
        id = "anthropic",
        name = "Anthropic",
        hasKey = false,
        isActive = false,
        hint = "sk-ant-...",
    ),
    UiProvider(
        id = "google",
        name = "Google Gemini",
        hasKey = false,
        isActive = false,
        hint = "AIza...",
    ),
    UiProvider(id = "xai", name = "xAI Grok", hasKey = false, isActive = false, hint = "xai-..."),
    UiProvider(
        id = "deepseek",
        name = "DeepSeek",
        hasKey = false,
        isActive = false,
        hint = "sk-...",
    ),
    UiProvider(
        id = "ollama",
        name = "Ollama (local, no key)",
        hasKey = false,
        isActive = false,
        hint = "No API key needed — runs on your machine",
    ),
    UiProvider(
        id = "custom",
        name = "Custom OpenAI-compatible",
        hasKey = false,
        isActive = false,
        hint = "API key from your own provider",
    ),
)

/**
 * Normalizes a user-entered backend URL: trims whitespace, prepends http://
 * when no scheme is present, and guarantees a trailing slash (Retrofit
 * requires base URLs to end with '/').
 */
fun String.normalizeBaseUrl(): String {
    var url = trim()
    if (url.isEmpty()) return DEFAULT_SERVER_URL
    if (!url.contains("://")) {
        url = "http://$url"
    }
    if (!url.endsWith("/")) {
        url += "/"
    }
    return url
}
