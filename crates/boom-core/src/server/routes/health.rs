//! `GET /api/health` and `GET /api/version`.

use axum::{extract::State, Json};
use serde_json::json;

use super::super::{state::AppState, ApiError};

/// `GET /api/health` →
/// `{status, version, model, uptime_sec, tools_count, skills_count}`.
/// `model` is `"unconfigured"` when no harness model is set, exactly like Python.
pub async fn health(State(state): State<AppState>) -> Result<Json<serde_json::Value>, ApiError> {
    let tools_count = state.tools.list_tools().len();
    let skills_count = count_skills(state.skills_dir.as_deref());
    Ok(Json(json!({
        "status": "ok",
        "version": state.version,
        "model": state.harness.model_name().unwrap_or_else(|| "unconfigured".to_string()),
        "uptime_sec": state.start.elapsed().as_secs(),
        "tools_count": tools_count,
        "skills_count": skills_count,
    })))
}

/// `GET /api/version` → `{version}`.
pub async fn version(State(state): State<AppState>) -> Json<serde_json::Value> {
    Json(json!({ "version": state.version }))
}

/// Count skill directories (each subdir of `skills_dir`), mirroring Python's
/// scan of the repo `skills/` directory.
fn count_skills(dir: Option<&std::path::Path>) -> usize {
    let dir = match dir {
        Some(d) => d,
        None => return 0,
    };
    std::fs::read_dir(dir)
        .map(|entries| {
            entries
                .filter_map(|e| e.ok())
                .filter(|e| e.file_type().map(|t| t.is_dir()).unwrap_or(false))
                .count()
        })
        .unwrap_or(0)
}
