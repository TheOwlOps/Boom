//! HTTP server for the Boom Rust backend core.
//!
//! Serves the same JSON API surface as the legacy Python backend so the
//! desktop UI keeps working unchanged. This module is a pure library:
//! the Tauri app (or any binary) starts the server with [`serve`], which
//! reads the port from the `BOOM_API_PORT` environment variable and falls
//! back to `18080`.
//!
//! Routes:
//!
//! - `GET  /api/health`            → liveness probe with version and counts
//! - `GET  /api/providers`         → provider list with `has_key` flags
//! - `POST /api/providers/config`  → store a provider's credentials
//!
//! CORS is fully permissive: the desktop frontend is a local webview that
//! fetches this API cross-origin.

pub mod chat;
pub mod providers;
pub mod tools;

use axum::{
    extract::State,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde_json::json;
use std::fmt;
use std::net::SocketAddr;
use std::sync::Arc;
use tower_http::cors::CorsLayer;

use providers::{ConfigError, ConfigResponse, ProviderConfigRequest, ProviderStore, ProvidersResponse};

/// Backend version reported by `/api/health`; kept in sync with the Tauri
/// package version in `apps/desktop/src-tauri/Cargo.toml`.
pub const VERSION: &str = "0.2.4";

/// Port used when `BOOM_API_PORT` is unset or unparsable.
pub const DEFAULT_PORT: u16 = 18080;

/// Placeholder counts until the tool/skill registries land in the Rust core.
pub const DEFAULT_TOOLS_COUNT: u32 = 0;
pub const DEFAULT_SKILLS_COUNT: u32 = 0;

/// Shared, cloneable state handed to every handler.
#[derive(Clone)]
pub struct AppState {
    store: ProviderStore,
    version: &'static str,
    tools_count: u32,
    skills_count: u32,
}

impl AppState {
    /// Builds state around the given provider store.
    pub fn new(store: ProviderStore) -> Self {
        AppState {
            store,
            version: VERSION,
            tools_count: DEFAULT_TOOLS_COUNT,
            skills_count: DEFAULT_SKILLS_COUNT,
        }
    }

    /// Builds state with explicit tool/skill counts (for future registry wiring).
    pub fn with_counts(store: ProviderStore, tools_count: u32, skills_count: u32) -> Self {
        AppState {
            store,
            version: VERSION,
            tools_count,
            skills_count,
        }
    }
}

/// Errors that can abort server startup.
#[derive(Debug)]
pub enum ServeError {
    /// The provider store's default path could not be resolved.
    Config(ConfigError),
    /// Binding or serving the TCP listener failed.
    Io(std::io::Error),
}

impl fmt::Display for ServeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ServeError::Config(e) => write!(f, "provider store setup failed: {e}"),
            ServeError::Io(e) => write!(f, "HTTP server error: {e}"),
        }
    }
}

impl std::error::Error for ServeError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            ServeError::Config(e) => Some(e),
            ServeError::Io(e) => Some(e),
        }
    }
}

/// Renders a [`ConfigError`] as a JSON error response with the matching
/// HTTP status (`400` for bad requests, `500` for server-side failures).
impl IntoResponse for ConfigError {
    fn into_response(self) -> Response {
        let status = self.status_code();
        (status, Json(json!({ "error": self.to_string() }))).into_response()
    }
}

/// `GET /api/health` — liveness probe.
async fn health(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    Json(json!({
        "status": "ok",
        "version": state.version,
        "tools_count": state.tools_count,
        "skills_count": state.skills_count,
    }))
}

/// `GET /api/providers` — provider list with `has_key` flags (keys never exposed).
async fn list_providers(
    State(state): State<Arc<AppState>>,
) -> Result<Json<ProvidersResponse>, ConfigError> {
    state.store.list().map(Json)
}

/// `POST /api/providers/config` — stores a provider's credentials.
async fn configure_provider(
    State(state): State<Arc<AppState>>,
    Json(req): Json<ProviderConfigRequest>,
) -> Result<Json<ConfigResponse>, ConfigError> {
    state.store.set_config(req).map(Json)
}

/// `GET /api/tools` — list of built-in tools.
async fn list_tools() -> impl IntoResponse {
    Json(tools::list_tools())
}

/// `POST /api/tools/run` — run a whitelisted tool.
async fn run_tool(Json(req): Json<tools::RunToolRequest>) -> impl IntoResponse {
    Json(tools::run_tool(&req.tool, &req.args))
}

/// `GET /api/marketplace` — static marketplace catalog.
async fn list_marketplace() -> impl IntoResponse {
    Json(tools::list_marketplace())
}

/// Builds the axum router with permissive CORS for the desktop webview.
///
/// The chat sub-router (`chat::router()`) is self-contained with its own
/// state, so it is merged at the `Router<()>` level after the core routes
/// have had their state provided.
pub fn router(state: AppState) -> Router {
    let core = Router::new()
        .route("/api/health", get(health))
        .route("/api/providers", get(list_providers))
        .route("/api/providers/config", post(configure_provider))
        .route("/api/tools", get(list_tools))
        .route("/api/tools/run", post(run_tool))
        .route("/api/marketplace", get(list_marketplace))
        .with_state(Arc::new(state));
    core
        .merge(chat::router())
        // The desktop app is a local webview fetching cross-origin; allow all origins.
        .layer(CorsLayer::permissive())
}

/// Resolves the listen port: `BOOM_API_PORT`, or [`DEFAULT_PORT`] when the
/// variable is unset or does not parse as a port number.
pub fn port_from_env() -> u16 {
    std::env::var("BOOM_API_PORT")
        .ok()
        .and_then(|raw| raw.trim().parse::<u16>().ok())
        .unwrap_or(DEFAULT_PORT)
}

/// Binds `127.0.0.1:<port>` and serves the API until the process exits.
///
/// Typical Tauri integration:
///
/// ```ignore
/// tauri::async_runtime::spawn(async {
///     if let Err(e) = boom_desktop_lib::backend::serve().await {
///         eprintln!("backend failed: {e}");
///     }
/// });
/// ```
pub async fn serve() -> Result<(), ServeError> {
    let store = ProviderStore::new(ProviderStore::default_path().map_err(ServeError::Config)?);
    let app = router(AppState::new(store));
    let port = port_from_env();
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .map_err(ServeError::Io)?;
    eprintln!("boom backend listening on http://{addr}");
    axum::serve(listener, app).await.map_err(ServeError::Io)
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::{to_bytes, Body};
    use axum::http::{header, Method, Request, StatusCode};
    use std::sync::atomic::{AtomicU64, Ordering};
    use tower::ServiceExt;

    static COUNTER: AtomicU64 = AtomicU64::new(0);

    /// A router backed by a store in a fresh, unique temp directory per test.
    fn test_router(test_name: &str) -> Router {
        let n = COUNTER.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!(
            "boom-backend-http-test-{}-{}-{}",
            test_name,
            std::process::id(),
            n
        ));
        let store = ProviderStore::new(dir.join("providers.json"));
        router(AppState::new(store))
    }

    async fn response_json(res: Response) -> serde_json::Value {
        let bytes = to_bytes(res.into_body(), 1024 * 1024)
            .await
            .expect("response body must be readable");
        serde_json::from_slice(&bytes).expect("response body must be JSON")
    }

    #[tokio::test]
    async fn health_returns_ok_with_version() {
        let res = test_router("health")
            .oneshot(Request::builder().uri("/api/health").body(Body::empty()).unwrap())
            .await
            .unwrap();

        assert_eq!(res.status(), StatusCode::OK);
        let body = response_json(res).await;
        assert_eq!(body["status"], "ok");
        assert_eq!(body["version"], "0.2.4");
        assert_eq!(body["tools_count"], 0);
        assert_eq!(body["skills_count"], 0);
    }

    #[tokio::test]
    async fn providers_get_lists_seven_defaults() {
        let res = test_router("providers_get")
            .oneshot(
                Request::builder()
                    .uri("/api/providers")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(res.status(), StatusCode::OK);
        let body = response_json(res).await;
        let providers = body["providers"].as_array().expect("providers must be an array");
        assert_eq!(providers.len(), 7);
        assert_eq!(providers[0]["id"], "openai");
        assert_eq!(providers[0]["name"], "OpenAI");
        assert_eq!(providers[0]["has_key"], false);
        assert_eq!(body["active"], "openai");
    }

    #[tokio::test]
    async fn providers_config_post_persists_without_echoing_key() {
        let app = test_router("providers_post");

        let payload = json!({
            "provider": "openai",
            "api_key": "sk-integration-secret",
            "model": "gpt-4o",
            "base_url": "https://api.openai.com/v1",
        });
        let res = app
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/api/providers/config")
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from(payload.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(res.status(), StatusCode::OK);
        let body = response_json(res).await;
        assert_eq!(body["ok"], true);
        assert_eq!(body["has_key"], true);
        // The raw response must not contain the API key anywhere.
        assert!(
            !body.to_string().contains("sk-integration-secret"),
            "API key leaked in POST response"
        );
    }

    #[tokio::test]
    async fn providers_config_post_then_get_shows_has_key() {
        // A second router sharing the same temp dir verifies persistence.
        let dir = {
            let n = COUNTER.fetch_add(1, Ordering::SeqCst);
            std::env::temp_dir().join(format!(
                "boom-backend-http-test-persist-{}-{}",
                std::process::id(),
                n
            ))
        };
        let make_router = || {
            router(AppState::new(ProviderStore::new(
                dir.join("providers.json"),
            )))
        };

        let payload = json!({ "provider": "xai", "api_key": "xai-secret" });
        let res = make_router()
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/api/providers/config")
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from(payload.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);

        let res = make_router()
            .oneshot(
                Request::builder()
                    .uri("/api/providers")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = response_json(res).await;
        let xai = body["providers"]
            .as_array()
            .unwrap()
            .iter()
            .find(|p| p["id"] == "xai")
            .expect("xai must be listed");
        assert_eq!(xai["has_key"], true);
        assert!(
            !body.to_string().contains("xai-secret"),
            "API key leaked in GET response"
        );
    }

    #[tokio::test]
    async fn providers_config_post_empty_provider_is_400() {
        let payload = json!({ "provider": "", "api_key": "x" });
        let res = test_router("providers_400")
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/api/providers/config")
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from(payload.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(res.status(), StatusCode::BAD_REQUEST);
        let body = response_json(res).await;
        assert!(body["error"].is_string());
    }

    #[tokio::test]
    async fn providers_config_post_invalid_json_is_422() {
        let res = test_router("providers_422")
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/api/providers/config")
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from("{ not json"))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(res.status(), StatusCode::UNPROCESSABLE_ENTITY);
    }

    #[tokio::test]
    async fn cors_allows_any_origin() {
        let res = test_router("cors")
            .oneshot(
                Request::builder()
                    .method(Method::OPTIONS)
                    .uri("/api/health")
                    .header(header::ORIGIN, "http://localhost:5173")
                    .header(header::ACCESS_CONTROL_REQUEST_METHOD, "GET")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(
            res.headers()
                .get(header::ACCESS_CONTROL_ALLOW_ORIGIN)
                .and_then(|v| v.to_str().ok()),
            Some("*")
        );
    }

    #[test]
    fn port_from_env_defaults_and_parses() {
        std::env::remove_var("BOOM_API_PORT");
        assert_eq!(port_from_env(), DEFAULT_PORT);

        std::env::set_var("BOOM_API_PORT", "19090");
        assert_eq!(port_from_env(), 19090);

        std::env::set_var("BOOM_API_PORT", "not-a-port");
        assert_eq!(port_from_env(), DEFAULT_PORT);

        std::env::remove_var("BOOM_API_PORT");
    }
}
