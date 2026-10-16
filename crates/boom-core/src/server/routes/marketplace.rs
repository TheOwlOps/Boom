//! Marketplace routes: `GET /api/marketplace`, `GET /api/marketplace/installed`,
//! `POST /api/marketplace/install`, `POST /api/marketplace/uninstall`.
//!
//! Catalog items are built from real plugin manifests + real tool specs —
//! never invented. Install/uninstall failures carry the HTTP status from
//! [`MarketplaceError`], mirroring Python's `MarketplaceError.status`.

use std::collections::HashSet;

use axum::{extract::State, Json};
use serde_json::{json, Value};

use super::super::{
    defaults::{plugin_map, tool_name_set},
    state::{AppState, ToolInfo},
    ApiError, JsonBody, MarketplaceError,
};
use crate::server::{split_marketplace_id, StorageError};

// ---------------------------------------------------------------------------
// Category mapping (ported from agent/src/marketplace.py keyword rules).
// ---------------------------------------------------------------------------

const CATEGORY_RULES: &[(&str, &[&str])] = &[
    ("Communication", &["telegram", "whatsapp", "discord", "slack", "messenger", "zalo", "line", "feishu", "sms", "broadcast", "sticker", "kick", "react"]),
    ("Productivity", &["gmail", "calendar", "drive", "email", "docs", "sheets", "notion", "todo", "task", "reminder", "note", "digest", "inbox"]),
    ("Developer Tools", &["github", "git", "gitops", "code", "review", "lint", "debug", "coverage", "deploy", "docker", "npm", "refactor", "ast", "depgraph", "unittest", "pytest"]),
    ("Web & Search", &["web", "search", "fetch", "scrape", "scraper", "crawl", "browser", "rss", "news", "feed", "extract", "http", "url"]),
    ("Media", &["image", "video", "audio", "tts", "stt", "voice", "ocr", "photo", "podcast", "banner", "icon", "imagegen"]),
    ("Files & Storage", &["file", "files", "fileops", "dir", "folder", "storage", "backup", "pdf", "csv", "excel", "spreadsheet", "document", "artifact"]),
    ("Data & AI", &["memory", "vector", "embedding", "llm", "model", "prompt", "rag", "dataset", "sql", "sqlite", "database", "knowledge"]),
    ("Automation", &["cron", "schedule", "scheduler", "trigger", "webhook", "workflow", "dag", "autonomous", "plan", "planner"]),
    ("Security", &["security", "auth", "authz", "secret", "secretscan", "scan", "sanitize", "audit", "permission", "redact", "guard", "ratelimit"]),
    ("System", &["shell", "terminal", "process", "system", "env", "config", "log", "logs", "monitor", "health", "ipc", "daemon", "lifecycle", "checkpoint"]),
];

const DEFAULT_CATEGORY: &str = "Utilities";
const FEATURED_CATEGORIES: &[&str] = &["Communication", "Productivity"];

fn category_icon(category: &str) -> &'static str {
    match category {
        "Communication" => "💬",
        "Productivity" => "📅",
        "Developer Tools" => "🛠️",
        "Web & Search" => "🌐",
        "Media" => "🎨",
        "Files & Storage" => "📁",
        "Data & AI" => "🧠",
        "Automation" => "⏰",
        "Security" => "🔒",
        "System" => "⚙️",
        _ => "🧩",
    }
}

/// Map an item to a category using the keyword rules (first match wins).
fn categorize_item(name: &str, description: &str) -> &'static str {
    let tokens: HashSet<String> = format!("{name} {description}")
        .to_lowercase()
        .split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|t| !t.is_empty())
        .map(|t| t.to_string())
        .collect();
    for (category, keywords) in CATEGORY_RULES {
        if keywords.iter().any(|k| tokens.contains(*k)) {
            return category;
        }
    }
    DEFAULT_CATEGORY
}

// ---------------------------------------------------------------------------
// Catalog
// ---------------------------------------------------------------------------

/// One catalog item. Plugins always carry `version`/`author` (possibly "");
/// tools omit them — exactly like Python's `_plugin_item` / `_tool_item`.
fn plugin_item(
    name: &str,
    description: &str,
    icon: Option<&str>,
    version: &str,
    author: &str,
    enabled: bool,
) -> Value {
    let category = categorize_item(name, description);
    json!({
        "id": format!("plugin:{name}"),
        "name": name,
        "icon": icon.unwrap_or_else(|| category_icon(category)),
        "description": description,
        "category": category,
        "featured": true,
        "kind": "plugin",
        "version": version,
        "author": author,
        "enabled": enabled,
    })
}

fn tool_item(tool: &ToolInfo, disabled: &HashSet<String>) -> Value {
    let category = categorize_item(&tool.name, &tool.description);
    json!({
        "id": format!("tool:{}", tool.name),
        "name": tool.name,
        "icon": category_icon(category),
        "description": tool.description,
        "category": category,
        "featured": FEATURED_CATEGORIES.contains(&category),
        "kind": "tool",
        "enabled": !disabled.contains(&tool.name),
    })
}

fn build_catalog(state: &AppState) -> Vec<Value> {
    let plugins = state.storage.plugins();
    let tools = state.tools.list_tools();
    let disabled: HashSet<String> = state.storage.disabled_tools().into_iter().collect();

    let mut items: Vec<Value> = plugins
        .iter()
        .map(|p| {
            plugin_item(
                &p.name,
                &p.description,
                p.icon.as_deref(),
                &p.version,
                &p.author,
                p.enabled,
            )
        })
        .collect();
    let mut sorted_tools = tools;
    sorted_tools.sort_by(|a, b| a.name.cmp(&b.name));
    items.extend(sorted_tools.iter().map(|t| tool_item(t, &disabled)));
    items
}

/// `GET /api/marketplace` → `{items, count}`.
pub async fn catalog(State(state): State<AppState>) -> Json<Value> {
    let items = build_catalog(&state);
    Json(json!({ "items": items, "count": items.len() }))
}

/// `GET /api/marketplace/installed` → `{installed: ["plugin:x", "tool:y", ...]}`.
pub async fn installed(State(state): State<AppState>) -> Json<Value> {
    let plugins = state.storage.plugins();
    let tools = state.tools.list_tools();
    let disabled: HashSet<String> = state.storage.disabled_tools().into_iter().collect();

    let mut ids: Vec<String> = plugins
        .iter()
        .map(|p| format!("plugin:{}", p.name))
        .collect();
    let mut tool_names: Vec<String> = tools
        .iter()
        .map(|t| t.name.clone())
        .filter(|n| !disabled.contains(n))
        .collect();
    tool_names.sort();
    ids.extend(tool_names.into_iter().map(|n| format!("tool:{n}")));
    Json(json!({ "installed": ids }))
}

// ---------------------------------------------------------------------------
// Install / uninstall
// ---------------------------------------------------------------------------

fn marketplace_error(e: MarketplaceError) -> ApiError {
    let status = axum::http::StatusCode::from_u16(e.status)
        .unwrap_or(axum::http::StatusCode::BAD_REQUEST);
    ApiError::new(status, e.message)
}

/// `POST /api/marketplace/install` — body `{id}` → 200 `{ok: true, id}`.
/// Unknown plugin → 404; unknown tool → 404; bad id → 400.
pub async fn install(
    State(state): State<AppState>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    let item_id = match body.get("id").and_then(|v| v.as_str()) {
        Some(id) if !id.trim().is_empty() => id.trim().to_string(),
        _ => return Err(ApiError::bad_request("id is required")),
    };
    let plugins = state.storage.plugins();
    let tools = state.tools.list_tools();
    let (kind, name) = split_marketplace_id(
        &item_id,
        &plugin_map(&plugins),
        &tool_name_set(&tools),
    )
    .map_err(marketplace_error)?;

    if kind == "plugin" {
        // No registry in the default build: only already-known plugins can
        // be enabled (Python: registry download → 404 "plugin 'x' not found").
        if !plugins.iter().any(|p| p.name == name) {
            return Err(marketplace_error(MarketplaceError::new(
                format!("plugin '{name}' not found"),
                404,
            )));
        }
        state
            .storage
            .enable_plugin(&name)
            .map_err(|e| match e {
                StorageError::NotFound(_) => marketplace_error(MarketplaceError::new(
                    format!("plugin '{name}' not found"),
                    404,
                )),
                _ => marketplace_error(MarketplaceError::new(
                    format!("could not enable plugin '{name}'"),
                    500,
                )),
            })?;
        return Ok(Json(json!({ "ok": true, "id": format!("plugin:{name}") })));
    }

    // Builtin tool: mark enabled.
    if !tools.iter().any(|t| t.name == name) {
        return Err(marketplace_error(MarketplaceError::new(
            format!("tool '{name}' does not exist"),
            404,
        )));
    }
    state.storage.set_tool_disabled(&name, false);
    Ok(Json(json!({ "ok": true, "id": format!("tool:{name}") })))
}

/// `POST /api/marketplace/uninstall` — body `{id}` → 200 `{ok: true, id}`.
/// Builtin tools are disabled (never deleted), like Python.
pub async fn uninstall(
    State(state): State<AppState>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    let item_id = match body.get("id").and_then(|v| v.as_str()) {
        Some(id) if !id.trim().is_empty() => id.trim().to_string(),
        _ => return Err(ApiError::bad_request("id is required")),
    };
    let plugins = state.storage.plugins();
    let tools = state.tools.list_tools();
    let (kind, name) = split_marketplace_id(
        &item_id,
        &plugin_map(&plugins),
        &tool_name_set(&tools),
    )
    .map_err(marketplace_error)?;

    if kind == "plugin" {
        if !state.storage.uninstall_plugin(&name) {
            return Err(marketplace_error(MarketplaceError::new(
                format!("plugin '{name}' is not installed"),
                404,
            )));
        }
        return Ok(Json(json!({ "ok": true, "id": format!("plugin:{name}") })));
    }

    if !tools.iter().any(|t| t.name == name) {
        return Err(marketplace_error(MarketplaceError::new(
            format!("tool '{name}' does not exist"),
            404,
        )));
    }
    state.storage.set_tool_disabled(&name, true);
    Ok(Json(json!({ "ok": true, "id": format!("tool:{name}") })))
}
