//! `GET /api/tools` and `POST /api/tools/run`.

use axum::{extract::State, http::StatusCode, Json};
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
/// Field aliases (frontend ↔ backend compatibility):
/// - tool name: `tool` (canonical) or `name` — `tool` wins when both present.
/// - arguments: `args`, `arguments`, `input`, `parameters` — first present wins.
///
/// Contract (mirrors `_handle_tool_run`):
/// - tool name missing/blank → 400 `"tool is required"`
/// - args present but not an object → 400 `"args must be an object"`
/// - unknown tool → 404 `"tool not found: {name}"`
/// - tool disabled via marketplace → 403 `"tool is disabled: {name}"`
/// - success → 200 `{ok: true, result}` (result truncated to 2000 chars)
/// - tool failure → 200 `{ok: false, error}` (truncated to 500 chars)
pub async fn run_tool(
    State(state): State<AppState>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    fn non_empty_str(v: Option<&Value>) -> Option<&str> {
        v.and_then(Value::as_str).filter(|s| !s.is_empty())
    }
    let tool_name = match non_empty_str(body.get("tool"))
        .or_else(|| non_empty_str(body.get("name")))
    {
        Some(n) => n.to_string(),
        None => return Err(ApiError::bad_request("tool is required")),
    };
    let args = ["args", "arguments", "input", "parameters"]
        .iter()
        .find_map(|k| body.get(*k));
    let args = match args {
        None | Some(Value::Null) => serde_json::Map::new(),
        Some(Value::Object(map)) => map.clone(),
        Some(_) => return Err(ApiError::bad_request("args must be an object")),
    };

    if !state.tools.list_tools().iter().any(|t| t.name == tool_name) {
        return Err(ApiError::not_found(format!("tool not found: {tool_name}")));
    }

    // Disabled tools (via marketplace) must not run, even through this raw API.
    if state.storage.disabled_tools().contains(&tool_name) {
        return Err(ApiError::new(
            StatusCode::FORBIDDEN,
            format!("tool is disabled: {tool_name}"),
        ));
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
