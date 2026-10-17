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
    state::{AppState, Bot, BotGroup, TurnRequest},
    ApiError, JsonBody, StorageError,
};
use super::chat::{resolve_bot_context, NOT_CONFIGURED_MESSAGE};
use crate::groups::{
    chief_system_prompt, parse_handoffs, resolve_handoff_target, roster_line,
    specialist_group_prompt, strip_handoff_lines,
};
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

/// Run one harness turn as `bot` inside `group_id`, with an (already
/// augmented) system prompt. Uses the per-bot-per-group session
/// `botmsg_<group>_<bot>` so each bot keeps isolated memory.
async fn run_group_bot_turn(
    state: &AppState,
    group_id: &str,
    bot: &Bot,
    prompt: String,
    system_prompt: Option<String>,
) -> Result<String, ApiError> {
    let (_, tools) = resolve_bot_context(state, Some(&bot.id))?;
    let session_id = format!("botmsg_{group_id}_{}", bot.id);
    state.storage.touch_session(&session_id);
    let req = TurnRequest {
        prompt,
        strategy: "auto".to_string(),
        session_id,
        system_prompt,
        tools,
    };
    state
        .harness
        .run_turn(req)
        .await
        .map(|r| r.reply)
        .map_err(turn_error)
}

/// Build the member roster text for prompts: one line per bot with its
/// role description, marking the chief.
fn build_roster(state: &AppState, group: &BotGroup, chief_id: &str) -> String {
    group
        .bot_ids
        .iter()
        .filter_map(|id| state.storage.get_bot(id))
        .map(|b| roster_line(&b.id, &b.name, &b.description, b.id == chief_id))
        .collect::<Vec<_>>()
        .join("\n")
}

/// Persist a bot reply to the group's log and return its JSON shape.
fn persist_reply(
    state: &AppState,
    group_id: &str,
    bot: &Bot,
    text: &str,
) -> Result<Value, ApiError> {
    state
        .storage
        .add_bot_group_message(group_id, &bot.id, &bot.name, text)
        .map_err(storage_error)?;
    Ok(json!({
        "bot_id": bot.id,
        "bot_name": bot.name,
        "reply": text,
    }))
}

/// Invoke a handoff target: run the specialist's turn on `task`, persist
/// the stripped reply, and return `(reply_json, nested_handoffs)`.
/// Nested handoffs are parsed but NOT auto-executed (one level only —
/// prevents infinite delegation loops).
async fn invoke_handoff(
    state: &AppState,
    group: &BotGroup,
    chief_id: &str,
    target_bot_id: &str,
    task: &str,
    sender_name: &str,
) -> Result<(Value, Vec<crate::groups::Handoff>), ApiError> {
    let bot = state
        .storage
        .get_bot(target_bot_id)
        .ok_or_else(|| ApiError::not_found(format!("unknown bot_id: '{target_bot_id}'")))?;
    let roster = build_roster(state, group, chief_id);
    let (base_prompt, _) = resolve_bot_context(state, Some(&bot.id))?;
    let system_prompt = Some(specialist_group_prompt(
        &bot.name,
        &group.name,
        &roster,
        base_prompt.as_deref().unwrap_or(""),
    ));
    let prompt = format!(
        "{sender_name} (via chief delegation) asks you to do this task:\n\n{task}\n\nDo it now. If the task truly belongs to a teammate's expertise, you may hand it off with [HANDOFF:@name] at the end."
    );
    let raw = run_group_bot_turn(state, &group.id, &bot, prompt, system_prompt).await?;
    let nested = parse_handoffs(&raw);
    let visible = strip_handoff_lines(&raw);
    let reply_json = persist_reply(state, &group.id, &bot, &visible)?;
    Ok((reply_json, nested))
}

/// `POST /api/bot-groups/{id}/message` — body `{text, from_bot_id?}`.
///
/// **Chief-of-Staff flow** (user message, no `from_bot_id`):
/// 1. The chief triages the request with the full team roster.
/// 2. The chief either answers directly or delegates via
///    `[HANDOFF:@name] task` directives.
/// 3. Each delegated specialist runs a real harness turn; its reply is
///    persisted and returned.
/// 4. When specialists were involved, the chief synthesizes one final
///    answer from their results.
///
/// **Fan-out flow** (a bot sent the message): every other member replies
/// in group order (legacy behavior), each with the team roster in context
/// so they can hand off via `[HANDOFF:@name]` (one level deep).
///
/// Body `{text, from_bot_id?}` → 200 `{ok: true, replies: [...],
/// chief_reply?}`. Blank `text` → 400, unknown group/sender → 404, no LLM
/// provider → 503, agent busy → 429, failed turn → 500. Replies are never
/// faked.
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

    if from_bot_id.is_none() {
        let resp = chief_triage_flow(&state, &group, &text).await?;
        drop(_guard);
        return Ok(Json(resp));
    }

    // Legacy fan-out: a member bot spoke; every other member replies in
    // group order, each seeing the roster so it can hand off.
    let chief_id = crate::storage::BotGroupStore::effective_chief(&group).unwrap_or_default();
    let roster = build_roster(&state, &group, &chief_id);
    let mut replies = Vec::new();
    for bot_id in group.bot_ids.iter().filter(|id| Some(*id) != from_bot_id.as_ref()) {
        let bot = state
            .storage
            .get_bot(bot_id)
            .ok_or_else(|| ApiError::not_found(format!("unknown bot_id: '{bot_id}'")))?;
        let (base_prompt, _) = resolve_bot_context(&state, Some(bot_id))?;
        let system_prompt = Some(specialist_group_prompt(
            &bot.name,
            &group.name,
            &roster,
            base_prompt.as_deref().unwrap_or(""),
        ));
        let prompt = format!(
            "You are chatting in the group \"{}\". {} says:\n\n{}\n\nReply as yourself, briefly and in character.",
            group.name, sender_name, text
        );
        let raw = run_group_bot_turn(&state, &group.id, &bot, prompt, system_prompt).await?;
        // One level of handoff: a replier may delegate to a teammate.
        let handoffs = parse_handoffs(&raw);
        let visible = strip_handoff_lines(&raw);
        replies.push(persist_reply(&state, &group.id, &bot, &visible)?);
        let roster_pairs: Vec<(String, String)> = group
            .bot_ids
            .iter()
            .filter_map(|id| state.storage.get_bot(id))
            .map(|b| (b.id.clone(), b.name.clone()))
            .collect();
        for h in handoffs {
            if let Some(target_id) =
                resolve_handoff_target(&h.target_name, &roster_pairs)
            {
                if target_id == bot.id {
                    continue; // never hand off to yourself
                }
                let (rj, _) =
                    invoke_handoff(&state, &group, &chief_id, &target_id, &h.task, &bot.name)
                        .await?;
                replies.push(rj);
            }
        }
    }
    drop(_guard);
    Ok(Json(json!({ "ok": true, "replies": replies })))
}

/// Chief-of-Staff triage for a user message:
/// chief analyzes → delegates via handoffs → specialists run →
/// chief synthesizes the final answer.
async fn chief_triage_flow(
    state: &AppState,
    group: &BotGroup,
    text: &str,
) -> Result<Value, ApiError> {
    use crate::storage::BotGroupStore;

    let chief_id = BotGroupStore::effective_chief(group)
        .ok_or_else(|| ApiError::bad_request("group has no bots to answer"))?;
    let chief = state
        .storage
        .get_bot(&chief_id)
        .ok_or_else(|| ApiError::not_found(format!("unknown bot_id: '{chief_id}'")))?;
    let roster = build_roster(state, group, &chief_id);
    let roster_pairs: Vec<(String, String)> = group
        .bot_ids
        .iter()
        .filter_map(|id| state.storage.get_bot(id))
        .map(|b| (b.id.clone(), b.name.clone()))
        .collect();

    // 1. Chief triage turn.
    let (base_prompt, _) = resolve_bot_context(state, Some(&chief.id))?;
    let chief_prompt = chief_system_prompt(
        &chief.name,
        &group.name,
        &roster,
        base_prompt.as_deref().unwrap_or(""),
    );
    let triage_raw = run_group_bot_turn(
        state,
        &group.id,
        &chief,
        format!("The user says in group \"{}\":\n\n{}\n\nTriage this now.", group.name, text),
        Some(chief_prompt),
    )
    .await?;
    let handoffs = parse_handoffs(&triage_raw);
    let chief_visible = strip_handoff_lines(&triage_raw);

    // 2. Delegate to specialists (dedupe targets, skip self-handoff).
    let mut seen_targets = std::collections::HashSet::new();
    let mut replies = Vec::new();
    let mut specialist_results: Vec<(String, String)> = Vec::new();
    for h in &handoffs {
        let Some(target_id) = resolve_handoff_target(&h.target_name, &roster_pairs) else {
            continue; // unknown @name — ignore rather than fail the turn
        };
        if target_id == chief.id || !seen_targets.insert(target_id.clone()) {
            continue;
        }
        let (rj, _nested) =
            invoke_handoff(state, group, &chief_id, &target_id, &h.task, "user").await?;
        if let (Some(name), Some(reply)) = (
            rj.get("bot_name").and_then(|v| v.as_str()),
            rj.get("reply").and_then(|v| v.as_str()),
        ) {
            specialist_results.push((name.to_string(), reply.to_string()));
        }
        replies.push(rj);
    }

    // 3. Final answer: synthesize when specialists ran, else the chief's
    //    own (stripped) reply.
    let final_answer = if specialist_results.is_empty() {
        chief_visible.clone()
    } else {
        let results_text = specialist_results
            .iter()
            .map(|(name, reply)| format!("--- {name} ---\n{reply}"))
            .collect::<Vec<_>>()
            .join("\n\n");
        let (base_prompt, _) = resolve_bot_context(state, Some(&chief.id))?;
        let synth_prompt = format!(
            "You are the CHIEF OF STAFF of \"{}\". You delegated parts of the user's request to specialists and now have their results.\n\nUSER REQUEST:\n{}\n\nSPECIALIST RESULTS:\n{}\n\nWrite ONE consolidated final answer for the user: merge the results, resolve conflicts, keep it concise and in character. Do not mention handoffs or internal routing.",
            group.name, text, results_text
        );
        run_group_bot_turn(
            state,
            &group.id,
            &chief,
            synth_prompt,
            base_prompt,
        )
        .await?
    };
    let _chief_json = persist_reply(state, &group.id, &chief, &final_answer)?;

    Ok(json!({
        "ok": true,
        "chief_id": chief.id,
        "chief_name": chief.name,
        "chief_reply": final_answer,
        "replies": replies,
        "delegated": !specialist_results.is_empty(),
    }))
}

/// `PUT /api/bot-groups/{id}/chief` — body `{bot_id}` → 200
/// `{ok: true, group}`. Designates the Chief-of-Staff bot. Missing group →
/// 404, unknown bot or non-member → 404, missing `bot_id` → 400.
pub async fn set_group_chief(
    State(state): State<AppState>,
    Path(group_id): Path<String>,
    JsonBody(body): JsonBody,
) -> Result<Json<Value>, ApiError> {
    let bot_id = match body.get("bot_id").and_then(|v| v.as_str()) {
        Some(id) if !id.trim().is_empty() => id.trim().to_string(),
        _ => return Err(ApiError::bad_request("bot_id is required")),
    };
    match state.storage.set_group_chief(&group_id, &bot_id) {
        Ok(group) => Ok(Json(json!({ "ok": true, "group": group }))),
        Err(e) => Err(storage_error(e)),
    }
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

        // No sender (user message): chief triage flow. EchoHarness emits no
        // handoffs, so the chief answers directly with no delegation.
        let Json(resp) = post_group_message(
            State(state.clone()),
            Path(gid.clone()),
            body(&[("text", json!("again"))]),
        )
        .await
        .unwrap();
        assert_eq!(resp["ok"], json!(true));
        assert_eq!(resp["delegated"], json!(false));
        // Chief is "One" (first member).
        assert_eq!(resp["chief_name"], json!("One"));
        assert!(resp["chief_reply"].as_str().unwrap().starts_with("echo from turn:"));
        let replies = resp["replies"].as_array().unwrap();
        assert!(replies.is_empty());
        // Persisted: prior 2 + user msg + chief reply = 4.
        assert_eq!(state.storage.list_bot_group_messages(&gid).len(), 4);
    }

    #[tokio::test]
    async fn chief_delegates_via_handoff() {
        // Harness whose triage reply contains a handoff to "Two".
        struct HandoffHarness;
        impl Harness for HandoffHarness {
            fn is_configured(&self) -> bool {
                true
            }
            fn run_turn(&self, req: TurnRequest) -> BoxFuture<'_, Result<TurnResult, TurnError>> {
                let reply = if req.prompt.contains("Triage this now") {
                    "I'll have Two handle it.\n[HANDOFF:@Two] do the task".to_string()
                } else if req.prompt.contains("consolidated final answer") {
                    "Final: done.".to_string()
                } else {
                    format!("specialist did: {}", req.prompt.chars().take(20).collect::<String>())
                };
                Box::pin(async move {
                    Ok(TurnResult {
                        reply,
                        strategy: "auto".to_string(),
                        errors: Map::new(),
                    })
                })
            }
        }
        let (state, b1, b2, gid) = state_with_group(Arc::new(HandoffHarness));
        let Json(resp) = post_group_message(
            State(state.clone()),
            Path(gid.clone()),
            body(&[("text", json!("please do the thing"))]),
        )
        .await
        .unwrap();
        assert_eq!(resp["ok"], json!(true));
        assert_eq!(resp["delegated"], json!(true));
        assert_eq!(resp["chief_id"], json!(b1));
        // One specialist reply from Two.
        let replies = resp["replies"].as_array().unwrap();
        assert_eq!(replies.len(), 1);
        assert_eq!(replies[0]["bot_id"], json!(b2));
        // Handoff directive stripped from the visible reply.
        assert!(!replies[0]["reply"].as_str().unwrap().contains("[HANDOFF:"));
        // Chief synthesized the final answer.
        assert_eq!(resp["chief_reply"], json!("Final: done."));
        // Persisted: user msg + specialist reply + chief final = 3.
        assert_eq!(state.storage.list_bot_group_messages(&gid).len(), 3);
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
