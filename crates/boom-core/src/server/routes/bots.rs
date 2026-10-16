//! Custom-bot routes (Grok-style): `GET/POST /api/bots`,
//! `POST /api/bots/active`, `GET/PUT/DELETE /api/bots/{id}`.

use axum::{
    extract::{Path, State},
    Json,
};
use serde_json::{json, Value};

use super::super::{
    normalize_str_list,
    state::{AppState, NewBot},
    ApiError, JsonBody, StorageError,
};

fn storage_error(e: StorageError, fallback_500: &str) -> ApiError {
    match e {
        StorageError::NotFound(msg) => ApiError::not_found(msg),
        StorageError::Invalid(msg) => ApiError::bad_request(msg),
        StorageError::Internal(_) => ApiError::new(
            axum::http::StatusCode::INTERNAL_SERVER_ERROR,
            fallback_500,
        ),
    }
}

/// `GET /api/bots` → `{bots, count}` (active bot first).
pub async fn list_bots(State(state): State<AppState>) -> Json<Value> {
    let bots = state.storage.list_bots();
    Json(json!({ "bots": bots, "count": bots.len() }))
}

fn opt_string(body: &serde_json::Map<String, Value>, key: &str) -> Result<String, ApiError> {
    match body.get(key) {
        None | Some(Value::Null) => Ok(String::new()),
        Some(Value::String(s)) => Ok(s.clone()),
        Some(_) => Err(ApiError::bad_request(format!("{key} must be a string"))),
    }
}

fn opt_str_list(
    body: &serde_json::Map<String, Value>,
    key: &str,
) -> Result<Option<Vec<String>>, ApiError> {
    match body.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(v) => normalize_str_list(v, key)
            .map(Some)
            .map_err(ApiError::bad_request),
    }
}

/// `POST /api/bots` → 201 `{ok: true, bot}`.
/// `name` missing/blank → 400 `"name is required"`.
pub async fn create_bot(
    State(state): State<AppState>,
    JsonBody(body): JsonBody,
) -> Result<(axum::http::StatusCode, Json<Value>), ApiError> {
    let name = match body.get("name").and_then(|v| v.as_str()) {
        Some(n) if !n.trim().is_empty() => n.trim().to_string(),
        _ => return Err(ApiError::bad_request("name is required")),
    };
    let input = NewBot {
        name,
        description: opt_string(&body, "description")?,
        system_prompt: opt_string(&body, "system_prompt")?,
        avatar_emoji: {
            let e = opt_string(&body, "avatar_emoji")?;
            if e.is_empty() { "🤖".to_string() } else { e }
        },
        allowed_tools: opt_str_list(&body, "allowed_tools")?,
        denied_tools: opt_str_list(&body, "denied_tools")?,
        channel_bindings: opt_str_list(&body, "channel_bindings")?,
    };
    match state.storage.create_bot(input) {
        Ok(bot) => Ok((
            axum::http::StatusCode::CREATED,
            Json(json!({ "ok": true, "bot": bot })),
        )),
        Err(e) => Err(storage_error(e, "failed to create bot")),
    }
}

/// `GET /api/bots/{id}` → 200 `{ok: true, bot}`; missing → 404.
/// (Previously this route was PUT/DELETE-only; GET returned 405.)
pub async fn get_bot(
    State(state): State<AppState>,
    Path(bot_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    if bot_id.is_empty() || bot_id == "active" {
        // "/api/bots/active" is POST-only — never a bot id.
        return Err(ApiError::not_found("not found"));
    }
    match state.storage.get_bot(&bot_id) {
        Some(bot) => Ok(Json(json!({ "ok": true, "bot": bot }))),
        None => Err(ApiError::not_found(format!("bot not found: '{bot_id}'"))),
    }
}

/// `PUT /api/bots/{id}` — partial update → 200 `{ok: true, bot}`.
/// Unknown fields are ignored; missing bot → 404.
pub async fn update_bot(
    State(state): State<AppState>,
    Path(bot_id): Path<String>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    if bot_id.is_empty() || bot_id == "active" {
        // "/api/bots/active" is POST-only — never a bot id.
        return Err(ApiError::not_found("not found"));
    }
    match state.storage.update_bot(&bot_id, &body) {
        Ok(bot) => Ok(Json(json!({ "ok": true, "bot": bot }))),
        Err(e) => Err(storage_error(e, "failed to update bot")),
    }
}

/// `DELETE /api/bots/{id}` → 200 `{ok: true}`; missing → 404.
pub async fn delete_bot(
    State(state): State<AppState>,
    Path(bot_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    if bot_id.is_empty() || bot_id == "active" {
        return Err(ApiError::not_found("not found"));
    }
    if !state.storage.delete_bot(&bot_id) {
        return Err(ApiError::not_found(format!("bot not found: '{bot_id}'")));
    }
    Ok(Json(json!({ "ok": true })))
}

/// `PUT`/`DELETE /api/bots/active` → 404: it is a POST-only action,
/// never a bot id (mirrors Python's explicit exclusion).
pub async fn active_not_a_bot_id() -> ApiError {
    ApiError::not_found("not found")
}

/// `POST /api/bots/active` — body `{bot_id}` → 200 `{ok: true, bot}`.
pub async fn set_active(
    State(state): State<AppState>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    let bot_id = match body.get("bot_id").and_then(|v| v.as_str()) {
        Some(id) if !id.trim().is_empty() => id.trim().to_string(),
        _ => return Err(ApiError::bad_request("bot_id is required")),
    };
    if !state.storage.set_active_bot(&bot_id) {
        return Err(ApiError::not_found(format!("bot not found: '{bot_id}'")));
    }
    Ok(Json(json!({ "ok": true, "bot": state.storage.active_bot() })))
}
