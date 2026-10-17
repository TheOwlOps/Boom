//! Channel routes: real Telegram connect/disconnect/test plus an honest
//! inventory. Platforms this build does not implement answer `unsupported`
//! — never "not wired".

use axum::{
    extract::{Path, State},
    Json,
};
use serde_json::{json, Value};

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
