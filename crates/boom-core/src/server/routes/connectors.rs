//! Connector HTTP endpoints (merge target:
//! `crates/boom-core/src/server/routes/connectors.rs`).
//!
//! ```text
//! GET  /api/connectors               → {connectors: [{id, name, description, auth_kind, connected}]}
//! POST /api/connectors/{id}/connect  → {connect_url, instructions}
//! POST /api/connectors/{id}/token    → {ok: true}   (body: {"code"} or {"access_token"})
//! POST /api/connectors/{id}/disconnect → {ok: true} (idempotent)
//! ```
//!
//! Unknown ids answer 404 `{"error": "unknown connector: <id>"}` (the
//! [`crate::server::ApiError`] shape). Token values are never logged and
//! never appear in responses — the `{"code"}` / `{"access_token"}` bodies
//! are consumed and dropped inside these handlers.
//!
//! The registry is process-global: connectors are process-global
//! configuration (tokens live on disk), so sharing one registry across
//! handlers avoids threading it through [`crate::server::state::AppState`].
//! `exchange_code` may hold the mutex across an `.await` (one HTTP round
//! trip); connect flows are rare and never contend with chat turns.

use std::sync::LazyLock;

use axum::{extract::Path, http::StatusCode, Json};
use serde_json::{json, Value};

use crate::connectors::{save_token, ConnectorRegistry, ConnectorToken};
use crate::server::{ApiError, JsonBody};

static REGISTRY: LazyLock<tokio::sync::Mutex<ConnectorRegistry>> =
    LazyLock::new(|| tokio::sync::Mutex::new(ConnectorRegistry::new()));

/// `GET /api/connectors` → `{connectors: [{id, name, description, auth_kind, connected}]}`.
pub async fn list() -> Json<Value> {
    let reg = REGISTRY.lock().await;
    Json(json!({ "connectors": reg.list() }))
}

/// `POST /api/connectors/{id}/connect` → `{connect_url, instructions}`.
///
/// 404 when the id is unknown; 400 with a clear, actionable message when the
/// connector cannot start a connect flow (e.g. OAuth client id not
/// configured, or an `"api_key"` connector explaining where to get a key).
#[allow(clippy::await_holding_lock)]
pub async fn connect(Path(id): Path<String>) -> Result<Json<Value>, ApiError> {
    let reg = REGISTRY.lock().await;
    let connector = reg
        .get(&id)
        .ok_or_else(|| ApiError::not_found(format!("unknown connector: {id}")))?;
    match connector.connect_url() {
        Ok(url) => Ok(Json(json!({
            "connect_url": url,
            "instructions": connector.instructions(),
        }))),
        Err(message) => Err(ApiError::bad_request(message)),
    }
}

/// `POST /api/connectors/{id}/token` → `{ok: true}`.
///
/// Holds the registry mutex across the OAuth exchange `.await`: connect flows
/// are rare and never contend with chat turns (deliberate, not a bug).
///
/// Body is either `{"code": "…"}` (OAuth2 — the connector exchanges it and
/// persists the token itself) or `{"access_token": "…"}` (API key / manual
/// token entry, stored directly; optional `"refresh_token"`, `"expires_at"`
/// (unix epoch), and `"extra"` object are accepted too).
///
/// 404 for unknown ids; 400 when neither field is present or the OAuth
/// exchange fails (provider errors are surfaced without secret material).
#[allow(clippy::await_holding_lock)]
pub async fn token(
    Path(id): Path<String>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    let code = body
        .get("code")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty());
    let access_token = body
        .get("access_token")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty());

    match (code, access_token) {
        (Some(code), _) => {
            let mut reg = REGISTRY.lock().await;
            let connector = reg
                .get_mut(&id)
                .ok_or_else(|| ApiError::not_found(format!("unknown connector: {id}")))?;
            // The code is never logged. The connector persists the token
            // itself via save_token (trait contract) — it is not returned.
            connector
                .exchange_code(code)
                .await
                .map_err(ApiError::bad_request)?;
            Ok(Json(json!({ "ok": true })))
        }
        (None, Some(access_token)) => {
            {
                let reg = REGISTRY.lock().await;
                if reg.get(&id).is_none() {
                    return Err(ApiError::not_found(format!("unknown connector: {id}")));
                }
            }
            let tok = ConnectorToken {
                access_token: access_token.to_string(),
                refresh_token: body
                    .get("refresh_token")
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                    .map(str::to_string),
                expires_at: body.get("expires_at").and_then(Value::as_i64),
                extra: body.get("extra").cloned().unwrap_or(Value::Null),
            };
            save_token(&id, &tok)
                .map_err(|e| ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, e))?;
            Ok(Json(json!({ "ok": true })))
        }
        (None, None) => Err(ApiError::bad_request(
            "body must contain \"code\" (OAuth2) or \"access_token\" (API key)",
        )),
    }
}

/// `POST /api/connectors/{id}/disconnect` → `{ok: true}`.
///
/// Idempotent: disconnecting a connector that was never connected still
/// returns `{ok: true}`. 404 for unknown ids.
#[allow(clippy::await_holding_lock)]
pub async fn disconnect(Path(id): Path<String>) -> Result<Json<Value>, ApiError> {
    let mut reg = REGISTRY.lock().await;
    let connector = reg
        .get_mut(&id)
        .ok_or_else(|| ApiError::not_found(format!("unknown connector: {id}")))?;
    connector
        .disconnect()
        .map_err(|e| ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, e))?;
    Ok(Json(json!({ "ok": true })))
}
