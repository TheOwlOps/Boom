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

// ---------------------------------------------------------------------------
// Webhook ingress: Messenger / WhatsApp / LINE / Zalo
//
// Platforms without a polling API deliver inbound messages to a webhook
// URL the user registers on their developer console. Boom exposes one
// endpoint per platform; verified events are parsed into InboundMessage
// and pushed to the registry's webhook queue, which
// `GET /api/channels/{id}/updates` drains.
// ---------------------------------------------------------------------------

/// Query params for Meta webhook verification (`hub.mode`,
/// `hub.verify_token`, `hub.challenge`).
#[derive(Debug, Deserialize)]
pub struct WebhookVerifyQuery {
    #[serde(default, rename = "hub.mode")]
    pub hub_mode: Option<String>,
    #[serde(default, rename = "hub.verify_token")]
    pub hub_verify_token: Option<String>,
    #[serde(default, rename = "hub.challenge")]
    pub hub_challenge: Option<String>,
}

/// Shared Meta (Messenger/WhatsApp) webhook verification: Facebook sends
/// `GET ?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...`.
/// We echo the challenge only when the verify token matches the one the
/// user configured (via `POST /api/channels/{id}/webhook/verify-token`).
fn verify_meta_webhook(
    registry: &dyn super::super::state::ChannelRegistry,
    id: &str,
    q: &WebhookVerifyQuery,
) -> Result<String, ApiError> {
    let mode_ok = q.hub_mode.as_deref() == Some("subscribe");
    let challenge = q.hub_challenge.as_deref().filter(|s| !s.is_empty());
    let provided = q.hub_verify_token.as_deref().unwrap_or("");
    let expected = registry.webhook_verify_token(id);
    match (mode_ok, challenge, expected) {
        (true, Some(c), Some(exp)) if provided == exp => Ok(c.to_string()),
        (true, Some(_), Some(_)) => Err(ApiError::new(
            StatusCode::FORBIDDEN,
            "webhook verify_token mismatch",
        )),
        (true, Some(_), None) => Err(ApiError::bad_request(
            "webhook verify token chưa được cấu hình — POST /api/channels/{id}/webhook/verify-token trước",
        )),
        _ => Err(ApiError::bad_request("invalid webhook verification request")),
    }
}

/// `GET /api/channels/messenger/webhook` — Meta verification handshake.
pub async fn messenger_webhook_verify(
    State(state): State<AppState>,
    Query(q): Query<WebhookVerifyQuery>,
) -> Result<String, ApiError> {
    verify_meta_webhook(state.channels.as_ref(), "messenger", &q)
}

/// `POST /api/channels/messenger/webhook` — receive Messenger events.
pub async fn messenger_webhook_receive(
    State(state): State<AppState>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    // Only accept events for a connected channel.
    if state.channels.info("messenger").status != "connected" {
        return Err(ApiError::bad_request("messenger chưa được kết nối"));
    }
    let msgs = super::super::channels::messenger::parse_webhook(&Value::Object(body));
    let count = msgs.len();
    state.channels.push_webhook_messages("messenger", msgs);
    Ok(Json(json!({ "ok": true, "received": count })))
}

/// `GET /api/channels/whatsapp/webhook` — Meta verification handshake
/// (same scheme as Messenger).
pub async fn whatsapp_webhook_verify(
    State(state): State<AppState>,
    Query(q): Query<WebhookVerifyQuery>,
) -> Result<String, ApiError> {
    verify_meta_webhook(state.channels.as_ref(), "whatsapp", &q)
}

/// `POST /api/channels/whatsapp/webhook` — receive WhatsApp events.
pub async fn whatsapp_webhook_receive(
    State(state): State<AppState>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    if state.channels.info("whatsapp").status != "connected" {
        return Err(ApiError::bad_request("whatsapp chưa được kết nối"));
    }
    let msgs = super::super::channels::whatsapp::parse_webhook(&Value::Object(body));
    let count = msgs.len();
    state.channels.push_webhook_messages("whatsapp", msgs);
    Ok(Json(json!({ "ok": true, "received": count })))
}

/// `POST /api/channels/line/webhook` — receive LINE events.
/// (Signature verification with the channel secret is recommended in
/// production; this endpoint parses the body and queues text messages.)
pub async fn line_webhook_receive(
    State(state): State<AppState>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    if state.channels.info("line").status != "connected" {
        return Err(ApiError::bad_request("line chưa được kết nối"));
    }
    let msgs = super::super::channels::line::parse_webhook(&Value::Object(body));
    let count = msgs.len();
    state.channels.push_webhook_messages("line", msgs);
    // LINE expects a fast 200; the body is ignored.
    Ok(Json(json!({ "ok": true, "received": count })))
}

/// `POST /api/channels/zalo/webhook` — receive Zalo OA events.
pub async fn zalo_webhook_receive(
    State(state): State<AppState>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    if state.channels.info("zalo").status != "connected" {
        return Err(ApiError::bad_request("zalo chưa được kết nối"));
    }
    let msgs = super::super::channels::zalo::parse_webhook(&Value::Object(body));
    let count = msgs.len();
    state.channels.push_webhook_messages("zalo", msgs);
    Ok(Json(json!({ "ok": true, "received": count })))
}

/// `POST /api/channels/{id}/webhook/verify-token` — body `{token}` →
/// `{ok: true}`. Stores the verify token used to validate the Meta
/// webhook handshake (in-memory only, never persisted).
pub async fn set_webhook_verify_token(
    State(state): State<AppState>,
    Path(id): Path<String>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    let token = body
        .get("token")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    if !["messenger", "whatsapp"].contains(&id.as_str()) {
        return Err(ApiError::bad_request(
            "verify token chỉ dùng cho messenger/whatsapp",
        ));
    }
    state.channels.set_webhook_verify_token(&id, &token);
    Ok(Json(json!({ "ok": true, "id": id })))
}

// ---------------------------------------------------------------------------
// Generic channel operations: POST /api/channels/{id}/{operation}
//
// Each operation maps to a real platform API call. Body carries the
// operation's parameters as JSON. The channel must be connected.
// ---------------------------------------------------------------------------

/// `POST /api/channels/{id}/{operation}` — body varies per operation →
/// 200 `{ok: true, ...result}`.
///
/// Supported operations:
/// - telegram: send_photo, send_document, send_sticker, edit_message,
///   delete_message, get_chat_info, get_chat_members, pin_message,
///   unpin_message
/// - discord: send_embed, edit_message, delete_message, add_reaction,
///   remove_reaction, get_channel_info, get_guild_info, list_members
/// - messenger: send_image, send_quick_replies, send_buttons,
///   get_user_profile, set_typing
/// - slack: send_blocks, update_message, delete_message, list_channels,
///   get_user_info
/// - whatsapp: send_text
/// - line: send_text
/// - zalo: send_text
pub async fn channel_operation(
    State(state): State<AppState>,
    Path((id, operation)): Path<(String, String)>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    use super::super::channels as ch;

    let token = state
        .channels
        .channel_token(&id)
        .map_err(channel_error)?;
    let api = |channel: &str| state.channels.channel_api_base(channel);
    let req_str = |key: &str| {
        body.get(key)
            .and_then(|v| v.as_str())
            .filter(|s| !s.trim().is_empty())
            .map(|s| s.trim().to_string())
    };
    let req_i64 = |key: &str| body.get(key).and_then(Value::as_i64);
    let op_err = |e: String| channel_error(ChannelError::upstream(e));

    let result: Value = match (id.as_str(), operation.as_str()) {
        // ---------------- Telegram ----------------
        ("telegram", "send_photo") => {
            let chat = req_str("chat_id").ok_or_else(|| ApiError::bad_request("chat_id is required"))?;
            let photo = req_str("photo").ok_or_else(|| ApiError::bad_request("photo is required"))?;
            let caption = req_str("caption");
            let mid = ch::telegram::send_photo(&api("telegram"), &token, &chat, &photo, caption.as_deref())
                .await
                .map_err(op_err)?;
            json!({ "message_id": mid })
        }
        ("telegram", "send_document") => {
            let chat = req_str("chat_id").ok_or_else(|| ApiError::bad_request("chat_id is required"))?;
            let doc = req_str("document").ok_or_else(|| ApiError::bad_request("document is required"))?;
            let caption = req_str("caption");
            let mid = ch::telegram::send_document(&api("telegram"), &token, &chat, &doc, caption.as_deref())
                .await
                .map_err(op_err)?;
            json!({ "message_id": mid })
        }
        ("telegram", "send_sticker") => {
            let chat = req_str("chat_id").ok_or_else(|| ApiError::bad_request("chat_id is required"))?;
            let sticker = req_str("sticker").ok_or_else(|| ApiError::bad_request("sticker is required"))?;
            let mid = ch::telegram::send_sticker(&api("telegram"), &token, &chat, &sticker)
                .await
                .map_err(op_err)?;
            json!({ "message_id": mid })
        }
        ("telegram", "edit_message") => {
            let chat = req_str("chat_id").ok_or_else(|| ApiError::bad_request("chat_id is required"))?;
            let mid = req_i64("message_id").ok_or_else(|| ApiError::bad_request("message_id is required"))?;
            let text = req_str("text").ok_or_else(|| ApiError::bad_request("text is required"))?;
            let new_mid = ch::telegram::edit_message(&api("telegram"), &token, &chat, mid, &text)
                .await
                .map_err(op_err)?;
            json!({ "message_id": new_mid })
        }
        ("telegram", "delete_message") => {
            let chat = req_str("chat_id").ok_or_else(|| ApiError::bad_request("chat_id is required"))?;
            let mid = req_i64("message_id").ok_or_else(|| ApiError::bad_request("message_id is required"))?;
            ch::telegram::delete_message(&api("telegram"), &token, &chat, mid)
                .await
                .map_err(op_err)?;
            json!({ "deleted": true })
        }
        ("telegram", "get_chat_info") => {
            let chat = req_str("chat_id").ok_or_else(|| ApiError::bad_request("chat_id is required"))?;
            let info = ch::telegram::get_chat_info(&api("telegram"), &token, &chat)
                .await
                .map_err(op_err)?;
            json!({ "id": info.id, "title": info.title, "type": info.kind })
        }
        ("telegram", "get_chat_members") => {
            let chat = req_str("chat_id").ok_or_else(|| ApiError::bad_request("chat_id is required"))?;
            let members = ch::telegram::get_chat_members(&api("telegram"), &token, &chat)
                .await
                .map_err(op_err)?;
            json!({ "members": members.iter().map(|m| json!({
                "user_id": m.user_id, "username": m.username,
                "first_name": m.first_name, "status": m.status,
            })).collect::<Vec<_>>() })
        }
        ("telegram", "pin_message") => {
            let chat = req_str("chat_id").ok_or_else(|| ApiError::bad_request("chat_id is required"))?;
            let mid = req_i64("message_id").ok_or_else(|| ApiError::bad_request("message_id is required"))?;
            ch::telegram::pin_message(&api("telegram"), &token, &chat, mid)
                .await
                .map_err(op_err)?;
            json!({ "pinned": true })
        }
        ("telegram", "unpin_message") => {
            let chat = req_str("chat_id").ok_or_else(|| ApiError::bad_request("chat_id is required"))?;
            let mid = req_i64("message_id");
            ch::telegram::unpin_message(&api("telegram"), &token, &chat, mid)
                .await
                .map_err(op_err)?;
            json!({ "unpinned": true })
        }
        // ---------------- Discord ----------------
        ("discord", "send_embed") => {
            let channel = req_str("channel_id").ok_or_else(|| ApiError::bad_request("channel_id is required"))?;
            let title = req_str("title").unwrap_or_default();
            let desc = req_str("description").unwrap_or_default();
            let color = req_i64("color");
            let mid = ch::discord::send_embed(&api("discord"), &token, &channel, &title, &desc, color)
                .await
                .map_err(op_err)?;
            json!({ "message_id": mid })
        }
        ("discord", "edit_message") => {
            let channel = req_str("channel_id").ok_or_else(|| ApiError::bad_request("channel_id is required"))?;
            let mid = req_str("message_id").ok_or_else(|| ApiError::bad_request("message_id is required"))?;
            let text = req_str("text").ok_or_else(|| ApiError::bad_request("text is required"))?;
            let new_mid = ch::discord::edit_message(&api("discord"), &token, &channel, &mid, &text)
                .await
                .map_err(op_err)?;
            json!({ "message_id": new_mid })
        }
        ("discord", "delete_message") => {
            let channel = req_str("channel_id").ok_or_else(|| ApiError::bad_request("channel_id is required"))?;
            let mid = req_str("message_id").ok_or_else(|| ApiError::bad_request("message_id is required"))?;
            ch::discord::delete_message(&api("discord"), &token, &channel, &mid)
                .await
                .map_err(op_err)?;
            json!({ "deleted": true })
        }
        ("discord", "add_reaction") => {
            let channel = req_str("channel_id").ok_or_else(|| ApiError::bad_request("channel_id is required"))?;
            let mid = req_str("message_id").ok_or_else(|| ApiError::bad_request("message_id is required"))?;
            let emoji = req_str("emoji").ok_or_else(|| ApiError::bad_request("emoji is required"))?;
            ch::discord::add_reaction(&api("discord"), &token, &channel, &mid, &emoji)
                .await
                .map_err(op_err)?;
            json!({ "reacted": true })
        }
        ("discord", "remove_reaction") => {
            let channel = req_str("channel_id").ok_or_else(|| ApiError::bad_request("channel_id is required"))?;
            let mid = req_str("message_id").ok_or_else(|| ApiError::bad_request("message_id is required"))?;
            let emoji = req_str("emoji").ok_or_else(|| ApiError::bad_request("emoji is required"))?;
            ch::discord::remove_reaction(&api("discord"), &token, &channel, &mid, &emoji)
                .await
                .map_err(op_err)?;
            json!({ "removed": true })
        }
        ("discord", "get_channel_info") => {
            let channel = req_str("channel_id").ok_or_else(|| ApiError::bad_request("channel_id is required"))?;
            let info = ch::discord::get_channel_info(&api("discord"), &token, &channel)
                .await
                .map_err(op_err)?;
            json!({ "id": info.id, "name": info.name, "type": info.kind, "guild_id": info.guild_id })
        }
        ("discord", "get_guild_info") => {
            let guild = req_str("guild_id").ok_or_else(|| ApiError::bad_request("guild_id is required"))?;
            let info = ch::discord::get_guild_info(&api("discord"), &token, &guild)
                .await
                .map_err(op_err)?;
            json!({ "id": info.id, "name": info.name, "member_count": info.member_count })
        }
        ("discord", "list_members") => {
            let guild = req_str("guild_id").ok_or_else(|| ApiError::bad_request("guild_id is required"))?;
            let members = ch::discord::list_members(&api("discord"), &token, &guild)
                .await
                .map_err(op_err)?;
            json!({ "members": members.iter().map(|m| json!({
                "user_id": m.user_id, "username": m.username, "nick": m.nick,
            })).collect::<Vec<_>>() })
        }
        // ---------------- Messenger ----------------
        ("messenger", "send_image") => {
            let psid = req_str("psid").ok_or_else(|| ApiError::bad_request("psid is required"))?;
            let url = req_str("image_url").ok_or_else(|| ApiError::bad_request("image_url is required"))?;
            let mid = ch::messenger::send_image(&api("messenger"), &token, &psid, &url)
                .await
                .map_err(op_err)?;
            json!({ "message_id": mid })
        }
        ("messenger", "send_quick_replies") => {
            let psid = req_str("psid").ok_or_else(|| ApiError::bad_request("psid is required"))?;
            let text = req_str("text").ok_or_else(|| ApiError::bad_request("text is required"))?;
            let buttons: Vec<(String, String)> = body
                .get("buttons")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(|b| {
                            let t = b.get("title")?.as_str()?.to_string();
                            let p = b.get("payload")?.as_str()?.to_string();
                            Some((t, p))
                        })
                        .collect()
                })
                .unwrap_or_default();
            if buttons.is_empty() {
                return Err(ApiError::bad_request("buttons (array of {title, payload}) is required"));
            }
            let mid = ch::messenger::send_quick_replies(&api("messenger"), &token, &psid, &text, &buttons)
                .await
                .map_err(op_err)?;
            json!({ "message_id": mid })
        }
        ("messenger", "send_buttons") => {
            let psid = req_str("psid").ok_or_else(|| ApiError::bad_request("psid is required"))?;
            let title = req_str("title").ok_or_else(|| ApiError::bad_request("title is required"))?;
            let buttons: Vec<(String, String)> = body
                .get("buttons")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(|b| {
                            let t = b.get("title")?.as_str()?.to_string();
                            let p = b.get("payload")?.as_str()?.to_string();
                            Some((t, p))
                        })
                        .collect()
                })
                .unwrap_or_default();
            if buttons.is_empty() {
                return Err(ApiError::bad_request("buttons (array of {title, payload}) is required"));
            }
            let mid = ch::messenger::send_buttons(&api("messenger"), &token, &psid, &title, &buttons)
                .await
                .map_err(op_err)?;
            json!({ "message_id": mid })
        }
        ("messenger", "get_user_profile") => {
            let psid = req_str("psid").ok_or_else(|| ApiError::bad_request("psid is required"))?;
            let p = ch::messenger::get_user_profile(&api("messenger"), &token, &psid)
                .await
                .map_err(op_err)?;
            json!({ "psid": p.psid, "first_name": p.first_name, "last_name": p.last_name, "profile_pic": p.profile_pic })
        }
        ("messenger", "set_typing") => {
            let psid = req_str("psid").ok_or_else(|| ApiError::bad_request("psid is required"))?;
            let action = req_str("action").unwrap_or_else(|| "typing_on".to_string());
            ch::messenger::set_typing(&api("messenger"), &token, &psid, &action)
                .await
                .map_err(op_err)?;
            json!({ "ok": true })
        }
        // ---------------- Slack ----------------
        ("slack", "send_blocks") => {
            let channel = req_str("channel").ok_or_else(|| ApiError::bad_request("channel is required"))?;
            let text = req_str("text").unwrap_or_else(|| " ".to_string());
            let blocks = body.get("blocks").cloned();
            let ts = ch::slack::send_blocks(&api("slack"), &token, &channel, &text, blocks.as_ref())
                .await
                .map_err(op_err)?;
            json!({ "ts": ts })
        }
        ("slack", "update_message") => {
            let channel = req_str("channel").ok_or_else(|| ApiError::bad_request("channel is required"))?;
            let ts = req_str("ts").ok_or_else(|| ApiError::bad_request("ts is required"))?;
            let text = req_str("text").ok_or_else(|| ApiError::bad_request("text is required"))?;
            let new_ts = ch::slack::update_message(&api("slack"), &token, &channel, &ts, &text)
                .await
                .map_err(op_err)?;
            json!({ "ts": new_ts })
        }
        ("slack", "delete_message") => {
            let channel = req_str("channel").ok_or_else(|| ApiError::bad_request("channel is required"))?;
            let ts = req_str("ts").ok_or_else(|| ApiError::bad_request("ts is required"))?;
            ch::slack::delete_message(&api("slack"), &token, &channel, &ts)
                .await
                .map_err(op_err)?;
            json!({ "deleted": true })
        }
        ("slack", "list_channels") => {
            let channels = ch::slack::list_channels(&api("slack"), &token)
                .await
                .map_err(op_err)?;
            json!({ "channels": channels.iter().map(|c| json!({
                "id": c.id, "name": c.name, "is_private": c.is_private,
            })).collect::<Vec<_>>() })
        }
        ("slack", "get_user_info") => {
            let user = req_str("user_id").ok_or_else(|| ApiError::bad_request("user_id is required"))?;
            let u = ch::slack::get_user_info(&api("slack"), &token, &user)
                .await
                .map_err(op_err)?;
            json!({ "id": u.id, "name": u.name, "real_name": u.real_name })
        }
        // ---------------- WhatsApp / LINE / Zalo (send_text) ----------------
        ("whatsapp", "send_text") => {
            let to = req_str("to").ok_or_else(|| ApiError::bad_request("to is required"))?;
            let text = req_str("text").ok_or_else(|| ApiError::bad_request("text is required"))?;
            let mid = ch::whatsapp::send_text(&api("whatsapp"), &token, &to, &text)
                .await
                .map_err(op_err)?;
            json!({ "message_id": mid })
        }
        ("line", "send_text") => {
            let to = req_str("to").ok_or_else(|| ApiError::bad_request("to is required"))?;
            let text = req_str("text").ok_or_else(|| ApiError::bad_request("text is required"))?;
            ch::line::send_text(&api("line"), &token, &to, &text)
                .await
                .map_err(op_err)?;
            json!({ "sent": true })
        }
        ("zalo", "send_text") => {
            let user = req_str("user_id").ok_or_else(|| ApiError::bad_request("user_id is required"))?;
            let text = req_str("text").ok_or_else(|| ApiError::bad_request("text is required"))?;
            let mid = ch::zalo::send_text(&api("zalo"), &token, &user, &text)
                .await
                .map_err(op_err)?;
            json!({ "message_id": mid })
        }
        _ => {
            return Err(ApiError::bad_request(format!(
                "unknown operation '{operation}' for channel '{id}'"
            )))
        }
    };

    Ok(Json(json!({ "ok": true, "id": id, "operation": operation, "result": result })))
}
