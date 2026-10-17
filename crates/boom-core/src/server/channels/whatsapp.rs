//! WhatsApp Business Cloud API client (Graph API): verify + send.
//!
//! Uses the same Graph API as Messenger: the credential is a
//! **WhatsApp Business access token** (System User token from the
//! Meta App), sent as `Authorization: Bearer <token>`.
//!
//! API base: `https://graph.facebook.com/v21.0` — sending goes to
//! `POST /{phone_number_id}/messages`.
//!
//! Ingress has no polling API: receiving messages requires a **webhook
//! subscription** on the Meta App. `poll_updates` answers an honest 501
//! pointing at `docs/channels.md`.

use std::time::Duration;

use serde_json::Value;

/// Graph API base (overridable in tests).
pub const API_BASE: &str = "https://graph.facebook.com/v21.0";
const TIMEOUT: Duration = Duration::from_secs(15);

/// WhatsApp Business identity from `GET /{phone_number_id}`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WhatsAppIdentity {
    pub phone_number_id: String,
    pub display_phone_number: String,
    pub verified_name: Option<String>,
}

/// Parse a `GET /{phone_number_id}` verify response.
pub fn parse_verify(body: &Value) -> Result<WhatsAppIdentity, String> {
    if let Some(err) = body.get("error") {
        return Err(describe_graph_error(err));
    }
    let id = body
        .get("id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "WhatsApp verify response missing phone number id".to_string())?;
    let display = body
        .get("display_phone_number")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let verified_name = body
        .get("verified_name")
        .and_then(Value::as_str)
        .map(|s| s.to_string());
    Ok(WhatsAppIdentity {
        phone_number_id: id.to_string(),
        display_phone_number: display,
        verified_name,
    })
}

/// Parse a `POST /{phone_number_id}/messages` response.
/// `Ok` → WhatsApp message id (`wamid...`).
pub fn parse_send(body: &Value) -> Result<String, String> {
    if let Some(err) = body.get("error") {
        return Err(describe_graph_error(err));
    }
    body.get("messages")
        .and_then(|m| m.as_array())
        .and_then(|a| a.first())
        .and_then(|m| m.get("id"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .ok_or_else(|| "WhatsApp send response missing message id".to_string())
}

/// Render a Graph API `{"error": {...}}` body.
fn describe_graph_error(err: &Value) -> String {
    let message = err
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or("unknown error")
        .trim();
    let code = err.get("code").and_then(Value::as_i64);
    match code {
        Some(c) => format!("WhatsApp request failed (code {c}): {message}"),
        None => format!("WhatsApp request failed: {message}"),
    }
}

/// Read a JSON response body; degrade non-JSON/empty to generic text.
async fn read_json_body(resp: reqwest::Response) -> Result<Value, String> {
    let status = resp.status();
    let text = resp.text().await.map_err(|_| "request failed".to_string())?;
    let text = text.trim();
    if text.is_empty() {
        return Err(format!(
            "WhatsApp returned HTTP {} with an empty body",
            status.as_u16()
        ));
    }
    serde_json::from_str::<Value>(text)
        .map_err(|_| format!("WhatsApp returned HTTP {} with a non-JSON body", status.as_u16()))
}

/// Generic transport failure (token is header-only, but stay generic).
fn transport_error(err: reqwest::Error) -> String {
    if err.is_timeout() {
        "connection to WhatsApp timed out".to_string()
    } else if err.is_connect() {
        "could not reach WhatsApp (connection failed)".to_string()
    } else {
        "request to WhatsApp failed".to_string()
    }
}

fn client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .timeout(TIMEOUT)
        .build()
        .map_err(|_| "failed to build HTTP client".to_string())
}

/// The stored credential format: `<access_token>:<phone_number_id>`.
/// The phone_number_id is after the LAST colon (tokens may contain colons).
/// Split it; `Err` when malformed.
fn split_credential(stored: &str) -> Result<(&str, &str), String> {
    match stored.rsplit_once(':') {
        Some((token, pnid)) if !token.trim().is_empty() && !pnid.trim().is_empty() => {
            Ok((token.trim(), pnid.trim()))
        }
        _ => Err(
            "WhatsApp credential format: <access_token>:<phone_number_id> — nhập token rồi dấu : rồi phone number id".to_string(),
        ),
    }
}

/// Live verify: `GET /{phone_number_id}` with the access token.
pub async fn verify_token(api_base: &str, stored: &str) -> Result<WhatsAppIdentity, String> {
    let (token, pnid) = split_credential(stored)?;
    let url = format!(
        "{}/{}?fields=id,display_phone_number,verified_name",
        api_base.trim_end_matches('/'),
        pnid
    );
    let resp = client()?
        .get(&url)
        .header("Authorization", format!("Bearer {token}"))
        .send()
        .await
        .map_err(transport_error)?;
    let body = read_json_body(resp).await?;
    parse_verify(&body)
}

/// Live send: `POST /{phone_number_id}/messages` (text message).
/// `Ok` → WhatsApp message id.
pub async fn send_text(
    api_base: &str,
    stored: &str,
    to: &str,
    text: &str,
) -> Result<String, String> {
    let (token, pnid) = split_credential(stored)?;
    let url = format!(
        "{}/{}/messages",
        api_base.trim_end_matches('/'),
        pnid
    );
    let resp = client()?
        .post(&url)
        .header("Authorization", format!("Bearer {token}"))
        .json(&serde_json::json!({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": { "body": text },
        }))
        .send()
        .await
        .map_err(transport_error)?;
    let body = read_json_body(resp).await?;
    parse_send(&body)
}

/// Poll for ingress — WhatsApp has no polling API. Honest 501.
pub fn poll_ingress_hint() -> String {
    "WhatsApp không có API polling — nhận tin nhắn cần webhook subscription trên Meta App: xem docs/channels.md".to_string()
}

/// Parse a WhatsApp Cloud API webhook event body into inbound messages.
/// Handles `messages` webhooks: `entry[].changes[].value.messages[]`
/// with `type: "text"`.
pub fn parse_webhook(body: &Value) -> Vec<super::InboundMessage> {
    let mut out = Vec::new();
    if body.get("object").and_then(Value::as_str) != Some("whatsapp_business_account") {
        return out;
    }
    let entries = body
        .get("entry")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    for entry in entries {
        let changes = entry
            .get("changes")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        for change in changes {
            let value = change.get("value");
            let messages = value
                .and_then(|v| v.get("messages"))
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            for m in messages {
                if m.get("type").and_then(Value::as_str) != Some("text") {
                    continue;
                }
                let text = m
                    .get("text")
                    .and_then(|t| t.get("body"))
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                if text.trim().is_empty() {
                    continue;
                }
                let id = m.get("id").and_then(Value::as_str).unwrap_or("").to_string();
                let sender = m.get("from").and_then(Value::as_str).unwrap_or("").to_string();
                let ts = m
                    .get("timestamp")
                    .and_then(Value::as_str)
                    .map(|s| s.to_string());
                out.push(super::InboundMessage {
                    id,
                    chat_id: sender.clone(),
                    sender: if sender.is_empty() { None } else { Some(sender) },
                    text,
                    ts,
                });
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::server::channels::mock::{closed_port_url, mock_server, GOOD_TOKEN};

    const GOOD_STORED: &str = "123456:TESTTOKEN:987654321";

    #[test]
    fn split_credential_ok() {
        let (t, p) = split_credential(GOOD_STORED).expect("split");
        assert_eq!(t, "123456:TESTTOKEN");
        assert_eq!(p, "987654321");
    }

    #[test]
    fn split_credential_malformed() {
        assert!(split_credential("no-colon").is_err());
        assert!(split_credential(":onlypnid").is_err());
        assert!(split_credential("token:").is_err());
    }

    #[test]
    fn parse_verify_ok() {
        let body: Value = serde_json::from_str(
            r#"{"id":"987654321","display_phone_number":"+84 90 123 4567","verified_name":"Boom"}"#,
        )
        .unwrap();
        let id = parse_verify(&body).expect("must parse");
        assert_eq!(id.phone_number_id, "987654321");
        assert_eq!(id.verified_name.as_deref(), Some("Boom"));
    }

    #[test]
    fn parse_send_ok() {
        let body: Value = serde_json::from_str(
            r#"{"messaging_product":"whatsapp","contacts":[{"input":"+84123","wa_id":"84123"}],"messages":[{"id":"wamid.HBgM..."}]}"#,
        )
        .unwrap();
        assert_eq!(parse_send(&body).expect("must parse"), "wamid.HBgM...");
    }

    #[test]
    fn parse_send_graph_error() {
        let body: Value = serde_json::from_str(
            r#"{"error":{"message":"Invalid parameter","code":131009}}"#,
        )
        .unwrap();
        let err = parse_send(&body).unwrap_err();
        assert!(err.contains("131009"), "err: {err}");
    }

    #[tokio::test]
    async fn verify_token_hits_phone_number_endpoint() {
        let (base, handle) = mock_server(
            200,
            r#"{"id":"987654321","display_phone_number":"+84","verified_name":"Boom"}"#,
        )
        .await;
        let id = verify_token(&base, GOOD_STORED).await.expect("verify");
        assert_eq!(id.phone_number_id, "987654321");
        let reqs = handle.await.expect("server task");
        assert!(
            reqs[0].starts_with("GET /987654321?fields="),
            "req: {}",
            reqs[0]
        );
        assert!(
            reqs[0].to_lowercase().contains("authorization: bearer "),
            "req: {}",
            reqs[0]
        );
    }

    #[tokio::test]
    async fn send_text_posts_whatsapp_payload() {
        let (base, handle) = mock_server(
            200,
            r#"{"messages":[{"id":"wamid.1"}]}"#,
        )
        .await;
        let id = send_text(&base, GOOD_STORED, "84123456789", "hello")
            .await
            .expect("send");
        assert_eq!(id, "wamid.1");
        let reqs = handle.await.expect("server task");
        assert!(
            reqs[0].starts_with("POST /987654321/messages "),
            "req: {}",
            reqs[0]
        );
        assert!(reqs[0].contains(r#""messaging_product":"whatsapp""#), "req: {}", reqs[0]);
        assert!(reqs[0].contains(r#""to":"84123456789""#), "req: {}", reqs[0]);
    }

    #[tokio::test]
    async fn verify_token_connection_refused_is_generic() {
        let base = closed_port_url().await;
        let err = verify_token(&base, GOOD_STORED).await.unwrap_err();
        assert!(!err.is_empty());
        assert!(!err.contains("TESTTOKEN"), "err: {err}");
    }

    #[test]
    fn parse_webhook_text_messages() {
        let body: Value = serde_json::from_str(
            r#"{"object":"whatsapp_business_account","entry":[{"id":"WABA1","changes":[{"value":{
                "messages":[
                    {"from":"84123456789","id":"wamid.1","timestamp":"1729000000","type":"text","text":{"body":"hello"}},
                    {"from":"84123456789","id":"wamid.2","timestamp":"1729000001","type":"image","image":{"id":"img1"}}
                ]}}]}]}"#,
        )
        .unwrap();
        let msgs = parse_webhook(&body);
        assert_eq!(msgs.len(), 1, "only text messages are kept");
        assert_eq!(msgs[0].id, "wamid.1");
        assert_eq!(msgs[0].chat_id, "84123456789");
        assert_eq!(msgs[0].text, "hello");
    }

    #[test]
    fn parse_webhook_ignores_non_waba() {
        let body: Value = serde_json::from_str(r#"{"object":"page"}"#).unwrap();
        assert!(parse_webhook(&body).is_empty());
    }
}
