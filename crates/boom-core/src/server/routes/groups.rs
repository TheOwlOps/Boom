//! Group routes: `GET /api/groups`, `POST /api/groups/mute`,
//! `POST /api/groups/policy`.
//!
//! ## How groups get registered (no HTTP register endpoint — by design)
//!
//! Neither this Rust core nor Python's `gateway/api.py` exposes a
//! "register group" endpoint: groups are registered by the **channel
//! runtime**, not by API callers.
//!
//! - Python: `agent/src/business/groups.py::GroupManager.record_message`
//!   auto-registers a group the first time a message arrives from it
//!   (default name `"channel:group_id"`); `register_group` /
//!   `unregister_group` are called by channel adapters and admin tools.
//! - Rust: [`SqliteStorage::register_group`](crate::storage::server_storage::SqliteStorage::register_group) (same auto-register
//!   semantics) — the HTTP layer here only lists, mutes and policies
//!   groups that already exist.
//!
//! Consequence for QA: `mute` / `policy` answer 404 for an unregistered
//! `(channel, group_id)` — that is the honest "no such group" contract,
//! not a missing feature. To exercise the happy path end-to-end, register
//! the group through the channel runtime (or seed it via
//! [`SqliteStorage::register_group`](crate::storage::server_storage::SqliteStorage::register_group),
//! e.g. in tests) instead of expecting a
//! `POST /api/groups/register` that neither backend has.

use axum::{extract::State, Json};
use serde_json::{json, Value};

use super::super::{
    state::AppState, ApiError, JsonBody, StorageError,
};

/// `GET /api/groups` → `{groups, count}`.
pub async fn list_groups(State(state): State<AppState>) -> Json<Value> {
    let groups = state.storage.list_groups();
    Json(json!({ "groups": groups, "count": groups.len() }))
}

fn ident(body: &serde_json::Map<String, Value>, key: &str) -> Result<String, ApiError> {
    match body.get(key).and_then(|v| v.as_str()) {
        Some(s) if !s.trim().is_empty() => Ok(s.trim().to_string()),
        _ => Err(ApiError::bad_request(format!("{key} is required"))),
    }
}

/// `POST /api/groups/mute` — body `{channel, group_id, muted: bool}` →
/// 200 `{ok, channel, group_id, muted}`. Unregistered group → 404.
pub async fn mute(
    State(state): State<AppState>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    let channel = ident(&body, "channel")?;
    let group_id = ident(&body, "group_id")?;
    let muted = match body.get("muted") {
        Some(Value::Bool(b)) => *b,
        _ => return Err(ApiError::bad_request("muted must be a boolean")),
    };
    match state.storage.set_group_muted(&channel, &group_id, muted) {
        Ok(()) => Ok(Json(json!({
            "ok": true,
            "channel": channel,
            "group_id": group_id,
            "muted": muted,
        }))),
        Err(StorageError::NotFound(msg)) => Err(ApiError::not_found(msg)),
        Err(_) => {
            state.api_log("/api/groups/mute error");
            Err(ApiError::new(
                axum::http::StatusCode::INTERNAL_SERVER_ERROR,
                "failed to update mute state",
            ))
        }
    }
}

/// `POST /api/groups/policy` — body `{channel, group_id, policy: {...}}` →
/// 200 `{ok: true, policy}`. Unregistered group → 404.
pub async fn policy(
    State(state): State<AppState>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    let channel = ident(&body, "channel")?;
    let group_id = ident(&body, "group_id")?;
    let policy = match body.get("policy") {
        Some(Value::Object(_)) => body["policy"].clone(),
        _ => return Err(ApiError::bad_request("policy must be an object")),
    };
    match state.storage.set_group_policy(&channel, &group_id, &policy) {
        Ok(saved) => Ok(Json(json!({ "ok": true, "policy": saved }))),
        Err(StorageError::NotFound(msg)) => Err(ApiError::not_found(msg)),
        Err(StorageError::Invalid(msg)) => Err(ApiError::bad_request(msg)),
        Err(_) => {
            state.api_log("/api/groups/policy error");
            Err(ApiError::new(
                axum::http::StatusCode::INTERNAL_SERVER_ERROR,
                "failed to save policy",
            ))
        }
    }
}
