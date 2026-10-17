//! Gmail connector — Google OAuth2 + `gmail_search` / `gmail_read` / `gmail_send`.
//!
//! Target path: `crates/boom-core/src/connectors/gmail.rs`
//!
//! Auth: standard Google OAuth2 (authorization-code flow with offline access).
//! The user opens the URL from [`Connector::connect_url`], authorizes the app
//! in the browser, then pastes the authorization `code` back into Boom
//! (`exchange_code`). There is no local callback HTTP server in the core yet,
//! so the manual paste step is required (documented in REPORT.md).
//!
//! The token lives in `$BOOM_HOME/connectors/gmail.json` (mode 0600), written
//! atomically by the `super::` token helpers. Tokens are never logged.
//!
//! Scopes:
//!   * `https://www.googleapis.com/auth/gmail.readonly`
//!   * `https://www.googleapis.com/auth/gmail.send`
//!
//! Assumptions about `super` (the framework worker owns `connectors/mod.rs`):
//!   * `super::{Connector, ConnectorToken, load_token, save_token, delete_token}`
//!     exist with the signatures from the shared contract.
//!   * `load_token`/`save_token` honour `$BOOM_HOME` (env) for the file dir.
//!   * `ConnectorToken` has a `Debug` impl that redacts `access_token`
//!     (there is a test below asserting it).

use std::sync::OnceLock;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use serde_json::Value;

use super::{delete_token, load_token, save_token, Connector, ConnectorToken};
use crate::tools::{opt_u64, req_str, schema_of, Tool, ToolError};

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/// Token file id under `$BOOM_HOME/connectors/`.
const CONNECTOR_ID: &str = "gmail";

const OAUTH_AUTH_URL: &str = "https://accounts.google.com/o/oauth2/v2/auth";
const OAUTH_TOKEN_URL: &str = "https://oauth2.googleapis.com/token";
const GMAIL_API: &str = "https://gmail.googleapis.com/gmail/v1";

/// Documented redirect URI. The code is pasted manually into `exchange_code`
/// until a local callback server exists.
const REDIRECT_URI: &str = "http://localhost:18080/api/connectors/gmail/callback";

const SCOPES: &str = "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send";

/// Every HTTP call (OAuth + Gmail API) gets this deadline.
const HTTP_TIMEOUT: Duration = Duration::from_secs(30);

/// Refresh a bit early so a call started just before expiry still succeeds.
const EXPIRY_SKEW_SECS: i64 = 60;

/// Honest-degrade message returned by every tool while not connected.
pub const NOT_CONNECTED_MSG: &str =
    "gmail is not connected — open Settings → Connectors to connect";

/// Cap decoded mail bodies so one huge email cannot blow up the context.
const MAX_BODY_CHARS: usize = 100_000;

static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();

fn client() -> &'static reqwest::Client {
    CLIENT.get_or_init(|| {
        reqwest::Client::builder()
            .timeout(HTTP_TIMEOUT)
            .user_agent("boom-core-gmail/0.1 (+https://github.com/TheOwlOps/Boom)")
            .build()
            .expect("gmail reqwest client must build")
    })
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

fn now_secs() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// Minimal percent-encoder for OAuth query/form values (no extra deps).
pub(crate) fn urlencode(s: &str) -> String {
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

fn token_expired(tok: &ConnectorToken) -> bool {
    match tok.expires_at {
        Some(exp) => now_secs() >= exp - EXPIRY_SKEW_SECS,
        None => false, // no expiry info — assume usable, the API 401 path catches it
    }
}

/// OAuth client id: env first, then the `extra.client_id` saved in the token
/// file from a previous exchange (lets `connect_url` keep working after the
/// env var is removed).
fn client_id() -> Option<String> {
    if let Ok(v) = std::env::var("BOOM_GOOGLE_CLIENT_ID") {
        if !v.trim().is_empty() {
            return Some(v);
        }
    }
    load_token(CONNECTOR_ID)
        .and_then(|t| t.extra.get("client_id").and_then(Value::as_str).map(str::to_string))
}

fn client_secret() -> Option<String> {
    if let Ok(v) = std::env::var("BOOM_GOOGLE_CLIENT_SECRET") {
        if !v.trim().is_empty() {
            return Some(v);
        }
    }
    None
}

/// Pure constructor for the OAuth consent URL — easy to unit-test.
pub(crate) fn build_connect_url(client_id: &str) -> String {
    format!(
        "{OAUTH_AUTH_URL}?client_id={}&redirect_uri={}&response_type=code&scope={}&access_type=offline&prompt=consent",
        urlencode(client_id),
        urlencode(REDIRECT_URI),
        urlencode(SCOPES),
    )
}

fn oauth_error(body: &Value) -> String {
    let desc = body
        .get("error_description")
        .or_else(|| body.get("error"))
        .and_then(Value::as_str)
        .unwrap_or("unknown OAuth error");
    format!("Google OAuth failed: {desc}")
}

// ---------------------------------------------------------------------------
// Connector
// ---------------------------------------------------------------------------

/// Gmail connector. Stateless — the token is reloaded from the token file on
/// every call so concurrent processes see fresh credentials.
#[derive(Debug, Default)]
pub struct GmailConnector;

impl GmailConnector {
    pub fn new() -> Self {
        Self
    }

    /// Valid access token, refreshing via the refresh token when expired.
    async fn access_token(&self) -> Result<String, ToolError> {
        let tok = load_token(CONNECTOR_ID)
            .ok_or_else(|| ToolError::Other(NOT_CONNECTED_MSG.to_string()))?;
        if token_expired(&tok) {
            return self.refresh(&tok).await;
        }
        Ok(tok.access_token.clone())
    }

    /// Exchange the stored refresh token for a new access token, persist it,
    /// and return the fresh access token.
    async fn refresh(&self, tok: &ConnectorToken) -> Result<String, ToolError> {
        let refresh_token = tok.refresh_token.clone().ok_or_else(|| {
            ToolError::Other(
                "gmail access token expired and no refresh token is stored — reconnect via Settings → Connectors".to_string(),
            )
        })?;
        let cid = client_id().ok_or_else(|| {
            ToolError::Other(
                "Google OAuth client id is missing (set BOOM_GOOGLE_CLIENT_ID)".to_string(),
            )
        })?;
        let secret = client_secret().ok_or_else(|| {
            ToolError::Other(
                "Google OAuth client secret is missing (set BOOM_GOOGLE_CLIENT_SECRET)".to_string(),
            )
        })?;

        let body = format!(
            "grant_type=refresh_token&refresh_token={}&client_id={}&client_secret={}",
            urlencode(&refresh_token),
            urlencode(&cid),
            urlencode(&secret),
        );
        let resp = client()
            .post(OAUTH_TOKEN_URL)
            .header("content-type", "application/x-www-form-urlencoded")
            .body(body)
            .send()
            .await
            .map_err(|e| ToolError::Http(format!("token refresh request failed: {e}")))?;
        let status = resp.status();
        let json: Value = resp
            .json()
            .await
            .map_err(|e| ToolError::Http(format!("token refresh: bad JSON: {e}")))?;
        if !status.is_success() {
            return Err(ToolError::Other(oauth_error(&json)));
        }
        let access = json
            .get("access_token")
            .and_then(Value::as_str)
            .ok_or_else(|| ToolError::Http("token refresh: missing access_token".into()))?
            .to_string();
        let expires_at = json
            .get("expires_in")
            .and_then(Value::as_i64)
            .map(|s| now_secs() + s);
        // Google usually does not rotate refresh tokens on refresh; keep the
        // old one when the response omits it.
        let refreshed = ConnectorToken {
            access_token: access.clone(),
            refresh_token: json
                .get("refresh_token")
                .and_then(Value::as_str)
                .map(str::to_string)
                .or(refresh_token.into()),
            expires_at,
            extra: tok.extra.clone(),
        };
        save_token(CONNECTOR_ID, &refreshed)
            .map_err(|e| ToolError::Other(format!("failed saving refreshed gmail token: {e}")))?;
        Ok(access)
    }

    /// GET a Gmail API URL, retrying once with a forced refresh on 401.
    async fn api_get(&self, url: &str) -> Result<Value, ToolError> {
        let token = self.access_token().await?;
        match self.get_with(&token, url).await {
            Err(ToolError::Http(ref e)) if e.contains("401") => {
                let tok = load_token(CONNECTOR_ID)
                    .ok_or_else(|| ToolError::Other(NOT_CONNECTED_MSG.to_string()))?;
                let fresh = self.refresh(&tok).await?;
                self.get_with(&fresh, url).await
            }
            other => other,
        }
    }

    async fn get_with(&self, token: &str, url: &str) -> Result<Value, ToolError> {
        let resp = client()
            .get(url)
            .bearer_auth(token)
            .send()
            .await
            .map_err(|e| ToolError::Http(format!("gmail request failed: {e}")))?;
        let status = resp.status();
        let json: Value = resp
            .json()
            .await
            .map_err(|e| ToolError::Http(format!("gmail: bad JSON response: {e}")))?;
        if status == reqwest::StatusCode::UNAUTHORIZED {
            return Err(ToolError::Http("401 Unauthorized from Gmail API".into()));
        }
        if !status.is_success() {
            let msg = json
                .get("error")
                .and_then(|e| e.get("message"))
                .and_then(Value::as_str)
                .unwrap_or("unknown Gmail API error");
            return Err(ToolError::Http(format!(
                "gmail API error (HTTP {}): {msg}",
                status.as_u16()
            )));
        }
        Ok(json)
    }

    /// POST a Gmail API URL, retrying once with a forced refresh on 401.
    async fn api_post(&self, url: &str, payload: &Value) -> Result<Value, ToolError> {
        let token = self.access_token().await?;
        match self.post_with(&token, url, payload).await {
            Err(ToolError::Http(ref e)) if e.contains("401") => {
                let tok = load_token(CONNECTOR_ID)
                    .ok_or_else(|| ToolError::Other(NOT_CONNECTED_MSG.to_string()))?;
                let fresh = self.refresh(&tok).await?;
                self.post_with(&fresh, url, payload).await
            }
            other => other,
        }
    }

    async fn post_with(&self, token: &str, url: &str, payload: &Value) -> Result<Value, ToolError> {
        let resp = client()
            .post(url)
            .bearer_auth(token)
            .json(payload)
            .send()
            .await
            .map_err(|e| ToolError::Http(format!("gmail request failed: {e}")))?;
        let status = resp.status();
        let json: Value = resp
            .json()
            .await
            .map_err(|e| ToolError::Http(format!("gmail: bad JSON response: {e}")))?;
        if status == reqwest::StatusCode::UNAUTHORIZED {
            return Err(ToolError::Http("401 Unauthorized from Gmail API".into()));
        }
        if !status.is_success() {
            let msg = json
                .get("error")
                .and_then(|e| e.get("message"))
                .and_then(Value::as_str)
                .unwrap_or("unknown Gmail API error");
            return Err(ToolError::Http(format!(
                "gmail API error (HTTP {}): {msg}",
                status.as_u16()
            )));
        }
        Ok(json)
    }
}

#[async_trait]
impl Connector for GmailConnector {
    fn id(&self) -> &'static str {
        CONNECTOR_ID
    }

    fn name(&self) -> &'static str {
        "Gmail"
    }

    fn description(&self) -> &'static str {
        "Search, read and send email through your Google account (OAuth2)."
    }

    fn auth_kind(&self) -> &'static str {
        "oauth2"
    }

    fn is_connected(&self) -> bool {
        load_token(CONNECTOR_ID).map(|t| !t.access_token.trim().is_empty()).unwrap_or(false)
    }

    fn connect_url(&self) -> Result<String, String> {
        let cid = client_id().ok_or_else(|| {
            "Google OAuth client is not configured: set BOOM_GOOGLE_CLIENT_ID (and BOOM_GOOGLE_CLIENT_SECRET) \
             from a Google Cloud OAuth 2.0 client ID, then retry"
                .to_string()
        })?;
        Ok(build_connect_url(&cid))
    }

    async fn exchange_code(&mut self, code: &str) -> Result<(), String> {
        let code = code.trim();
        if code.is_empty() {
            return Err("authorization code is empty".to_string());
        }
        let cid = client_id().ok_or_else(|| {
            "BOOM_GOOGLE_CLIENT_ID is not set — create an OAuth 2.0 client in Google Cloud first"
                .to_string()
        })?;
        let secret = client_secret().ok_or_else(|| {
            "BOOM_GOOGLE_CLIENT_SECRET is not set".to_string()
        })?;

        let body = format!(
            "code={}&client_id={}&client_secret={}&redirect_uri={}&grant_type=authorization_code",
            urlencode(code),
            urlencode(&cid),
            urlencode(&secret),
            urlencode(REDIRECT_URI),
        );
        let resp = client()
            .post(OAUTH_TOKEN_URL)
            .header("content-type", "application/x-www-form-urlencoded")
            .body(body)
            .send()
            .await
            .map_err(|e| format!("token exchange request failed: {e}"))?;
        let status = resp.status();
        let json: Value = resp
            .json()
            .await
            .map_err(|e| format!("token exchange: bad JSON: {e}"))?;
        if !status.is_success() {
            return Err(oauth_error(&json));
        }
        let access = json
            .get("access_token")
            .and_then(Value::as_str)
            .ok_or_else(|| "token exchange: missing access_token".to_string())?
            .to_string();
        let refresh = json
            .get("refresh_token")
            .and_then(Value::as_str)
            .map(str::to_string);
        let expires_at = json
            .get("expires_in")
            .and_then(Value::as_i64)
            .map(|s| now_secs() + s);

        let token = ConnectorToken {
            access_token: access,
            refresh_token: refresh,
            expires_at,
            extra: serde_json::json!({
                "client_id": cid,
                "obtained_at": now_secs(),
            }),
        };
        save_token(CONNECTOR_ID, &token)
            .map_err(|e| format!("failed saving gmail token: {e}"))?;
        Ok(())
    }

    fn disconnect(&mut self) -> Result<(), String> {
        delete_token(CONNECTOR_ID).map_err(|e| format!("failed deleting gmail token: {e}"))?;
        Ok(())
    }

    fn register_tools(&self, reg: &mut crate::tools::ToolRegistry) {
        reg.register("email", GmailSearch);
        reg.register("email", GmailRead);
        reg.register("email", GmailSend);
    }
}

// ---------------------------------------------------------------------------
// Message parsing (pure, unit-testable)
// ---------------------------------------------------------------------------

/// Headers of a message payload as a lowercase-name → value map.
pub(crate) fn header_map(payload: &Value) -> std::collections::HashMap<String, String> {
    let mut out = std::collections::HashMap::new();
    if let Some(arr) = payload.get("headers").and_then(Value::as_array) {
        for h in arr {
            if let (Some(n), Some(v)) = (
                h.get("name").and_then(Value::as_str),
                h.get("value").and_then(Value::as_str),
            ) {
                out.entry(n.to_ascii_lowercase())
                    .or_insert_with(|| v.to_string());
            }
        }
    }
    out
}

/// Recursively collect `(mime, base64url_data)` leaf parts, depth-first.
fn collect_parts(payload: &Value, out: &mut Vec<(String, String)>) {
    let mime = payload
        .get("mimeType")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    if let Some(parts) = payload.get("parts").and_then(Value::as_array) {
        for p in parts {
            collect_parts(p, out);
        }
    } else if let Some(data) = payload
        .get("body")
        .and_then(|b| b.get("data"))
        .and_then(Value::as_str)
    {
        out.push((mime, data.to_string()));
    }
}

/// Decode Gmail's base64url (no padding) body data into text.
pub(crate) fn decode_body_data(data: &str) -> Result<String, String> {
    let bytes = URL_SAFE_NO_PAD
        .decode(data)
        .map_err(|e| format!("invalid base64url body data: {e}"))?;
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

/// Very small HTML → text stripper for the `text/html` fallback.
pub(crate) fn strip_html(html: &str) -> String {
    let mut out = String::with_capacity(html.len());
    let mut in_tag = false;
    for ch in html.chars() {
        match ch {
            '<' => in_tag = true,
            '>' => {
                in_tag = false;
                out.push(' ');
            }
            _ if !in_tag => out.push(ch),
            _ => {}
        }
    }
    out.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&#39;", "'")
}

/// Parsed view of a `users.messages.get?format=full` response.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MessageView {
    pub id: String,
    pub from: String,
    pub to: String,
    pub subject: String,
    pub date: String,
    pub body_text: String,
}

/// Parse a full message resource. Prefers `text/plain` parts; falls back to
/// `text/html` (stripped). Body is truncated to `MAX_BODY_CHARS`.
pub(crate) fn parse_message_full(msg: &Value) -> Result<MessageView, String> {
    let id = msg
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let payload = msg
        .get("payload")
        .ok_or_else(|| "message is missing payload".to_string())?;
    let headers = header_map(payload);
    let get = |k: &str| headers.get(k).cloned().unwrap_or_default();

    let mut parts = Vec::new();
    collect_parts(payload, &mut parts);

    let pick = |want: &str| {
        parts
            .iter()
            .find(|(m, _)| m.eq_ignore_ascii_case(want))
            .map(|(_, d)| d.clone())
    };

    let mut body_text = String::new();
    if let Some(data) = pick("text/plain") {
        body_text = decode_body_data(&data)?;
    } else if let Some(data) = pick("text/html") {
        body_text = strip_html(&decode_body_data(&data)?);
    }
    // Collapse excessive whitespace left by tag stripping.
    let body_text: String = body_text
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(MAX_BODY_CHARS)
        .collect();

    Ok(MessageView {
        id,
        from: get("from"),
        to: get("to"),
        subject: get("subject"),
        date: get("date"),
        body_text,
    })
}

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

// ---------------- gmail_search ----------------

pub struct GmailSearch;

#[async_trait]
impl Tool for GmailSearch {
    fn name(&self) -> &str {
        "gmail_search"
    }

    fn description(&self) -> &str {
        "Search Gmail (Gmail search syntax). Returns id/from/subject/date/snippet per message."
    }

    fn schema(&self) -> Value {
        schema_of(
            &[
                ("q", "string", "Gmail search query, e.g. 'from:boss subject:invoice newer_than:7d'"),
                ("max_results", "integer", "Max messages to return (default 10, max 50)"),
            ],
            &["q"],
        )
    }

    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let conn = GmailConnector::new();
        let q = req_str(&args, "q")?;
        let max = opt_u64(&args, "max_results", 10).clamp(1, 50);

        let list_url = format!(
            "{GMAIL_API}/users/me/messages?q={}&maxResults={max}&fields=messages(id),resultSizeEstimate",
            urlencode(&q),
        );
        let list = conn.api_get(&list_url).await?;
        let ids: Vec<String> = list
            .get("messages")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(|m| m.get("id").and_then(Value::as_str).map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();

        let mut out = Vec::with_capacity(ids.len());
        for id in ids {
            let url = format!(
                "{GMAIL_API}/users/me/messages/{}?format=metadata&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date&fields=id,snippet,payload/headers",
                urlencode(&id),
            );
            let m = conn.api_get(&url).await?;
            let headers = m.get("payload").map(header_map).unwrap_or_default();
            let get = |k: &str| headers.get(k).cloned().unwrap_or_default();
            out.push(serde_json::json!({
                "id": m.get("id").and_then(Value::as_str).unwrap_or(""),
                "from": get("from"),
                "subject": get("subject"),
                "date": get("date"),
                "snippet": m.get("snippet").and_then(Value::as_str).unwrap_or(""),
            }));
        }
        Ok(serde_json::json!({ "messages": out }))
    }
}

// ---------------- gmail_read ----------------

pub struct GmailRead;

#[async_trait]
impl Tool for GmailRead {
    fn name(&self) -> &str {
        "gmail_read"
    }

    fn description(&self) -> &str {
        "Read a full Gmail message by id. Returns from/to/subject/date/body_text (prefers text/plain)."
    }

    fn schema(&self) -> Value {
        schema_of(
            &[("id", "string", "Gmail message id (from gmail_search)")],
            &["id"],
        )
    }

    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let conn = GmailConnector::new();
        let id = req_str(&args, "id")?;
        let url = format!("{GMAIL_API}/users/me/messages/{}?format=full", urlencode(&id));
        let msg = conn.api_get(&url).await?;
        let view =
            parse_message_full(&msg).map_err(|e| ToolError::Http(format!("bad message payload: {e}")))?;
        Ok(serde_json::json!({
            "id": view.id,
            "from": view.from,
            "to": view.to,
            "subject": view.subject,
            "date": view.date,
            "body_text": view.body_text,
        }))
    }
}

// ---------------- gmail_send ----------------

pub struct GmailSend;

#[async_trait]
impl Tool for GmailSend {
    fn name(&self) -> &str {
        "gmail_send"
    }

    fn description(&self) -> &str {
        "Send an email via Gmail. Drafts are preferred for anything sensitive — this sends immediately."
    }

    fn schema(&self) -> Value {
        schema_of(
            &[
                ("to", "string", "Recipient email address"),
                ("subject", "string", "Email subject"),
                ("body", "string", "Plain-text email body"),
            ],
            &["to", "subject", "body"],
        )
    }

    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let conn = GmailConnector::new();
        let to = req_str(&args, "to")?;
        let subject = req_str(&args, "subject")?;
        let body = req_str(&args, "body")?;
        let raw = build_raw_message(&to, &subject, &body)?;
        let payload = serde_json::json!({ "raw": raw });
        let resp = conn
            .api_post(&format!("{GMAIL_API}/users/me/messages/send"), &payload)
            .await?;
        Ok(serde_json::json!({
            "id": resp.get("id").and_then(Value::as_str).unwrap_or(""),
            "thread_id": resp.get("threadId").and_then(Value::as_str).unwrap_or(""),
        }))
    }
}

/// Build a base64url-encoded RFC 2822 message. Rejects CR/LF in headers to
/// block header injection. Non-ASCII subjects get RFC 2047 encoding.
pub(crate) fn build_raw_message(to: &str, subject: &str, body: &str) -> Result<String, ToolError> {
    for (label, v) in [("to", to), ("subject", subject)] {
        if v.contains('\r') || v.contains('\n') {
            return Err(ToolError::InvalidArgs(format!(
                "{label} must not contain line breaks"
            )));
        }
    }
    if !to.contains('@') {
        return Err(ToolError::InvalidArgs("to is not a valid email address".into()));
    }
    let subject_hdr = if subject.is_ascii() {
        subject.to_string()
    } else {
        format!(
            "=?utf-8?B?{}?=",
            URL_SAFE_NO_PAD.encode(subject.as_bytes())
        )
    };
    let rfc2822 = format!(
        "To: {to}\r\nSubject: {subject_hdr}\r\nMIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n{body}"
    );
    Ok(URL_SAFE_NO_PAD.encode(rfc2822.as_bytes()))
}

// ---------------------------------------------------------------------------
// Tests (all offline — no Google network access)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // Serialize tests that mutate process-global env vars.

    struct EnvGuard {
        saved: Vec<(String, Option<String>)>,
    }

    impl EnvGuard {
        fn set(pairs: &[(&str, &str)]) -> Self {
            let saved = pairs
                .iter()
                .map(|(k, _)| (k.to_string(), std::env::var(k).ok()))
                .collect();
            for (k, v) in pairs {
                std::env::set_var(k, v);
            }
            EnvGuard { saved }
        }
        fn unset(keys: &[&str]) -> Self {
            let saved = keys
                .iter()
                .map(|k| (k.to_string(), std::env::var(k).ok()))
                .collect();
            for k in keys {
                std::env::remove_var(k);
            }
            EnvGuard { saved }
        }
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            for (k, v) in &self.saved {
                match v {
                    Some(val) => std::env::set_var(k, val),
                    None => std::env::remove_var(k),
                }
            }
        }
    }

    /// Point BOOM_HOME at an empty temp dir so token lookups see "not
    /// connected" deterministically. Assumes `super::` helpers read
    /// `$BOOM_HOME/connectors/gmail.json`.
    fn isolated_home() -> (tempfile::TempDir, EnvGuard) {
        let dir = tempfile::tempdir().expect("tempdir");
        let guard = EnvGuard::set(&[("BOOM_HOME", dir.path().to_str().unwrap())]);
        (dir, guard)
    }

    fn sample_full_message() -> Value {
        // text/plain nested inside multipart/alternative inside multipart/mixed,
        // plus a text/html sibling that must lose to the plain part.
        let plain = URL_SAFE_NO_PAD.encode(b"Hello, this is the plain body.");
        let html = URL_SAFE_NO_PAD.encode(b"<html><body><p>HTML <b>body</b></p></body></html>");
        serde_json::json!({
            "id": "18c4abc123",
            "snippet": "Hello, this is the plain",
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [
                    {"name": "From", "value": "alice@example.com"},
                    {"name": "To", "value": "me@gmail.com"},
                    {"name": "Subject", "value": "Test subject"},
                    {"name": "Date", "value": "Fri, 16 Oct 2026 09:00:00 +0700"}
                ],
                "parts": [
                    {
                        "mimeType": "multipart/alternative",
                        "parts": [
                            {"mimeType": "text/plain", "body": {"data": plain}},
                            {"mimeType": "text/html", "body": {"data": html}}
                        ]
                    }
                ]
            }
        })
    }

    #[tokio::test]
    async fn connect_url_contains_scope_and_client_id() {
        let _lock = crate::connectors::TEST_ENV_LOCK.lock().await;
        let _guard = EnvGuard::set(&[("BOOM_GOOGLE_CLIENT_ID", "test-client-id-123")]);
        let url = GmailConnector::new()
            .connect_url()
            .expect("connect_url with env client id");
        assert!(url.starts_with("https://accounts.google.com/o/oauth2/v2/auth?"));
        assert!(url.contains("client_id=test-client-id-123"), "url={url}");
        assert!(url.contains("response_type=code"));
        assert!(url.contains("access_type=offline"));
        assert!(url.contains("prompt=consent"));
        // Both scopes must be present (percent-encoded).
        assert!(url.contains("gmail.readonly"), "url={url}");
        assert!(url.contains("gmail.send"), "url={url}");
        assert!(
            url.contains(&urlencode("http://localhost:18080/api/connectors/gmail/callback")),
            "url={url}"
        );
    }

    #[tokio::test]
    async fn connect_url_errors_without_client_id() {
        let _lock = crate::connectors::TEST_ENV_LOCK.lock().await;
        let (_dir, _home) = isolated_home(); // no token file to fall back to
        let _guard = EnvGuard::unset(&["BOOM_GOOGLE_CLIENT_ID"]);
        let err = GmailConnector::new().connect_url().unwrap_err();
        assert!(err.contains("BOOM_GOOGLE_CLIENT_ID"), "err={err}");
    }

    #[allow(clippy::await_holding_lock)]
    #[tokio::test]
    async fn tools_degrade_honestly_when_not_connected() {
        let _lock = crate::connectors::TEST_ENV_LOCK.lock().await;
        let (_dir, _home) = isolated_home();

        let err = GmailSearch
            .execute(serde_json::json!({"q": "hello"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::Other(_)), "err={err}");
        assert!(err.to_string().contains("not connected"), "err={err}");

        let err = GmailRead
            .execute(serde_json::json!({"id": "abc"}))
            .await
            .unwrap_err();
        assert!(err.to_string().contains("not connected"), "err={err}");

        let err = GmailSend
            .execute(serde_json::json!({"to": "a@b.c", "subject": "s", "body": "b"}))
            .await
            .unwrap_err();
        assert!(err.to_string().contains("not connected"), "err={err}");

        assert!(!GmailConnector::new().is_connected());
    }

    #[test]
    fn schemas_are_valid_json_schema() {
        let check = |tool: &dyn Tool, required: &[&str]| {
            let s = tool.schema();
            assert_eq!(s["type"], "object", "{}", tool.name());
            assert_eq!(s["additionalProperties"], false, "{}", tool.name());
            let req: Vec<&str> = s["required"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap())
                .collect();
            for r in required {
                assert!(req.contains(r), "{} missing required {r}", tool.name());
            }
            for r in required {
                assert!(s["properties"][r]["type"].is_string(), "{} prop {r}", tool.name());
            }
        };
        check(&GmailSearch, &["q"]);
        check(&GmailRead, &["id"]);
        check(&GmailSend, &["to", "subject", "body"]);
        // `max_results` is optional and documented.
        assert!(GmailSearch.schema()["properties"]["max_results"]["description"]
            .as_str()
            .unwrap()
            .contains("50"));
    }

    #[test]
    fn parse_full_message_prefers_text_plain() {
        let view = parse_message_full(&sample_full_message()).expect("parse fixture");
        assert_eq!(view.id, "18c4abc123");
        assert_eq!(view.from, "alice@example.com");
        assert_eq!(view.to, "me@gmail.com");
        assert_eq!(view.subject, "Test subject");
        assert_eq!(view.date, "Fri, 16 Oct 2026 09:00:00 +0700");
        assert_eq!(view.body_text, "Hello, this is the plain body.");
        assert!(!view.body_text.contains("HTML"));
    }

    #[test]
    fn parse_falls_back_to_stripped_html() {
        let html = URL_SAFE_NO_PAD.encode(b"<p>Hi <b>there</b>&nbsp;&amp;&nbsp;bye</p>");
        let msg = serde_json::json!({
            "id": "x1",
            "payload": {
                "mimeType": "text/html",
                "headers": [{"name": "Subject", "value": "s"}],
                "body": {"data": html}
            }
        });
        let view = parse_message_full(&msg).expect("parse html-only");
        assert_eq!(view.body_text, "Hi there & bye");
    }

    #[test]
    fn parse_message_missing_payload_is_error() {
        let err = parse_message_full(&serde_json::json!({"id": "x"})).unwrap_err();
        assert!(err.contains("payload"), "err={err}");
    }

    #[test]
    fn build_raw_message_rejects_header_injection() {
        let err = build_raw_message("a@b.c", "line1\nline2", "body").unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "err={err}");
        let err = build_raw_message("not-an-email", "s", "b").unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "err={err}");
    }

    #[test]
    fn build_raw_message_roundtrips_rfc2822() {
        let raw = build_raw_message("to@example.com", "héllo", "bòdy ✓").expect("build");
        let decoded = String::from_utf8(
            URL_SAFE_NO_PAD.decode(&raw).expect("base64url decodes"),
        )
        .expect("utf8");
        assert!(decoded.starts_with("To: to@example.com\r\n"));
        assert!(decoded.contains("Subject: =?utf-8?B?"), "subject must be RFC2047: {decoded}");
        assert!(decoded.ends_with("\r\n\r\nbòdy ✓"));
        // Gmail requires base64url without padding.
        assert!(!raw.contains('+') && !raw.contains('/') && !raw.contains('='));
    }

    #[test]
    fn token_debug_must_not_leak_access_token() {
        // Contract requirement: `ConnectorToken`'s Debug impl redacts secrets.
        let tok = ConnectorToken {
            access_token: "ya29.secret-access-token-xyz".to_string(),
            refresh_token: Some("1//secret-refresh".to_string()),
            expires_at: Some(9_999_999_999),
            extra: serde_json::json!({"client_id": "cid"}),
        };
        let dbg = format!("{tok:?}");
        assert!(!dbg.contains("ya29.secret-access-token-xyz"), "Debug leaks! {dbg}");
        assert!(!dbg.contains("secret-refresh"), "Debug leaks! {dbg}");
    }

    #[test]
    fn strip_html_basic() {
        assert_eq!(strip_html("<p>a<br>b</p>").split_whitespace().collect::<Vec<_>>().join(" "), "a b");
    }
}
