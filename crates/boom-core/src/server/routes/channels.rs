//! Channel routes: real Telegram/Discord/Messenger connect/disconnect/test
//! plus ingress polling, and an honest inventory. Platforms this build
//! does not implement answer `unsupported` — never "not wired".

use axum::{
    extract::{Path, Query, State},
    http::{header, HeaderMap, StatusCode},
    response::{Html, IntoResponse},
    Json,
};
use serde::Deserialize;
use serde_json::{json, Value};

/// Read the request's Host header (`localhost:18080`) so the OAuth
/// redirect URI always points back at this server.
fn request_host(headers: &HeaderMap) -> String {
    headers
        .get(header::HOST)
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty())
        .unwrap_or("localhost:18080")
        .to_string()
}

use super::super::{
    state::{AppState, ChannelError},
    ApiError, JsonBody,
};

fn channel_error(e: ChannelError) -> ApiError {
    let status = axum::http::StatusCode::from_u16(e.status)
        .unwrap_or(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
    ApiError::new(status, e.message)
}

/// `GET /api/channels` →
/// `{channels: [{id, name, status, supported, detail}], count}`.
///
/// `status` is one of `connected`, `not-configured`, `unsupported`
/// (Telegram additionally reports its bot username in `detail` when
/// connected).
pub async fn list_channels(State(state): State<AppState>) -> Json<Value> {
    let channels: Vec<Value> = state
        .channels
        .channel_ids()
        .iter()
        .map(|id| {
            let info = state.channels.info(id);
            json!({
                "id": info.id,
                "name": info.name,
                "status": info.status,
                "supported": info.supported,
                "detail": info.detail,
            })
        })
        .collect();
    Json(json!({ "channels": channels, "count": channels.len() }))
}

/// `POST /api/channels/{id}/connect` — body `{token}` →
/// 200 `{ok: true, id, bot_username, message}`.
///
/// The token is verified live against the platform API *before* it is
/// persisted; a rejected token is never saved. Missing token → 400,
/// unknown channel → 404, unsupported channel → 400, rejected token →
/// 502. The token never appears in the response.
pub async fn connect_channel(
    State(state): State<AppState>,
    Path(id): Path<String>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    let token = match body.get("token").and_then(|v| v.as_str()) {
        Some(t) if !t.trim().is_empty() => t.trim().to_string(),
        _ => return Err(ApiError::bad_request("token is required")),
    };
    // Drop the cleartext as soon as the registry call returns; the
    // registry itself never echoes it back.
    let result = state.channels.connect(&id, &token).await;
    drop(token);
    match result {
        Ok(info) => Ok(Json(json!({
            "ok": true,
            "id": id,
            "bot_username": info.bot_username,
            "message": info.message,
        }))),
        Err(e) => Err(channel_error(e)),
    }
}

/// `POST /api/channels/{id}/disconnect` → 200 `{ok: true, id}`.
/// Idempotent: disconnecting an unconfigured channel still succeeds.
/// Unknown channel → 404, unsupported → 400.
pub async fn disconnect_channel(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match state.channels.disconnect(&id) {
        Ok(()) => Ok(Json(json!({ "ok": true, "id": id }))),
        Err(e) => Err(channel_error(e)),
    }
}

/// `POST /api/channels/{id}/test` — body `{chat_id?, message?}` →
/// 200 `{ok: true, id, message}`.
///
/// With `chat_id` the channel sends a real test message through the
/// platform API; without it the stored credential is re-verified.
/// Unconnected channel → 400, rejected credential → 502.
pub async fn test_channel(
    State(state): State<AppState>,
    Path(id): Path<String>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    let chat_id = body
        .get("chat_id")
        .and_then(|v| v.as_str())
        .filter(|s| !s.trim().is_empty())
        .map(|s| s.trim().to_string());
    let message = body
        .get("message")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string());
    match state
        .channels
        .test(&id, chat_id.as_deref(), message.as_deref())
        .await
    {
        Ok(msg) => Ok(Json(json!({ "ok": true, "id": id, "message": msg }))),
        Err(e) => Err(channel_error(e)),
    }
}

/// Query params for `GET /api/channels/{id}/updates`.
#[derive(Debug, Deserialize)]
pub struct UpdatesQuery {    /// Optional chat filter: Telegram chat id, Discord channel id,
    /// Messenger PSID.
    pub chat_id: Option<String>,
    /// Cursor: last seen update/message id (exclusive).
    pub after: Option<String>,
}

/// `GET /api/channels/{id}/updates?chat_id&after` → 200
/// `{ok: true, id, messages: [{id, chat_id, sender, text, ts}], count}`.
///
/// Polls the platform for inbound messages (ingress):
/// - Telegram: `getUpdates` (short poll, `after` = last `update_id`).
/// - Discord: recent channel messages (`chat_id` = channel id required,
///   `after` = last message id).
/// - Messenger: 501 — no polling API exists; ingress needs a webhook
///   (see `docs/channels.md`).
///
/// Unconnected channel → 400, unknown → 404, unsupported → 400.
pub async fn poll_updates(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Query(q): Query<UpdatesQuery>,
) -> Result<Json<Value>, ApiError> {
    match state
        .channels
        .poll_updates(&id, q.chat_id.as_deref(), q.after.as_deref())
        .await
    {
        Ok(messages) => Ok(Json(json!({
            "ok": true,
            "id": id,
            "messages": messages,
            "count": messages.len(),
        }))),
        Err(e) => Err(channel_error(e)),
    }
}

// ---------------------------------------------------------------------------
// Messenger Facebook OAuth login flow
//
// The user clicks "Connect with Facebook" in the UI: the backend mints a
// login URL, the system browser opens Facebook, and Facebook redirects
// back to this server's own callback URL (localhost). The callback
// exchanges the code, picks the first managed Page, and persists its Page
// token — the user never pastes a token by hand.
// ---------------------------------------------------------------------------

/// `GET /api/channels/messenger/oauth/config` →
/// `{ok: true, configured: bool, app_id: string|null}`.
///
/// `app_id` is a public identifier (not a secret); the app secret is
/// never returned.
pub async fn messenger_oauth_config_status(State(state): State<AppState>) -> Json<Value> {
    match state.channels.messenger_oauth_config() {
        Some(cfg) => Json(json!({ "ok": true, "configured": true, "app_id": cfg.app_id })),
        None => Json(json!({ "ok": true, "configured": false, "app_id": Value::Null })),
    }
}

/// Body for `POST /api/channels/messenger/oauth/config`.
#[derive(Debug, Deserialize)]
pub struct MessengerOAuthConfigBody {
    #[serde(default)]
    pub app_id: String,
    #[serde(default)]
    pub app_secret: String,
}

/// `POST /api/channels/messenger/oauth/config` — body
/// `{app_id, app_secret}` → `{ok: true}`.
///
/// Stores the Facebook App credentials (0600). Get them at
/// https://developers.facebook.com → create an app → add the
/// "Facebook Login" product. Empty values → 400.
pub async fn save_messenger_oauth_config(
    State(state): State<AppState>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    let req: MessengerOAuthConfigBody = serde_json::from_value(Value::Object(body))
        .map_err(|_| ApiError::bad_request("body must be {app_id, app_secret}"))?;
    match state
        .channels
        .save_messenger_oauth_config(req.app_id.trim(), req.app_secret.trim())
    {
        Ok(()) => Ok(Json(json!({ "ok": true }))),
        Err(e) => Err(channel_error(e)),
    }
}

/// `GET /api/channels/messenger/oauth/start` →
/// `{ok: true, url}`.
///
/// Builds the Facebook Login URL with this server as the redirect target
/// (from the request Host, so it always matches the running server).
/// Unconfigured app → 400 with setup guidance.
pub async fn messenger_oauth_start(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let redirect_uri = format!(
        "http://{}/api/channels/messenger/oauth/callback",
        request_host(&headers)
    );
    match state.channels.messenger_oauth_start(&redirect_uri) {
        Ok(url) => Ok(Json(json!({ "ok": true, "url": url }))),
        Err(e) => Err(channel_error(e)),
    }
}

/// Query params for the Facebook redirect.
#[derive(Debug, Deserialize)]
pub struct MessengerOAuthCallbackQuery {
    #[serde(default)]
    pub code: Option<String>,
    #[serde(default)]
    pub state: Option<String>,
    #[serde(default)]
    pub error: Option<String>,
    #[serde(default)]
    pub error_description: Option<String>,
}

/// Render the post-login landing page shown in the user's browser tab.
fn oauth_landing_html(ok: bool, title: &str, message: &str) -> String {
    // Minimal inline page: no external assets, works in any browser.
    let color = if ok { "#16a34a" } else { "#dc2626" };
    let icon = if ok { "✓" } else { "✕" };
    format!(
        "<!DOCTYPE html><html lang=\"vi\"><head><meta charset=\"utf-8\">\
         <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\
         <title>{title}</title>\
         <style>body{{font-family:system-ui,sans-serif;background:#1a1a1a;color:#f4f4f5;\
         display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}\
         .card{{text-align:center;max-width:420px;padding:32px}}\
         .icon{{font-size:48px;color:{color}}}\
         h1{{font-size:20px;margin:16px 0 8px}}p{{color:#a1a1aa;font-size:14px;line-height:1.6}}</style>\
         </head><body><div class=\"card\"><div class=\"icon\">{icon}</div>\
         <h1>{title}</h1><p>{message}</p>\
         <p>Bạn có thể đóng tab này và quay lại Boom.</p>\
         </div></body></html>"
    )
}

/// `GET /api/channels/messenger/oauth/callback` — Facebook redirects here.
///
/// Always answers with a small HTML page (this URL loads in the user's
/// browser, not in the app): success → 200, user denied → 400, bad
/// state/code or upstream failure → 4xx/5xx. Nothing secret is rendered.
pub async fn messenger_oauth_callback(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(q): Query<MessengerOAuthCallbackQuery>,
) -> impl IntoResponse {
    // The user denied the login on Facebook's side.
    if let Some(err) = q.error.as_deref().filter(|s| !s.is_empty()) {
        let detail = q
            .error_description
            .as_deref()
            .filter(|s| !s.is_empty())
            .unwrap_or("bạn đã từ chối cấp quyền");
        return (
            StatusCode::BAD_REQUEST,
            Html(oauth_landing_html(
                false,
                "Đăng nhập bị hủy",
                &format!("Facebook báo lỗi: {err} — {detail}"),
            )),
        );
    }
    let (Some(code), Some(cb_state)) = (q.code.as_deref(), q.state.as_deref()) else {
        return (
            StatusCode::BAD_REQUEST,
            Html(oauth_landing_html(
                false,
                "Thiếu thông tin",
                "Facebook không trả về mã xác thực — hãy bấm Connect lại trong Boom.",
            )),
        );
    };
    let redirect_uri = format!(
        "http://{}/api/channels/messenger/oauth/callback",
        request_host(&headers)
    );
    match state
        .channels
        .messenger_oauth_callback(code, cb_state, &redirect_uri)
        .await
    {
        Ok(page_name) => (
            StatusCode::OK,
            Html(oauth_landing_html(
                true,
                "Đã kết nối Messenger",
                &format!("Page “{page_name}” đã được liên kết với Boom."),
            )),
        ),
        Err(e) => {
            let status =
                StatusCode::from_u16(e.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
            (
                status,
                Html(oauth_landing_html(false, "Kết nối thất bại", &e.message)),
            )
        }
    }
}
