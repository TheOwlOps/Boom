//! Remaining routes: logs, activity, sessions, channels/status, dag/status,
//! skills, updates/check, and the zalo-personal channel endpoints.

use axum::{
    extract::{Query, State},
    Json,
};
use serde::Deserialize;
use serde_json::{json, Value};

use super::super::{
    channel_credential_hint, now_epoch, parse_ts_iso,
    state::{AppState, ACTIVITY_FRESH_SEC, KNOWN_CHANNELS, LOG_MAX},
    ApiError, JsonBody, ZaloLoginError,
};

// ---------------------------------------------------------------------------
// GET /api/logs
// ---------------------------------------------------------------------------

#[derive(Deserialize, Default)]
pub struct LogQuery {
    limit: Option<String>,
    offset: Option<String>,
}

fn as_int(s: Option<&String>, default: i64) -> i64 {
    s.and_then(|v| v.parse::<i64>().ok()).unwrap_or(default)
}

/// `GET /api/logs?limit&offset` → `{logs: [{ts, msg}], total, limit, offset}`.
/// Newest entries are last; `offset` counts back from the newest.
pub async fn logs(
    State(state): State<AppState>,
    Query(q): Query<LogQuery>,
) -> Json<Value> {
    let limit = as_int(q.limit.as_ref(), 50).clamp(1, LOG_MAX as i64) as usize;
    let offset = as_int(q.offset.as_ref(), 0).max(0) as usize;

    let log = state.log.lock().map(|g| g.clone()).unwrap_or_default();
    let total = log.len();
    let end = total.saturating_sub(offset);
    let start = end.saturating_sub(limit);
    let entries: Vec<Value> = log
        .range(start..end)
        .map(|e| json!({ "ts": e.ts, "msg": e.msg }))
        .collect();
    Json(json!({
        "logs": entries,
        "total": total,
        "limit": limit,
        "offset": offset,
    }))
}

// ---------------------------------------------------------------------------
// GET /api/activity
// ---------------------------------------------------------------------------

const ACTIVITY_KEYWORDS: &[&str] = &["tool_call", "thinking", "tool", "error", "fail"];

/// `GET /api/activity` → `{activities: [{id, timestamp, type, message, detail}], current}`.
///
/// Built from the in-memory gateway log buffer only (the trajectory/audit
/// file sources of the Python version need repo files this port does not
/// assume). Never invents events: no matches → empty activities + idle.
pub async fn activity(State(state): State<AppState>) -> Json<Value> {
    let log = state.log.lock().map(|g| g.clone()).unwrap_or_default();
    let mut activities: Vec<Value> = Vec::new();
    for e in log.iter().filter(|e| {
        let low = e.msg.to_lowercase();
        ACTIVITY_KEYWORDS.iter().any(|k| low.contains(k))
    }) {
        let low = e.msg.to_lowercase();
        let act_type = if low.contains("error") || low.contains("fail") {
            "error"
        } else if low.contains("tool") {
            "tool_call"
        } else {
            "thinking"
        };
        let msg: String = e.msg.chars().take(160).collect();
        activities.push(json!({
            "id": format!("log-{}", e.ts),
            "timestamp": e.ts,
            "type": act_type,
            "message": msg,
            "detail": e.msg,
        }));
    }
    // Keep the last 20, like Python's `matches[-20:]`.
    if activities.len() > 20 {
        activities.drain(..activities.len() - 20);
    }

    let now = now_epoch();
    let current = match activities.last() {
        Some(last) => {
            let ts = last["timestamp"].as_str().unwrap_or("");
            let fresh = parse_ts_iso(ts).map(|t| now - t <= ACTIVITY_FRESH_SEC).unwrap_or(false);
            if fresh {
                json!({
                    "type": last["type"],
                    "message": last["message"],
                    "started_at": last["timestamp"],
                })
            } else {
                json!({ "type": "idle", "message": "idle" })
            }
        }
        None => json!({ "type": "idle", "message": "idle" }),
    };
    Json(json!({ "activities": activities, "current": current }))
}

// ---------------------------------------------------------------------------
// GET /api/sessions
// ---------------------------------------------------------------------------

/// `GET /api/sessions` → `{sessions: []}`. Honest empty state: the session
/// store exposes load but no listing (mirrors Python's comment).
pub async fn sessions() -> Json<Value> {
    Json(json!({ "sessions": [] }))
}

// ---------------------------------------------------------------------------
// GET /api/channels/status
// ---------------------------------------------------------------------------

/// `GET /api/channels/status` → `{channels: [{name, connected, detail}]}`.
/// `detail` is null when connected, otherwise the honest reason.
pub async fn channels_status(State(state): State<AppState>) -> Json<Value> {
    let channels: Vec<Value> = KNOWN_CHANNELS
        .iter()
        .map(|name| {
            let connected = state.harness.can_deliver(name);
            let detail = if connected {
                Value::Null
            } else if state.daemon_wired {
                json!(channel_credential_hint(name))
            } else {
                json!("gateway daemon not wired")
            };
            json!({ "name": name, "connected": connected, "detail": detail })
        })
        .collect();
    Json(json!({ "channels": channels }))
}

// ---------------------------------------------------------------------------
// GET /api/dag/status
// ---------------------------------------------------------------------------

/// `GET /api/dag/status` → `{nodes: [{id, name, status, dependencies}]}`.
/// Empty when the brain records no last-run state (the honest default).
pub async fn dag_status(State(state): State<AppState>) -> Json<Value> {
    Json(json!({ "nodes": state.harness.dag_nodes() }))
}

// ---------------------------------------------------------------------------
// GET /api/skills
// ---------------------------------------------------------------------------

/// First non-empty, non-heading line among SKILL.md lines 2..=10,
/// truncated to 200 chars (mirrors Python's scan).
fn skill_description(skill_dir: &std::path::Path) -> String {
    let md = skill_dir.join("SKILL.md");
    let text = match std::fs::read_to_string(md) {
        Ok(t) => t,
        Err(_) => return String::new(),
    };
    let lines: Vec<&str> = text.lines().collect();
    for ln in lines.iter().skip(1).take(9) {
        let ln = ln.trim();
        if !ln.is_empty() && !ln.starts_with('#') {
            return ln.chars().take(200).collect();
        }
    }
    String::new()
}

/// `GET /api/skills` → `{skills: [{name, description}], count}`.
pub async fn list_skills(State(state): State<AppState>) -> Json<Value> {
    let mut skills: Vec<Value> = Vec::new();
    if let Some(dir) = state.skills_dir.as_deref() {
        if let Ok(entries) = std::fs::read_dir(dir) {
            let mut dirs: Vec<_> = entries
                .filter_map(|e| e.ok())
                .filter(|e| e.file_type().map(|t| t.is_dir()).unwrap_or(false))
                .collect();
            dirs.sort_by_key(|e| e.file_name());
            for entry in dirs {
                let name = entry.file_name().to_string_lossy().to_string();
                skills.push(json!({
                    "name": name,
                    "description": skill_description(&entry.path()),
                }));
            }
        }
    }
    Json(json!({ "skills": skills, "count": skills.len() }))
}

// ---------------------------------------------------------------------------
// GET /api/updates/check
// ---------------------------------------------------------------------------

/// `GET /api/updates/check` →
/// `{has_update, current_version, latest_version, download_url, release_notes, published_at}`.
/// Failure → 502 `{"error": ...}` (never a fabricated payload).
pub async fn updates_check(State(state): State<AppState>) -> Result<Json<Value>, ApiError> {
    match state.updater.check().await {
        Ok(info) => Ok(Json(json!(info))),
        Err(e) => {
            let e: String = e.chars().take(500).collect();
            Err(ApiError::new(
                axum::http::StatusCode::BAD_GATEWAY,
                e,
            ))
        }
    }
}

// ---------------------------------------------------------------------------
// Zalo-personal channel
// ---------------------------------------------------------------------------

/// `GET /api/channels/zalo-personal/status` →
/// `{connected: bool, phone_masked: str | null}`.
/// The raw phone number is never exposed.
pub async fn zalo_status(State(state): State<AppState>) -> Json<Value> {
    let (connected, phone_masked) = state.zalo.status();
    Json(json!({ "connected": connected, "phone_masked": phone_masked }))
}

/// `POST /api/channels/zalo-personal/logout` — always safe, even with no
/// session → 200 `{ok: true}`.
pub async fn zalo_logout(State(state): State<AppState>) -> Result<Json<Value>, ApiError> {
    match state.zalo.logout() {
        Ok(()) => Ok(Json(json!({ "ok": true }))),
        Err(_) => Err(ApiError::new(
            axum::http::StatusCode::INTERNAL_SERVER_ERROR,
            "failed to log out of zalo-personal",
        )),
    }
}

/// Replace credential substrings before they can reach a response or log.
fn scrub(text: &str, phone: &str, password: &str) -> String {
    text.replace(password, "***").replace(phone, "***")
}

/// `POST /api/channels/zalo-personal/login` — body `{phone, password}` →
/// 200 `{ok: true, phone_masked}`. Missing phone/password → 400. The password
/// is never echoed into the response or the logs.
pub async fn zalo_login(
    State(state): State<AppState>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    let phone = match body.get("phone").and_then(|v| v.as_str()) {
        Some(p) if !p.trim().is_empty() => p.trim().to_string(),
        _ => return Err(ApiError::bad_request("phone is required")),
    };
    let password = match body.get("password").and_then(|v| v.as_str()) {
        Some(p) if !p.is_empty() => p.to_string(),
        _ => return Err(ApiError::bad_request("password is required")),
    };

    let result = state.zalo.login(&phone, &password).await;
    // Scrub credential substrings before they can reach a response or a log,
    // then drop the cleartext reference as soon as the call returns.
    match result {
        Ok(phone_masked) => {
            drop(password);
            Ok(Json(json!({ "ok": true, "phone_masked": phone_masked })))
        }
        Err(ZaloLoginError::Unavailable) => {
            drop(password);
            state.api_log("zalo-personal login error: adapter unavailable");
            Err(ApiError::new(
                axum::http::StatusCode::INTERNAL_SERVER_ERROR,
                "zalo-personal adapter is unavailable",
            ))
        }
        Err(ZaloLoginError::InvalidCredentials(msg)) => {
            let msg = scrub(&msg, &phone, &password);
            drop(password);
            let msg = if msg.trim().is_empty() {
                "invalid credentials".to_string()
            } else {
                msg
            };
            Err(ApiError::bad_request(msg))
        }
        Err(ZaloLoginError::Failed(msg)) => {
            let msg = scrub(&msg, &phone, &password);
            drop(password);
            let msg = if msg.trim().is_empty() {
                "zalo-personal login failed".to_string()
            } else {
                msg
            };
            state.api_log(format!("zalo-personal login failed: {msg}"));
            Err(ApiError::new(
                axum::http::StatusCode::INTERNAL_SERVER_ERROR,
                msg,
            ))
        }
    }
}
