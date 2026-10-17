//! Axum HTTP server: router construction, CORS, and `serve()`.
//!
//! Route inventory (ported from `gateway/api.py`):
//!
//! ```text
//! GET    /api/health  /api/version  /api/updates/check  /api/logs
//!        /api/activity  /api/sessions  /api/channels  /api/channels/status
//!        /api/channels/zalo-personal/status  /api/dag/status
//!        /api/tools  /api/skills  /api/groups  /api/bots
//!        /api/bot-groups  /api/bot-groups/{id}
//!        /api/providers  /api/providers/ollama/status
//!        /api/marketplace  /api/marketplace/installed
//! POST   /api/chat  /api/tools/run  /api/groups/mute  /api/groups/policy
//!        /api/bots  /api/bots/active  /api/bots/{id}/message
//!        /api/bot-groups  /api/bot-groups/{id}/bots
//!        /api/bot-groups/{id}/message  /api/providers/config
//!        /api/providers/test  /api/marketplace/install
//!        /api/marketplace/uninstall
//!        /api/channels/{id}/connect  /api/channels/{id}/disconnect
//!        /api/channels/{id}/test
//!        /api/channels/zalo-personal/login  /api/channels/zalo-personal/logout
//! PUT    /api/bots/{id}
//! DELETE /api/bots/{id}  /api/bot-groups/{id}
//!        /api/bot-groups/{id}/bots/{bot_id}
//! ```

pub mod channels;
pub mod defaults;
pub mod file_providers;
pub mod plugin_registry;
pub mod routes;
pub mod state;

pub use channels::FileChannelRegistry;
pub use file_providers::FileProviderRegistry;
pub use state::{
    channel_credential_hint, epoch_to_iso, normalize_str_list, now_epoch, now_iso, parse_ts_iso,
    split_marketplace_id, AppState, Bot, BotGroup, BotGroupMessage, BotTools, BoxFuture, ChannelConnectInfo,
    ChannelError, ChannelInfo, ChannelRegistry, DagNode, Group, Harness, LogEntry,
    MarketplaceError, NewBot, OllamaStatus, PluginMeta, ProviderConfig, ProviderConfigError,
    ProviderMeta, ProviderRegistry, SessionSummary, Storage, StorageError, ToolInfo, ToolRegistry, TurnError,
    TurnRequest, TurnResult, UpdateInfo, Updater, ZaloLoginError, ZaloPersonal,
    ACTIVITY_FRESH_SEC, CHAT_STRATEGIES, KNOWN_CHANNELS, LOG_MAX, MAX_BODY_BYTES,
};

use std::{
    net::SocketAddr,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};

use axum::{
    body::Bytes,
    extract::FromRequest,
    http::{header, request::Parts, HeaderValue, Method, Request, StatusCode},
    response::{IntoResponse, Response},
    routing::{delete, get, post, put},
    Json, Router,
};
use serde_json::{Map, Value};
use tower_http::cors::{AllowOrigin, CorsLayer};

// ---------------------------------------------------------------------------
// ApiError: every error answers {"error": "..."} with the right status code.
// ---------------------------------------------------------------------------

/// API error: always serializes as `{"error": message}` — the contract shape
/// Python's `_error` / `send_error` produce for every failure.
#[derive(Debug)]
pub struct ApiError {
    status: StatusCode,
    message: String,
}

impl ApiError {
    pub fn new(status: StatusCode, message: impl Into<String>) -> Self {
        Self { status, message: message.into() }
    }

    pub fn bad_request(message: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_REQUEST, message)
    }

    pub fn not_found(message: impl Into<String>) -> Self {
        Self::new(StatusCode::NOT_FOUND, message)
    }

    pub fn status(&self) -> StatusCode {
        self.status
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let body = serde_json::json!({ "error": self.message });
        (self.status, Json(body)).into_response()
    }
}

// ---------------------------------------------------------------------------
// JsonBody: strict JSON-object extractor with the Python error shapes.
// ---------------------------------------------------------------------------

/// Extracts the request body as a JSON object, enforcing Python's
/// `_read_json_body` contract:
/// - body > 1 MiB → 400 `"request body too large"`
/// - empty/whitespace body → `{}`
/// - invalid JSON → 400 `"request body is not valid JSON"`
/// - valid JSON that is not an object → 400 `"request body must be a JSON object"`
pub struct JsonBody(pub Map<String, Value>);

impl<S> FromRequest<S> for JsonBody
where
    S: Send + Sync,
{
    type Rejection = ApiError;

    async fn from_request(req: Request<axum::body::Body>, _state: &S) -> Result<Self, Self::Rejection> {
        let bytes = Bytes::from_request(req, _state)
            .await
            .map_err(|_| ApiError::bad_request("failed to read request body"))?;
        if bytes.len() > MAX_BODY_BYTES {
            return Err(ApiError::bad_request("request body too large"));
        }
        if bytes.iter().all(|b| b.is_ascii_whitespace()) {
            return Ok(JsonBody(Map::new()));
        }
        let value: Value = serde_json::from_slice(&bytes)
            .map_err(|_| ApiError::bad_request("request body is not valid JSON"))?;
        match value {
            Value::Object(map) => Ok(JsonBody(map)),
            _ => Err(ApiError::bad_request("request body must be a JSON object")),
        }
    }
}

// ---------------------------------------------------------------------------
// CORS: any local origin (localhost / 127.0.0.1, any port), mirroring Python.
// ---------------------------------------------------------------------------

fn cors_layer() -> CorsLayer {
    CorsLayer::new()
        .allow_origin(AllowOrigin::predicate(
            |origin: &HeaderValue, _parts: &Parts| {
                let b = origin.as_bytes();
                // Desktop (Tauri): custom protocols + localhost dev servers.
                // The server only binds to 127.0.0.1, so this is safe.
                b.starts_with(b"http://localhost:")
                    || b.starts_with(b"http://127.0.0.1:")
                    || b.starts_with(b"https://tauri.localhost")
                    || b.starts_with(b"tauri://")
                    || b.starts_with(b"https://")
                    || b.starts_with(b"http://")
            },
        ))
        .allow_methods([
            Method::GET,
            Method::POST,
            Method::PUT,
            Method::DELETE,
            Method::OPTIONS,
        ])
        .allow_headers([header::CONTENT_TYPE, header::ACCEPT, header::AUTHORIZATION])
        .max_age(Duration::from_secs(600))
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

/// Unknown routes answer 404 `{"error": "not found"}` (Python's else branch).
async fn not_found() -> ApiError {
    ApiError::not_found("not found")
}

/// Build the full v1 router. Exported so tests and embedders can drive it
/// in-process with a mock [`AppState`].
pub fn router(state: AppState) -> Router {
    use routes::*;

    Router::new()
        // health / version
        .route("/api/health", get(health::health))
        .route("/api/version", get(health::version))
        .route("/api/updates/check", get(misc::updates_check))
        // chat
        .route("/api/chat", post(chat::chat))
        // tools
        .route("/api/tools", get(tools::list_tools))
        .route("/api/tools/run", post(tools::run_tool))
        // skills
        .route("/api/skills", get(misc::list_skills))
        // channels (fully qualified: `channels` also names the sibling
        // `server::channels` module)
        .route("/api/channels", get(routes::channels::list_channels))
        .route(
            "/api/channels/{id}/connect",
            post(routes::channels::connect_channel),
        )
        .route(
            "/api/channels/{id}/disconnect",
            post(routes::channels::disconnect_channel),
        )
        .route(
            "/api/channels/{id}/test",
            post(routes::channels::test_channel),
        )
        .route(
            "/api/channels/{id}/updates",
            get(routes::channels::poll_updates),
        )
        // Messenger Facebook OAuth login flow (browser-based, no token paste).
        .route(
            "/api/channels/messenger/oauth/config",
            get(routes::channels::messenger_oauth_config_status)
                .post(routes::channels::save_messenger_oauth_config),
        )
        .route(
            "/api/channels/messenger/oauth/start",
            get(routes::channels::messenger_oauth_start),
        )
        .route(
            "/api/channels/messenger/oauth/callback",
            get(routes::channels::messenger_oauth_callback),
        )
        // Webhook ingress for platforms without a polling API.
        .route(
            "/api/channels/messenger/webhook",
            get(routes::channels::messenger_webhook_verify)
                .post(routes::channels::messenger_webhook_receive),
        )
        .route(
            "/api/channels/whatsapp/webhook",
            get(routes::channels::whatsapp_webhook_verify)
                .post(routes::channels::whatsapp_webhook_receive),
        )
        .route(
            "/api/channels/line/webhook",
            post(routes::channels::line_webhook_receive),
        )
        .route(
            "/api/channels/zalo/webhook",
            post(routes::channels::zalo_webhook_receive),
        )
        .route(
            "/api/channels/{id}/webhook/verify-token",
            post(routes::channels::set_webhook_verify_token),
        )
        // Generic channel operations: POST /api/channels/{id}/ops/{operation}.
        .route(
            "/api/channels/{id}/ops/{operation}",
            post(routes::channels::channel_operation),
        )
        .route("/api/channels/status", get(misc::channels_status))
        .route(
            "/api/channels/zalo-personal/status",
            get(misc::zalo_status),
        )
        .route(
            "/api/channels/zalo-personal/login",
            post(misc::zalo_login),
        )
        .route(
            "/api/channels/zalo-personal/logout",
            post(misc::zalo_logout),
        )
        // dag
        .route("/api/dag/status", get(misc::dag_status))
        // logs / activity / sessions
        .route("/api/logs", get(misc::logs))
        .route("/api/activity", get(misc::activity))
        .route("/api/sessions", get(misc::sessions))
        // groups
        .route("/api/groups", get(groups::list_groups))
        .route("/api/groups/mute", post(groups::mute))
        .route("/api/groups/policy", post(groups::policy))
        // bots
        .route("/api/bots", get(bots::list_bots).post(bots::create_bot))
        .route(
            "/api/bots/active",
            post(bots::set_active)
                .get(bots::active_not_a_bot_id)
                .put(bots::active_not_a_bot_id)
                .delete(bots::active_not_a_bot_id),
        )
        .route(
            "/api/bots/{id}",
            get(bots::get_bot)
                .put(bots::update_bot)
                .delete(bots::delete_bot),
        )
        .route("/api/bots/{id}/message", post(bots::message_bot))
        // bot groups (multi-bot chat rooms)
        .route(
            "/api/bot-groups",
            get(bot_groups::list_bot_groups).post(bot_groups::create_bot_group),
        )
        .route(
            "/api/bot-groups/{id}",
            get(bot_groups::get_bot_group).delete(bot_groups::delete_bot_group),
        )
        .route(
            "/api/bot-groups/{id}/bots",
            post(bot_groups::add_bot_to_group),
        )
        .route(
            "/api/bot-groups/{id}/bots/{bot_id}",
            delete(bot_groups::remove_bot_from_group),
        )
        .route(
            "/api/bot-groups/{id}/message",
            post(bot_groups::post_group_message),
        )
        .route(
            "/api/bot-groups/{id}/chief",
            put(bot_groups::set_group_chief),
        )
        // providers
        .route("/api/providers", get(providers::list_providers))
        .route("/api/providers/config", post(providers::save_config))
        .route("/api/providers/test", post(providers::test_connection))
        .route(
            "/api/providers/ollama/status",
            get(providers::ollama_status),
        )
        // boom_proxy observability
        .route("/api/proxy/stats", get(proxy::stats))
        .route("/api/proxy/logs", get(proxy::logs))
        // connectors (external service integrations)
        .route("/api/connectors", get(connectors::list))
        .route("/api/connectors/{id}/connect", post(connectors::connect))
        .route("/api/connectors/{id}/token", post(connectors::token))
        .route(
            "/api/connectors/{id}/disconnect",
            post(connectors::disconnect),
        )
        // marketplace
        .route("/api/marketplace", get(marketplace::catalog))
        .route(
            "/api/marketplace/installed",
            get(marketplace::installed),
        )
        .route("/api/marketplace/install", post(marketplace::install))
        .route(
            "/api/marketplace/uninstall",
            post(marketplace::uninstall),
        )
        .fallback(not_found)
        // The default 2 MiB body limit is disabled: JsonBody enforces the
        // Python-compatible 1 MiB cap with the exact {"error"} shape.
        .layer(axum::extract::DefaultBodyLimit::disable())
        .layer(cors_layer())
        .with_state(state)
}

/// Serve the API on 127.0.0.1 (localhost-only, like Python — v1 has no auth).
///
/// Port comes from the `BOOM_API_PORT` env var (default 18080); use
/// [`serve_on_port`] when the port is already known.
pub async fn serve(port: u16, state: AppState) {
    serve_on_port(port, state).await
}

/// Serve on an explicit port.
pub async fn serve_on_port(port: u16, state: AppState) {
    let app = router(state);
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let listener = match tokio::net::TcpListener::bind(addr).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("[boom-core] failed to bind {addr}: {e}");
            return;
        }
    };
    eprintln!("[boom-core] Boom core API listening on http://127.0.0.1:{port}");
    if let Err(e) = axum::serve(listener, app).await {
        eprintln!("[boom-core] server error: {e}");
    }
}

/// Resolve the listen port: `BOOM_API_PORT` env var, default 18080
/// (mirrors Python's `DEFAULT_PORT`).
pub fn resolve_port() -> u16 {
    std::env::var("BOOM_API_PORT")
        .ok()
        .and_then(|v| v.parse::<u16>().ok())
        .unwrap_or(18080)
}

// ---------------------------------------------------------------------------
// In-process embedding (Tauri desktop)
//
// The desktop shell embeds boom-core directly instead of spawning a
// `boom-backend` sidecar binary or a `python3 gateway/api.py` process.
// ---------------------------------------------------------------------------

/// Handle to a boom-core server running as a background tokio task inside the
/// host process.
///
/// Dropping the handle signals graceful shutdown and aborts the task, so the
/// port is released even if [`ServerHandle::shutdown`] was never called
/// (e.g. the host exited through an unusual path).
pub struct ServerHandle {
    shutdown_tx: Option<tokio::sync::oneshot::Sender<()>>,
    task: tokio::task::JoinHandle<()>,
    alive: Arc<AtomicBool>,
}

impl ServerHandle {
    /// True while the background serving task has not exited.
    pub fn is_alive(&self) -> bool {
        self.alive.load(Ordering::SeqCst)
    }

    /// Ask the server to stop gracefully and release the port.
    /// Fire-and-forget: returns immediately; the task winds down in the
    /// background. The `Drop` impl aborts the task if it lingers.
    pub fn shutdown(mut self) {
        if let Some(tx) = self.shutdown_tx.take() {
            let _ = tx.send(());
        }
    }
}

impl Drop for ServerHandle {
    fn drop(&mut self) {
        if let Some(tx) = self.shutdown_tx.take() {
            let _ = tx.send(());
        }
        self.task.abort();
    }
}

/// Bind `127.0.0.1:port` and serve the v1 API in a background tokio task.
///
/// Unlike [`serve`], the bind happens *before* returning: `Ok` means the port
/// is held and the server is accepting connections; `Err` carries the bind
/// failure (e.g. "address already in use") so the embedder can report it.
///
/// Must be called from within a Tokio runtime (e.g. via
/// `tauri::async_runtime::block_on` from a sync context).
pub async fn spawn_server(port: u16, state: AppState) -> Result<ServerHandle, String> {
    let app = router(state);
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .map_err(|e| format!("failed to bind {addr}: {e}"))?;
    let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel::<()>();
    let alive = Arc::new(AtomicBool::new(true));
    let alive_task = Arc::clone(&alive);
    let task = tokio::spawn(async move {
        eprintln!("[boom-core] Boom core API listening on http://127.0.0.1:{port}");
        let result = axum::serve(listener, app)
            .with_graceful_shutdown(async move {
                let _ = shutdown_rx.await;
            })
            .await;
        if let Err(e) = result {
            eprintln!("[boom-core] server error: {e}");
        }
        eprintln!("[boom-core] server on port {port} stopped");
        alive_task.store(false, Ordering::SeqCst);
    });
    Ok(ServerHandle {
        shutdown_tx: Some(shutdown_tx),
        task,
        alive,
    })
}
