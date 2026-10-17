//! Telegram Bot API client: `getMe` verify, `sendMessage`, `getUpdates`
//! polling for ingress.
//!
//! The token travels in the URL path, exactly like every Bot API client.
//! Error messages never contain the token: transport failures degrade to
//! generic text because reqwest errors can echo the request URL.

use std::time::Duration;

use serde_json::Value;

use super::InboundMessage;

/// The real Telegram Bot API base (overridable in tests).
pub const API_BASE: &str = "https://api.telegram.org";
/// Verify timeout (matches the old Python probe style).
const VERIFY_TIMEOUT: Duration = Duration::from_secs(10);
/// Send timeout.
const SEND_TIMEOUT: Duration = Duration::from_secs(35);
/// `getUpdates` short-poll timeout: return immediately with whatever is
/// pending so the HTTP endpoint never hangs.
const POLL_TIMEOUT: Duration = Duration::from_secs(15);

/// Bot identity returned by `getMe`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BotIdentity {
    pub id: i64,
    pub username: Option<String>,
    pub first_name: String,
}

/// Telegram bot tokens look like `123456789:AAE...` — digits, a colon,
/// then the secret part. This is a cheap format pre-check; the real
/// validation is the live `getMe` call.
pub fn valid_bot_token_format(token: &str) -> bool {
    match token.split_once(':') {
        Some((head, tail)) => {
            !head.is_empty()
                && head.bytes().all(|b| b.is_ascii_digit())
                && !tail.trim().is_empty()
        }
        None => false,
    }
}

/// Parse a `getMe` response body. `Ok` → bot identity; `Err` → a
/// human-readable message that never contains the token.
pub fn parse_get_me(body: &Value) -> Result<BotIdentity, String> {
    if body.get("ok").and_then(Value::as_bool).unwrap_or(false) {
        let result = body.get("result").ok_or_else(|| {
            "Telegram returned ok:true without a result".to_string()
        })?;
        let id = result.get("id").and_then(Value::as_i64).ok_or_else(|| {
            "Telegram getMe response missing bot id".to_string()
        })?;
        let first_name = result
            .get("first_name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let username = result
            .get("username")
            .and_then(Value::as_str)
            .map(|s| s.to_string());
        Ok(BotIdentity { id, username, first_name })
    } else {
        Err(describe_api_error(body, "rejected the token"))
    }
}

/// Parse a `sendMessage` response body. `Ok` → the sent message id.
pub fn parse_send_message(body: &Value) -> Result<i64, String> {
    if body.get("ok").and_then(Value::as_bool).unwrap_or(false) {
        body.get("result")
            .and_then(|r| r.get("message_id"))
            .and_then(Value::as_i64)
            .ok_or_else(|| "Telegram sendMessage response missing message_id".to_string())
    } else {
        Err(describe_api_error(body, "refused to send"))
    }
}

/// Shared `{"ok":false,...}` error rendering: `Telegram <action> (HTTP
/// <code>): <description>`. Descriptions come from Telegram, never echo
/// the token.
fn describe_api_error(body: &Value, action: &str) -> String {
    let description = body
        .get("description")
        .and_then(Value::as_str)
        .unwrap_or("unknown error")
        .trim();
    let code = body.get("error_code").and_then(Value::as_i64);
    match code {
        Some(c) => format!("Telegram {action} (HTTP {c}): {description}"),
        None => format!("Telegram {action}: {description}"),
    }
}

/// Parse a `getUpdates` response body into normalized inbound messages.
/// `chat_filter` optionally keeps only one chat id.
pub fn parse_updates(body: &Value, chat_filter: Option<&str>) -> Result<Vec<InboundMessage>, String> {
    if !body.get("ok").and_then(Value::as_bool).unwrap_or(false) {
        return Err(describe_api_error(body, "getUpdates failed"));
    }
    let mut out = Vec::new();
    let updates = body
        .get("result")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    for u in updates {
        let update_id = u
            .get("update_id")
            .and_then(Value::as_i64)
            .map(|n| n.to_string())
            .unwrap_or_default();
        // Accept plain messages and channel posts; skip the rest.
        let msg = u
            .get("message")
            .or_else(|| u.get("channel_post"))
            .or_else(|| u.get("edited_message"));
        let Some(msg) = msg else { continue };
        let chat_id = msg
            .get("chat")
            .and_then(|c| c.get("id"))
            .and_then(Value::as_i64)
            .map(|n| n.to_string())
            .unwrap_or_default();
        if let Some(filter) = chat_filter {
            if chat_id != filter {
                continue;
            }
        }
        let text = msg
            .get("text")
            .and_then(Value::as_str)
            .or_else(|| msg.get("caption").and_then(Value::as_str))
            .unwrap_or("")
            .to_string();
        if text.trim().is_empty() {
            continue;
        }
        let sender = msg
            .get("from")
            .and_then(|f| {
                f.get("username")
                    .and_then(Value::as_str)
                    .map(|s| format!("@{s}"))
                    .or_else(|| {
                        f.get("first_name")
                            .and_then(Value::as_str)
                            .map(|s| s.to_string())
                    })
            });
        let ts = msg
            .get("date")
            .and_then(Value::as_i64)
            .map(|n| n.to_string());
        out.push(InboundMessage {
            id: update_id,
            chat_id,
            sender,
            text,
            ts,
        });
    }
    Ok(out)
}

/// Read a JSON response body; non-JSON or empty bodies degrade to a
/// generic message (a proxy/CDN may answer HTML).
async fn read_json_body(resp: reqwest::Response) -> Result<Value, String> {
    let status = resp.status();
    let text = resp.text().await.map_err(|_| "request failed".to_string())?;
    let text = text.trim();
    if text.is_empty() {
        return Err(format!(
            "Telegram returned HTTP {} with an empty body",
            status.as_u16()
        ));
    }
    serde_json::from_str::<Value>(text)
        .map_err(|_| format!("Telegram returned HTTP {} with a non-JSON body", status.as_u16()))
}

/// Generic transport failure message. Deliberately generic: reqwest errors
/// can echo the request URL, which carries the bot token in the path.
fn transport_error(err: reqwest::Error) -> String {
    if err.is_timeout() {
        "connection to Telegram timed out".to_string()
    } else if err.is_connect() {
        "could not reach Telegram (connection failed)".to_string()
    } else {
        "request to Telegram failed".to_string()
    }
}

fn client(timeout: Duration) -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .timeout(timeout)
        .build()
        .map_err(|_| "failed to build HTTP client".to_string())
}

/// Live `getMe` verify against the Bot API. `Err` messages never contain
/// the token.
pub async fn verify_token(api_base: &str, token: &str) -> Result<BotIdentity, String> {
    let url = format!("{}/bot{}/getMe", api_base.trim_end_matches('/'), token);
    let resp = client(VERIFY_TIMEOUT)?.get(&url).send().await.map_err(transport_error)?;
    let body = read_json_body(resp).await?;
    parse_get_me(&body)
}

/// Live `sendMessage` against the Bot API. `Ok` → sent message id.
pub async fn send_text(
    api_base: &str,
    token: &str,
    chat_id: &str,
    text: &str,
) -> Result<i64, String> {
    let url = format!(
        "{}/bot{}/sendMessage",
        api_base.trim_end_matches('/'),
        token
    );
    let resp = client(SEND_TIMEOUT)?
        .post(&url)
        .json(&serde_json::json!({ "chat_id": chat_id, "text": text }))
        .send()
        .await
        .map_err(transport_error)?;
    let body = read_json_body(resp).await?;
    parse_send_message(&body)
}

/// Live `getUpdates` poll for ingress. `after` is the last seen
/// `update_id` (exclusive); pass `None` to fetch whatever is pending.
/// `timeout=0` so the call returns immediately instead of long-polling.
pub async fn get_updates(
    api_base: &str,
    token: &str,
    after: Option<&str>,
    chat_filter: Option<&str>,
) -> Result<Vec<InboundMessage>, String> {
    let mut url = format!(
        "{}/bot{}/getUpdates?timeout=0",
        api_base.trim_end_matches('/'),
        token
    );
    if let Some(a) = after.filter(|s| !s.trim().is_empty()) {
        // `offset` is exclusive in the Bot API: after=N skips update N.
        url.push_str(&format!("&offset={}", a.trim()));
    }
    let resp = client(POLL_TIMEOUT)?
        .get(&url)
        .send()
        .await
        .map_err(transport_error)?;
    let body = read_json_body(resp).await?;
    parse_updates(&body, chat_filter)
}

// ---------------------------------------------------------------------------
// Extended operations: media, message management, chat info
// ---------------------------------------------------------------------------

/// Generic `ok:true` response check for methods that return no payload.
fn parse_ok(body: &Value, action: &str) -> Result<(), String> {
    if body.get("ok").and_then(Value::as_bool).unwrap_or(false) {
        Ok(())
    } else {
        Err(describe_api_error(body, action))
    }
}

/// Parse a media-send response (`sendPhoto`/`sendDocument`/`sendSticker`).
/// `Ok` → sent message id.
fn parse_media_send(body: &Value) -> Result<i64, String> {
    parse_send_message(body)
}

/// POST helper for Bot API methods with a JSON body. `Ok` → parsed body.
async fn post_method(
    api_base: &str,
    token: &str,
    method: &str,
    payload: &Value,
) -> Result<Value, String> {
    let url = format!(
        "{}/bot{}/{}",
        api_base.trim_end_matches('/'),
        token,
        method
    );
    let resp = client(SEND_TIMEOUT)?
        .post(&url)
        .json(payload)
        .send()
        .await
        .map_err(transport_error)?;
    read_json_body(resp).await
}

/// Live `sendPhoto`: `photo` is a URL or file_id. `Ok` → message id.
pub async fn send_photo(
    api_base: &str,
    token: &str,
    chat_id: &str,
    photo: &str,
    caption: Option<&str>,
) -> Result<i64, String> {
    let mut payload = serde_json::json!({ "chat_id": chat_id, "photo": photo });
    if let Some(c) = caption.filter(|s| !s.trim().is_empty()) {
        payload["caption"] = serde_json::json!(c);
    }
    let body = post_method(api_base, token, "sendPhoto", &payload).await?;
    parse_media_send(&body)
}

/// Live `sendDocument`: `document` is a URL or file_id. `Ok` → message id.
pub async fn send_document(
    api_base: &str,
    token: &str,
    chat_id: &str,
    document: &str,
    caption: Option<&str>,
) -> Result<i64, String> {
    let mut payload = serde_json::json!({ "chat_id": chat_id, "document": document });
    if let Some(c) = caption.filter(|s| !s.trim().is_empty()) {
        payload["caption"] = serde_json::json!(c);
    }
    let body = post_method(api_base, token, "sendDocument", &payload).await?;
    parse_media_send(&body)
}

/// Live `sendSticker`: `sticker` is a file_id or sticker set name.
/// `Ok` → message id.
pub async fn send_sticker(
    api_base: &str,
    token: &str,
    chat_id: &str,
    sticker: &str,
) -> Result<i64, String> {
    let payload = serde_json::json!({ "chat_id": chat_id, "sticker": sticker });
    let body = post_method(api_base, token, "sendSticker", &payload).await?;
    parse_media_send(&body)
}

/// Live `editMessageText`. `Ok` → edited message id.
pub async fn edit_message(
    api_base: &str,
    token: &str,
    chat_id: &str,
    message_id: i64,
    text: &str,
) -> Result<i64, String> {
    let payload = serde_json::json!({
        "chat_id": chat_id, "message_id": message_id, "text": text
    });
    let body = post_method(api_base, token, "editMessageText", &payload).await?;
    parse_send_message(&body)
}

/// Live `deleteMessage`.
pub async fn delete_message(
    api_base: &str,
    token: &str,
    chat_id: &str,
    message_id: i64,
) -> Result<(), String> {
    let payload = serde_json::json!({ "chat_id": chat_id, "message_id": message_id });
    let body = post_method(api_base, token, "deleteMessage", &payload).await?;
    parse_ok(&body, "failed to delete message")
}

/// Chat info from `getChat`: id, title/name, type, member count.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChatInfo {
    pub id: String,
    pub title: String,
    pub kind: String,
    pub member_count: Option<i64>,
}

/// Parse a `getChat` response.
pub fn parse_chat_info(body: &Value) -> Result<ChatInfo, String> {
    if !body.get("ok").and_then(Value::as_bool).unwrap_or(false) {
        return Err(describe_api_error(body, "getChat failed"));
    }
    let r = body.get("result").ok_or_else(|| {
        "Telegram getChat response missing result".to_string()
    })?;
    let id = r
        .get("id")
        .and_then(Value::as_i64)
        .map(|n| n.to_string())
        .unwrap_or_default();
    let title = r
        .get("title")
        .and_then(Value::as_str)
        .or_else(|| r.get("first_name").and_then(Value::as_str))
        .unwrap_or("")
        .to_string();
    let kind = r
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    Ok(ChatInfo {
        id,
        title,
        kind,
        member_count: None,
    })
}

/// Live `getChat`.
pub async fn get_chat_info(
    api_base: &str,
    token: &str,
    chat_id: &str,
) -> Result<ChatInfo, String> {
    let payload = serde_json::json!({ "chat_id": chat_id });
    let body = post_method(api_base, token, "getChat", &payload).await?;
    parse_chat_info(&body)
}

/// One chat member from `getChatAdministrators` / `getChatMember`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChatMember {
    pub user_id: i64,
    pub username: Option<String>,
    pub first_name: String,
    pub status: String,
}

/// Parse a `getChatAdministrators` response.
pub fn parse_chat_members(body: &Value) -> Result<Vec<ChatMember>, String> {
    if !body.get("ok").and_then(Value::as_bool).unwrap_or(false) {
        return Err(describe_api_error(body, "getChatAdministrators failed"));
    }
    let arr = body
        .get("result")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut out = Vec::new();
    for entry in arr {
        let user = entry.get("user");
        let (Some(uid), status) = (
            user.and_then(|u| u.get("id")).and_then(Value::as_i64),
            entry
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
        ) else {
            continue;
        };
        out.push(ChatMember {
            user_id: uid,
            username: user
                .and_then(|u| u.get("username"))
                .and_then(Value::as_str)
                .map(|s| s.to_string()),
            first_name: user
                .and_then(|u| u.get("first_name"))
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            status,
        });
    }
    Ok(out)
}

/// Live `getChatAdministrators` (member listing for groups/channels).
pub async fn get_chat_members(
    api_base: &str,
    token: &str,
    chat_id: &str,
) -> Result<Vec<ChatMember>, String> {
    let payload = serde_json::json!({ "chat_id": chat_id });
    let body = post_method(api_base, token, "getChatAdministrators", &payload).await?;
    parse_chat_members(&body)
}

/// Live `pinChatMessage`.
pub async fn pin_message(
    api_base: &str,
    token: &str,
    chat_id: &str,
    message_id: i64,
) -> Result<(), String> {
    let payload = serde_json::json!({ "chat_id": chat_id, "message_id": message_id });
    let body = post_method(api_base, token, "pinChatMessage", &payload).await?;
    parse_ok(&body, "failed to pin message")
}

/// Live `unpinChatMessage`.
pub async fn unpin_message(
    api_base: &str,
    token: &str,
    chat_id: &str,
    message_id: Option<i64>,
) -> Result<(), String> {
    let mut payload = serde_json::json!({ "chat_id": chat_id });
    if let Some(mid) = message_id {
        payload["message_id"] = serde_json::json!(mid);
    }
    let body = post_method(api_base, token, "unpinChatMessage", &payload).await?;
    parse_ok(&body, "failed to unpin message")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::server::channels::mock::{closed_port_url, mock_server, mock_server_n, GOOD_TOKEN};

    #[test]
    fn token_format_validation() {
        assert!(valid_bot_token_format("123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"));
        assert!(valid_bot_token_format("1:x"));
        assert!(!valid_bot_token_format(""));
        assert!(!valid_bot_token_format("no-colon-here"));
        assert!(!valid_bot_token_format(":onlysecret"));
        assert!(!valid_bot_token_format("abc:secret"));
        assert!(!valid_bot_token_format("123:"));
        assert!(!valid_bot_token_format("123:   "));
    }

    #[test]
    fn parse_get_me_ok() {
        let body: Value = serde_json::from_str(
            r#"{"ok":true,"result":{"id":123456789,"is_bot":true,"first_name":"Boomi","username":"boomi_bot"}}"#,
        )
        .unwrap();
        let id = parse_get_me(&body).expect("must parse");
        assert_eq!(
            id,
            BotIdentity {
                id: 123456789,
                username: Some("boomi_bot".to_string()),
                first_name: "Boomi".to_string(),
            }
        );
    }

    #[test]
    fn parse_get_me_ok_without_username() {
        let body: Value = serde_json::from_str(
            r#"{"ok":true,"result":{"id":7,"first_name":"X"}}"#,
        )
        .unwrap();
        let id = parse_get_me(&body).expect("must parse");
        assert_eq!(id.username, None);
    }

    #[test]
    fn parse_get_me_rejected_never_echoes_token() {
        let body: Value = serde_json::from_str(
            r#"{"ok":false,"error_code":401,"description":"Unauthorized"}"#,
        )
        .unwrap();
        let err = parse_get_me(&body).unwrap_err();
        assert!(err.contains("401"), "err: {err}");
        assert!(err.contains("Unauthorized"), "err: {err}");
        assert!(!err.contains("bot"), "err: {err}");
    }

    #[test]
    fn parse_get_me_malformed() {
        let body: Value = serde_json::from_str(r#"{"ok":true}"#).unwrap();
        assert!(parse_get_me(&body).is_err());
        let body: Value = serde_json::from_str(r#"{"unexpected":1}"#).unwrap();
        let err = parse_get_me(&body).unwrap_err();
        assert!(err.contains("unknown error"), "err: {err}");
    }

    #[test]
    fn parse_send_message_ok() {
        let body: Value = serde_json::from_str(
            r#"{"ok":true,"result":{"message_id":42,"chat":{"id":1},"text":"hi"}}"#,
        )
        .unwrap();
        assert_eq!(parse_send_message(&body).expect("must parse"), 42);
    }

    #[test]
    fn parse_send_message_refused() {
        let body: Value = serde_json::from_str(
            r#"{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}"#,
        )
        .unwrap();
        let err = parse_send_message(&body).unwrap_err();
        assert!(err.contains("chat not found"), "err: {err}");
    }

    #[test]
    fn parse_updates_message_and_channel_post() {
        let body: Value = serde_json::from_str(
            r#"{"ok":true,"result":[
                {"update_id":10,"message":{"message_id":1,"from":{"id":5,"first_name":"An","username":"an_dev"},"chat":{"id":99,"type":"private"},"date":1760680000,"text":"hello bot"}},
                {"update_id":11,"channel_post":{"message_id":2,"chat":{"id":-1001,"type":"channel"},"date":1760680001,"text":"announcement"}},
                {"update_id":12,"message":{"message_id":3,"chat":{"id":99},"date":1760680002}},
                {"update_id":13,"inline_query":{"id":"q1"}}
            ]}"#,
        )
        .unwrap();
        let msgs = parse_updates(&body, None).expect("must parse");
        assert_eq!(msgs.len(), 2, "msgs: {msgs:?}");
        assert_eq!(msgs[0].id, "10");
        assert_eq!(msgs[0].chat_id, "99");
        assert_eq!(msgs[0].sender.as_deref(), Some("@an_dev"));
        assert_eq!(msgs[0].text, "hello bot");
        assert_eq!(msgs[0].ts.as_deref(), Some("1760680000"));
        assert_eq!(msgs[1].id, "11");
        assert_eq!(msgs[1].chat_id, "-1001");

        // Chat filter keeps only the matching chat.
        let filtered = parse_updates(&body, Some("-1001")).expect("must parse");
        assert_eq!(filtered.len(), 1);
        assert_eq!(filtered[0].id, "11");
    }

    #[test]
    fn parse_updates_rejected() {
        let body: Value = serde_json::from_str(
            r#"{"ok":false,"error_code":409,"description":"Conflict: terminated by other getUpdates"}"#,
        )
        .unwrap();
        let err = parse_updates(&body, None).unwrap_err();
        assert!(err.contains("409"), "err: {err}");
    }

    #[tokio::test]
    async fn verify_token_ok_against_mock() {
        let (base, handle) = mock_server(
            200,
            r#"{"ok":true,"result":{"id":1,"first_name":"T","username":"testbot"}}"#,
        )
        .await;
        let id = verify_token(&base, GOOD_TOKEN).await.expect("verify");
        assert_eq!(id.username.as_deref(), Some("testbot"));
        let reqs = handle.await.expect("server task");
        // Token travels in the URL path, exactly like the Bot API requires.
        assert_eq!(reqs.len(), 1);
        assert!(
            reqs[0].starts_with("GET /bot123456:TESTTOKEN/getMe "),
            "req: {}",
            reqs[0]
        );
    }

    #[tokio::test]
    async fn verify_token_rejected_against_mock() {
        let (base, _handle) = mock_server(
            401,
            r#"{"ok":false,"error_code":401,"description":"Unauthorized"}"#,
        )
        .await;
        let err = verify_token(&base, GOOD_TOKEN).await.unwrap_err();
        assert!(err.contains("Unauthorized"), "err: {err}");
        assert!(!err.contains(GOOD_TOKEN), "err: {err}");
        assert!(!err.contains("TESTTOKEN"), "err: {err}");
    }

    #[tokio::test]
    async fn verify_token_connection_refused_is_generic() {
        let base = closed_port_url().await;
        let err = verify_token(&base, GOOD_TOKEN).await.unwrap_err();
        assert!(!err.is_empty());
        assert!(!err.contains(GOOD_TOKEN), "err: {err}");
        assert!(!err.contains("TESTTOKEN"), "err: {err}");
    }

    #[tokio::test]
    async fn send_text_posts_json_body() {
        let (base, handle) = mock_server(
            200,
            r#"{"ok":true,"result":{"message_id":99}}"#,
        )
        .await;
        let msg_id = send_text(&base, GOOD_TOKEN, "12345", "hello")
            .await
            .expect("send");
        assert_eq!(msg_id, 99);
        let reqs = handle.await.expect("server task");
        assert_eq!(reqs.len(), 1);
        assert!(
            reqs[0].starts_with("POST /bot123456:TESTTOKEN/sendMessage "),
            "req: {}",
            reqs[0]
        );
        assert!(reqs[0].contains(r#""chat_id":"12345""#), "req: {}", reqs[0]);
        assert!(reqs[0].contains(r#""text":"hello""#), "req: {}", reqs[0]);
    }

    #[tokio::test]
    async fn send_text_refused() {
        let (base, _handle) = mock_server(
            400,
            r#"{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}"#,
        )
        .await;
        let err = send_text(&base, GOOD_TOKEN, "nope", "hi")
            .await
            .unwrap_err();
        assert!(err.contains("chat not found"), "err: {err}");
        assert!(!err.contains("TESTTOKEN"), "err: {err}");
    }

    #[tokio::test]
    async fn get_updates_uses_offset_and_parses() {
        let (base, handle) = mock_server(
            200,
            r#"{"ok":true,"result":[{"update_id":77,"message":{"message_id":1,"from":{"id":2,"first_name":"B"},"chat":{"id":5},"date":1,"text":"ping"}}]}"#,
        )
        .await;
        let msgs = get_updates(&base, GOOD_TOKEN, Some("77"), None)
            .await
            .expect("poll");
        assert_eq!(msgs.len(), 1);
        assert_eq!(msgs[0].id, "77");
        assert_eq!(msgs[0].text, "ping");
        let reqs = handle.await.expect("server task");
        assert!(
            reqs[0].contains("GET /bot123456:TESTTOKEN/getUpdates?timeout=0&offset=77 "),
            "req: {}",
            reqs[0]
        );
        assert!(!reqs[0].contains("TESTTOKEN ") || reqs[0].contains("/bot123456:TESTTOKEN/"));
    }

    // ---------------- Extended operations ----------------

    #[test]
    fn parse_chat_info_ok() {
        let body: Value = serde_json::from_str(
            r#"{"ok":true,"result":{"id":-100123,"title":"Boom Group","type":"supergroup"}}"#,
        )
        .unwrap();
        let info = parse_chat_info(&body).expect("must parse");
        assert_eq!(info.id, "-100123");
        assert_eq!(info.title, "Boom Group");
        assert_eq!(info.kind, "supergroup");
    }

    #[test]
    fn parse_chat_members_ok() {
        let body: Value = serde_json::from_str(
            r#"{"ok":true,"result":[
                {"user":{"id":1,"first_name":"An","username":"an_dev"},"status":"creator"},
                {"user":{"id":2,"first_name":"Boomi"},"status":"administrator"}
            ]}"#,
        )
        .unwrap();
        let members = parse_chat_members(&body).expect("must parse");
        assert_eq!(members.len(), 2);
        assert_eq!(members[0].username.as_deref(), Some("an_dev"));
        assert_eq!(members[0].status, "creator");
        assert_eq!(members[1].user_id, 2);
    }

    #[tokio::test]
    async fn send_photo_posts_json() {
        let (base, handle) = mock_server(
            200,
            r#"{"ok":true,"result":{"message_id":55}}"#,
        )
        .await;
        let mid = send_photo(&base, GOOD_TOKEN, "99", "https://x/y.png", Some("cap"))
            .await
            .expect("send");
        assert_eq!(mid, 55);
        let reqs = handle.await.expect("server task");
        assert!(reqs[0].starts_with("POST /bot123456:TESTTOKEN/sendPhoto "), "req: {}", reqs[0]);
        assert!(reqs[0].contains(r#""photo":"https://x/y.png""#), "req: {}", reqs[0]);
        assert!(reqs[0].contains(r#""caption":"cap""#), "req: {}", reqs[0]);
    }

    #[tokio::test]
    async fn edit_and_delete_message() {
        let (base, handle) = mock_server_n(
            200,
            r#"{"ok":true,"result":{"message_id":7}}"#,
            2,
        )
        .await;
        let mid = edit_message(&base, GOOD_TOKEN, "99", 7, "new text")
            .await
            .expect("edit");
        assert_eq!(mid, 7);
        // delete answers {"ok":true,"result":true}
        delete_message(&base, GOOD_TOKEN, "99", 7)
            .await
            .expect("delete");
        let reqs = handle.await.expect("server task");
        assert_eq!(reqs.len(), 2);
        assert!(reqs[0].contains("/editMessageText "), "req: {}", reqs[0]);
        assert!(reqs[1].contains("/deleteMessage "), "req: {}", reqs[1]);
    }

    #[tokio::test]
    async fn pin_and_unpin_message() {
        let (base, handle) = mock_server_n(200, r#"{"ok":true,"result":true}"#, 2).await;
        pin_message(&base, GOOD_TOKEN, "99", 7).await.expect("pin");
        unpin_message(&base, GOOD_TOKEN, "99", None).await.expect("unpin");
        let reqs = handle.await.expect("server task");
        assert!(reqs[0].contains("/pinChatMessage "), "req: {}", reqs[0]);
        assert!(reqs[1].contains("/unpinChatMessage "), "req: {}", reqs[1]);
    }
}
