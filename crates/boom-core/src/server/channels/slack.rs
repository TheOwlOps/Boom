//! Slack Web API client: `auth.test` verify, `chat.postMessage` send,
//! Block Kit, message update/delete, channel listing, user info.
//!
//! Slack uses a Bot User OAuth Token (`xoxb-...`) sent as
//! `Authorization: Bearer <token>` — the token never appears in URLs.
//! Error messages never contain the token.
//!
//! Ingress: Slack has no simple polling API for bot DMs — receiving
//! messages needs **Socket Mode** (WebSocket) or the **Events API**
//! (webhook). `poll_updates` answers an honest 501 pointing at
//! `docs/channels.md` instead of faking it.

use std::time::Duration;

use serde_json::Value;

use super::InboundMessage;

/// Slack Web API base (overridable in tests).
pub const API_BASE: &str = "https://slack.com/api";
const TIMEOUT: Duration = Duration::from_secs(15);

/// Bot identity returned by `auth.test`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SlackIdentity {
    pub user_id: String,
    pub team: String,
    pub bot_name: Option<String>,
}

/// One Slack channel from `conversations.list`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SlackChannel {
    pub id: String,
    pub name: String,
    pub is_private: bool,
}

/// One Slack user from `users.info`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SlackUser {
    pub id: String,
    pub name: String,
    pub real_name: Option<String>,
}

/// Parse an `auth.test` response. Slack answers 200 with
/// `{"ok":true,...}` on success, `{"ok":false,"error":"invalid_auth"}`
/// on failure.
pub fn parse_verify(body: &Value) -> Result<SlackIdentity, String> {
    if !body.get("ok").and_then(Value::as_bool).unwrap_or(false) {
        return Err(describe_error(body, "rejected the token"));
    }
    let user_id = body
        .get("user_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "Slack auth.test response missing user_id".to_string())?;
    let team = body
        .get("team")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let bot_name = body
        .get("bot_id")
        .and_then(Value::as_str)
        .map(|s| s.to_string());
    Ok(SlackIdentity {
        user_id: user_id.to_string(),
        team,
        bot_name,
    })
}

/// Parse a `chat.postMessage` / `chat.update` response. `Ok` → message ts.
pub fn parse_send(body: &Value) -> Result<String, String> {
    if !body.get("ok").and_then(Value::as_bool).unwrap_or(false) {
        return Err(describe_error(body, "refused to send"));
    }
    body.get("ts")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .ok_or_else(|| "Slack send response missing ts".to_string())
}

/// Parse a `chat.delete` response.
pub fn parse_delete(body: &Value) -> Result<(), String> {
    if body.get("ok").and_then(Value::as_bool).unwrap_or(false) {
        Ok(())
    } else {
        Err(describe_error(body, "refused to delete"))
    }
}

/// Parse a `conversations.list` response.
pub fn parse_channels(body: &Value) -> Result<Vec<SlackChannel>, String> {
    if !body.get("ok").and_then(Value::as_bool).unwrap_or(false) {
        return Err(describe_error(body, "could not list channels"));
    }
    let arr = body
        .get("channels")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut out = Vec::new();
    for c in arr {
        let (Some(id), Some(name)) = (
            c.get("id").and_then(Value::as_str),
            c.get("name").and_then(Value::as_str),
        ) else {
            continue;
        };
        out.push(SlackChannel {
            id: id.to_string(),
            name: name.to_string(),
            is_private: c.get("is_private").and_then(Value::as_bool).unwrap_or(false),
        });
    }
    Ok(out)
}

/// Parse a `users.info` response.
pub fn parse_user_info(body: &Value) -> Result<SlackUser, String> {
    if !body.get("ok").and_then(Value::as_bool).unwrap_or(false) {
        return Err(describe_error(body, "could not fetch user"));
    }
    let user = body.get("user").ok_or_else(|| {
        "Slack users.info response missing user".to_string()
    })?;
    let id = user
        .get("id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "Slack user missing id".to_string())?;
    let name = user
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let real_name = user
        .get("real_name")
        .and_then(Value::as_str)
        .map(|s| s.to_string());
    Ok(SlackUser {
        id: id.to_string(),
        name,
        real_name,
    })
}

/// Render a Slack error body: `{"ok":false,"error":"channel_not_found"}`.
fn describe_error(body: &Value, action: &str) -> String {
    let error = body
        .get("error")
        .and_then(Value::as_str)
        .unwrap_or("unknown error")
        .trim();
    format!("Slack {action} ({error})")
}

/// Read a JSON response body; degrade non-JSON/empty to generic text.
async fn read_json_body(resp: reqwest::Response) -> Result<Value, String> {
    let status = resp.status();
    let text = resp.text().await.map_err(|_| "request failed".to_string())?;
    let text = text.trim();
    if text.is_empty() {
        return Err(format!(
            "Slack returned HTTP {} with an empty body",
            status.as_u16()
        ));
    }
    serde_json::from_str::<Value>(text)
        .map_err(|_| format!("Slack returned HTTP {} with a non-JSON body", status.as_u16()))
}

/// Generic transport failure (token is header-only, but stay generic).
fn transport_error(err: reqwest::Error) -> String {
    if err.is_timeout() {
        "connection to Slack timed out".to_string()
    } else if err.is_connect() {
        "could not reach Slack (connection failed)".to_string()
    } else {
        "request to Slack failed".to_string()
    }
}

fn client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .timeout(TIMEOUT)
        .build()
        .map_err(|_| "failed to build HTTP client".to_string())
}

fn bearer(token: &str) -> String {
    format!("Bearer {token}")
}

/// Live verify: `POST /auth.test`.
pub async fn verify_token(api_base: &str, token: &str) -> Result<SlackIdentity, String> {
    let url = format!("{}/auth.test", api_base.trim_end_matches('/'));
    let resp = client()?
        .post(&url)
        .header("Authorization", bearer(token))
        .send()
        .await
        .map_err(transport_error)?;
    let body = read_json_body(resp).await?;
    parse_verify(&body)
}

/// Live send: `POST /chat.postMessage`. `Ok` → message ts.
pub async fn send_text(
    api_base: &str,
    token: &str,
    channel: &str,
    text: &str,
) -> Result<String, String> {
    send_blocks(api_base, token, channel, text, None).await
}

/// Live send with Block Kit: `POST /chat.postMessage` with `blocks`.
/// `Ok` → message ts.
pub async fn send_blocks(
    api_base: &str,
    token: &str,
    channel: &str,
    text: &str,
    blocks: Option<&Value>,
) -> Result<String, String> {
    let url = format!("{}/chat.postMessage", api_base.trim_end_matches('/'));
    let mut payload = serde_json::json!({ "channel": channel, "text": text });
    if let Some(b) = blocks {
        payload["blocks"] = b.clone();
    }
    let resp = client()?
        .post(&url)
        .header("Authorization", bearer(token))
        .json(&payload)
        .send()
        .await
        .map_err(transport_error)?;
    let body = read_json_body(resp).await?;
    parse_send(&body)
}

/// Live update: `POST /chat.update`. `Ok` → message ts.
pub async fn update_message(
    api_base: &str,
    token: &str,
    channel: &str,
    ts: &str,
    text: &str,
) -> Result<String, String> {
    let url = format!("{}/chat.update", api_base.trim_end_matches('/'));
    let resp = client()?
        .post(&url)
        .header("Authorization", bearer(token))
        .json(&serde_json::json!({ "channel": channel, "ts": ts, "text": text }))
        .send()
        .await
        .map_err(transport_error)?;
    let body = read_json_body(resp).await?;
    parse_send(&body)
}

/// Live delete: `POST /chat.delete`.
pub async fn delete_message(
    api_base: &str,
    token: &str,
    channel: &str,
    ts: &str,
) -> Result<(), String> {
    let url = format!("{}/chat.delete", api_base.trim_end_matches('/'));
    let resp = client()?
        .post(&url)
        .header("Authorization", bearer(token))
        .json(&serde_json::json!({ "channel": channel, "ts": ts }))
        .send()
        .await
        .map_err(transport_error)?;
    let body = read_json_body(resp).await?;
    parse_delete(&body)
}

/// Live list: `GET /conversations.list`.
pub async fn list_channels(
    api_base: &str,
    token: &str,
) -> Result<Vec<SlackChannel>, String> {
    let url = format!(
        "{}/conversations.list?types=public_channel,private_channel,im,mpim&limit=200",
        api_base.trim_end_matches('/')
    );
    let resp = client()?
        .get(&url)
        .header("Authorization", bearer(token))
        .send()
        .await
        .map_err(transport_error)?;
    let body = read_json_body(resp).await?;
    parse_channels(&body)
}

/// Live user info: `GET /users.info?user=U...`.
pub async fn get_user_info(
    api_base: &str,
    token: &str,
    user_id: &str,
) -> Result<SlackUser, String> {
    let url = format!(
        "{}/users.info?user={}",
        api_base.trim_end_matches('/'),
        user_id.trim()
    );
    let resp = client()?
        .get(&url)
        .header("Authorization", bearer(token))
        .send()
        .await
        .map_err(transport_error)?;
    let body = read_json_body(resp).await?;
    parse_user_info(&body)
}

/// Poll for ingress — Slack has no simple polling API. Honest 501:
/// receiving messages needs Socket Mode or the Events API webhook.
pub fn poll_ingress_hint() -> String {
    "Slack không có API polling — nhận tin nhắn cần Socket Mode hoặc Events API webhook: xem docs/channels.md".to_string()
}

#[allow(dead_code)]
fn _use_inbound() -> Option<InboundMessage> {
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::server::channels::mock::{closed_port_url, mock_server, GOOD_TOKEN};

    #[test]
    fn parse_verify_ok() {
        let body: Value = serde_json::from_str(
            r#"{"ok":true,"url":"https://t.slack.com/","team":"Boom","user":"boomi","user_id":"U123","bot_id":"B456"}"#,
        )
        .unwrap();
        let id = parse_verify(&body).expect("must parse");
        assert_eq!(id.user_id, "U123");
        assert_eq!(id.team, "Boom");
        assert_eq!(id.bot_name.as_deref(), Some("B456"));
    }

    #[test]
    fn parse_verify_rejected() {
        let body: Value =
            serde_json::from_str(r#"{"ok":false,"error":"invalid_auth"}"#).unwrap();
        let err = parse_verify(&body).unwrap_err();
        assert!(err.contains("invalid_auth"), "err: {err}");
        assert!(!err.contains("TESTTOKEN"), "err: {err}");
    }

    #[test]
    fn parse_send_ok() {
        let body: Value = serde_json::from_str(
            r#"{"ok":true,"channel":"C1","ts":"1729000000.000100","message":{"text":"hi"}}"#,
        )
        .unwrap();
        assert_eq!(parse_send(&body).expect("must parse"), "1729000000.000100");
    }

    #[test]
    fn parse_channels_ok() {
        let body: Value = serde_json::from_str(
            r#"{"ok":true,"channels":[{"id":"C1","name":"general","is_private":false},{"id":"C2","name":"secret","is_private":true}]}"#,
        )
        .unwrap();
        let chs = parse_channels(&body).expect("must parse");
        assert_eq!(chs.len(), 2);
        assert_eq!(chs[0].name, "general");
        assert!(chs[1].is_private);
    }

    #[test]
    fn parse_user_info_ok() {
        let body: Value = serde_json::from_str(
            r#"{"ok":true,"user":{"id":"U9","name":"an","real_name":"An Dev"}}"#,
        )
        .unwrap();
        let u = parse_user_info(&body).expect("must parse");
        assert_eq!(u.id, "U9");
        assert_eq!(u.real_name.as_deref(), Some("An Dev"));
    }

    #[tokio::test]
    async fn verify_token_sends_bearer_header() {
        let (base, handle) = mock_server(
            200,
            r#"{"ok":true,"user_id":"U1","team":"T","bot_id":"B1"}"#,
        )
        .await;
        let id = verify_token(&base, GOOD_TOKEN).await.expect("verify");
        assert_eq!(id.user_id, "U1");
        let reqs = handle.await.expect("server task");
        assert!(reqs[0].starts_with("POST /auth.test "), "req: {}", reqs[0]);
        assert!(
            reqs[0].to_lowercase().contains("authorization: bearer "),
            "req: {}",
            reqs[0]
        );
        let request_line = reqs[0].lines().next().unwrap_or("");
        assert!(!request_line.contains("TESTTOKEN"), "req: {request_line}");
    }

    #[tokio::test]
    async fn send_blocks_posts_json() {
        let (base, handle) = mock_server(
            200,
            r#"{"ok":true,"channel":"C1","ts":"1.2"}"#,
        )
        .await;
        let ts = send_blocks(
            &base,
            GOOD_TOKEN,
            "C1",
            "fallback",
            Some(&serde_json::json!([{"type":"section","text":{"type":"mrkdwn","text":"hi"}}])),
        )
        .await
        .expect("send");
        assert_eq!(ts, "1.2");
        let reqs = handle.await.expect("server task");
        assert!(reqs[0].starts_with("POST /chat.postMessage "), "req: {}", reqs[0]);
        assert!(reqs[0].contains(r#""channel":"C1""#), "req: {}", reqs[0]);
        assert!(reqs[0].contains(r#""blocks":"#) || reqs[0].contains("\"blocks\""), "req: {}", reqs[0]);
    }

    #[tokio::test]
    async fn verify_token_connection_refused_is_generic() {
        let base = closed_port_url().await;
        let err = verify_token(&base, GOOD_TOKEN).await.unwrap_err();
        assert!(!err.is_empty());
        assert!(!err.contains("TESTTOKEN"), "err: {err}");
    }
}
