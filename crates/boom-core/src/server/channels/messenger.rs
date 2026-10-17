//! Facebook Messenger Platform client (Graph API): Page-token verify and
//! message send.
//!
//! The credential is a **Page access token** (from a Facebook Page linked
//! to the app). The token travels as the `access_token` query parameter,
//! exactly like every Graph API client.
//!
//! Ingress has no polling API on Messenger: receiving user messages
//! requires a **webhook subscription** (`messages` webhook → your public
//! HTTPS endpoint → `POST /api/channels/messenger/webhook`). The registry
//! answers `poll_updates` with an honest 501 pointing at `docs/channels.md`
//! instead of faking it.

use std::time::Duration;

use serde_json::Value;

/// Graph API base (overridable in tests).
pub const API_BASE: &str = "https://graph.facebook.com/v21.0";
const TIMEOUT: Duration = Duration::from_secs(15);

/// Page identity returned by `GET /me`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PageIdentity {
    pub id: String,
    pub name: String,
}

/// Parse a `GET /me?fields=id,name` response.
pub fn parse_verify(body: &Value) -> Result<PageIdentity, String> {
    if let Some(err) = body.get("error") {
        return Err(describe_graph_error(err));
    }
    let id = body
        .get("id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "Messenger verify response missing page id".to_string())?;
    let name = body
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    Ok(PageIdentity {
        id: id.to_string(),
        name,
    })
}

/// Parse a `POST /me/messages` response. `Ok` → message id (`mid`).
pub fn parse_send(body: &Value) -> Result<String, String> {
    if let Some(err) = body.get("error") {
        return Err(describe_graph_error(err));
    }
    body.get("message_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .ok_or_else(|| "Messenger send response missing message_id".to_string())
}

/// Render a Graph API `{"error": {"message", "code", ...}}` body.
/// Never contains the token (tokens are query params; the error body is
/// Graph's own JSON).
fn describe_graph_error(err: &Value) -> String {
    let message = err
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or("unknown error")
        .trim();
    let code = err.get("code").and_then(Value::as_i64);
    match code {
        Some(c) => format!("Messenger request failed (code {c}): {message}"),
        None => format!("Messenger request failed: {message}"),
    }
}

/// Read a JSON response body; degrade non-JSON/empty to generic text.
async fn read_json_body(resp: reqwest::Response) -> Result<Value, String> {
    let status = resp.status();
    let text = resp.text().await.map_err(|_| "request failed".to_string())?;
    let text = text.trim();
    if text.is_empty() {
        return Err(format!(
            "Messenger returned HTTP {} with an empty body",
            status.as_u16()
        ));
    }
    serde_json::from_str::<Value>(text)
        .map_err(|_| format!("Messenger returned HTTP {} with a non-JSON body", status.as_u16()))
}

/// Generic transport failure. The token is a query param, so reqwest
/// errors could echo it — stay generic.
fn transport_error(err: reqwest::Error) -> String {
    if err.is_timeout() {
        "connection to Messenger timed out".to_string()
    } else if err.is_connect() {
        "could not reach Messenger (connection failed)".to_string()
    } else {
        "request to Messenger failed".to_string()
    }
}

fn client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .timeout(TIMEOUT)
        .build()
        .map_err(|_| "failed to build HTTP client".to_string())
}

/// Live verify: `GET /me?fields=id,name` with the Page access token.
/// `Err` never contains the token.
pub async fn verify_token(api_base: &str, page_token: &str) -> Result<PageIdentity, String> {
    let url = format!(
        "{}/me?fields=id,name&access_token={}",
        api_base.trim_end_matches('/'),
        page_token
    );
    let resp = client()?.get(&url).send().await.map_err(transport_error)?;
    let body = read_json_body(resp).await?;
    parse_verify(&body)
}

/// Live send: `POST /me/messages` to a PSID recipient.
/// `Ok` → Graph message id.
pub async fn send_text(
    api_base: &str,
    page_token: &str,
    psid: &str,
    text: &str,
) -> Result<String, String> {
    let url = format!(
        "{}/me/messages?access_token={}",
        api_base.trim_end_matches('/'),
        page_token
    );
    let resp = client()?
        .post(&url)
        .json(&serde_json::json!({
            "recipient": { "id": psid },
            "messaging_type": "RESPONSE",
            "message": { "text": text },
        }))
        .send()
        .await
        .map_err(transport_error)?;
    let body = read_json_body(resp).await?;
    parse_send(&body)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::server::channels::mock::{closed_port_url, mock_server, GOOD_TOKEN};

    #[test]
    fn parse_verify_ok() {
        let body: Value =
            serde_json::from_str(r#"{"id":"987","name":"Boom Page"}"#).unwrap();
        let id = parse_verify(&body).expect("must parse");
        assert_eq!(
            id,
            PageIdentity {
                id: "987".to_string(),
                name: "Boom Page".to_string(),
            }
        );
    }

    #[test]
    fn parse_verify_graph_error() {
        let body: Value = serde_json::from_str(
            r#"{"error":{"message":"Invalid OAuth access token.","type":"OAuthException","code":190}}"#,
        )
        .unwrap();
        let err = parse_verify(&body).unwrap_err();
        assert!(err.contains("190"), "err: {err}");
        assert!(err.contains("Invalid OAuth access token"), "err: {err}");
        assert!(!err.contains("TESTTOKEN"), "err: {err}");
    }

    #[test]
    fn parse_send_ok() {
        let body: Value =
            serde_json::from_str(r#"{"recipient_id":"111","message_id":"mid.123"}"#).unwrap();
        assert_eq!(parse_send(&body).expect("must parse"), "mid.123");
    }

    #[test]
    fn parse_send_graph_error() {
        let body: Value = serde_json::from_str(
            r#"{"error":{"message":"This person isn't available right now.","code":200}}"#,
        )
        .unwrap();
        let err = parse_send(&body).unwrap_err();
        assert!(err.contains("200"), "err: {err}");
    }

    #[tokio::test]
    async fn verify_token_hits_me_endpoint() {
        let (base, handle) = mock_server(
            200,
            r#"{"id":"987","name":"Boom Page"}"#,
        )
        .await;
        let id = verify_token(&base, GOOD_TOKEN).await.expect("verify");
        assert_eq!(id.id, "987");
        let reqs = handle.await.expect("server task");
        assert_eq!(reqs.len(), 1);
        assert!(
            reqs[0].starts_with("GET /me?fields=id,name&access_token="),
            "req: {}",
            reqs[0]
        );
    }

    #[tokio::test]
    async fn verify_token_rejected_never_echoes_token() {
        let (base, _handle) = mock_server(
            400,
            r#"{"error":{"message":"Invalid OAuth access token.","code":190}}"#,
        )
        .await;
        let err = verify_token(&base, GOOD_TOKEN).await.unwrap_err();
        assert!(err.contains("Invalid OAuth access token"), "err: {err}");
        assert!(!err.contains("TESTTOKEN"), "err: {err}");
    }

    #[tokio::test]
    async fn verify_token_connection_refused_is_generic() {
        let base = closed_port_url().await;
        let err = verify_token(&base, GOOD_TOKEN).await.unwrap_err();
        assert!(!err.is_empty());
        assert!(!err.contains("TESTTOKEN"), "err: {err}");
    }

    #[tokio::test]
    async fn send_text_posts_recipient_json() {
        let (base, handle) = mock_server(
            200,
            r#"{"recipient_id":"111","message_id":"mid.abc"}"#,
        )
        .await;
        let id = send_text(&base, GOOD_TOKEN, "111", "hello")
            .await
            .expect("send");
        assert_eq!(id, "mid.abc");
        let reqs = handle.await.expect("server task");
        assert!(
            reqs[0].starts_with("POST /me/messages?access_token="),
            "req: {}",
            reqs[0]
        );
        assert!(reqs[0].contains(r#""id":"111""#), "req: {}", reqs[0]);
        assert!(reqs[0].contains(r#""text":"hello""#), "req: {}", reqs[0]);
        assert!(
            reqs[0].contains(r#""messaging_type":"RESPONSE""#),
            "req: {}",
            reqs[0]
        );
    }
}
