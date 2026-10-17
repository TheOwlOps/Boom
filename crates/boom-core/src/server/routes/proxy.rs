//! Proxy observability routes: `GET /api/proxy/stats` and
//! `GET /api/proxy/logs`.
//!
//! Both read the process-global proxy state published by the most recently
//! wrapped provider client ([`crate::proxy::current_state`]). When no client
//! has been wrapped yet (e.g. no provider configured), both return empty
//! payloads — never an error.

use axum::{extract::Query, Json};
use serde::Deserialize;
use serde_json::{json, Value};

/// `GET /api/proxy/stats` →
/// `{providers: {<id>: {calls, errors, total_latency_ms, avg_latency_ms,
/// last_error?, last_call_ts}}, total_calls, total_errors}`.
pub async fn stats() -> Json<Value> {
    let Some(state) = crate::proxy::current_state() else {
        return Json(json!({
            "providers": {},
            "total_calls": 0,
            "total_errors": 0,
        }));
    };
    let snapshot = state.stats_snapshot();
    let mut providers = serde_json::Map::new();
    let mut total_calls: u64 = 0;
    let mut total_errors: u64 = 0;
    for (id, s) in &snapshot {
        total_calls += s.calls;
        total_errors += s.errors;
        let avg = s.total_latency_ms.checked_div(s.calls).unwrap_or(0);
        providers.insert(
            id.clone(),
            json!({
                "calls": s.calls,
                "errors": s.errors,
                "total_latency_ms": s.total_latency_ms,
                "avg_latency_ms": avg,
                "last_error": s.last_error,
                "last_call_ts": s.last_call_ts,
            }),
        );
    }
    Json(json!({
        "providers": providers,
        "total_calls": total_calls,
        "total_errors": total_errors,
    }))
}

#[derive(Deserialize, Default)]
pub struct ProxyLogQuery {
    limit: Option<usize>,
}

/// `GET /api/proxy/logs?limit=50` → `{logs: [...], total}` (newest first).
/// Log entries never contain API keys; message bodies only appear when the
/// proxy config explicitly enables `log_bodies`.
pub async fn logs(Query(q): Query<ProxyLogQuery>) -> Json<Value> {
    let limit = q.limit.unwrap_or(50).clamp(1, 200);
    let Some(state) = crate::proxy::current_state() else {
        return Json(json!({ "logs": [], "total": 0 }));
    };
    let entries = state.logs_snapshot(limit);
    let total = entries.len();
    Json(json!({ "logs": entries, "total": total }))
}
