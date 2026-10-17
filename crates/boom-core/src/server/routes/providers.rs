//! Provider routes: `GET /api/providers`, `POST /api/providers/config`,
//! `POST /api/providers/test`, `GET /api/providers/ollama/status`.
//!
//! Responses never contain API keys (mirrors Python's contract).

use axum::{extract::State, Json};
use serde_json::{json, Value};

use super::super::{
    state::{AppState, ProviderConfig},
    ApiError, JsonBody, ProviderConfigError,
};

/// `GET /api/providers` → `{providers: [{id, name, models, needs_key,
/// default_base_url, show_base_url, allow_custom_model, has_key}], active}`.
pub async fn list_providers(
    State(state): State<AppState>,
) -> Result<Json<Value>, ApiError> {
    let providers: Vec<Value> = state
        .providers
        .list_providers()
        .into_iter()
        .map(|m| {
            json!({
                "id": m.id,
                "name": m.name,
                "models": m.models,
                "needs_key": m.needs_key,
                "default_base_url": m.default_base_url,
                "show_base_url": m.show_base_url,
                "allow_custom_model": m.allow_custom_model,
                "has_key": state.providers.has_key(&m.id),
            })
        })
        .collect();
    Ok(Json(json!({
        "providers": providers,
        "active": state.providers.active_provider(),
    })))
}

/// `POST /api/providers/config` — body `{provider_id, api_key?, model?, base_url?}`.
///
/// Validation mirrors `_handle_providers_config`:
/// - `provider_id` missing/blank → 400 `"provider_id is required"`
/// - non-string `api_key`/`model` → 400 `"{field} must be a string"`
/// - non-string `base_url` → 400 `"base_url must be a string"`
/// - unknown provider → 400 `"unknown provider: {id}"`
/// - empty `api_key` keeps the previously stored key (sentinel "keep
///   existing"); 400 `"{name} requires an API key"` only when no key was
///   ever stored and the provider needs one
/// - otherwise → 200 `{ok: true, provider_id}` (key never echoed back).
pub async fn save_config(
    State(state): State<AppState>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    let provider_id = match body.get("provider_id").and_then(|v| v.as_str()) {
        Some(id) if !id.trim().is_empty() => id.trim().to_string(),
        _ => return Err(ApiError::bad_request("provider_id is required")),
    };
    let api_key = match body.get("api_key") {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(_) => return Err(ApiError::bad_request("api_key must be a string")),
    };
    let model = match body.get("model") {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(_) => return Err(ApiError::bad_request("model must be a string")),
    };
    let base_url = match body.get("base_url") {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) => Some(s.clone()),
        Some(_) => return Err(ApiError::bad_request("base_url must be a string")),
    };

    // The cleartext key is never logged.
    state.api_log(format!("providers/config: {provider_id}"));
    match state.providers.save_config(ProviderConfig {
        provider_id: provider_id.clone(),
        api_key,
        model,
        base_url,
    }) {
        Ok(()) => Ok(Json(json!({ "ok": true, "provider_id": provider_id }))),
        Err(ProviderConfigError::UnknownProvider(id)) => {
            Err(ApiError::bad_request(format!("unknown provider: {id}")))
        }
        Err(ProviderConfigError::Invalid(msg)) => Err(ApiError::bad_request(msg)),
        Err(ProviderConfigError::Internal(_)) => Err(ApiError::new(
            axum::http::StatusCode::INTERNAL_SERVER_ERROR,
            "failed to save provider config",
        )),
    }
}

/// `POST /api/providers/test` — body `{provider_id}` → `{ok, message}`.
/// Unknown provider → 400 `"unknown provider: {id}"`; any other failure →
/// 500 `"failed to test provider connection"`.
pub async fn test_connection(
    State(state): State<AppState>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    let provider_id = match body.get("provider_id").and_then(|v| v.as_str()) {
        Some(id) if !id.trim().is_empty() => id.trim().to_string(),
        _ => return Err(ApiError::bad_request("provider_id is required")),
    };
    match state.providers.test_connection(&provider_id).await {
        Ok((ok, message)) => Ok(Json(json!({ "ok": ok, "message": message }))),
        Err(e) => {
            // The registry reports an unknown id as `Err("unknown provider:
            // {id}")` — that's a client error, not a server failure.
            if e.starts_with("unknown provider:") {
                Err(ApiError::bad_request(e))
            } else {
                state.api_log(format!("/api/providers/test error: {e}"));
                Err(ApiError::new(
                    axum::http::StatusCode::INTERNAL_SERVER_ERROR,
                    "failed to test provider connection",
                ))
            }
        }
    }
}

/// `GET /api/providers/ollama/status` → `{running, models}`.
/// Always 200 — the probe degrades to `{running: false, models: []}`
/// instead of failing.
pub async fn ollama_status(State(state): State<AppState>) -> Json<Value> {
    let status = state.providers.ollama_status().await;
    Json(json!({ "running": status.running, "models": status.models }))
}
