//! Bot-group routes (multi-bot chat rooms):
//! `GET`/`POST /api/bot-groups`, `GET`/`DELETE /api/bot-groups/{id}`,
//! `POST /api/bot-groups/{id}/bots`, `DELETE /api/bot-groups/{id}/bots/{bot_id}`,
//! `POST /api/bot-groups/{id}/message`.
//!
//! The message endpoint fans a message out to every bot in the group (except
//! the sender): each bot generates its reply through the real harness turn,
//! in group order, and every message — the incoming one and each reply — is
//! persisted to the group's log. Replies are never faked: an unconfigured
//! harness answers 503, a busy agent 429, a failed turn 500.

use axum::{
    extract::{Path, State},
    http::StatusCode,
    Json,
};
use serde_json::{json, Value};

use super::super::{
    normalize_str_list,
    state::{AppState, TurnRequest},
    ApiError, JsonBody, StorageError,
};
use super::chat::{resolve_bot_context, NOT_CONFIGURED_MESSAGE};
use crate::server::TurnError;

fn storage_error(e: StorageError) -> ApiError {
    match e {
        StorageError::NotFound(msg) => ApiError::not_found(msg),
        StorageError::Invalid(msg) => ApiError::bad_request(msg),
        StorageError::Internal(_) => {
            ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "bot group storage failed")
        }
    }
}

fn turn_error(e: TurnError) -> ApiError {
    match e {
        TurnError::NotConfigured => ApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            NOT_CONFIGURED_MESSAGE,
        ),
        TurnError::Failed(_) => {
            ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "chat turn failed")
        }
    }
}

/// `POST /api/bot-groups` — body `{name, bot_ids?}` → 201 `{ok: true, group}`.
/// Blank name → 400 `"name is required"`; unknown bot id → 404
/// `"unknown bot_id: '...'"`.
pub async fn create_bot_group(
    State(state): State<AppState>,
    JsonBody(body): JsonBody,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let name = match body.get("name").and_then(|v| v.as_str()) {
        Some(n) if !n.trim().is_empty() => n.trim().to_string(),
        _ => return Err(ApiError::bad_request("name is required")),
    };
    let bot_ids = match body.get("bot_ids") {
        None | Some(Value::Null) => Vec::new(),
        Some(v) => normalize_str_list(v, "bot_ids").map_err(ApiError::bad_request)?,
    };
    match state.storage.create_bot_group(&name, bot_ids) {
        Ok(group) => Ok((StatusCode::CREATED, Json(json!({ "ok": true, "group": group })))),
        Err(e) => Err(storage_error(e)),
    }
}

/// `GET /api/bot-groups` → `{groups, count}`.
pub async fn list_bot_groups(State(state): State<AppState>) -> Json<Value> {
    let groups = state.storage.list_bot_groups();
    Json(json!({ "groups": groups, "count": groups.len() }))
}

/// `GET /api/bot-groups/{id}` → 200 `{ok: true, group, messages}`;
/// missing → 404.
pub async fn get_bot_group(
    State(state): State<AppState>,
    Path(group_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let group = state
        .storage
        .get_bot_group(&group_id)
        .ok_or_else(|| ApiError::not_found(format!("bot group not found: '{group_id}'")))?;
    let messages = state.storage.list_bot_group_messages(&group_id);
    Ok(Json(json!({ "ok": true, "group": group, "messages": messages })))
}

/// `DELETE /api/bot-groups/{id}` → 200 `{ok: true}` (also deletes the
/// group's messages); missing → 404.
pub async fn delete_bot_group(
    State(state): State<AppState>,
    Path(group_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    if !state.storage.delete_bot_group(&group_id) {
        return Err(ApiError::not_found(format!(
            "bot group not found: '{group_id}'"
        )));
    }
    Ok(Json(json!({ "ok": true })))
}

/// `POST /api/bot-groups/{id}/bots` — body `{bot_id}` → 200
/// `{ok: true, group}`. Missing group → 404, unknown bot → 404,
/// missing `bot_id` → 400. Adding a bot that is already a member is a
/// no-op success.
pub async fn add_bot_to_group(
    State(state): State<AppState>,
    Path(group_id): Path<String>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    let bot_id = match body.get("bot_id").and_then(|v| v.as_str()) {
        Some(id) if !id.trim().is_empty() => id.trim().to_string(),
        _ => return Err(ApiError::bad_request("bot_id is required")),
    };
    match state.storage.add_bot_to_group(&group_id, &bot_id) {
        Ok(group) => Ok(Json(json!({ "ok": true, "group": group }))),
        Err(e) => Err(storage_error(e)),
    }
}

/// `DELETE /api/bot-groups/{id}/bots/{bot_id}` → 200 `{ok: true, group}`.
/// Missing group → 404; bot not a member → 404.
pub async fn remove_bot_from_group(
    State(state): State<AppState>,
    Path((group_id, bot_id)): Path<(String, String)>,
) -> Result<Json<Value>, ApiError> {
    match state.storage.remove_bot_from_group(&group_id, &bot_id) {
        Ok(group) => Ok(Json(json!({ "ok": true, "group": group }))),
        Err(e) => Err(storage_error(e)),
    }
}

/// `POST /api/bot-groups/{id}/message` — body `{text, from_bot_id?}`.
///
/// Fans the message out to every bot in the group except the sender, in
/// group order. Each bot's reply is a real harness turn (its own bot
/// context: system prompt + tool allowlist, session
/// `botmsg_<group>_<bot>`). Returns 200
/// `{ok: true, replies: [{bot_id, bot_name, reply}]}`.
///
/// The incoming message and every reply are persisted to the group's log —
/// but only once the turn lock is held, so a 503/429 leaves no partial
/// state. `text` blank → 400, unknown group or sender → 404, no LLM
/// provider → 503, agent busy → 429, failed turn → 500.
pub async fn post_group_message(
    State(state): State<AppState>,
    Path(group_id): Path<String>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    let text = match body.get("text").and_then(|v| v.as_str()) {
        Some(t) if !t.trim().is_empty() => t.trim().to_string(),
        _ => return Err(ApiError::bad_request("text is required")),
    };
    let group = state
        .storage
        .get_bot_group(&group_id)
        .ok_or_else(|| ApiError::not_found(format!("bot group not found: '{group_id}'")))?;
    let from_bot_id: Option<String> = match body.get("from_bot_id") {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) => Some(s.clone()),
        Some(_) => return Err(ApiError::bad_request("from_bot_id must be a string")),
    };
    // Validate the sender up front; an unknown bot id is a 404 even when
    // it is not a group member (the sender may be an external bot).
    let (sender_id, sender_name) = match &from_bot_id {
        Some(id) => {
            let bot = state
                .storage
                .get_bot(id)
                .ok_or_else(|| ApiError::not_found(format!("unknown bot_id: '{id}'")))?;
            (id.clone(), bot.name.clone())
        }
        None => (String::new(), "user".to_string()),
    };

    if !state.harness.is_configured() {
        return Err(ApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            NOT_CONFIGURED_MESSAGE,
        ));
    }
    // Serialize against other turns: a second concurrent request gets 429.
    let _guard = state
        .turn
        .clone()
        .try_lock_owned()
        .map_err(|_| ApiError::new(StatusCode::TOO_MANY_REQUESTS, "agent busy"))?;

    // Persist the incoming message now that the turn is actually running.
    state
        .storage
        .add_bot_group_message(&group.id, &sender_id, &sender_name, &text)
        .map_err(storage_error)?;

    let mut replies = Vec::new();
    for bot_id in group.bot_ids.iter().filter(|id| Some(*id) != from_bot_id.as_ref()) {
        let bot = state
            .storage
            .get_bot(bot_id)
            .ok_or_else(|| ApiError::not_found(format!("unknown bot_id: '{bot_id}'")))?;
        let (system_prompt, tools) = resolve_bot_context(&state, Some(bot_id))?;
        let session_id = format!("botmsg_{}_{}", group.id, bot_id);
        state.storage.touch_session(&session_id);
        let req = TurnRequest {
            prompt: format!(
                "You are chatting in the group \"{}\". {} says:\n\n{}\n\nReply as yourself, briefly and in character.",
                group.name, sender_name, text
            ),
            strategy: "auto".to_string(),
            session_id,
            system_prompt,
            tools,
        };
        let result = state.harness.run_turn(req).await.map_err(turn_error)?;
        state
            .storage
            .add_bot_group_message(&group.id, bot_id, &bot.name, &result.reply)
            .map_err(storage_error)?;
        replies.push(json!({
            "bot_id": bot_id,
            "bot_name": bot.name,
            "reply": result.reply,
        }));
    }
    drop(_guard);
    Ok(Json(json!({ "ok": true, "replies": replies })))
}

#[cfg(test)]
mod tests {
    use std::collections::VecDeque;
    use std::sync::{Arc, Mutex};
    use std::time::Instant;

    use axum::extract::State;
    use axum::http::StatusCode;
    use serde_json::{json, Map, Value};

    use super::super::bots::message_bot;
    use super::*;
    use crate::server::channels::FileChannelRegistry;
    use crate::server::defaults::{
        EmptyToolRegistry, GithubUpdater, MemoryStorage, NoHarness, NoZalo, StaticProviderRegistry,
    };
    use crate::server::state::{NewBot, TurnResult};
    use crate::server::{BoxFuture, Harness};

    /// Test double: configured, answers every turn with a canned echo.
    /// Production never fakes replies — this only exercises the route
    /// plumbing (fan-out order, persistence, error mapping).
    struct EchoHarness;

    impl Harness for EchoHarness {
        fn is_configured(&self) -> bool {
            true
        }

        fn run_turn(&self, req: TurnRequest) -> BoxFuture<'_, Result<TurnResult, TurnError>> {
            let reply = format!("echo from turn: {}", req.prompt.chars().take(24).collect::<String>());
            Box::pin(async move {
                Ok(TurnResult {
                    reply,
                    strategy: "auto".to_string(),
                    errors: Map::new(),
                })
            })
        }
    }

    fn test_state(harness: Arc<dyn Harness>) -> AppState {
        AppState {
            harness,
            tools: Arc::new(EmptyToolRegistry),
            providers: Arc::new(StaticProviderRegistry::default()),
            storage: Arc::new(MemoryStorage::new()),
            updater: Arc::new(GithubUpdater::new("test")),
            zalo: Arc::new(NoZalo),
            channels: Arc::new(FileChannelRegistry::new()),
            version: "test".to_string(),
            start: Instant::now(),
            skills_dir: None,
            log: Arc::new(Mutex::new(VecDeque::new())),
            turn: Arc::new(tokio::sync::Mutex::new(())),
            daemon_wired: false,
        }
    }

    fn new_bot(name: &str) -> NewBot {
        NewBot {
            name: name.to_string(),
            description: String::new(),
            system_prompt: format!("You are {name}."),
            avatar_emoji: "🤖".to_string(),
            allowed_tools: None,
            denied_tools: None,
            channel_bindings: None,
        }
    }

    fn body(pairs: &[(&str, Value)]) -> JsonBody {
        let mut map = Map::new();
        for (k, v) in pairs {
            map.insert((*k).to_string(), v.clone());
        }
        JsonBody(map)
    }

    /// State with two bots ("One", "Two") and a group containing both.
    fn state_with_group(harness: Arc<dyn Harness>) -> (AppState, String, String, String) {
        let state = test_state(harness);
        let b1 = state.storage.create_bot(new_bot("One")).unwrap();
        let b2 = state.storage.create_bot(new_bot("Two")).unwrap();
        let g = state
            .storage
            .create_bot_group("Team", vec![b1.id.clone(), b2.id.clone()])
            .unwrap();
        (state, b1.id, b2.id, g.id)
    }

    #[tokio::test]
    async fn create_rejects_blank_name() {
        let state = test_state(Arc::new(NoHarness));
        let err = create_bot_group(State(state), body(&[("name", json!("  "))]))
            .await
            .unwrap_err();
        assert_eq!(err.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn create_rejects_unknown_bot_id() {
        let state = test_state(Arc::new(NoHarness));
        let err = create_bot_group(
            State(state),
            body(&[("name", json!("Team")), ("bot_ids", json!(["ghost"]))]),
        )
        .await
        .unwrap_err();
        assert_eq!(err.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn create_rejects_non_list_bot_ids() {
        let state = test_state(Arc::new(NoHarness));
        let err = create_bot_group(
            State(state),
            body(&[("name", json!("Team")), ("bot_ids", json!("nope"))]),
        )
        .await
        .unwrap_err();
        assert_eq!(err.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn create_ok_returns_201_with_group() {
        let state = test_state(Arc::new(NoHarness));
        let b1 = state.storage.create_bot(new_bot("One")).unwrap();
        let (status, Json(resp)) = create_bot_group(
            State(state),
            body(&[("name", json!("Team")), ("bot_ids", json!([b1.id]))]),
        )
        .await
        .unwrap();
        assert_eq!(status, StatusCode::CREATED);
        assert_eq!(resp["ok"], json!(true));
        assert_eq!(resp["group"]["name"], json!("Team"));
        assert_eq!(resp["group"]["bot_ids"].as_array().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn get_missing_group_is_404() {
        let state = test_state(Arc::new(NoHarness));
        let err = get_bot_group(State(state), Path("nope".to_string()))
            .await
            .unwrap_err();
        assert_eq!(err.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn get_group_returns_group_and_messages() {
        let (state, _, _, gid) = state_with_group(Arc::new(NoHarness));
        state
            .storage
            .add_bot_group_message(&gid, "", "user", "hi")
            .unwrap();
        let Json(resp) = get_bot_group(State(state), Path(gid.clone())).await.unwrap();
        assert_eq!(resp["ok"], json!(true));
        assert_eq!(resp["group"]["id"], json!(gid));
        assert_eq!(resp["messages"].as_array().unwrap().len(), 1);
        assert_eq!(resp["messages"][0]["text"], json!("hi"));
    }

    #[tokio::test]
    async fn delete_missing_group_is_404() {
        let state = test_state(Arc::new(NoHarness));
        let err = delete_bot_group(State(state), Path("nope".to_string()))
            .await
            .unwrap_err();
        assert_eq!(err.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn delete_group_removes_it_and_its_messages() {
        let (state, _, _, gid) = state_with_group(Arc::new(NoHarness));
        state
            .storage
            .add_bot_group_message(&gid, "", "user", "hi")
            .unwrap();
        let Json(resp) = delete_bot_group(State(state.clone()), Path(gid.clone()))
            .await
            .unwrap();
        assert_eq!(resp["ok"], json!(true));
        assert!(state.storage.get_bot_group(&gid).is_none());
        assert!(state.storage.list_bot_group_messages(&gid).is_empty());
    }

    #[tokio::test]
    async fn add_bot_validations() {
        let (state, b1, _, gid) = state_with_group(Arc::new(NoHarness));
        // Missing bot_id → 400.
        let err = add_bot_to_group(State(state.clone()), Path(gid.clone()), body(&[]))
            .await
            .unwrap_err();
        assert_eq!(err.status(), StatusCode::BAD_REQUEST);
        // Unknown bot → 404.
        let err = add_bot_to_group(
            State(state.clone()),
            Path(gid.clone()),
            body(&[("bot_id", json!("ghost"))]),
        )
        .await
        .unwrap_err();
        assert_eq!(err.status(), StatusCode::NOT_FOUND);
        // Unknown group → 404.
        let err = add_bot_to_group(
            State(state.clone()),
            Path("nope".to_string()),
            body(&[("bot_id", json!(b1))]),
        )
        .await
        .unwrap_err();
        assert_eq!(err.status(), StatusCode::NOT_FOUND);
        // Adding an existing member is a no-op success.
        let Json(resp) = add_bot_to_group(
            State(state.clone()),
            Path(gid.clone()),
            body(&[("bot_id", json!(b1))]),
        )
        .await
        .unwrap();
        assert_eq!(resp["ok"], json!(true));
        assert_eq!(resp["group"]["bot_ids"].as_array().unwrap().len(), 2);
    }

    #[tokio::test]
    async fn remove_bot_validations() {
        let (state, b1, _, gid) = state_with_group(Arc::new(NoHarness));
        // Unknown group → 404.
        let err = remove_bot_from_group(
            State(state.clone()),
            Path(("nope".to_string(), b1.clone())),
        )
        .await
        .unwrap_err();
        assert_eq!(err.status(), StatusCode::NOT_FOUND);
        // Bot not a member → 404.
        let other = state.storage.create_bot(new_bot("Other")).unwrap();
        let err = remove_bot_from_group(
            State(state.clone()),
            Path((gid.clone(), other.id.clone())),
        )
        .await
        .unwrap_err();
        assert_eq!(err.status(), StatusCode::NOT_FOUND);
        // Happy path.
        let Json(resp) = remove_bot_from_group(State(state.clone()), Path((gid.clone(), b1.clone())))
            .await
            .unwrap();
        assert_eq!(resp["ok"], json!(true));
        assert_eq!(resp["group"]["bot_ids"].as_array().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn group_message_validations() {
        let (state, b1, _, gid) = state_with_group(Arc::new(EchoHarness));
        // Unknown group → 404.
        let err = post_group_message(
            State(state.clone()),
            Path("nope".to_string()),
            body(&[("text", json!("hi"))]),
        )
        .await
        .unwrap_err();
        assert_eq!(err.status(), StatusCode::NOT_FOUND);
        // Blank text → 400.
        let err = post_group_message(
            State(state.clone()),
            Path(gid.clone()),
            body(&[("text", json!("  "))]),
        )
        .await
        .unwrap_err();
        assert_eq!(err.status(), StatusCode::BAD_REQUEST);
        // Unknown sender → 404.
        let err = post_group_message(
            State(state.clone()),
            Path(gid.clone()),
            body(&[("text", json!("hi")), ("from_bot_id", json!("ghost"))]),
        )
        .await
        .unwrap_err();
        assert_eq!(err.status(), StatusCode::NOT_FOUND);
        // Non-string sender → 400.
        let err = post_group_message(
            State(state.clone()),
            Path(gid.clone()),
            body(&[("text", json!("hi")), ("from_bot_id", json!(42))]),
        )
        .await
        .unwrap_err();
        assert_eq!(err.status(), StatusCode::BAD_REQUEST);
        // Nothing persisted on validation failures.
        assert!(state.storage.list_bot_group_messages(&gid).is_empty());
        let _ = b1;
    }

    #[tokio::test]
    async fn group_message_503_when_harness_unconfigured() {
        let (state, _, _, gid) = state_with_group(Arc::new(NoHarness));
        let err = post_group_message(
            State(state.clone()),
            Path(gid.clone()),
            body(&[("text", json!("hi"))]),
        )
        .await
        .unwrap_err();
        assert_eq!(err.status(), StatusCode::SERVICE_UNAVAILABLE);
        // No partial state: the incoming message is only persisted once the
        // turn actually runs.
        assert!(state.storage.list_bot_group_messages(&gid).is_empty());
    }

    #[tokio::test]
    async fn group_message_429_when_agent_busy() {
        let (state, _, _, gid) = state_with_group(Arc::new(EchoHarness));
        let _held = state.turn.clone().try_lock_owned().expect("hold lock");
        let err = post_group_message(
            State(state.clone()),
            Path(gid.clone()),
            body(&[("text", json!("hi"))]),
        )
        .await
        .unwrap_err();
        assert_eq!(err.status(), StatusCode::TOO_MANY_REQUESTS);
        assert!(state.storage.list_bot_group_messages(&gid).is_empty());
    }

    #[tokio::test]
    async fn group_message_fans_out_in_order_and_persists() {
        let (state, b1, b2, gid) = state_with_group(Arc::new(EchoHarness));
        // From b1: only b2 replies.
        let Json(resp) = post_group_message(
            State(state.clone()),
            Path(gid.clone()),
            body(&[("text", json!("hello")), ("from_bot_id", json!(b1))]),
        )
        .await
        .unwrap();
        assert_eq!(resp["ok"], json!(true));
        let replies = resp["replies"].as_array().unwrap();
        assert_eq!(replies.len(), 1);
        assert_eq!(replies[0]["bot_id"], json!(b2));
        assert_eq!(replies[0]["bot_name"], json!("Two"));
        assert!(replies[0]["reply"].as_str().unwrap().starts_with("echo from turn:"));

        // Persisted: the incoming message + the one reply, oldest first.
        let messages = state.storage.list_bot_group_messages(&gid);
        assert_eq!(messages.len(), 2);
        assert_eq!(messages[0].bot_id, b1);
        assert_eq!(messages[0].bot_name, "One");
        assert_eq!(messages[0].text, "hello");
        assert_eq!(messages[1].bot_id, b2);

        // No sender: both bots reply, in group order.
        let Json(resp) = post_group_message(
            State(state.clone()),
            Path(gid.clone()),
            body(&[("text", json!("again"))]),
        )
        .await
        .unwrap();
        let replies = resp["replies"].as_array().unwrap();
        assert_eq!(replies.len(), 2);
        assert_eq!(replies[0]["bot_id"], json!(b1));
        assert_eq!(replies[1]["bot_id"], json!(b2));
        assert_eq!(state.storage.list_bot_group_messages(&gid).len(), 5);
    }

    #[tokio::test]
    async fn message_bot_validations_and_503() {
        let state = test_state(Arc::new(NoHarness));
        let bot = state.storage.create_bot(new_bot("Solo")).unwrap();
        // Unknown bot → 404.
        let err = message_bot(
            State(state.clone()),
            Path("ghost".to_string()),
            body(&[("text", json!("hi"))]),
        )
        .await
        .unwrap_err();
        assert_eq!(err.status(), StatusCode::NOT_FOUND);
        // Blank text → 400.
        let err = message_bot(
            State(state.clone()),
            Path(bot.id.clone()),
            body(&[("text", json!("  "))]),
        )
        .await
        .unwrap_err();
        assert_eq!(err.status(), StatusCode::BAD_REQUEST);
        // Unconfigured harness → 503.
        let err = message_bot(
            State(state.clone()),
            Path(bot.id.clone()),
            body(&[("text", json!("hi"))]),
        )
        .await
        .unwrap_err();
        assert_eq!(err.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn message_bot_returns_harness_reply() {
        let state = test_state(Arc::new(EchoHarness));
        let bot = state.storage.create_bot(new_bot("Solo")).unwrap();
        let Json(resp) = message_bot(
            State(state.clone()),
            Path(bot.id.clone()),
            body(&[("text", json!("hello")), ("from_name", json!("Son"))]),
        )
        .await
        .unwrap();
        assert_eq!(resp["ok"], json!(true));
        assert_eq!(resp["bot_id"], json!(bot.id));
        assert!(resp["reply"].as_str().unwrap().starts_with("echo from turn:"));
    }

    #[tokio::test]
    async fn message_bot_429_when_agent_busy() {
        let state = test_state(Arc::new(EchoHarness));
        let bot = state.storage.create_bot(new_bot("Solo")).unwrap();
        let _held = state.turn.clone().try_lock_owned().expect("hold lock");
        let err = message_bot(
            State(state.clone()),
            Path(bot.id.clone()),
            body(&[("text", json!("hi"))]),
        )
        .await
        .unwrap_err();
        assert_eq!(err.status(), StatusCode::TOO_MANY_REQUESTS);
    }
}
