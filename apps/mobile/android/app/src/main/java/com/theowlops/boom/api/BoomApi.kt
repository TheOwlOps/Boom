package com.theowlops.boom.api

import com.google.gson.annotations.SerializedName
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.HttpException
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.POST
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Data models for the Boom Python backend REST API.
 *
 * Backend source of truth: gateway/api.py (plain stdlib HTTP server,
 * served at http://<server>:18080). All JSON uses snake_case keys, so
 * every multi-word field is mapped with @SerializedName.
 *
 * Verified endpoint contracts:
 * - GET  /api/health            -> {status, version, model, uptime_sec, tools_count, skills_count}
 * - POST /api/chat              -> request {prompt, strategy, session_id, bot_id}
 *                                  reply   {reply, session_id, strategy, errors}
 * - GET  /api/providers         -> {providers: [{id, name, ..., has_key}], active: <provider id>}
 * - POST /api/providers/config  -> request {provider_id, api_key, model, base_url?}
 *                                  reply   {ok, provider_id}
 * - GET  /api/bots              -> {bots: [{id, name, description, avatar_emoji, is_active}], count}
 * - GET  /api/marketplace        -> {items: [...]}        (not wrapped here yet)
 * - GET  /api/channels/status   -> fixed channel list    (not wrapped here yet)
 */

/** GET /api/health */
data class HealthResponse(
    @SerializedName("status") val status: String = "",
    @SerializedName("model") val model: String? = null,
    @SerializedName("version") val version: String? = null,
    @SerializedName("uptime_sec") val uptimeSec: Long = 0,
    @SerializedName("tools_count") val toolsCount: Int = 0,
    @SerializedName("skills_count") val skillsCount: Int = 0
)

/**
 * POST /api/chat request.
 *
 * The wire field is named "prompt" (the backend rejects the request without it),
 * so the Kotlin property keeps the friendly name `message` and is remapped.
 */
data class ChatRequest(
    @SerializedName("prompt") val message: String,
    @SerializedName("session_id") val sessionId: String? = null,
    @SerializedName("bot_id") val botId: String? = null,
    @SerializedName("strategy") val strategy: String = "auto"
)

/**
 * POST /api/chat reply.
 *
 * The backend returns the assistant's text in "reply" (see _handle_chat).
 * "action" and "response" are kept as lenient fallbacks so the client does
 * not break if the backend ever renames the field.
 */
data class ChatResponse(
    @SerializedName("reply") val reply: String? = null,
    @SerializedName("action") val action: String? = null,
    @SerializedName("response") val response: String? = null,
    @SerializedName("session_id") val sessionId: String? = null,
    @SerializedName("strategy") val strategy: String? = null,
    @SerializedName("errors") val errors: List<String>? = null,
    @SerializedName("success") val success: Boolean = true,
    @SerializedName("error") val error: String? = null
) {
    /** The assistant's message text, whichever wire field carried it. */
    val text: String?
        get() = reply ?: action ?: response
}

/** One entry of GET /api/providers. `has_key` is injected by the backend. */
data class ProviderInfo(
    @SerializedName("id") val id: String = "",
    @SerializedName("name") val name: String = "",
    @SerializedName("has_key") val hasKey: Boolean = false,
    @SerializedName("active") val active: Boolean = false
) {
    /** Whether this provider is the currently active one, per the top-level `active` id. */
    fun isActive(activeId: String?): Boolean = active || id == activeId
}

/** GET /api/providers — "active" is the id of the currently active provider. */
data class ProvidersResponse(
    @SerializedName("providers") val providers: List<ProviderInfo> = emptyList(),
    @SerializedName("active") val activeId: String? = null
)

/** POST /api/providers/config request (api_key is never returned by the backend). */
data class ProviderConfigRequest(
    @SerializedName("provider_id") val providerId: String,
    @SerializedName("api_key") val apiKey: String,
    @SerializedName("model") val model: String? = null,
    @SerializedName("base_url") val baseUrl: String? = null
)

/** One entry of GET /api/bots. */
data class BotInfo(
    @SerializedName("id") val id: String = "",
    @SerializedName("name") val name: String = "",
    @SerializedName("description") val description: String? = null,
    @SerializedName("avatar_emoji") val avatarEmoji: String? = null,
    @SerializedName("is_active") val isActive: Boolean = false
)

/** GET /api/bots */
data class BotsResponse(
    @SerializedName("bots") val bots: List<BotInfo> = emptyList(),
    @SerializedName("count") val count: Int = 0
)

/** Retrofit service for the Boom backend REST API. */
interface BoomApiService {

    @GET("api/health")
    suspend fun health(): HealthResponse

    @POST("api/chat")
    suspend fun chat(@Body req: ChatRequest): ChatResponse

    @GET("api/providers")
    suspend fun providers(): ProvidersResponse

    @POST("api/providers/config")
    suspend fun saveProvider(@Body req: ProviderConfigRequest): Map<String, Any>

    @GET("api/bots")
    suspend fun bots(): BotsResponse
}

/**
 * Result wrapper for API calls that keeps network/HTTP failures out of
 * the caller's control flow.
 */
sealed class ApiResult<out T> {
    data class Success<out T>(val value: T) : ApiResult<T>()
    data class Failure(val error: Throwable, val message: String = error.message ?: "Unknown error") :
        ApiResult<Nothing>()
}

/**
 * Wraps a suspending API call, converting [IOException] (no network,
 * timeouts, unreachable host) and [HttpException] (non-2xx) into
 * [ApiResult.Failure]. Any other throwable is rethrown so programming
 * bugs stay loud.
 */
suspend fun <T> safeCall(block: suspend () -> T): ApiResult<T> =
    try {
        ApiResult.Success(block())
    } catch (e: IOException) {
        ApiResult.Failure(e)
    } catch (e: HttpException) {
        ApiResult.Failure(e, "HTTP ${e.code()}: ${e.message()}")
    }

/** Builds [BoomApiService] instances. */
object BoomApiClient {

    /** Base URL for the Android emulator loopback to the host machine. */
    const val EMULATOR_BASE_URL = "http://10.0.2.2:18080/"

    private const val TIMEOUT_SECONDS = 15L

    /**
     * Creates a [BoomApiService] for the given base URL.
     * A trailing slash is appended when missing (Retrofit requires one).
     */
    fun create(baseUrl: String): BoomApiService {
        val normalized = if (baseUrl.endsWith("/")) baseUrl else "$baseUrl/"

        val logging = HttpLoggingInterceptor().apply {
            level = HttpLoggingInterceptor.Level.BASIC
        }

        val okHttp = OkHttpClient.Builder()
            .connectTimeout(TIMEOUT_SECONDS, TimeUnit.SECONDS)
            .readTimeout(TIMEOUT_SECONDS, TimeUnit.SECONDS)
            .writeTimeout(TIMEOUT_SECONDS, TimeUnit.SECONDS)
            .addInterceptor(logging)
            .build()

        return Retrofit.Builder()
            .baseUrl(normalized)
            .addConverterFactory(GsonConverterFactory.create())
            .client(okHttp)
            .build()
            .create(BoomApiService::class.java)
    }
}
