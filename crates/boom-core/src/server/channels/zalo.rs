//! Zalo Official Account (OA) API client: verify + send.
//!
//! The credential is a **Zalo OA access token** (from
//! https://developers.zalo.me → OA → access token), sent as
//! `access_token` in the request body / query — the token never appears
//! in error messages.
//!
//! API base: `https://openapi.zalo.me`.
//!
//! Ingress has no polling API: receiving messages requires a **webhook
//! URL** registered on the OA. `poll_updates` answers an honest 501
//! pointing at `docs/channels.md`.

use std::time::Duration;

use serde_json::Value;

use super::InboundMessage;

/// Zalo OA API base (overridable in tests).
pub const API_BASE: &str = "https://openapi.zalo.me";
const TIMEOUT: Duration = Duration::from_secs(15);

/// OA identity from `GET /v2.0/oa/getoa`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ZaloIdentity {
    pub oa_id: String,
    pub name: String,
}

/// Parse a `GET /v2.0/oa/getoa` response.
/// Zalo answers `{"error":0,"data":{...}}` on success, `{"error":-201,...}`
/// on failure.
pub fn parse_verify(body: &Value) -> Result<ZaloIdentity, String> {
    let code = body.get("error").and_then(Value::as_i64).unwrap_or(-1);
    if code != 0 {
        return Err(describe_error(body, "rejected the token"));
    }
    let data = body.get("data").ok_or_else(|| {
        "Zalo getoa response missing data".to_string()
    })?;
    let oa_id = data
        .get("oa_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "Zalo getoa response missing oa_id".to_string())?;
    let name = data
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    Ok(ZaloIdentity {
        oa_id: oa_id.to_string(),
        name,
    })
}

/// Parse a `POST /v3.0/oa/message/cs` response. `Ok` → sent message id.
pub fn parse_send(body: &Value) -> Result<String, String> {
    let code = body.get("error").and_then(Value::as_i64).unwrap_or(-1);
    if code != 0 {
        return Err(describe_error(body, "refused to send"));
    }
    body.get("data")
        .and_then(|d| d.get("message_id"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .ok_or_else(|| "Zalo send response missing message_id".to_string())
}

/// Render a Zalo error body: `{"error":-201,"message":"..."}`.
fn describe_error(body: &Value, action: &str) -> String {
    let code = body.get("error").and_then(Value::as_i64);
    let message = body
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or("unknown error")
        .trim();
    match code {
        Some(c) => format!("Zalo {action} (error {c}): {message}"),
        None => format!("Zalo {action}: {message}"),
    }
}

/// Read a JSON response body; degrade non-JSON/empty to generic text.
async fn read_json_body(resp: reqwest::Response) -> Result<Value, String> {
    let status = resp.status();
    let text = resp.text().await.map_err(|_| "request failed".to_string())?;
    let text = text.trim();
    if text.is_empty() {
        return Err(format!(
            "Zalo returned HTTP {} with an empty body",
            status.as_u16()
        ));
    }
    serde_json::from_str::<Value>(text)
        .map_err(|_| format!("Zalo returned HTTP {} with a non-JSON body", status.as_u16()))
}

/// Generic transport failure. Never echo the token.
fn transport_error(err: reqwest::Error) -> String {
    if err.is_timeout() {
        "connection to Zalo timed out".to_string()
    } else if err.is_connect() {
        "could not reach Zalo (connection failed)".to_string()
    } else {
        "request to Zalo failed".to_string()
    }
}

fn client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .timeout(TIMEOUT)
        .build()
        .map_err(|_| "failed to build HTTP client".to_string())
}

/// Live verify: `GET /v2.0/oa/getoa?access_token=...`.
/// The token travels as a query param like every Zalo OA client; error
/// paths never echo the request URL.
pub async fn verify_token(api_base: &str, token: &str) -> Result<ZaloIdentity, String> {
    let url = format!(
        "{}/v2.0/oa/getoa?access_token={}",
        api_base.trim_end_matches('/'),
        token
    );
    let resp = client()?.get(&url).send().await.map_err(transport_error)?;
    let body = read_json_body(resp).await?;
    parse_verify(&body)
}

/// Live send: `POST /v3.0/oa/message/cs` (customer-service text message).
/// `Ok` → Zalo message id.
pub async fn send_text(
    api_base: &str,
    token: &str,
    user_id: &str,
    text: &str,
) -> Result<String, String> {
    let url = format!("{}/v3.0/oa/message/cs", api_base.trim_end_matches('/'));
    let resp = client()?
        .post(&url)
        .json(&serde_json::json!({
            "recipient": { "user_id": user_id },
            "message": { "text": text },
        }))
        .header("access_token", token)
        .send()
        .await
        .map_err(transport_error)?;
    let body = read_json_body(resp).await?;
    parse_send(&body)
}

/// Parse a Zalo OA webhook event body into inbound messages.
/// Zalo posts `{"event_name":"user_send_text","sender":{"id":...},"message":{"text":...},...}`.
pub fn parse_webhook(body: &Value) -> Vec<InboundMessage> {
    let mut out = Vec::new();
    let event = body
        .get("event_name")
        .and_then(Value::as_str)
        .unwrap_or("");
    if event != "user_send_text" {
        return out;
    }
    let text = body
        .get("message")
        .and_then(|m| m.get("text"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    if text.trim().is_empty() {
        return out;
    }
    let chat_id = body
        .get("sender")
        .and_then(|s| s.get("id"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let id = body
        .get("message")
        .and_then(|m| m.get("msg_id"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let ts = body
        .get("timestamp")
        .and_then(Value::as_str)
        .map(|s| s.to_string());
    out.push(InboundMessage {
        id,
        chat_id,
        sender: None,
        text,
        ts,
    });
    out
}

/// Poll for ingress — Zalo has no polling API. Honest 501.
pub fn poll_ingress_hint() -> String {
    "Zalo không có API polling — nhận tin nhắn cần webhook URL đăng ký trên developers.zalo.me: xem docs/channels.md".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::server::channels::mock::{closed_port_url, mock_server, GOOD_TOKEN};

    #[test]
    fn parse_verify_ok() {
        let body: Value = serde_json::from_str(
            r#"{"error":0,"message":"Success","data":{"oa_id":"123456789","name":"Boom OA","avatar":"https://..."}}"#,
        )
        .unwrap();
        let id = parse_verify(&body).expect("must parse");
        assert_eq!(id.oa_id, "123456789");
        assert_eq!(id.name, "Boom OA");
    }

    #[test]
    fn parse_verify_rejected() {
        let body: Value = serde_json::from_str(
            r#"{"error":-201,"message":"Invalid access token"}"#,
        )
        .unwrap();
        let err = parse_verify(&body).unwrap_err();
        assert!(err.contains("-201"), "err: {err}");
        assert!(!err.contains("TESTTOKEN"), "err: {err}");
    }

    #[test]
    fn parse_send_ok() {
        let body: Value = serde_json::from_str(
            r#"{"error":0,"message":"Success","data":{"message_id":"abc123"}}"#,
        )
        .unwrap();
        assert_eq!(parse_send(&body).expect("must parse"), "abc123");
    }

    #[test]
    fn parse_webhook_text_event() {
        let body: Value = serde_json::from_str(
            r#"{"event_name":"user_send_text","sender":{"id":"user42"},
                "message":{"text":"xin chào","msg_id":"m99"},"timestamp":"1729000000000"}"#,
        )
        .unwrap();
        let msgs = parse_webhook(&body);
        assert_eq!(msgs.len(), 1);
        assert_eq!(msgs[0].chat_id, "user42");
        assert_eq!(msgs[0].text, "xin chào");
        assert_eq!(msgs[0].id, "m99");
    }

    #[test]
    fn parse_webhook_ignores_other_events() {
        let body: Value = serde_json::from_str(
            r#"{"event_name":"user_follow","sender":{"id":"u1"}}"#,
        )
        .unwrap();
        assert!(parse_webhook(&body).is_empty());
    }

    #[tokio::test]
    async fn verify_token_hits_getoa_endpoint() {
        let (base, handle) = mock_server(
            200,
            r#"{"error":0,"data":{"oa_id":"123","name":"Boom"}}"#,
        )
        .await;
        let id = verify_token(&base, GOOD_TOKEN).await.expect("verify");
        assert_eq!(id.oa_id, "123");
        let reqs = handle.await.expect("server task");
        assert!(
            reqs[0].starts_with("GET /v2.0/oa/getoa?access_token="),
            "req: {}",
            reqs[0]
        );
    }

    #[tokio::test]
    async fn send_text_posts_cs_payload_with_header_token() {
        let (base, handle) = mock_server(
            200,
            r#"{"error":0,"data":{"message_id":"m1"}}"#,
        )
        .await;
        let id = send_text(&base, GOOD_TOKEN, "user42", "hello")
            .await
            .expect("send");
        assert_eq!(id, "m1");
        let reqs = handle.await.expect("server task");
        assert!(
            reqs[0].starts_with("POST /v3.0/oa/message/cs "),
            "req: {}",
            reqs[0]
        );
        assert!(reqs[0].contains(r#""user_id":"user42""#), "req: {}", reqs[0]);
        // Token travels in the access_token header, never in the URL path.
        let request_line = reqs[0].lines().next().unwrap_or("");
        assert!(!request_line.contains("TESTTOKEN"), "req: {request_line}");
    }

    #[tokio::test]
    async fn verify_token_connection_refused_is_generic() {
        let base = closed_port_url().await;
        let err = verify_token(&base, GOOD_TOKEN).await.unwrap_err();
        assert!(!err.is_empty());
        assert!(!err.contains("TESTTOKEN"), "err: {err}");
    }
}
