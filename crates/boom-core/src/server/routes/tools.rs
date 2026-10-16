//! `GET /api/tools` and `POST /api/tools/run`.

use axum::{extract::State, Json};
use serde_json::{json, Value};

use super::super::{state::AppState, ApiError, JsonBody};

/// `GET /api/tools` → `{tools: [{name, description, group}], count}`.
pub async fn list_tools(State(state): State<AppState>) -> Json<Value> {
    let tools: Vec<Value> = state
        .tools
        .list_tools()
        .into_iter()
        .map(|t| {
            json!({
                "name": t.name,
                "description": t.description,
                "group": if t.group.is_empty() { "other".to_string() } else { t.group },
            })
        })
        .collect();
    Json(json!({ "tools": tools, "count": tools.len() }))
}

/// `POST /api/tools/run` — body `{tool, args?}`.
///
/// Contract (mirrors `_handle_tool_run`):
/// - `tool` missing/blank → 400 `"tool is required"`
/// - `args` present but not an object → 400 `"args must be an object"`
/// - unknown tool → 404 `"tool not found: {name}"`
/// - success → 200 `{ok: true, result}` (result truncated to 2000 chars)
/// - tool failure → 200 `{ok: false, error}` (truncated to 500 chars)
pub async fn run_tool(
    State(state): State<AppState>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    let tool_name = match body.get("tool").and_then(|v| v.as_str()) {
        Some(n) if !n.is_empty() => n.to_string(),
        _ => return Err(ApiError::bad_request("tool is required")),
    };
    let args = match body.get("args") {
        None | Some(Value::Null) => serde_json::Map::new(),
        Some(Value::Object(map)) => map.clone(),
        Some(_) => return Err(ApiError::bad_request("args must be an object")),
    };

    if !state.tools.list_tools().iter().any(|t| t.name == tool_name) {
        return Err(ApiError::not_found(format!("tool not found: {tool_name}")));
    }

    state.api_log(format!("tool_run: {tool_name} requested"));
    match state.tools.run_tool(&tool_name, &args).await {
        Ok(result) => {
            let result: String = result.chars().take(2000).collect();
            state.api_log(format!("tool_run: {tool_name} ok"));
            Ok(Json(json!({ "ok": true, "result": result })))
        }
        Err(e) => {
            let e: String = e.chars().take(500).collect();
            state.api_log(format!("tool_run: {tool_name} error: {e}"));
            Ok(Json(json!({ "ok": false, "error": e })))
        }
    }
}
