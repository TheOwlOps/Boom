//! Chat endpoints for the Boom desktop backend.
//!
//! Pure-Rust replacement for the Python gateway's `POST /api/chat` and
//! `GET /api/bots`, built on axum + reqwest. The active AI provider is read
//! from `$BOOM_HOME/providers.json` (default `~/.boom/providers.json`) — the
//! same file the `boom setup` wizard writes — and the prompt is forwarded to
//! that provider's HTTP API with a 120s timeout.
//!
//! Wiring: `backend/mod.rs` (owned by another worker) declares
//! `pub mod chat;` and merges this sub-router into the app router:
//!
//! ```ignore
//! pub fn app_router() -> Router {
//!     Router::new().merge(chat::router())
//! }
//! ```
//!
//! # Security notes
//!
//! * The provider API key is loaded from the config file but is **never**
//!   logged and never appears in any HTTP response.
//! * Transport errors are reported with a static message: reqwest's error
//!   text can embed the request URL, and the Gemini key travels in the
//!   query string, so the raw error is only used for log metadata
//!   (timeout/connect flags), never echoed.

use std::collections::HashMap;
use std::path::PathBuf;
use std::time::Duration;

use axum::{
    extract::State,
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use reqwest::Client;
use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/// Per-request timeout for LLM calls. The desktop UI budgets 120s for chat
/// (`CHAT_TIMEOUT_MS` in `apps/desktop/src/lib/api.ts`), so the backend matches.
const LLM_TIMEOUT: Duration = Duration::from_secs(120);

/// Exact error string returned with HTTP 503 when no provider is configured.
/// The desktop UI keys on this value (`PROVIDER_NOT_CONFIGURED_ERROR` in
/// `apps/desktop/src/lib/api.ts`) to show its "open Settings" prompt instead of
/// a generic backend-unreachable error.
const NOT_CONFIGURED_ERROR: &str = "agent brain not configured";

/// Upper bound (in chars) for an upstream error body echoed back to the
/// caller, so a huge HTML error page never floods the chat response.
const MAX_UPSTREAM_DETAIL_CHARS: usize = 500;

// ---------------------------------------------------------------------------
// Provider registry (mirrors agent/src/providers/manager.py PROVIDER_DEFS)
// ---------------------------------------------------------------------------

/// Which HTTP API shape a provider speaks.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ProviderKind {
    /// `POST {base}/chat/completions` with `{"model", "messages"}`.
    OpenAiCompatible,
    /// `POST {base}/v1/messages` with Anthropic headers and body.
    Anthropic,
    /// `POST {base}/v1beta/models/{model}:generateContent?key=...`.
    Google,
}

/// Static provider metadata. `default_model` / `default_base_url` fill in
/// blanks left by older wizard runs, mirroring
/// `ProviderManager.get_config()` in the Python codebase.
struct ProviderDef {
    id: &'static str,
    kind: ProviderKind,
    needs_key: bool,
    default_base_url: &'static str,
    default_model: &'static str,
}

const PROVIDER_DEFS: &[ProviderDef] = &[
    ProviderDef {
        id: "openai",
        kind: ProviderKind::OpenAiCompatible,
        needs_key: true,
        default_base_url: "https://api.openai.com/v1",
        default_model: "gpt-4o",
    },
    ProviderDef {
        id: "anthropic",
        kind: ProviderKind::Anthropic,
        needs_key: true,
        default_base_url: "https://api.anthropic.com",
        default_model: "claude-sonnet-4-5",
    },
    ProviderDef {
        id: "google",
        kind: ProviderKind::Google,
        needs_key: true,
        default_base_url: "https://generativelanguage.googleapis.com",
        default_model: "gemini-2.5-flash",
    },
    ProviderDef {
        id: "xai",
        kind: ProviderKind::OpenAiCompatible,
        needs_key: true,
        default_base_url: "https://api.x.ai/v1",
        default_model: "grok-4",
    },
    ProviderDef {
        id: "deepseek",
        kind: ProviderKind::OpenAiCompatible,
        needs_key: true,
        default_base_url: "https://api.deepseek.com/v1",
        default_model: "deepseek-chat",
    },
    ProviderDef {
        id: "ollama",
        kind: ProviderKind::OpenAiCompatible,
        needs_key: false,
        default_base_url: "http://localhost:11434/v1",
        default_model: "llama3.3",
    },
    ProviderDef {
        id: "custom",
        kind: ProviderKind::OpenAiCompatible,
        needs_key: true,
        default_base_url: "",
        default_model: "",
    },
];

// ---------------------------------------------------------------------------
// Provider configuration file ($BOOM_HOME/providers.json)
// ---------------------------------------------------------------------------

/// On-disk shape written by the `boom setup` wizard
/// (`agent/src/providers/manager.py`). Unknown fields are ignored so the
/// file stays forward-compatible.
#[derive(Debug, Deserialize)]
struct ProvidersFile {
    #[serde(default)]
    active: Option<String>,
    #[serde(default)]
    providers: HashMap<String, SavedProviderConfig>,
}

#[derive(Debug, Deserialize)]
struct SavedProviderConfig {
    #[serde(default)]
    api_key: String,
    #[serde(default)]
    model: String,
    #[serde(default)]
    base_url: String,
}

/// Resolved credentials for the currently active provider.
#[derive(Debug)]
struct ActiveProvider {
    id: String,
    kind: ProviderKind,
    /// May be empty for keyless providers (ollama). Never logged.
    api_key: String,
    model: String,
    base_url: String,
}

/// `$BOOM_HOME` wins; falls back to `~/.boom` (mirrors
/// `agent/src/config.py::boom_home`).
fn boom_home() -> Option<PathBuf> {
    if let Ok(dir) = std::env::var("BOOM_HOME") {
        if !dir.trim().is_empty() {
            return Some(PathBuf::from(dir));
        }
    }
    std::env::var("HOME")
        .ok()
        .map(|home| PathBuf::from(home).join(".boom"))
}

fn providers_path() -> Option<PathBuf> {
    boom_home().map(|dir| dir.join("providers.json"))
}

/// Resolve the active provider from `$BOOM_HOME/providers.json`.
///
/// Any problem — missing file, invalid JSON, no active provider, unknown
/// provider id, missing key/model/base URL — maps to
/// [`ChatError::NotConfigured`] (HTTP 503), mirroring the Python gateway's
/// "agent brain not configured" behavior.
fn load_active_provider() -> Result<ActiveProvider, ChatError> {
    let path = providers_path().ok_or(ChatError::NotConfigured)?;
    let raw = std::fs::read_to_string(&path).map_err(|_| ChatError::NotConfigured)?;
    let file: ProvidersFile = serde_json::from_str(&raw).map_err(|_| ChatError::NotConfigured)?;
    let active_id = file
        .active
        .as_deref()
        .map(str::trim)
        .filter(|id| !id.is_empty())
        .ok_or(ChatError::NotConfigured)?;
    let def = PROVIDER_DEFS
        .iter()
        .find(|def| def.id == active_id)
        .ok_or(ChatError::NotConfigured)?;
    let saved = file
        .providers
        .get(active_id)
        .ok_or(ChatError::NotConfigured)?;

    let api_key = saved.api_key.trim().to_string();
    if def.needs_key && api_key.is_empty() {
        return Err(ChatError::NotConfigured);
    }
    let model = if saved.model.trim().is_empty() {
        def.default_model
    } else {
        saved.model.trim()
    }
    .to_string();
    let base_url = if saved.base_url.trim().is_empty() {
        def.default_base_url
    } else {
        saved.base_url.trim()
    }
    .to_string();
    // A custom provider with no model or base URL cannot be called.
    if model.is_empty() || base_url.is_empty() {
        return Err(ChatError::NotConfigured);
    }
    Ok(ActiveProvider {
        id: def.id.to_string(),
        kind: def.kind,
        api_key,
        model,
        base_url,
    })
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/// Failures of the chat endpoints. Every variant maps to a JSON
/// `{"error": ...}` body; the API key never appears in any message.
#[derive(Debug)]
pub enum ChatError {
    /// No usable provider configuration — HTTP 503.
    NotConfigured,
    /// Malformed request (e.g. empty prompt) — HTTP 400.
    BadRequest(String),
    /// Network/TLS/timeout while calling the provider — HTTP 502.
    Transport(reqwest::Error),
    /// Provider answered with a non-2xx status — HTTP 502.
    Upstream { status: u16, detail: String },
    /// Provider answered 2xx but the body carried no usable text — HTTP 502.
    Unparsable(&'static str),
    /// Backend-side problem (HTTP client could not be built) — HTTP 500.
    Internal(String),
}

impl ChatError {
    fn upstream(status: StatusCode, body: &str) -> Self {
        ChatError::Upstream {
            status: status.as_u16(),
            detail: truncate_chars(body.trim(), MAX_UPSTREAM_DETAIL_CHARS),
        }
    }

    fn unparsable(what: &'static str) -> Self {
        ChatError::Unparsable(what)
    }
}

impl IntoResponse for ChatError {
    fn into_response(self) -> Response {
        // Server-side diagnostics. The API key is never logged: transport
        // errors only contribute timeout/connect flags because reqwest's
        // error text can embed the request URL (and the Gemini key travels
        // in the query string).
        let (status, message) = match &self {
            ChatError::NotConfigured => {
                log::debug!("chat: no provider configured");
                (
                    StatusCode::SERVICE_UNAVAILABLE,
                    NOT_CONFIGURED_ERROR.to_string(),
                )
            }
            ChatError::BadRequest(msg) => (StatusCode::BAD_REQUEST, msg.clone()),
            ChatError::Transport(err) => {
                log::warn!(
                    "chat: provider request failed (timeout={}, connect={})",
                    err.is_timeout(),
                    err.is_connect()
                );
                (
                    StatusCode::BAD_GATEWAY,
                    "provider request failed".to_string(),
                )
            }
            ChatError::Upstream { status, detail } => {
                log::warn!("chat: provider returned http {status}: {detail}");
                (
                    StatusCode::BAD_GATEWAY,
                    format!("provider error (http {status}): {detail}"),
                )
            }
            ChatError::Unparsable(what) => {
                log::warn!("chat: provider response had no usable text ({what})");
                (
                    StatusCode::BAD_GATEWAY,
                    format!("provider returned an unusable response ({what})"),
                )
            }
            ChatError::Internal(msg) => {
                log::error!("chat: internal error: {msg}");
                (StatusCode::INTERNAL_SERVER_ERROR, msg.clone())
            }
        };
        (status, Json(serde_json::json!({ "error": message }))).into_response()
    }
}

/// Truncate to `max_chars` characters (char-boundary safe), appending an
/// ellipsis when truncated.
fn truncate_chars(s: &str, max_chars: usize) -> String {
    let truncated: String = s.chars().take(max_chars).collect();
    if truncated.len() < s.len() {
        format!("{truncated}…")
    } else {
        truncated
    }
}

// ---------------------------------------------------------------------------
// LLM calls
// ---------------------------------------------------------------------------

/// Send one request and return the response body text. Transport failures
/// and non-2xx statuses become typed errors.
async fn post_json(request: reqwest::RequestBuilder) -> Result<String, ChatError> {
    let response = request.send().await.map_err(ChatError::Transport)?;
    let status = response.status();
    let body = response.text().await.map_err(ChatError::Transport)?;
    if !status.is_success() {
        return Err(ChatError::upstream(status, &body));
    }
    Ok(body)
}

/// Reject empty completions with a typed error.
fn non_empty(text: String, what: &'static str) -> Result<String, ChatError> {
    if text.trim().is_empty() {
        Err(ChatError::unparsable(what))
    } else {
        Ok(text)
    }
}

// --- OpenAI-compatible (openai, deepseek, xai, ollama, custom) ------------

#[derive(Debug, Deserialize)]
struct OpenAiChatResponse {
    #[serde(default)]
    choices: Vec<OpenAiChoice>,
}

#[derive(Debug, Deserialize)]
struct OpenAiChoice {
    message: OpenAiMessage,
}

#[derive(Debug, Deserialize)]
struct OpenAiMessage {
    /// `content` is usually a string, but some providers return an array of
    /// parts, and reasoning models may return null.
    #[serde(default)]
    content: Option<OpenAiContent>,
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum OpenAiContent {
    Text(String),
    Parts(Vec<OpenAiPart>),
}

#[derive(Debug, Deserialize)]
struct OpenAiPart {
    #[serde(default)]
    text: Option<String>,
}

/// `POST {base}/chat/completions` with `{"model", "messages"}`. No
/// `Authorization` header is sent when the provider has no key (ollama,
/// keyless local servers).
async fn complete_openai_compatible(
    client: &Client,
    cfg: &ActiveProvider,
    prompt: &str,
) -> Result<String, ChatError> {
    let url = format!("{}/chat/completions", cfg.base_url.trim_end_matches('/'));
    let mut request = client.post(&url).json(&serde_json::json!({
        "model": cfg.model,
        "messages": [{ "role": "user", "content": prompt }],
    }));
    if !cfg.api_key.is_empty() {
        request = request.bearer_auth(&cfg.api_key);
    }
    let body = post_json(request).await?;
    extract_openai_text(&body)
}

fn extract_openai_text(body: &str) -> Result<String, ChatError> {
    let parsed: OpenAiChatResponse = serde_json::from_str(body)
        .map_err(|_| ChatError::unparsable("choices[].message.content"))?;
    let text = parsed
        .choices
        .into_iter()
        .next()
        .and_then(|choice| choice.message.content)
        .map(|content| match content {
            OpenAiContent::Text(text) => text,
            OpenAiContent::Parts(parts) => parts
                .into_iter()
                .filter_map(|part| part.text)
                .collect::<Vec<_>>()
                .join(""),
        })
        .unwrap_or_default();
    non_empty(text, "empty completion")
}

// --- Anthropic ---------------------------------------------------------------

#[derive(Debug, Deserialize)]
struct AnthropicResponse {
    #[serde(default)]
    content: Vec<AnthropicBlock>,
}

#[derive(Debug, Deserialize)]
struct AnthropicBlock {
    #[serde(rename = "type", default)]
    kind: String,
    #[serde(default)]
    text: Option<String>,
}

/// `POST {base}/v1/messages` with the `x-api-key` header and
/// `{"model", "max_tokens", "messages"}` body.
async fn complete_anthropic(
    client: &Client,
    cfg: &ActiveProvider,
    prompt: &str,
) -> Result<String, ChatError> {
    let url = format!("{}/v1/messages", cfg.base_url.trim_end_matches('/'));
    let request = client
        .post(&url)
        .header("x-api-key", &cfg.api_key)
        .header("anthropic-version", "2023-06-01")
        .json(&serde_json::json!({
            "model": cfg.model,
            "max_tokens": 1024,
            "messages": [{ "role": "user", "content": prompt }],
        }));
    let body = post_json(request).await?;
    extract_anthropic_text(&body)
}

fn extract_anthropic_text(body: &str) -> Result<String, ChatError> {
    let parsed: AnthropicResponse =
        serde_json::from_str(body).map_err(|_| ChatError::unparsable("content[].text"))?;
    let text = parsed
        .content
        .into_iter()
        .filter(|block| block.kind == "text")
        .filter_map(|block| block.text)
        .next()
        .unwrap_or_default();
    non_empty(text, "no text block")
}

// --- Google (Gemini) ----------------------------------------------------------

#[derive(Debug, Deserialize)]
struct GeminiResponse {
    #[serde(default)]
    candidates: Vec<GeminiCandidate>,
}

#[derive(Debug, Deserialize)]
struct GeminiCandidate {
    #[serde(default)]
    content: Option<GeminiContent>,
}

#[derive(Debug, Deserialize)]
struct GeminiContent {
    #[serde(default)]
    parts: Vec<GeminiPart>,
}

#[derive(Debug, Deserialize)]
struct GeminiPart {
    #[serde(default)]
    text: Option<String>,
}

/// `POST {base}/v1beta/models/{model}:generateContent?key=...` with
/// `{"contents": [{"parts": [{"text": prompt}]}]}`. `.query()` percent-encodes
/// the key.
async fn complete_google(
    client: &Client,
    cfg: &ActiveProvider,
    prompt: &str,
) -> Result<String, ChatError> {
    let url = format!(
        "{}/v1beta/models/{}:generateContent",
        cfg.base_url.trim_end_matches('/'),
        cfg.model
    );
    let request = client
        .post(&url)
        .query(&[("key", cfg.api_key.as_str())])
        .json(&serde_json::json!({
            "contents": [{ "parts": [{ "text": prompt }] }],
        }));
    let body = post_json(request).await?;
    extract_gemini_text(&body)
}

fn extract_gemini_text(body: &str) -> Result<String, ChatError> {
    let parsed: GeminiResponse = serde_json::from_str(body)
        .map_err(|_| ChatError::unparsable("candidates[].content.parts[].text"))?;
    let text = parsed
        .candidates
        .into_iter()
        .next()
        .and_then(|candidate| candidate.content)
        .map(|content| {
            content
                .parts
                .into_iter()
                .filter_map(|part| part.text)
                .collect::<Vec<_>>()
                .join("")
        })
        .unwrap_or_default();
    non_empty(text, "no text part")
}

// --- Dispatch -----------------------------------------------------------------

async fn complete(
    client: &Client,
    cfg: &ActiveProvider,
    prompt: &str,
) -> Result<String, ChatError> {
    match cfg.kind {
        ProviderKind::OpenAiCompatible => complete_openai_compatible(client, cfg, prompt).await,
        ProviderKind::Anthropic => complete_anthropic(client, cfg, prompt).await,
        ProviderKind::Google => complete_google(client, cfg, prompt).await,
    }
}

// ---------------------------------------------------------------------------
// HTTP API
// ---------------------------------------------------------------------------

/// `POST /api/chat` request body. Matches the desktop UI's `ChatRequest`
/// (`apps/desktop/src/lib/api.ts`): `{"prompt", "bot_id"?, "session_id"?}`.
/// `bot_id` / `session_id` are accepted for forward compatibility; routing to
/// a specific bot is not implemented yet.
#[derive(Debug, Deserialize)]
pub struct ChatRequest {
    pub prompt: String,
    #[serde(default)]
    pub bot_id: Option<String>,
    #[serde(default)]
    pub session_id: Option<String>,
}

/// `POST /api/chat` response body: `{"reply", "model", "provider"}`.
#[derive(Debug, Serialize)]
pub struct ChatResponse {
    pub reply: String,
    pub model: String,
    pub provider: String,
}

/// One bot entry for `GET /api/bots`. Full bot management (SQLite) lands
/// later; the desktop always exposes the single default bot for now.
#[derive(Debug, Serialize)]
pub struct Bot {
    pub id: String,
    pub name: String,
    pub active: bool,
}

/// `GET /api/bots` response body: `{"bots": [...]}`.
#[derive(Debug, Serialize)]
pub struct BotsResponse {
    pub bots: Vec<Bot>,
}

#[derive(Clone)]
struct ChatState {
    /// `None` when the HTTP client could not be built (broken TLS/proxy
    /// setup); the chat handler then answers HTTP 500 instead of panicking.
    client: Option<Client>,
}

async fn post_chat(
    State(state): State<ChatState>,
    Json(req): Json<ChatRequest>,
) -> Result<Json<ChatResponse>, ChatError> {
    if req.prompt.trim().is_empty() {
        return Err(ChatError::BadRequest("prompt is required".to_string()));
    }
    let cfg = load_active_provider()?;
    let client = state
        .client
        .as_ref()
        .ok_or_else(|| ChatError::Internal("http client unavailable".to_string()))?;
    let reply = complete(client, &cfg, req.prompt.trim()).await?;
    Ok(Json(ChatResponse {
        reply,
        model: cfg.model,
        provider: cfg.id,
    }))
}

async fn get_bots() -> Json<BotsResponse> {
    Json(BotsResponse {
        bots: vec![Bot {
            id: "default".to_string(),
            name: "Default bot".to_string(),
            active: true,
        }],
    })
}

/// Build the chat sub-router. Merge it into the app router in
/// `backend/mod.rs`: `Router::new().merge(chat::router())`.
pub fn router() -> Router {
    let client = Client::builder().timeout(LLM_TIMEOUT).build().ok();
    if client.is_none() {
        log::warn!("chat: failed to build HTTP client; POST /api/chat will answer 500");
    }
    Router::new()
        .route("/api/chat", post(post_chat))
        .route("/api/bots", get(get_bots))
        .with_state(ChatState { client })
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Mutex, OnceLock};

    type TestResult = Result<(), Box<dyn std::error::Error>>;

    /// Tests share one process; serialize the ones that touch `BOOM_HOME`.
    fn env_lock() -> &'static Mutex<()> {
        static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
        LOCK.get_or_init(|| Mutex::new(()))
    }

    fn locked() -> std::sync::MutexGuard<'static, ()> {
        env_lock().lock().unwrap_or_else(|e| e.into_inner())
    }

    /// RAII guard restoring the previous value of an env var on drop.
    struct EnvGuard {
        key: &'static str,
        prev: Option<String>,
    }

    impl EnvGuard {
        fn set(key: &'static str, value: &str) -> Self {
            let prev = std::env::var(key).ok();
            std::env::set_var(key, value);
            Self { key, prev }
        }
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            match &self.prev {
                Some(value) => std::env::set_var(self.key, value),
                None => std::env::remove_var(self.key),
            }
        }
    }

    /// Fresh scratch dir for one test (under the OS temp dir).
    fn scratch_dir(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("boom-chat-test-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("create scratch dir");
        dir
    }

    fn write_providers(dir: &std::path::Path, value: serde_json::Value) {
        std::fs::write(dir.join("providers.json"), value.to_string())
            .expect("write providers.json");
    }

    fn test_client() -> Client {
        Client::builder()
            .timeout(Duration::from_secs(10))
            .build()
            .expect("test client")
    }

    fn chat_state() -> ChatState {
        ChatState {
            client: Some(test_client()),
        }
    }

    fn chat_request(prompt: &str) -> ChatRequest {
        ChatRequest {
            prompt: prompt.to_string(),
            bot_id: Some("default".to_string()),
            session_id: None,
        }
    }

    /// Spawn a mock provider on 127.0.0.1 and return its base URL. Waits
    /// until the socket accepts so tests never race the server startup.
    async fn spawn_mock(app: Router) -> String {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind mock");
        let addr = listener.local_addr().expect("mock addr");
        tokio::spawn(async move {
            axum::serve(listener, app).await.expect("mock serve");
        });
        for _ in 0..50 {
            if tokio::net::TcpStream::connect(addr).await.is_ok() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        format!("http://{addr}")
    }

    // --- Response parsing (no network) -------------------------------------

    #[test]
    fn parse_openai_response() {
        let body = r#"{"id":"chatcmpl-1","model":"gpt-4o","choices":[{"index":0,"message":{"role":"assistant","content":"Hello there"},"finish_reason":"stop"}]}"#;
        assert_eq!(extract_openai_text(body).expect("parse"), "Hello there");
    }

    #[test]
    fn parse_openai_part_array_content() {
        let body = r#"{"choices":[{"message":{"role":"assistant","content":[{"type":"text","text":"Hel"},{"type":"text","text":"lo"}]}}]}"#;
        assert_eq!(extract_openai_text(body).expect("parse"), "Hello");
    }

    #[test]
    fn parse_openai_null_content_is_unparsable() {
        // Reasoning models may return null content.
        let body = r#"{"choices":[{"message":{"role":"assistant","content":null}}]}"#;
        assert!(matches!(
            extract_openai_text(body),
            Err(ChatError::Unparsable(_))
        ));
    }

    #[test]
    fn parse_openai_empty_choices_is_unparsable() {
        assert!(extract_openai_text(r#"{"choices":[]}"#).is_err());
    }

    #[test]
    fn parse_anthropic_response() {
        let body = r#"{"id":"msg_1","type":"message","model":"claude-sonnet-4-5","content":[{"type":"text","text":"Hi from Claude"}]}"#;
        assert_eq!(
            extract_anthropic_text(body).expect("parse"),
            "Hi from Claude"
        );
    }

    #[test]
    fn parse_anthropic_skips_non_text_blocks() {
        let body =
            r#"{"content":[{"type":"thinking","thinking":"hmm"},{"type":"text","text":"answer"}]}"#;
        assert_eq!(extract_anthropic_text(body).expect("parse"), "answer");
    }

    #[test]
    fn parse_gemini_response() {
        let body =
            r#"{"candidates":[{"content":{"role":"model","parts":[{"text":"Hi from Gemini"}]}}]}"#;
        assert_eq!(extract_gemini_text(body).expect("parse"), "Hi from Gemini");
    }

    #[test]
    fn parse_gemini_no_candidates_is_unparsable() {
        assert!(extract_gemini_text(r#"{"candidates":[]}"#).is_err());
    }

    // --- Provider config loading --------------------------------------------

    #[test]
    fn missing_providers_file_is_not_configured() {
        let _lock = locked();
        let dir = scratch_dir("missing-file");
        let _env = EnvGuard::set("BOOM_HOME", dir.to_str().expect("utf8"));
        assert!(matches!(
            load_active_provider(),
            Err(ChatError::NotConfigured)
        ));
    }

    #[test]
    fn malformed_providers_file_is_not_configured() {
        let _lock = locked();
        let dir = scratch_dir("malformed");
        std::fs::write(dir.join("providers.json"), "{oops").expect("write");
        let _env = EnvGuard::set("BOOM_HOME", dir.to_str().expect("utf8"));
        assert!(matches!(
            load_active_provider(),
            Err(ChatError::NotConfigured)
        ));
    }

    #[test]
    fn provider_without_key_is_not_configured() {
        let _lock = locked();
        let dir = scratch_dir("no-key");
        write_providers(
            &dir,
            serde_json::json!({
                "active": "openai",
                "providers": {"openai": {"api_key": "", "model": "gpt-4o", "base_url": ""}}
            }),
        );
        let _env = EnvGuard::set("BOOM_HOME", dir.to_str().expect("utf8"));
        assert!(matches!(
            load_active_provider(),
            Err(ChatError::NotConfigured)
        ));
    }

    #[test]
    fn unknown_provider_id_is_not_configured() {
        let _lock = locked();
        let dir = scratch_dir("unknown");
        write_providers(
            &dir,
            serde_json::json!({
                "active": "mystery",
                "providers": {"mystery": {"api_key": "k", "model": "m", "base_url": "http://x"}}
            }),
        );
        let _env = EnvGuard::set("BOOM_HOME", dir.to_str().expect("utf8"));
        assert!(matches!(
            load_active_provider(),
            Err(ChatError::NotConfigured)
        ));
    }

    #[test]
    fn ollama_without_key_loads_with_defaults() {
        let _lock = locked();
        let dir = scratch_dir("ollama");
        write_providers(
            &dir,
            serde_json::json!({
                "active": "ollama",
                "providers": {"ollama": {"api_key": "", "model": "", "base_url": ""}}
            }),
        );
        let _env = EnvGuard::set("BOOM_HOME", dir.to_str().expect("utf8"));
        let cfg = load_active_provider().expect("ollama loads without a key");
        assert_eq!(cfg.id, "ollama");
        assert_eq!(cfg.kind, ProviderKind::OpenAiCompatible);
        assert_eq!(cfg.model, "llama3.3");
        assert_eq!(cfg.base_url, "http://localhost:11434/v1");
    }

    #[test]
    fn saved_values_win_over_defaults() {
        let _lock = locked();
        let dir = scratch_dir("saved-wins");
        write_providers(
            &dir,
            serde_json::json!({
                "active": "openai",
                "providers": {"openai": {"api_key": "sk-test", "model": "gpt-4o-mini", "base_url": "https://proxy.example/v1"}}
            }),
        );
        let _env = EnvGuard::set("BOOM_HOME", dir.to_str().expect("utf8"));
        let cfg = load_active_provider().expect("loads");
        assert_eq!(cfg.model, "gpt-4o-mini");
        assert_eq!(cfg.base_url, "https://proxy.example/v1");
        assert_eq!(cfg.api_key, "sk-test");
    }

    // --- Error -> HTTP mapping ------------------------------------------------

    #[tokio::test]
    // The env lock below is held across awaits. Safe in tests: the lock's
    // only users are these tests, and each one only awaits its own mock
    // server, so no deadlock is possible.
    #[allow(clippy::await_holding_lock)]
    async fn not_configured_maps_to_503_with_exact_error() -> TestResult {
        let _lock = locked();
        let dir = scratch_dir("503");
        let _env = EnvGuard::set("BOOM_HOME", dir.to_str().expect("utf8"));

        let err = post_chat(State(chat_state()), Json(chat_request("hi")))
            .await
            .expect_err("no provider configured");
        assert!(matches!(err, ChatError::NotConfigured));
        let response = err.into_response();
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
        let body = axum::body::to_bytes(response.into_body(), 1024).await?;
        let json: serde_json::Value = serde_json::from_slice(&body)?;
        assert_eq!(json["error"], NOT_CONFIGURED_ERROR);
        Ok(())
    }

    #[tokio::test]
    // The env lock below is held across awaits. Safe in tests: the lock's
    // only users are these tests, and each one only awaits its own mock
    // server, so no deadlock is possible.
    #[allow(clippy::await_holding_lock)]
    async fn empty_prompt_is_400() -> TestResult {
        let _lock = locked();
        let dir = scratch_dir("empty-prompt");
        let _env = EnvGuard::set("BOOM_HOME", dir.to_str().expect("utf8"));

        let err = post_chat(State(chat_state()), Json(chat_request("   ")))
            .await
            .expect_err("empty prompt");
        let response = err.into_response();
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
        Ok(())
    }

    #[tokio::test]
    async fn bots_returns_default_bot() {
        let Json(resp) = get_bots().await;
        assert_eq!(resp.bots.len(), 1);
        assert_eq!(resp.bots[0].id, "default");
        assert_eq!(resp.bots[0].name, "Default bot");
        assert!(resp.bots[0].active);
    }

    // --- End-to-end against mock providers ------------------------------------

    #[tokio::test]
    // The env lock below is held across awaits. Safe in tests: the lock's
    // only users are these tests, and each one only awaits its own mock
    // server, so no deadlock is possible.
    #[allow(clippy::await_holding_lock)]
    async fn chat_end_to_end_openai_compatible() -> TestResult {
        let _lock = locked();
        let base = spawn_mock(Router::new().route(
            "/chat/completions",
            post(|| async {
                Json(serde_json::json!({
                    "choices": [{"message": {"role": "assistant", "content": "mock openai reply"}}]
                }))
            }),
        ))
        .await;
        let dir = scratch_dir("e2e-openai");
        write_providers(
            &dir,
            serde_json::json!({
                "active": "custom",
                "providers": {"custom": {"api_key": "k", "model": "mock-model", "base_url": base}}
            }),
        );
        let _env = EnvGuard::set("BOOM_HOME", dir.to_str().expect("utf8"));

        let Json(resp) = post_chat(State(chat_state()), Json(chat_request("hello")))
            .await
            .map_err(|e| format!("handler failed: {e:?}"))?;
        assert_eq!(resp.reply, "mock openai reply");
        assert_eq!(resp.model, "mock-model");
        assert_eq!(resp.provider, "custom");
        Ok(())
    }

    #[tokio::test]
    // The env lock below is held across awaits. Safe in tests: the lock's
    // only users are these tests, and each one only awaits its own mock
    // server, so no deadlock is possible.
    #[allow(clippy::await_holding_lock)]
    async fn chat_end_to_end_anthropic() -> TestResult {
        let _lock = locked();
        let base = spawn_mock(Router::new().route(
            "/v1/messages",
            post(|req: axum::http::Request<axum::body::Body>| async move {
                // The x-api-key header must be forwarded.
                assert!(req.headers().contains_key("x-api-key"));
                Json(serde_json::json!({
                    "content": [{"type": "text", "text": "mock anthropic reply"}]
                }))
            }),
        ))
        .await;
        let dir = scratch_dir("e2e-anthropic");
        write_providers(
            &dir,
            serde_json::json!({
                "active": "anthropic",
                "providers": {"anthropic": {"api_key": "sk-ant-test", "model": "claude-mock", "base_url": base}}
            }),
        );
        let _env = EnvGuard::set("BOOM_HOME", dir.to_str().expect("utf8"));

        let Json(resp) = post_chat(State(chat_state()), Json(chat_request("hello")))
            .await
            .map_err(|e| format!("handler failed: {e:?}"))?;
        assert_eq!(resp.reply, "mock anthropic reply");
        assert_eq!(resp.provider, "anthropic");
        Ok(())
    }

    #[tokio::test]
    // The env lock below is held across awaits. Safe in tests: the lock's
    // only users are these tests, and each one only awaits its own mock
    // server, so no deadlock is possible.
    #[allow(clippy::await_holding_lock)]
    async fn chat_end_to_end_google() -> TestResult {
        let _lock = locked();
        let base = spawn_mock(Router::new().route(
            "/v1beta/{*rest}",
            post(|| async {
                Json(serde_json::json!({
                    "candidates": [{"content": {"parts": [{"text": "mock gemini reply"}]}}]
                }))
            }),
        ))
        .await;
        let dir = scratch_dir("e2e-google");
        write_providers(
            &dir,
            serde_json::json!({
                "active": "google",
                "providers": {"google": {"api_key": "AIza-test", "model": "gemini-mock", "base_url": base}}
            }),
        );
        let _env = EnvGuard::set("BOOM_HOME", dir.to_str().expect("utf8"));

        let Json(resp) = post_chat(State(chat_state()), Json(chat_request("hello")))
            .await
            .map_err(|e| format!("handler failed: {e:?}"))?;
        assert_eq!(resp.reply, "mock gemini reply");
        assert_eq!(resp.provider, "google");
        Ok(())
    }

    #[tokio::test]
    // The env lock below is held across awaits. Safe in tests: the lock's
    // only users are these tests, and each one only awaits its own mock
    // server, so no deadlock is possible.
    #[allow(clippy::await_holding_lock)]
    async fn provider_401_maps_to_502() -> TestResult {
        let _lock = locked();
        let base = spawn_mock(Router::new().route(
            "/chat/completions",
            post(|| async { (StatusCode::UNAUTHORIZED, "bad key") }),
        ))
        .await;
        let dir = scratch_dir("e2e-401");
        write_providers(
            &dir,
            serde_json::json!({
                "active": "openai",
                "providers": {"openai": {"api_key": "bad", "model": "gpt-4o", "base_url": base}}
            }),
        );
        let _env = EnvGuard::set("BOOM_HOME", dir.to_str().expect("utf8"));

        let err = post_chat(State(chat_state()), Json(chat_request("hello")))
            .await
            .expect_err("provider 401");
        assert!(matches!(err, ChatError::Upstream { status: 401, .. }));
        let response = err.into_response();
        assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
        let body = axum::body::to_bytes(response.into_body(), 1024).await?;
        let json: serde_json::Value = serde_json::from_slice(&body)?;
        assert!(
            json["error"].as_str().unwrap_or_default().contains("401"),
            "error body: {json}"
        );
        Ok(())
    }
}
