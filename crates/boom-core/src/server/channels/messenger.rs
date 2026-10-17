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
/// Facebook Login dialog base (the API base is overridable in tests, but
/// the login dialog always lives on www.facebook.com).
pub const OAUTH_DIALOG_BASE: &str = "https://www.facebook.com/v21.0/dialog/oauth";
/// Scopes the OAuth flow requests: list the user's Pages and use
/// Messenger on the chosen Page.
pub const OAUTH_SCOPES: &str = "pages_messaging,pages_show_list";
const TIMEOUT: Duration = Duration::from_secs(15);

/// Page identity returned by `GET /me`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PageIdentity {
    pub id: String,
    pub name: String,
}

/// Facebook App credentials for the Messenger OAuth login flow.
/// Stored by the registry at `$BOOM_HOME/channels/messenger_oauth.json`
/// (0600); never echoed into responses, logs, or errors.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OAuthConfig {
    pub app_id: String,
    pub app_secret: String,
}

/// One Facebook Page the user manages, with its Page access token.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PageToken {
    pub id: String,
    pub name: String,
    pub access_token: String,
}

/// Minimal RFC 3986 percent-encoding for OAuth query values (keeps the
/// unreserved set `A-Z a-z 0-9 - _ . ~` verbatim).
fn pct_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// Build the Facebook Login URL the user opens in their browser.
/// `redirect_uri` must be registered in the Facebook App's "Valid OAuth
/// Redirect URIs" (localhost is exempt while the app is in dev mode).
pub fn authorize_url(app_id: &str, redirect_uri: &str, state: &str) -> String {
    format!(
        "{OAUTH_DIALOG_BASE}?client_id={}&redirect_uri={}&scope={}&response_type=code&state={}",
        pct_encode(app_id.trim()),
        pct_encode(redirect_uri),
        pct_encode(OAUTH_SCOPES),
        pct_encode(state),
    )
}

/// Parse a `GET /oauth/access_token` response. `Ok` → user access token.
/// Never echoes the code or secret: Graph error bodies are rendered via
/// [`describe_graph_error`].
pub fn parse_token_exchange(body: &Value) -> Result<String, String> {
    if let Some(err) = body.get("error") {
        return Err(describe_graph_error(err));
    }
    body.get("access_token")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .ok_or_else(|| "Messenger token exchange returned no access_token".to_string())
}

/// Parse a `GET /me/accounts` response into page tokens.
pub fn parse_accounts(body: &Value) -> Result<Vec<PageToken>, String> {
    if let Some(err) = body.get("error") {
        return Err(describe_graph_error(err));
    }
    let data = body
        .get("data")
        .and_then(Value::as_array)
        .ok_or_else(|| "Messenger accounts response has no data array".to_string())?;
    let mut pages = Vec::new();
    for entry in data {
        let (Some(id), Some(name), Some(token)) = (
            entry.get("id").and_then(Value::as_str),
            entry.get("name").and_then(Value::as_str),
            entry.get("access_token").and_then(Value::as_str),
        ) else {
            continue;
        };
        if token.is_empty() {
            continue;
        }
        pages.push(PageToken {
            id: id.to_string(),
            name: name.to_string(),
            access_token: token.to_string(),
        });
    }
    Ok(pages)
}

/// Live exchange: `GET /oauth/access_token` turns the login `code` into a
/// short-lived user access token. `Err` never contains the code or secret.
pub async fn exchange_code(
    api_base: &str,
    cfg: &OAuthConfig,
    redirect_uri: &str,
    code: &str,
) -> Result<String, String> {
    let url = format!(
        "{}/oauth/access_token?client_id={}&client_secret={}&redirect_uri={}&code={}",
        api_base.trim_end_matches('/'),
        pct_encode(cfg.app_id.trim()),
        pct_encode(&cfg.app_secret),
        pct_encode(redirect_uri),
        pct_encode(code),
    );
    let body = read_json_body(client()?.get(&url).send().await.map_err(transport_error)?).await?;
    parse_token_exchange(&body)
}

/// Live list: `GET /me/accounts` returns the Pages the user manages with
/// their Page access tokens. `Err` never contains the user token.
pub async fn list_pages(api_base: &str, user_token: &str) -> Result<Vec<PageToken>, String> {
    let url = format!(
        "{}/me/accounts?fields=id,name,access_token&access_token={}",
        api_base.trim_end_matches('/'),
        pct_encode(user_token),
    );
    let body = read_json_body(client()?.get(&url).send().await.map_err(transport_error)?).await?;
    parse_accounts(&body)
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

    // ---------------- OAuth login flow ----------------

    #[test]
    fn pct_encode_keeps_unreserved_verbatim() {
        assert_eq!(pct_encode("abc123-_.~"), "abc123-_.~");
        assert_eq!(
            pct_encode("http://localhost:18080/cb?x=1"),
            "http%3A%2F%2Flocalhost%3A18080%2Fcb%3Fx%3D1"
        );
        assert_eq!(pct_encode("a b+c"), "a%20b%2Bc");
    }

    #[test]
    fn authorize_url_has_all_params() {
        let url = authorize_url("123456", "http://localhost:18080/cb", "state9");
        assert!(
            url.starts_with("https://www.facebook.com/v21.0/dialog/oauth?"),
            "url: {url}"
        );
        assert!(url.contains("client_id=123456"), "url: {url}");
        assert!(
            url.contains("redirect_uri=http%3A%2F%2Flocalhost%3A18080%2Fcb"),
            "url: {url}"
        );
        assert!(url.contains("response_type=code"), "url: {url}");
        assert!(url.contains("state=state9"), "url: {url}");
        assert!(url.contains("scope=pages_messaging%2Cpages_show_list"), "url: {url}");
    }

    #[test]
    fn parse_token_exchange_ok() {
        let body: Value = serde_json::from_str(
            r#"{"access_token":"USERTOKEN","token_type":"bearer","expires_in":3600}"#,
        )
        .unwrap();
        assert_eq!(parse_token_exchange(&body).expect("parse"), "USERTOKEN");
    }

    #[test]
    fn parse_token_exchange_graph_error_never_echoes_code() {
        let body: Value = serde_json::from_str(
            r#"{"error":{"message":"Invalid verification code format.","code":100}}"#,
        )
        .unwrap();
        let err = parse_token_exchange(&body).unwrap_err();
        assert!(err.contains("100"), "err: {err}");
        assert!(!err.contains("SECRET"), "err: {err}");
    }

    #[test]
    fn parse_accounts_ok() {
        let body: Value = serde_json::from_str(
            r#"{"data":[{"id":"987","name":"Boom Page","access_token":"PAGETOKEN"},{"id":"654","name":"NoToken"}]}"#,
        )
        .unwrap();
        let pages = parse_accounts(&body).expect("parse");
        assert_eq!(pages.len(), 1, "entries without a token are skipped");
        assert_eq!(pages[0].id, "987");
        assert_eq!(pages[0].name, "Boom Page");
        assert_eq!(pages[0].access_token, "PAGETOKEN");
    }

    #[test]
    fn parse_accounts_empty_is_ok() {
        let body: Value = serde_json::from_str(r#"{"data":[]}"#).unwrap();
        assert!(parse_accounts(&body).expect("parse").is_empty());
    }

    #[tokio::test]
    async fn exchange_code_hits_token_endpoint() {
        let (base, handle) = mock_server(
            200,
            r#"{"access_token":"USERTOKEN","token_type":"bearer"}"#,
        )
        .await;
        let cfg = OAuthConfig {
            app_id: "123".to_string(),
            app_secret: "s3cr3t".to_string(),
        };
        let token = exchange_code(&base, &cfg, "http://localhost:1/cb", "thecode")
            .await
            .expect("exchange");
        assert_eq!(token, "USERTOKEN");
        let reqs = handle.await.expect("server task");
        assert_eq!(reqs.len(), 1);
        assert!(
            reqs[0].starts_with("GET /oauth/access_token?"),
            "req: {}",
            reqs[0]
        );
        assert!(reqs[0].contains("client_id=123"), "req: {}", reqs[0]);
        assert!(reqs[0].contains("code=thecode"), "req: {}", reqs[0]);
        // The app secret must be sent for the code exchange (Facebook
        // requires it); it is a query param like every Graph call, and our
        // error paths never echo request URLs.
    }

    #[tokio::test]
    async fn list_pages_hits_accounts_endpoint() {
        let (base, handle) = mock_server(
            200,
            r#"{"data":[{"id":"987","name":"Boom Page","access_token":"PAGETOKEN"}]}"#,
        )
        .await;
        let pages = list_pages(&base, "USERTOKEN").await.expect("pages");
        assert_eq!(pages.len(), 1);
        assert_eq!(pages[0].access_token, "PAGETOKEN");
        let reqs = handle.await.expect("server task");
        assert!(
            reqs[0].starts_with("GET /me/accounts?"),
            "req: {}",
            reqs[0]
        );
    }
}
