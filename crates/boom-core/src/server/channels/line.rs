//! LINE Messaging API client: `GET /v2/bot/profile` verify,
//! `POST /v2/bot/message/push` send.
//!
//! The credential is a **Channel access token** (long-lived, from the
//! LINE Developers Console → Messaging API channel), sent as
//! `Authorization: Bearer <token>` — never in URLs.
//!
//! Ingress has no polling API: receiving messages requires a **webhook
//! URL** registered on the channel. `poll_updates` answers an honest 501
//! pointing at `docs/channels.md`.

use std::time::Duration;

use serde_json::Value;

use super::InboundMessage;

/// LINE API base (overridable in tests).
pub const API_BASE: &str = "https://api.line.me";
const TIMEOUT: Duration = Duration::from_secs(15);

/// Bot identity from `GET /v2/bot/profile`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LineIdentity {
    pub user_id: String,
    pub display_name: String,
}

/// Parse a `GET /v2/bot/profile` response.
pub fn parse_verify(body: &Value) -> Result<LineIdentity, String> {
    let user_id = body
        .get("userId")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "LINE profile response missing userId".to_string())?;
    let display_name = body
        .get("displayName")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    Ok(LineIdentity {
        user_id: user_id.to_string(),
        display_name,
    })
}

/// Parse a `POST /v2/bot/message/push` response. LINE answers 200 with
/// `{}` on success — `Ok(())` means accepted.
pub fn parse_send(body: &Value, status_ok: bool) -> Result<(), String> {
    if !status_ok {
        return Err(describe_error(body, "refused to send"));
    }
    Ok(())
}

/// Render a LINE error body: `{"message":"...","details":[...]}`.
fn describe_error(body: &Value, action: &str) -> String {
    let message = body
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or("unknown error")
        .trim();
    format!("LINE {action} ({message})")
}

/// Read a JSON response body; degrade non-JSON/empty to generic text.
/// Returns (parsed body, http success).
async fn read_body(resp: reqwest::Response) -> Result<(Value, bool), String> {
    let status = resp.status();
    let text = resp.text().await.map_err(|_| "request failed".to_string())?;
    let text = text.trim();
    if text.is_empty() {
        // LINE push answers 200 with `{}` — empty is fine on success.
        return Ok((Value::Null, status.is_success()));
    }
    let body: Value = serde_json::from_str(text)
        .map_err(|_| format!("LINE returned HTTP {} with a non-JSON body", status.as_u16()))?;
    Ok((body, status.is_success()))
}

/// Generic transport failure (token is header-only, but stay generic).
fn transport_error(err: reqwest::Error) -> String {
    if err.is_timeout() {
        "connection to LINE timed out".to_string()
    } else if err.is_connect() {
        "could not reach LINE (connection failed)".to_string()
    } else {
        "request to LINE failed".to_string()
    }
}

fn client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .timeout(TIMEOUT)
        .build()
        .map_err(|_| "failed to build HTTP client".to_string())
}

/// Live verify: `GET /v2/bot/profile`.
pub async fn verify_token(api_base: &str, token: &str) -> Result<LineIdentity, String> {
    let url = format!("{}/v2/bot/profile", api_base.trim_end_matches('/'));
    let resp = client()?
        .get(&url)
        .header("Authorization", format!("Bearer {token}"))
        .send()
        .await
        .map_err(transport_error)?;
    let (body, ok) = read_body(resp).await?;
    if !ok {
        return Err(describe_error(&body, "rejected the token"));
    }
    parse_verify(&body)
}

/// Live send: `POST /v2/bot/message/push` with a text message.
pub async fn send_text(
    api_base: &str,
    token: &str,
    to: &str,
    text: &str,
) -> Result<(), String> {
    let url = format!("{}/v2/bot/message/push", api_base.trim_end_matches('/'));
    let resp = client()?
        .post(&url)
        .header("Authorization", format!("Bearer {token}"))
        .json(&serde_json::json!({
            "to": to,
            "messages": [{ "type": "text", "text": text }],
        }))
        .send()
        .await
        .map_err(transport_error)?;
    let (body, ok) = read_body(resp).await?;
    parse_send(&body, ok)
}

/// Parse a LINE webhook event body into inbound messages.
/// `Ok` → normalized messages (text messages only).
pub fn parse_webhook(body: &Value) -> Vec<InboundMessage> {
    let mut out = Vec::new();
    let events = body
        .get("events")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    for e in events {
        if e.get("type").and_then(Value::as_str) != Some("message") {
            continue;
        }
        let msg = e.get("message");
        if msg.and_then(|m| m.get("type")).and_then(Value::as_str) != Some("text") {
            continue;
        }
        let text = msg
            .and_then(|m| m.get("text"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if text.trim().is_empty() {
            continue;
        }
        let id = e
            .get("webhookEventId")
            .and_then(Value::as_str)
            .or_else(|| msg.and_then(|m| m.get("id")).and_then(Value::as_str))
            .unwrap_or("")
            .to_string();
        let chat_id = e
            .get("source")
            .and_then(|s| s.get("userId"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let ts = e
            .get("timestamp")
            .and_then(Value::as_i64)
            .map(|n| n.to_string());
        out.push(InboundMessage {
            id,
            chat_id,
            sender: None,
            text,
            ts,
        });
    }
    out
}

/// Poll for ingress — LINE has no polling API. Honest 501.
pub fn poll_ingress_hint() -> String {
    "LINE không có API polling — nhận tin nhắn cần webhook URL đăng ký trên LINE Developers Console: xem docs/channels.md".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::server::channels::mock::{closed_port_url, mock_server, GOOD_TOKEN};

    #[test]
    fn parse_verify_ok() {
        let body: Value = serde_json::from_str(
            r#"{"userId":"U123abc","displayName":"Boomi Bot","pictureUrl":"https://..."}"#,
        )
        .unwrap();
        let id = parse_verify(&body).expect("must parse");
        assert_eq!(id.user_id, "U123abc");
        assert_eq!(id.display_name, "Boomi Bot");
    }

    #[test]
    fn parse_webhook_text_events() {
        let body: Value = serde_json::from_str(
            r#"{"destination":"Ubot","events":[
                {"type":"message","webhookEventId":"ev1","timestamp":1729000000000,
                 "source":{"type":"user","userId":"Uuser1"},
                 "message":{"id":"m1","type":"text","text":"hello"}},
                {"type":"follow","webhookEventId":"ev2","source":{"userId":"Uuser1"}},
                {"type":"message","webhookEventId":"ev3",
                 "source":{"type":"user","userId":"Uuser1"},
                 "message":{"id":"m2","type":"sticker","stickerId":"1"}}
            ]}"#,
        )
        .unwrap();
        let msgs = parse_webhook(&body);
        assert_eq!(msgs.len(), 1, "only text messages are kept");
        assert_eq!(msgs[0].id, "ev1");
        assert_eq!(msgs[0].chat_id, "Uuser1");
        assert_eq!(msgs[0].text, "hello");
        assert_eq!(msgs[0].ts.as_deref(), Some("1729000000000"));
    }

    #[tokio::test]
    async fn verify_token_hits_profile_endpoint() {
        let (base, handle) = mock_server(
            200,
            r#"{"userId":"U123","displayName":"Boomi"}"#,
        )
        .await;
        let id = verify_token(&base, GOOD_TOKEN).await.expect("verify");
        assert_eq!(id.user_id, "U123");
        let reqs = handle.await.expect("server task");
        assert!(reqs[0].starts_with("GET /v2/bot/profile "), "req: {}", reqs[0]);
        assert!(
            reqs[0].to_lowercase().contains("authorization: bearer "),
            "req: {}",
            reqs[0]
        );
    }

    #[tokio::test]
    async fn send_text_posts_push_payload() {
        let (base, handle) = mock_server(200, r#"{}"#).await;
        send_text(&base, GOOD_TOKEN, "Uuser1", "hello")
            .await
            .expect("send");
        let reqs = handle.await.expect("server task");
        assert!(
            reqs[0].starts_with("POST /v2/bot/message/push "),
            "req: {}",
            reqs[0]
        );
        assert!(reqs[0].contains(r#""to":"Uuser1""#), "req: {}", reqs[0]);
        assert!(reqs[0].contains(r#""type":"text""#), "req: {}", reqs[0]);
    }

    #[tokio::test]
    async fn verify_token_connection_refused_is_generic() {
        let base = closed_port_url().await;
        let err = verify_token(&base, GOOD_TOKEN).await.unwrap_err();
        assert!(!err.is_empty());
        assert!(!err.contains("TESTTOKEN"), "err: {err}");
    }
}
