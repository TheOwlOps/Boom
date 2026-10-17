//! `POST /api/chat` — one agent turn, JSON or SSE.
//!
//! Request body: `{prompt, strategy?, session_id?, bot_id?}`.
//! Response (JSON): `{reply, session_id, strategy, errors}` — the exact shape
//! the frontend's `parseChatReply` requires.
//! Response (SSE, when `Accept` contains `text/event-stream`): events
//! `status {phase:"thinking"}` → `message {text}` → `done {session_id, strategy, errors}`,
//! or `error {error: "chat turn failed"}`.
//!
//! When no LLM provider is configured (nothing saved via
//! `POST /api/providers/config` yet) the route answers 503
//! [`NOT_CONFIGURED_MESSAGE`] — before the turn lock and before the SSE
//! branch — instead of running a turn.

use std::{
    collections::{HashSet, VecDeque},
    pin::Pin,
    task::{Context, Poll},
};

use axum::{
    extract::State,
    http::{header, HeaderMap},
    response::{sse::Event, IntoResponse, Response, Sse},
    Json,
};
use futures_core::Stream;
use serde_json::{json, Value};
use tokio::sync::OwnedMutexGuard;

use super::super::{
    state::{now_epoch, AppState, BotTools, TurnRequest, TurnResult},
    ApiError, JsonBody,
};
use crate::server::{TurnError, CHAT_STRATEGIES};

/// The single 503 message used both by the pre-turn `is_configured()`
/// check and by the `TurnError::NotConfigured` mapping below: the chat
/// brain has no LLM provider to call. Save one via
/// `POST /api/providers/config` first.
pub(crate) const NOT_CONFIGURED_MESSAGE: &str = "no LLM provider configured";

/// Turn a raw harness failure into a user-friendly message.
///
/// The raw `TurnError::Failed` string is technical (e.g. "LLM API error
/// (HTTP 401): ...") — this maps common cases to actionable guidance while
/// keeping the technical detail as a suffix for debugging. Never includes
/// secrets: provider error messages are already sanitized at construction
/// (see `ProviderError` docs).
pub(crate) fn friendly_turn_error(raw: &str) -> String {
    let lower = raw.to_lowercase();
    // Truncate the raw technical detail so the response stays readable.
    let detail: String = raw.chars().take(300).collect();
    if lower.contains("401") || lower.contains("unauthorized") || lower.contains("invalid api key") || lower.contains("incorrect api key") {
        format!("LLM API key không đúng hoặc hết hạn — kiểm tra lại trong Settings → Providers. Chi tiết: {detail}")
    } else if lower.contains("404") || lower.contains("model_not_found") || lower.contains("does not exist") {
        format!("Model không tồn tại — kiểm tra tên model trong Settings → Providers. Chi tiết: {detail}")
    } else if lower.contains("429") || lower.contains("rate limit") || lower.contains("too many requests") {
        format!("Bị giới hạn tốc độ (rate limit) — đợi một lúc rồi thử lại. Chi tiết: {detail}")
    } else if lower.contains("timed out") || lower.contains("timeout") {
        format!("LLM quá lâu không trả lời (timeout) — thử lại. Chi tiết: {detail}")
    } else if lower.contains("connection") || lower.contains("dns") || lower.contains("network") {
        format!("Lỗi kết nối tới LLM provider — kiểm tra mạng. Chi tiết: {detail}")
    } else if lower.contains("missing api key") {
        format!("Thiếu API key — cấu hình trong Settings → Providers. Chi tiết: {detail}")
    } else if lower.contains("llm provider unavailable") {
        format!("Không gọi được LLM provider — kiểm tra cấu hình trong Settings → Providers. Chi tiết: {detail}")
    } else {
        format!("Chat thất bại. Chi tiết: {detail}")
    }
}

fn sse_event(name: &str, payload: &Value) -> Event {
    Event::default()
        .event(name)
        .data(serde_json::to_string(payload).unwrap_or_default())
}

/// Resolve `(system_prompt, tools)` for a bot id, mirroring Python's
/// `_resolve_bot_context`. Unknown id → 404 `"unknown bot_id: '...'"`.
pub(crate) fn resolve_bot_context(
    state: &AppState,
    bot_id: Option<&str>,
) -> Result<(Option<String>, BotTools), ApiError> {
    let bot_id = match bot_id {
        Some(id) if !id.is_empty() => id,
        _ => return Ok((None, BotTools::All)),
    };
    let bot = state
        .storage
        .get_bot(bot_id)
        .ok_or_else(|| ApiError::not_found(format!("unknown bot_id: '{bot_id}'")))?;
    let system_prompt = if bot.system_prompt.is_empty() {
        None
    } else {
        Some(bot.system_prompt.clone())
    };
    let denied: HashSet<&str> = bot.denied_tools.iter().map(String::as_str).collect();
    let tools = if !bot.allowed_tools.is_empty() {
        BotTools::Allow(
            bot.allowed_tools
                .iter()
                .filter(|t| !denied.contains(t.as_str()))
                .cloned()
                .collect(),
        )
    } else if !bot.denied_tools.is_empty() {
        BotTools::DenyOnly(bot.denied_tools.clone())
    } else {
        BotTools::All
    };
    Ok((system_prompt, tools))
}

pub async fn chat(
    State(state): State<AppState>,
    headers: HeaderMap,
    JsonBody(body): JsonBody,
) -> Result<Response, ApiError> {
    // -- validate ---------------------------------------------------------
    let prompt = match body.get("prompt").and_then(|v| v.as_str()) {
        Some(p) if !p.trim().is_empty() => p.to_string(),
        _ => return Err(ApiError::bad_request("prompt is required")),
    };
    let strategy = body
        .get("strategy")
        .and_then(|v| v.as_str())
        .unwrap_or("auto");
    if !CHAT_STRATEGIES.contains(&strategy) {
        return Err(ApiError::bad_request(format!(
            "strategy must be one of: {}",
            CHAT_STRATEGIES.join(", ")
        )));
    }
    let strategy = strategy.to_string();
    let session_id = match body.get("session_id").and_then(|v| v.as_str()) {
        Some(s) if !s.is_empty() => s.to_string(),
        _ => format!("session_{}", now_epoch()),
    };
    let bot_id: Option<String> = match body.get("bot_id") {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) => Some(s.clone()),
        Some(_) => return Err(ApiError::bad_request("bot_id must be a string")),
    };
    let (bot_system_prompt, bot_tools) =
        resolve_bot_context(&state, bot_id.as_deref())?;

    // -- session + brain ---------------------------------------------------
    state
        .harness
        .load_session(&session_id)
        .map_err(|e| ApiError::new(axum::http::StatusCode::INTERNAL_SERVER_ERROR, format!("failed to load session: {e}")))?;
    if !state.harness.is_configured() {
        // No provider configured → 503, checked before the lock (mirrors
        // Python's `brain is None` branch, with the message naming the
        // actual missing piece: the provider, not the brain).
        return Err(ApiError::new(
            axum::http::StatusCode::SERVICE_UNAVAILABLE,
            NOT_CONFIGURED_MESSAGE,
        ));
    }

    // -- serialize turns: a second concurrent turn gets 429 ---------------
    let guard = state
        .turn
        .clone()
        .try_lock_owned()
        .map_err(|_| ApiError::new(axum::http::StatusCode::TOO_MANY_REQUESTS, "agent busy"))?;

    // Record the session row (idempotent) so `GET /api/sessions` lists the
    // real conversations instead of always answering empty. Placed after the
    // 503/429 gates: only turns that actually run create sessions.
    state.storage.touch_session(&session_id);

    let req = TurnRequest {
        prompt,
        strategy: strategy.clone(),
        session_id: session_id.clone(),
        system_prompt: bot_system_prompt,
        tools: bot_tools,
    };

    let wants_sse = headers
        .get(header::ACCEPT)
        .and_then(|v| v.to_str().ok())
        .map(|v| v.contains("text/event-stream"))
        .unwrap_or(false);

    if wants_sse {
        let stream = ChatSseStream::new(state, guard, req, session_id, strategy);
        Ok(Sse::new(stream).into_response())
    } else {
        let result = state.harness.run_turn(req).await.map_err(|e| match e {
            TurnError::NotConfigured => ApiError::new(
                axum::http::StatusCode::SERVICE_UNAVAILABLE,
                NOT_CONFIGURED_MESSAGE,
            ),
            TurnError::Failed(msg) => {
                state.api_log(format!("chat turn failed: {msg}"));
                ApiError::new(
                    axum::http::StatusCode::INTERNAL_SERVER_ERROR,
                    friendly_turn_error(&msg),
                )
            }
        })?;
        drop(guard);
        let used_strategy = if result.strategy.is_empty() {
            strategy
        } else {
            result.strategy
        };
        Ok(Json(json!({
            "reply": result.reply,
            "session_id": session_id,
            "strategy": used_strategy,
            "errors": result.errors,
        }))
        .into_response())
    }
}

// ---------------------------------------------------------------------------
// SSE stream: `status` is emitted immediately, then the turn runs; afterwards
// `message` + `done` (or a single `error`). The turn lock is held until the
// stream ends, mirroring Python's `finally: turn_lock.release()`.
// ---------------------------------------------------------------------------

struct ChatSseStream {
    started: bool,
    finished: bool,
    turn: Option<crate::server::BoxFuture<'static, Result<TurnResult, TurnError>>>,
    guard: Option<OwnedMutexGuard<()>>,
    events: VecDeque<Result<Event, axum::Error>>,
    session_id: String,
    strategy: String,
}

impl ChatSseStream {
    fn new(
        state: AppState,
        guard: OwnedMutexGuard<()>,
        req: TurnRequest,
        session_id: String,
        strategy: String,
    ) -> Self {
        let harness = state.harness.clone();
        let turn = Box::pin(async move { harness.run_turn(req).await })
            as crate::server::BoxFuture<'static, Result<TurnResult, TurnError>>;
        Self {
            started: false,
            finished: false,
            turn: Some(turn),
            guard: Some(guard),
            events: VecDeque::new(),
            session_id,
            strategy,
        }
    }
}

impl Stream for ChatSseStream {
    type Item = Result<Event, axum::Error>;

    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let this = self.get_mut();
        loop {
            if this.finished {
                return Poll::Ready(None);
            }
            if !this.started {
                this.started = true;
                return Poll::Ready(Some(Ok(sse_event(
                    "status",
                    &json!({ "phase": "thinking" }),
                ))));
            }
            if let Some(ev) = this.events.pop_front() {
                if this.events.is_empty() && this.turn.is_none() {
                    this.finished = true;
                    this.guard = None; // release the turn lock
                }
                return Poll::Ready(Some(ev));
            }
            match this.turn.as_mut() {
                Some(turn) => match turn.as_mut().poll(cx) {
                    Poll::Ready(result) => {
                        this.turn = None;
                        match result {
                            Ok(r) => {
                                let used = if r.strategy.is_empty() {
                                    this.strategy.clone()
                                } else {
                                    r.strategy.clone()
                                };
                                this.events.push_back(Ok(sse_event(
                                    "message",
                                    &json!({ "text": r.reply }),
                                )));
                                this.events.push_back(Ok(sse_event(
                                    "done",
                                    &json!({ "session_id": this.session_id, "strategy": used, "errors": r.errors }),
                                )));
                            }
                            Err(e) => {
                                let msg = match e {
                                    TurnError::NotConfigured => NOT_CONFIGURED_MESSAGE.to_string(),
                                    TurnError::Failed(m) => friendly_turn_error(&m),
                                };
                                this.events.push_back(Ok(sse_event(
                                    "error",
                                    &json!({ "error": msg }),
                                )));
                            }
                        }
                        // Loop around to emit the first queued event.
                    }
                    Poll::Pending => return Poll::Pending,
                },
                None => {
                    this.finished = true;
                    this.guard = None;
                    return Poll::Ready(None);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::friendly_turn_error;

    #[test]
    fn friendly_error_401_mentions_api_key() {
        let msg = friendly_turn_error("LLM API error (HTTP 401): invalid api key");
        assert!(msg.contains("API key"), "got: {msg}");
        assert!(msg.contains("Settings"), "got: {msg}");
    }

    #[test]
    fn friendly_error_404_mentions_model() {
        let msg = friendly_turn_error("LLM API error (HTTP 404): model does not exist");
        assert!(msg.contains("Model"), "got: {msg}");
    }

    #[test]
    fn friendly_error_429_mentions_rate_limit() {
        let msg = friendly_turn_error("LLM API error (HTTP 429): rate limit exceeded");
        assert!(msg.contains("giới hạn tốc độ"), "got: {msg}");
    }

    #[test]
    fn friendly_error_timeout() {
        let msg = friendly_turn_error("request timed out");
        assert!(msg.contains("timeout"), "got: {msg}");
    }

    #[test]
    fn friendly_error_unknown_keeps_detail() {
        let msg = friendly_turn_error("something weird happened");
        assert!(msg.contains("something weird happened"), "got: {msg}");
    }

    #[test]
    fn friendly_error_truncates_long_detail() {
        let long = "x".repeat(1000);
        let msg = friendly_turn_error(&long);
        assert!(msg.len() < 600, "too long: {}", msg.len());
    }
}
