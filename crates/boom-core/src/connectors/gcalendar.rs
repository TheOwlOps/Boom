//! Google Calendar connector for boom-core.
//!
//! **Destination:** `crates/boom-core/src/connectors/gcalendar.rs`
//! (module `crate::connectors::gcalendar`).
//!
//! Implements the shared [`Connector`] trait (OAuth2 authorization-code flow):
//!
//! * [`GCalendarConnector::connect_url`] builds the Google consent URL.
//!   The operator opens it in a browser; Google redirects to
//!   `http://localhost:18080/api/connectors/gcalendar/callback?code=…&state=…`.
//! * [`GCalendarConnector::exchange_code`] trades the `code` for tokens and
//!   persists them to `$BOOM_HOME/connectors/gcalendar.json` (0600).
//! * Access tokens are refreshed automatically — proactively when
//!   `expires_at` is near, and reactively on a `401` from the API.
//!
//! ## Assumptions about the host crate
//!
//! The coordinator's `crate::connectors` module must provide:
//!
//! ```rust,ignore
//! #[derive(Clone, Serialize, Deserialize)]
//! pub struct ConnectorToken {
//!     pub access_token: String,
//!     pub refresh_token: Option<String>,
//!     pub expires_at: Option<i64>,      // unix seconds
//!     pub extra: serde_json::Value,     // e.g. {"client_id":..,"client_secret":..}
//! }
//! pub fn load_token(id: &str) -> Option<ConnectorToken>;            // $BOOM_HOME/connectors/<id>.json
//! pub fn save_token(id: &str, tok: &ConnectorToken) -> Result<(), String>; // atomic 0600 write
//! pub fn delete_token(id: &str) -> Result<(), String>;
//! #[async_trait] pub trait Connector: Send + Sync { /* per the shared contract */ }
//! ```
//!
//! Tools registered: `gcalendar_list`, `gcalendar_create` (toolset `"gcalendar"`).
//!
//! Security: tokens are never logged, never embedded in error strings, and
//! never appear in `Debug` output (see the manual [`fmt::Debug`] impl).

use std::fmt;
use std::sync::{Arc, RwLock};
use std::time::Duration;

use async_trait::async_trait;
use chrono::Utc;
use serde_json::Value;

use super::{Connector, ConnectorToken, delete_token, load_token, save_token};
use crate::tools::{opt_str, opt_u64, req_str, schema_of, Tool, ToolError, ToolRegistry};

/// Stable connector id (also the token file name: `gcalendar.json`).
pub const CONNECTOR_ID: &str = "gcalendar";

const AUTH_URL: &str = "https://accounts.google.com/o/oauth2/v2/auth";
const TOKEN_URL: &str = "https://oauth2.googleapis.com/token";
const API_BASE: &str = "https://www.googleapis.com/calendar/v3";
const REDIRECT_URI: &str = "http://localhost:18080/api/connectors/gcalendar/callback";
const SCOPES: &str = "https://www.googleapis.com/auth/calendar.readonly https://www.googleapis.com/auth/calendar.events";
const REQUEST_TIMEOUT_SECS: u64 = 30;
/// Seconds of clock skew tolerated before treating a token as expired.
const EXPIRY_SKEW_SECS: i64 = 60;

const NOT_CONNECTED_MSG: &str =
    "google calendar is not connected — open Settings → Connectors to connect";

// ---------------------------------------------------------------------------
// Pure helpers (unit-testable, no I/O)
// ---------------------------------------------------------------------------

fn utc_now() -> i64 {
    Utc::now().timestamp()
}

/// True when the token should be refreshed before use.
pub(crate) fn token_expired(tok: &ConnectorToken, now: i64) -> bool {
    match tok.expires_at {
        Some(exp) => exp <= now + EXPIRY_SKEW_SECS,
        None => false,
    }
}

/// Percent-encode one `application/x-www-form-urlencoded` component.
fn pct_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            b' ' => out.push('+'),
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// Build a `application/x-www-form-urlencoded` body from key/value pairs.
pub(crate) fn form_encode(pairs: &[(&str, &str)]) -> String {
    pairs
        .iter()
        .map(|(k, v)| format!("{}={}", pct_encode(k), pct_encode(v)))
        .collect::<Vec<_>>()
        .join("&")
}

/// Build the Google OAuth2 consent URL. Pure: easy to test the exact shape.
pub(crate) fn build_auth_url(client_id: &str, state: &str) -> String {
    let mut url = reqwest::Url::parse(AUTH_URL).expect("hardcoded auth URL must parse");
    url.query_pairs_mut()
        .append_pair("client_id", client_id)
        .append_pair("redirect_uri", REDIRECT_URI)
        .append_pair("response_type", "code")
        .append_pair("scope", SCOPES)
        .append_pair("access_type", "offline")
        .append_pair("prompt", "consent")
        .append_pair("state", state);
    url.to_string()
}

/// Human-readable error from a failed token-endpoint call.
/// Never includes tokens — only the status and Google's error detail.
fn token_endpoint_error(status: u16, data: &Value) -> String {
    let detail = match data.get("error") {
        Some(Value::String(s)) => {
            let desc = data
                .get("error_description")
                .and_then(Value::as_str)
                .unwrap_or("");
            if desc.is_empty() {
                s.clone()
            } else {
                format!("{s}: {desc}")
            }
        }
        Some(obj) => obj
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        None => String::new(),
    };
    let detail: String = detail.chars().take(300).collect();
    format!(
        "google token request failed (HTTP {status}): {}",
        if detail.is_empty() {
            "no detail"
        } else {
            &detail
        }
    )
}

/// Human-readable error from a failed Calendar API call.
fn api_error(status: u16, data: &Value) -> ToolError {
    let msg = data
        .get("error")
        .and_then(|e| e.get("message"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let msg: String = msg.chars().take(300).collect();
    ToolError::Http(format!(
        "google calendar api error {status}: {}",
        if msg.is_empty() { "no detail" } else { &msg }
    ))
}

/// Normalize one Calendar API event into `{id, summary, start, end,
/// location?, description?}`. `start`/`end` prefer `dateTime`, falling back
/// to `date` for all-day events.
pub(crate) fn parse_event(item: &Value) -> Value {
    fn field<'a>(item: &'a Value, key: &str) -> &'a str {
        item.get(key).and_then(Value::as_str).unwrap_or("")
    }
    fn when<'a>(item: &'a Value, key: &str) -> &'a str {
        item.get(key)
            .and_then(|w| w.get("dateTime").or_else(|| w.get("date")))
            .and_then(Value::as_str)
            .unwrap_or("")
    }
    let mut obj = serde_json::json!({
        "id": field(item, "id"),
        "summary": field(item, "summary"),
        "start": when(item, "start"),
        "end": when(item, "end"),
    });
    let location = field(item, "location");
    if !location.is_empty() {
        obj["location"] = Value::String(location.to_string());
    }
    let description = field(item, "description");
    if !description.is_empty() {
        obj["description"] = Value::String(description.to_string());
    }
    obj
}

fn validate_rfc3339(value: &str, arg: &str) -> Result<(), ToolError> {
    chrono::DateTime::parse_from_rfc3339(value).map_err(|_| {
        ToolError::InvalidArgs(format!(
            "'{arg}' must be RFC3339, e.g. 2026-10-17T10:00:00+07:00 (got '{value}')"
        ))
    })?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Connector
// ---------------------------------------------------------------------------

struct Inner {
    /// In-memory token cache; the disk file (`gcalendar.json`) is the source
    /// of truth and is re-read when the cache is empty.
    token: RwLock<Option<ConnectorToken>>,
    api_base: String,
    token_url: String,
    client: reqwest::Client,
}

/// One completed HTTP round-trip.
struct HttpResp {
    status: u16,
    body: String,
}

/// Google Calendar connector (OAuth2).
///
/// `Clone` shares the same underlying state (token cache, endpoints) via
/// `Arc`, so tools registered through [`Connector::register_tools`] (which
/// only gets `&self`) keep working after the original handle is dropped.
#[derive(Clone)]
pub struct GCalendarConnector {
    inner: Arc<Inner>,
}

impl GCalendarConnector {
    /// Production constructor: real Google endpoints.
    pub fn new() -> Self {
        Self::with_endpoints_inner(API_BASE, TOKEN_URL)
    }

    fn with_endpoints_inner(api_base: &str, token_url: &str) -> Self {
        let client = reqwest::Client::builder()
            .user_agent("boom-core-connectors/0.1 (+https://github.com/TheOwlOps/Boom)")
            .build()
            .expect("reqwest client must build");
        Self {
            inner: Arc::new(Inner {
                token: RwLock::new(None),
                api_base: api_base.trim_end_matches('/').to_string(),
                token_url: token_url.to_string(),
                client,
            }),
        }
    }

    /// Test-only constructor: point the connector at a mock HTTP server.
    #[cfg(test)]
    pub fn with_endpoints(api_base: &str, token_url: &str) -> Self {
        Self::with_endpoints_inner(api_base, token_url)
    }

    // -- token plumbing ----------------------------------------------------

    fn set_cached(&self, tok: Option<ConnectorToken>) {
        *self.inner.token.write().unwrap() = tok;
    }

    /// Cached token if present, otherwise load from the 0600 token file.
    fn load_token_cached(&self) -> Option<ConnectorToken> {
        if let Some(tok) = self.inner.token.read().unwrap().clone() {
            return Some(tok);
        }
        let tok = load_token(CONNECTOR_ID);
        *self.inner.token.write().unwrap() = tok.clone();
        tok
    }

    fn cached_refresh_token(&self) -> Option<String> {
        self.load_token_cached()
            .and_then(|t| t.refresh_token)
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
    }

    fn require_connected(&self) -> Result<(), ToolError> {
        match self.load_token_cached() {
            Some(t) if !t.access_token.trim().is_empty() => Ok(()),
            _ => Err(ToolError::Other(NOT_CONNECTED_MSG.to_string())),
        }
    }

    /// OAuth client credentials: env first, then the `extra` object of the
    /// stored token file (lets the Settings UI persist them without env).
    fn client_credentials(&self) -> Result<(String, String), String> {
        let env_id = std::env::var("BOOM_GOOGLE_CLIENT_ID")
            .ok()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty());
        let env_secret = std::env::var("BOOM_GOOGLE_CLIENT_SECRET")
            .ok()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty());
        if let (Some(id), Some(secret)) = (env_id, env_secret) {
            return Ok((id, secret));
        }
        if let Some(tok) = self.load_token_cached() {
            let file_id = tok
                .extra
                .get("client_id")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty());
            let file_secret = tok
                .extra
                .get("client_secret")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty());
            if let (Some(id), Some(secret)) = (file_id, file_secret) {
                return Ok((id.to_string(), secret.to_string()));
            }
        }
        Err("BOOM_GOOGLE_CLIENT_ID / BOOM_GOOGLE_CLIENT_SECRET are not set \
             (and no client_id/client_secret in the stored connector config) — \
             set them before connecting"
            .to_string())
    }

    /// Build a [`ConnectorToken`] from a token-endpoint JSON response,
    /// preserving the previous refresh token and `extra` when the response
    /// omits them (Google only returns `refresh_token` on first consent).
    fn build_token_from_response(&self, data: &Value) -> Result<ConnectorToken, String> {
        let access = data
            .get("access_token")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| "token endpoint response did not contain an access_token".to_string())?;
        let old = self.load_token_cached();
        let refresh = data
            .get("refresh_token")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .or_else(|| old.as_ref().and_then(|t| t.refresh_token.clone()));
        let expires_at = data
            .get("expires_in")
            .and_then(Value::as_i64)
            .map(|s| utc_now() + s)
            .or_else(|| old.as_ref().and_then(|t| t.expires_at));
        let extra = old
            .as_ref()
            .map(|t| t.extra.clone())
            .filter(|v| !v.is_null())
            .unwrap_or_else(|| serde_json::json!({}));
        Ok(ConnectorToken {
            access_token: access.to_string(),
            refresh_token: refresh,
            expires_at,
            extra,
        })
    }

    // -- HTTP ---------------------------------------------------------------

    /// POST a form body to the token endpoint (30s timeout). Used by both
    /// the authorization-code exchange and refreshes.
    async fn post_token_form(&self, form: &str) -> Result<HttpResp, String> {
        let resp = tokio::time::timeout(
            Duration::from_secs(REQUEST_TIMEOUT_SECS),
            self.inner
                .client
                .post(self.inner.token_url.as_str())
                .header("content-type", "application/x-www-form-urlencoded")
                .body(form.to_string())
                .send(),
        )
        .await
        .map_err(|_| {
            format!("google token request timed out after {REQUEST_TIMEOUT_SECS}s")
        })?
        .map_err(|e| format!("google token request failed: {e}"))?;
        let status = resp.status().as_u16();
        let body = resp
            .text()
            .await
            .map_err(|e| format!("failed reading token response: {e}"))?;
        Ok(HttpResp { status, body })
    }

    /// Exchange a refresh token for a new access token, persist it, and
    /// update the cache. Returns the fresh token.
    async fn refresh_access_token(&self, refresh_token: &str) -> Result<ConnectorToken, String> {
        let (client_id, client_secret) = self.client_credentials()?;
        let form = form_encode(&[
            ("grant_type", "refresh_token"),
            ("refresh_token", refresh_token),
            ("client_id", &client_id),
            ("client_secret", &client_secret),
        ]);
        let resp = self.post_token_form(&form).await?;
        let data: Value = serde_json::from_str(&resp.body)
            .map_err(|_| format!("token endpoint returned non-JSON (HTTP {})", resp.status))?;
        if resp.status >= 400 {
            return Err(token_endpoint_error(resp.status, &data));
        }
        let tok = self.build_token_from_response(&data)?;
        save_token(CONNECTOR_ID, &tok)?;
        self.set_cached(Some(tok.clone()));
        Ok(tok)
    }

    /// A usable access token: refresh proactively when expired.
    async fn valid_access_token(&self) -> Result<String, String> {
        let tok = self
            .load_token_cached()
            .filter(|t| !t.access_token.trim().is_empty())
            .ok_or_else(|| NOT_CONNECTED_MSG.to_string())?;
        if token_expired(&tok, utc_now()) {
            let rt = tok
                .refresh_token
                .clone()
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .ok_or_else(|| {
                    "google calendar token expired and no refresh token is stored — \
                     reconnect the connector (Settings → Connectors)"
                        .to_string()
                })?;
            return Ok(self.refresh_access_token(&rt).await?.access_token);
        }
        Ok(tok.access_token)
    }

    fn api_url(&self, path: &str, query: &[(&str, String)]) -> String {
        let mut url = reqwest::Url::parse(&format!("{}{}", self.inner.api_base, path))
            .expect("api base URL must parse");
        {
            let mut qp = url.query_pairs_mut();
            for (k, v) in query {
                qp.append_pair(k, v);
            }
        }
        url.to_string()
    }

    async fn send_api(
        &self,
        method: &str,
        url: &str,
        bearer: &str,
        body: Option<Vec<u8>>,
    ) -> Result<HttpResp, ToolError> {
        let mut req = match method {
            "POST" => self.inner.client.post(url),
            _ => self.inner.client.get(url),
        }
        .bearer_auth(bearer);
        if let Some(b) = body {
            req = req
                .header("content-type", "application/json")
                .body(b);
        }
        let resp = tokio::time::timeout(Duration::from_secs(REQUEST_TIMEOUT_SECS), req.send())
            .await
            .map_err(|_| ToolError::Timeout(REQUEST_TIMEOUT_SECS))?
            .map_err(|e| ToolError::Http(format!("request failed: {e}")))?;
        let status = resp.status().as_u16();
        let text = resp
            .text()
            .await
            .map_err(|e| ToolError::Http(format!("failed reading response: {e}")))?;
        Ok(HttpResp { status, body: text })
    }

    /// One Calendar API call with a 30s timeout.
    ///
    /// On `401` the access token is refreshed once (when a refresh token is
    /// stored) and the request is retried — this covers tokens that look
    /// valid but were revoked server-side.
    async fn api_request(
        &self,
        method: &str,
        path: &str,
        query: &[(&str, String)],
        body: Option<Value>,
    ) -> Result<Value, ToolError> {
        let mut bearer = self.valid_access_token().await.map_err(ToolError::Other)?;
        let url = self.api_url(path, query);
        for attempt in 0..2u8 {
            let body_bytes = match &body {
                Some(b) => Some(serde_json::to_vec(b).map_err(|e| {
                    ToolError::Other(format!("cannot serialize request body: {e}"))
                })?),
                None => None,
            };
            let resp = self.send_api(method, &url, &bearer, body_bytes).await?;
            if resp.status == 401 && attempt == 0 {
                let rt = self.cached_refresh_token().ok_or_else(|| {
                    ToolError::Other(
                        "google calendar rejected the access token (401) and no refresh \
                         token is stored — reconnect the connector (Settings → Connectors)"
                            .to_string(),
                    )
                })?;
                bearer = self
                    .refresh_access_token(&rt)
                    .await
                    .map(|t| t.access_token)
                    .map_err(ToolError::Other)?;
                continue;
            }
            let data: Value = serde_json::from_str(&resp.body).map_err(|_| {
                ToolError::Http(format!(
                    "google calendar returned non-JSON (HTTP {})",
                    resp.status
                ))
            })?;
            if !(200..300).contains(&resp.status) {
                return Err(api_error(resp.status, &data));
            }
            return Ok(data);
        }
        unreachable!("retry loop always returns on the first two attempts")
    }
}

impl Default for GCalendarConnector {
    fn default() -> Self {
        Self::new()
    }
}

/// Debug never prints tokens — only the connector id and whether the
/// in-memory cache currently holds a (non-empty) access token.
impl fmt::Debug for GCalendarConnector {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let cached = self
            .inner
            .token
            .read()
            .map(|g| {
                g.as_ref()
                    .map(|t| !t.access_token.is_empty())
                    .unwrap_or(false)
            })
            .unwrap_or(false);
        f.debug_struct("GCalendarConnector")
            .field("id", &CONNECTOR_ID)
            .field("connected_cached", &cached)
            .finish()
    }
}

// ---------------------------------------------------------------------------
// Connector trait
// ---------------------------------------------------------------------------

#[async_trait]
impl Connector for GCalendarConnector {
    fn id(&self) -> &'static str {
        CONNECTOR_ID
    }

    fn name(&self) -> &'static str {
        "Google Calendar"
    }

    fn description(&self) -> &'static str {
        "Google Calendar: list upcoming events and create events on the primary calendar (OAuth2)."
    }

    fn auth_kind(&self) -> &'static str {
        "oauth2"
    }

    fn is_connected(&self) -> bool {
        match self.load_token_cached() {
            Some(t) if !t.access_token.trim().is_empty() => {
                // Expired-but-refreshable still counts as connected: the
                // next call will refresh transparently.
                !token_expired(&t, utc_now())
                    || t.refresh_token
                        .as_deref()
                        .map(|s| !s.trim().is_empty())
                        .unwrap_or(false)
            }
            _ => false,
        }
    }

    fn connect_url(&self) -> Result<String, String> {
        let (client_id, _) = self.client_credentials()?;
        let state = uuid::Uuid::new_v4().to_string();
        Ok(build_auth_url(&client_id, &state))
    }

    async fn exchange_code(&mut self, code: &str) -> Result<(), String> {
        let code = code.trim();
        if code.is_empty() {
            return Err("authorization code is empty".to_string());
        }
        let (client_id, client_secret) = self.client_credentials()?;
        let form = form_encode(&[
            ("code", code),
            ("client_id", &client_id),
            ("client_secret", &client_secret),
            ("redirect_uri", REDIRECT_URI),
            ("grant_type", "authorization_code"),
        ]);
        let resp = self.post_token_form(&form).await?;
        let data: Value = serde_json::from_str(&resp.body)
            .map_err(|_| format!("token endpoint returned non-JSON (HTTP {})", resp.status))?;
        if resp.status >= 400 {
            return Err(token_endpoint_error(resp.status, &data));
        }
        let tok = self.build_token_from_response(&data)?;
        save_token(CONNECTOR_ID, &tok)?;
        self.set_cached(Some(tok));
        Ok(())
    }

    fn disconnect(&mut self) -> Result<(), String> {
        delete_token(CONNECTOR_ID)?;
        self.set_cached(None);
        Ok(())
    }

    fn register_tools(&self, reg: &mut ToolRegistry) {
        // `self.clone()` shares state via Arc, so the tools stay usable even
        // if the original connector handle is dropped.
        reg.register("gcalendar", GCalendarListTool { conn: self.clone() });
        reg.register("gcalendar", GCalendarCreateTool { conn: self.clone() });
    }
}

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

/// `gcalendar_list` — list events on the primary calendar.
pub struct GCalendarListTool {
    conn: GCalendarConnector,
}

#[async_trait]
impl Tool for GCalendarListTool {
    fn name(&self) -> &str {
        "gcalendar_list"
    }

    fn description(&self) -> &str {
        "List events from the primary Google Calendar in a time window. \
         Returns id, summary, start, end per event (plus location/description when set)."
    }

    fn schema(&self) -> Value {
        schema_of(
            &[
                (
                    "time_min",
                    "string",
                    "Window start, RFC3339 (default: now), e.g. 2026-10-17T10:00:00+07:00",
                ),
                (
                    "time_max",
                    "string",
                    "Window end, RFC3339 (optional).",
                ),
                (
                    "max_results",
                    "integer",
                    "Max events to return, 1-100 (default 20).",
                ),
                (
                    "query",
                    "string",
                    "Free-text search across event fields (optional).",
                ),
            ],
            &[],
        )
    }

    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        self.conn.require_connected()?;

        let time_min = args
            .get("time_min")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true));
        validate_rfc3339(&time_min, "time_min")?;

        let max_results = opt_u64(&args, "max_results", 20).clamp(1, 100);

        let mut query: Vec<(&str, String)> = vec![
            ("timeMin", time_min),
            ("maxResults", max_results.to_string()),
            ("singleEvents", "true".to_string()),
            ("orderBy", "startTime".to_string()),
        ];
        if let Some(time_max) = args
            .get("time_max")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
        {
            validate_rfc3339(time_max, "time_max")?;
            query.push(("timeMax", time_max.to_string()));
        }
        if let Some(q) = args
            .get("query")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
        {
            query.push(("q", q.to_string()));
        }

        let data = self
            .conn
            .api_request("GET", "/calendars/primary/events", &query, None)
            .await?;
        let items = data
            .get("items")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let events: Vec<Value> = items.iter().map(parse_event).collect();
        Ok(serde_json::json!({ "events": events, "count": events.len() }))
    }
}

/// `gcalendar_create` — create an event on the primary calendar.
pub struct GCalendarCreateTool {
    conn: GCalendarConnector,
}

#[async_trait]
impl Tool for GCalendarCreateTool {
    fn name(&self) -> &str {
        "gcalendar_create"
    }

    fn description(&self) -> &str {
        "Create an event on the primary Google Calendar. \
         Returns the event id and html_link."
    }

    fn schema(&self) -> Value {
        schema_of(
            &[
                ("summary", "string", "Event title."),
                (
                    "start",
                    "string",
                    "Start time, RFC3339, e.g. 2026-10-18T09:00:00+07:00.",
                ),
                (
                    "end",
                    "string",
                    "End time, RFC3339, e.g. 2026-10-18T10:00:00+07:00.",
                ),
                ("description", "string", "Event description (optional)."),
                ("location", "string", "Event location (optional)."),
            ],
            &["summary", "start", "end"],
        )
    }

    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        self.conn.require_connected()?;

        let summary = req_str(&args, "summary")?;
        let start = req_str(&args, "start")?;
        let end = req_str(&args, "end")?;
        validate_rfc3339(&start, "start")?;
        validate_rfc3339(&end, "end")?;
        let description = opt_str(&args, "description", "");
        let location = opt_str(&args, "location", "");

        let mut body = serde_json::json!({
            "summary": summary,
            "start": { "dateTime": start },
            "end": { "dateTime": end },
        });
        if !description.trim().is_empty() {
            body["description"] = Value::String(description.to_string());
        }
        if !location.trim().is_empty() {
            body["location"] = Value::String(location.to_string());
        }

        let data = self
            .conn
            .api_request("POST", "/calendars/primary/events", &[], Some(body))
            .await?;
        Ok(serde_json::json!({
            "id": data.get("id").and_then(Value::as_str).unwrap_or(""),
            "html_link": data.get("htmlLink").and_then(Value::as_str).unwrap_or(""),
        }))
    }
}

// ---------------------------------------------------------------------------
// Tests (all offline: no Google network access)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::connectors::{load_token as ct_load, save_token as ct_save};
    use std::sync::Mutex;

    /// RAII: fresh `$BOOM_HOME` temp dir + env lock for the whole test.
    struct TempHome {
        _guard: tokio::sync::MutexGuard<'static, ()>,
        _dir: tempfile::TempDir,
        prev_home: Option<String>,
    }

    impl TempHome {
        async fn new() -> Self {
            let guard = crate::connectors::TEST_ENV_LOCK.lock().await;
            let dir = tempfile::tempdir().unwrap();
            let prev_home = std::env::var("BOOM_HOME").ok();
            std::env::set_var("BOOM_HOME", dir.path());
            Self {
                _guard: guard,
                _dir: dir,
                prev_home,
            }
        }

        fn with_client_env(self, id: &str, secret: &str) -> ClientEnv {
            std::env::set_var("BOOM_GOOGLE_CLIENT_ID", id);
            std::env::set_var("BOOM_GOOGLE_CLIENT_SECRET", secret);
            ClientEnv { home: self }
        }
    }

    impl Drop for TempHome {
        fn drop(&mut self) {
            match &self.prev_home {
                Some(v) => std::env::set_var("BOOM_HOME", v),
                None => std::env::remove_var("BOOM_HOME"),
            }
        }
    }

    /// TempHome + Google client env vars, both restored on drop.
    struct ClientEnv {
        home: TempHome,
    }

    impl Drop for ClientEnv {
        fn drop(&mut self) {
            std::env::remove_var("BOOM_GOOGLE_CLIENT_ID");
            std::env::remove_var("BOOM_GOOGLE_CLIENT_SECRET");
            let _ = &self.home;
        }
    }

    fn seed_token(tok: &ConnectorToken) {
        ct_save(CONNECTOR_ID, tok).expect("seed token must save");
    }

    fn sample_token(access: &str) -> ConnectorToken {
        ConnectorToken {
            access_token: access.to_string(),
            refresh_token: Some("refresh-1".to_string()),
            expires_at: Some(utc_now() + 3600),
            extra: serde_json::json!({}),
        }
    }

    // -- connect_url -------------------------------------------------------

    #[tokio::test]
    async fn connect_url_shape() {
        let _env = TempHome::new().await.with_client_env("cid-123", "csecret-456");
        let conn = GCalendarConnector::new();
        let url = conn.connect_url().expect("connect_url must succeed");
        let parsed = reqwest::Url::parse(&url).expect("connect_url must be a valid URL");
        assert_eq!(
            format!(
                "{}://{}{}",
                parsed.scheme(),
                parsed.host_str().unwrap(),
                parsed.path()
            ),
            "https://accounts.google.com/o/oauth2/v2/auth"
        );
        let q: std::collections::HashMap<_, _> = parsed.query_pairs().into_owned().collect();
        assert_eq!(q.get("client_id").map(String::as_str), Some("cid-123"));
        assert_eq!(
            q.get("redirect_uri").map(String::as_str),
            Some("http://localhost:18080/api/connectors/gcalendar/callback")
        );
        assert_eq!(q.get("response_type").map(String::as_str), Some("code"));
        assert_eq!(q.get("access_type").map(String::as_str), Some("offline"));
        assert_eq!(q.get("prompt").map(String::as_str), Some("consent"));
        let scope = q.get("scope").expect("scope param present");
        assert!(
            scope.contains("https://www.googleapis.com/auth/calendar.readonly"),
            "readonly scope missing: {scope}"
        );
        assert!(
            scope.contains("https://www.googleapis.com/auth/calendar.events"),
            "events scope missing: {scope}"
        );
        assert!(
            q.get("state").map(|s| !s.is_empty()).unwrap_or(false),
            "state param must be non-empty"
        );
    }

    #[tokio::test]
    async fn connect_url_fails_without_client_id() {
        let _home = TempHome::new().await;
        std::env::remove_var("BOOM_GOOGLE_CLIENT_ID");
        std::env::remove_var("BOOM_GOOGLE_CLIENT_SECRET");
        let conn = GCalendarConnector::new();
        let err = conn.connect_url().unwrap_err();
        assert!(
            err.contains("BOOM_GOOGLE_CLIENT_ID"),
            "error must name the missing env var: {err}"
        );
    }

    #[tokio::test]
    async fn connect_url_falls_back_to_extra_in_token_file() {
        let _home = TempHome::new().await;
        std::env::remove_var("BOOM_GOOGLE_CLIENT_ID");
        std::env::remove_var("BOOM_GOOGLE_CLIENT_SECRET");
        seed_token(&ConnectorToken {
            access_token: "at".to_string(),
            refresh_token: None,
            expires_at: None,
            extra: serde_json::json!({
                "client_id": "file-cid",
                "client_secret": "file-csecret",
            }),
        });
        let conn = GCalendarConnector::new();
        let url = conn.connect_url().expect("extra fallback must work");
        assert!(url.contains("client_id=file-cid"), "url={url}");
    }

    // -- honest degrade ----------------------------------------------------

    #[tokio::test]
    async fn tools_degrade_honestly_when_not_connected() {
        let _home = TempHome::new().await;
        let conn = GCalendarConnector::new();
        assert!(!conn.is_connected());

        let mut reg = ToolRegistry::default();
        conn.register_tools(&mut reg);
        assert!(reg.get("gcalendar_list").is_some());
        assert!(reg.get("gcalendar_create").is_some());
        assert!(reg.tools_in_toolset("gcalendar").len() == 2);

        for (name, args) in [
            ("gcalendar_list", serde_json::json!({})),
            (
                "gcalendar_create",
                serde_json::json!({
                    "summary": "x",
                    "start": "2026-10-18T09:00:00+07:00",
                    "end": "2026-10-18T10:00:00+07:00",
                }),
            ),
        ] {
            let tool = reg.get(name).unwrap();
            let err = tool.execute(args).await.unwrap_err();
            assert!(
                matches!(err, ToolError::Other(_)),
                "{name} must fail with ToolError::Other, got {err:?}"
            );
            assert_eq!(err.to_string(), NOT_CONNECTED_MSG, "{name}");
        }
    }

    #[tokio::test]
    async fn is_connected_honesty() {
        let _home = TempHome::new().await;
        let conn = GCalendarConnector::new();
        assert!(!conn.is_connected(), "no token file → not connected");

        seed_token(&sample_token("fresh"));
        let conn2 = GCalendarConnector::new();
        assert!(conn2.is_connected(), "fresh token → connected");

        // Expired, no refresh token → not connected.
        seed_token(&ConnectorToken {
            access_token: "old".to_string(),
            refresh_token: None,
            expires_at: Some(utc_now() - 10),
            extra: serde_json::json!({}),
        });
        let conn3 = GCalendarConnector::new();
        assert!(!conn3.is_connected(), "expired w/o refresh → not connected");

        // Expired but refreshable → connected (next call refreshes).
        seed_token(&ConnectorToken {
            access_token: "old".to_string(),
            refresh_token: Some("rt".to_string()),
            expires_at: Some(utc_now() - 10),
            extra: serde_json::json!({}),
        });
        let conn4 = GCalendarConnector::new();
        assert!(conn4.is_connected(), "expired w/ refresh → connected");
    }

    // -- schemas ------------------------------------------------------------

    #[test]
    fn tool_schemas_are_valid() {
        let conn = GCalendarConnector::new();
        let mut reg = ToolRegistry::default();
        conn.register_tools(&mut reg);
        for name in ["gcalendar_list", "gcalendar_create"] {
            let tool = reg.get(name).unwrap_or_else(|| panic!("{name} missing"));
            assert!(!tool.name().is_empty());
            assert!(!tool.description().is_empty());
            let schema = tool.schema();
            let obj = schema.as_object().expect("schema must be an object");
            assert_eq!(obj.get("type").and_then(|t| t.as_str()), Some("object"));
            let props = obj
                .get("properties")
                .and_then(|p| p.as_object())
                .expect("properties object");
            assert!(!props.is_empty(), "{name} documents no properties");
            for req in obj
                .get("required")
                .and_then(|r| r.as_array())
                .cloned()
                .unwrap_or_default()
            {
                let key = req.as_str().expect("required entry must be a string");
                assert!(props.contains_key(key), "{name}: required '{key}' not in properties");
            }
        }
        // create requires summary/start/end
        let create = reg.get("gcalendar_create").unwrap();
        let required: Vec<String> = create.schema()["required"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect();
        assert!(required.contains(&"summary".to_string()));
        assert!(required.contains(&"start".to_string()));
        assert!(required.contains(&"end".to_string()));
    }

    // -- event parsing fixture ----------------------------------------------

    fn events_fixture() -> Value {
        serde_json::json!({
            "kind": "calendar#events",
            "items": [
                {
                    "id": "evt-1",
                    "summary": "Team standup",
                    "location": "Room A",
                    "description": "Daily sync",
                    "start": { "dateTime": "2026-10-19T09:00:00+07:00" },
                    "end": { "dateTime": "2026-10-19T09:30:00+07:00" },
                    "htmlLink": "https://www.google.com/calendar/event?eid=evt-1"
                },
                {
                    "id": "evt-2",
                    "summary": "Holiday",
                    "start": { "date": "2026-10-20" },
                    "end": { "date": "2026-10-21" }
                },
                { "id": "evt-3" }
            ]
        })
    }

    #[test]
    fn parse_event_list_fixture() {
        let fixture = events_fixture();
        let items = fixture["items"].as_array().unwrap();
        let e1 = parse_event(&items[0]);
        assert_eq!(e1["id"], "evt-1");
        assert_eq!(e1["summary"], "Team standup");
        assert_eq!(e1["start"], "2026-10-19T09:00:00+07:00");
        assert_eq!(e1["end"], "2026-10-19T09:30:00+07:00");
        assert_eq!(e1["location"], "Room A");
        assert_eq!(e1["description"], "Daily sync");

        // All-day event: falls back to `date`.
        let e2 = parse_event(&items[1]);
        assert_eq!(e2["start"], "2026-10-20");
        assert_eq!(e2["end"], "2026-10-21");
        assert!(e2.get("location").is_none(), "absent optional keys stay absent");
        assert!(e2.get("description").is_none());

        // Missing everything: empty strings, never panics.
        let e3 = parse_event(&items[2]);
        assert_eq!(e3["id"], "evt-3");
        assert_eq!(e3["summary"], "");
        assert_eq!(e3["start"], "");
    }

    // -- arg validation ------------------------------------------------------

    #[tokio::test]
    async fn create_rejects_bad_rfc3339() {
        let _home = TempHome::new().await;
        seed_token(&sample_token("at"));
        let conn = GCalendarConnector::new();
        let mut reg = ToolRegistry::default();
        conn.register_tools(&mut reg);
        let tool = reg.get("gcalendar_create").unwrap();
        let err = tool
            .execute(serde_json::json!({
                "summary": "x",
                "start": "tomorrow at 9",
                "end": "2026-10-18T10:00:00+07:00",
            }))
            .await
            .unwrap_err();
        assert!(
            matches!(err, ToolError::InvalidArgs(_)),
            "bad start must be InvalidArgs, got {err:?}"
        );
        assert!(err.to_string().contains("RFC3339"));
    }

    // -- pure helpers --------------------------------------------------------

    #[test]
    fn form_encoding_escapes() {
        let out = form_encode(&[
            ("grant_type", "authorization_code"),
            ("redirect_uri", "http://localhost:18080/api/connectors/gcalendar/callback"),
            ("scope", "a b"),
        ]);
        assert!(out.contains("grant_type=authorization_code"));
        assert!(out.contains("redirect_uri=http%3A%2F%2Flocalhost%3A18080%2Fapi%2Fconnectors%2Fgcalendar%2Fcallback"), "got {out}");
        assert!(out.contains("scope=a+b"));
    }

    #[test]
    fn token_expiry_logic() {
        let now = 1_700_000_000i64;
        let tok = |exp: Option<i64>| ConnectorToken {
            access_token: "a".into(),
            refresh_token: None,
            expires_at: exp,
            extra: Value::Null,
        };
        assert!(!token_expired(&tok(None), now), "no expiry → not expired");
        assert!(!token_expired(&tok(Some(now + 3600)), now));
        assert!(token_expired(&tok(Some(now - 1)), now));
        assert!(
            token_expired(&tok(Some(now + EXPIRY_SKEW_SECS - 1)), now),
            "within skew window → expired"
        );
    }

    #[tokio::test]
    async fn debug_redacts_token() {
        let _home = TempHome::new().await;
        seed_token(&sample_token("super-secret-access-token-xyz"));
        let conn = GCalendarConnector::new();
        assert!(conn.is_connected()); // populate the cache
        let dbg = format!("{conn:?}");
        assert!(
            !dbg.contains("super-secret-access-token-xyz"),
            "Debug must never print the token: {dbg}"
        );
        assert!(
            !dbg.contains("refresh-1"),
            "Debug must never print the refresh token: {dbg}"
        );
    }

    #[tokio::test]
    async fn token_file_roundtrip() {
        let _home = TempHome::new().await;
        let tok = ConnectorToken {
            access_token: "at-1".to_string(),
            refresh_token: Some("rt-1".to_string()),
            expires_at: Some(1_800_000_000),
            extra: serde_json::json!({"client_id": "cid"}),
        };
        ct_save(CONNECTOR_ID, &tok).unwrap();
        let back = ct_load(CONNECTOR_ID).expect("token must round-trip");
        assert_eq!(back.access_token, "at-1");
        assert_eq!(back.refresh_token.as_deref(), Some("rt-1"));
        assert_eq!(back.expires_at, Some(1_800_000_000));
        assert_eq!(back.extra["client_id"], "cid");

        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let path = std::env::var("BOOM_HOME").unwrap() + "/connectors/gcalendar.json";
            let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode, 0o600, "token file must be 0600");
        }
    }

    // -- mock HTTP server -----------------------------------------------------

    #[derive(Debug, Clone)]
    struct RecordedRequest {
        method: String,
        path: String,
        headers: Vec<(String, String)>,
        body: Vec<u8>,
    }

    impl RecordedRequest {
        fn header(&self, name: &str) -> Option<&str> {
            self.headers
                .iter()
                .find(|(k, _)| k == name)
                .map(|(_, v)| v.as_str())
        }
    }

    /// Mock HTTP handler: request in, (status, JSON body) out.
    type MockHandler = std::sync::Arc<dyn Fn(&RecordedRequest) -> (u16, Value) + Send + Sync>;

    async fn serve_one(
        mut socket: tokio::net::TcpStream,
        handler: &MockHandler,
        recorded: &std::sync::Arc<Mutex<Vec<RecordedRequest>>>,
    ) -> std::io::Result<()> {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let mut buf = Vec::new();
        let mut tmp = [0u8; 4096];
        loop {
            let n = socket.read(&mut tmp).await?;
            if n == 0 {
                break;
            }
            buf.extend_from_slice(&tmp[..n]);
            if buf.windows(4).any(|w| w == b"\r\n\r\n") || buf.len() > 65536 {
                break;
            }
        }
        let header_end = buf
            .windows(4)
            .position(|w| w == b"\r\n\r\n")
            .map(|i| i + 4)
            .unwrap_or(buf.len());
        let head = String::from_utf8_lossy(&buf[..header_end]).to_string();
        let mut lines = head.lines();
        let request_line = lines.next().unwrap_or("");
        let mut parts = request_line.split_whitespace();
        let method = parts.next().unwrap_or("").to_string();
        let path = parts.next().unwrap_or("").to_string();
        let mut headers = Vec::new();
        let mut content_length = 0usize;
        for line in lines {
            if let Some((k, v)) = line.split_once(':') {
                let k = k.trim().to_lowercase();
                let v = v.trim().to_string();
                if k == "content-length" {
                    content_length = v.parse().unwrap_or(0);
                }
                headers.push((k, v));
            }
        }
        let mut body = buf[header_end..].to_vec();
        while body.len() < content_length {
            let n = socket.read(&mut tmp).await?;
            if n == 0 {
                break;
            }
            body.extend_from_slice(&tmp[..n]);
        }
        body.truncate(content_length);
        let req = RecordedRequest {
            method,
            path,
            headers,
            body,
        };
        let (status, json) = {
            let mut rec = recorded.lock().unwrap();
            rec.push(req);
            let last = rec.last().unwrap();
            handler(last)
        };
        let body_str = serde_json::to_string(&json).unwrap();
        let reason = match status {
            200 => "OK",
            400 => "Bad Request",
            401 => "Unauthorized",
            404 => "Not Found",
            _ => "OK",
        };
        let resp = format!(
            "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body_str}",
            body_str.len()
        );
        socket.write_all(resp.as_bytes()).await?;
        let _ = socket.shutdown().await;
        Ok(())
    }

    /// Spawn a mock HTTP server. Returns (addr, recorded requests, task).
    async fn spawn_mock(
        handler: impl Fn(&RecordedRequest) -> (u16, Value) + Send + Sync + 'static,
    ) -> (
        std::net::SocketAddr,
        std::sync::Arc<Mutex<Vec<RecordedRequest>>>,
        tokio::task::JoinHandle<()>,
    ) {
        let handler: MockHandler = std::sync::Arc::new(handler);
        let recorded: std::sync::Arc<Mutex<Vec<RecordedRequest>>> =
            std::sync::Arc::new(Mutex::new(Vec::new()));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let recorded_for_task = std::sync::Arc::clone(&recorded);
        let task = tokio::spawn(async move {
            let recorded = recorded_for_task;
            loop {
                let Ok((socket, _)) = listener.accept().await else {
                    break;
                };
                let handler = handler.clone();
                let recorded = recorded.clone();
                tokio::spawn(async move {
                    let _ = serve_one(socket, &handler, &recorded).await;
                });
            }
        });
        (addr, recorded, task)
    }

    /// 401 on a stale token → refresh → retry with the new token → success,
    /// and the refreshed token is persisted to the token file.
    #[tokio::test]
    async fn refresh_on_401_then_retry_succeeds() {
        let _env = TempHome::new().await.with_client_env("cid", "csecret");
        // Looks valid (not expired) but Google rejects it → exercises the
        // reactive 401 → refresh → retry path.
        seed_token(&sample_token("stale-access"));

        let fixture = events_fixture();
        let (addr, recorded, task) = spawn_mock(move |req: &RecordedRequest| {
            if req.path == "/token" && req.method == "POST" {
                let body = String::from_utf8_lossy(&req.body).to_string();
                assert!(
                    body.contains("grant_type=refresh_token"),
                    "must use the refresh grant: {body}"
                );
                assert!(body.contains("refresh_token=refresh-1"), "body={body}");
                return (
                    200,
                    serde_json::json!({
                        "access_token": "fresh-access",
                        "expires_in": 3600,
                        "token_type": "Bearer",
                    }),
                );
            }
            if req.path.starts_with("/calendars/primary/events") {
                match req.header("authorization") {
                    Some("Bearer stale-access") => {
                        return (
                            401,
                            serde_json::json!({"error": {"message": "Invalid Credentials"}}),
                        )
                    }
                    Some("Bearer fresh-access") => return (200, fixture.clone()),
                    other => {
                        return (
                            401,
                            serde_json::json!({"error": {"message": format!("bad auth: {other:?}")}}),
                        )
                    }
                }
            }
            (404, serde_json::json!({}))
        })
        .await;

        let conn = GCalendarConnector::with_endpoints(
            &format!("http://{addr}"),
            &format!("http://{addr}/token"),
        );
        let mut reg = ToolRegistry::default();
        conn.register_tools(&mut reg);
        let tool = reg.get("gcalendar_list").unwrap();
        let out = tool
            .execute(serde_json::json!({"max_results": 5}))
            .await
            .expect("list must succeed after refresh");
        assert_eq!(out["count"], 3);
        assert_eq!(out["events"][0]["id"], "evt-1");
        assert_eq!(out["events"][1]["start"], "2026-10-20");

        // The refresh endpoint was hit and the events endpoint was called twice
        // (stale → 401, fresh → 200).
        let rec = recorded.lock().unwrap();
        assert!(
            rec.iter().any(|r| r.path == "/token"),
            "token endpoint must have been called"
        );
        assert_eq!(
            rec.iter().filter(|r| r.path.starts_with("/calendars/primary/events")).count(),
            2,
            "events endpoint must have been retried after refresh"
        );

        // Persisted: the token file now holds the fresh access token with a
        // renewed expiry, and the old refresh token is preserved.
        let saved = ct_load(CONNECTOR_ID).expect("token file must exist");
        assert_eq!(saved.access_token, "fresh-access");
        assert_eq!(saved.refresh_token.as_deref(), Some("refresh-1"));
        let exp = saved.expires_at.expect("expires_at must be set");
        assert!(
            (utc_now() + 3590..=utc_now() + 3610).contains(&exp),
            "expires_at ≈ now+3600, got {exp}"
        );

        task.abort();
    }

    /// exchange_code trades the auth code for tokens and persists them.
    #[tokio::test]
    async fn exchange_code_persists_tokens() {
        let _env = TempHome::new().await.with_client_env("cid", "csecret");
        let (addr, recorded, task) = spawn_mock(|req: &RecordedRequest| {
            assert_eq!(req.path, "/token");
            assert_eq!(req.method, "POST");
            let body = String::from_utf8_lossy(&req.body).to_string();
            assert!(body.contains("grant_type=authorization_code"), "body={body}");
            assert!(body.contains("code=authcode-xyz"), "body={body}");
            assert!(
                body.contains("redirect_uri=http%3A%2F%2Flocalhost%3A18080%2Fapi%2Fconnectors%2Fgcalendar%2Fcallback"),
                "body={body}"
            );
            (
                200,
                serde_json::json!({
                    "access_token": "at-new",
                    "refresh_token": "rt-new",
                    "expires_in": 7200,
                    "token_type": "Bearer",
                }),
            )
        })
        .await;

        let mut conn = GCalendarConnector::with_endpoints(
            &format!("http://{addr}"),
            &format!("http://{addr}/token"),
        );
        conn.exchange_code("authcode-xyz")
            .await
            .expect("exchange must succeed");
        assert!(conn.is_connected());

        let saved = ct_load(CONNECTOR_ID).expect("token file must exist");
        assert_eq!(saved.access_token, "at-new");
        assert_eq!(saved.refresh_token.as_deref(), Some("rt-new"));
        let exp = saved.expires_at.expect("expires_at must be set");
        assert!(
            (utc_now() + 7190..=utc_now() + 7210).contains(&exp),
            "expires_at ≈ now+7200, got {exp}"
        );
        assert_eq!(recorded.lock().unwrap().len(), 1);

        // disconnect() clears the token file and the cache.
        conn.disconnect().expect("disconnect must succeed");
        assert!(!conn.is_connected());
        assert!(ct_load(CONNECTOR_ID).is_none());
        task.abort();
    }

    /// A failed token exchange surfaces Google's error honestly.
    #[tokio::test]
    async fn exchange_code_surfaces_google_error() {
        let _env = TempHome::new().await.with_client_env("cid", "csecret");
        let (addr, _recorded, task) = spawn_mock(|_req: &RecordedRequest| {
            (
                400,
                serde_json::json!({
                    "error": "invalid_grant",
                    "error_description": "Bad Request",
                }),
            )
        })
        .await;
        let mut conn = GCalendarConnector::with_endpoints(
            &format!("http://{addr}"),
            &format!("http://{addr}/token"),
        );
        let err = conn.exchange_code("bogus").await.unwrap_err();
        assert!(err.contains("invalid_grant"), "err={err}");
        assert!(!conn.is_connected());
        task.abort();
    }
}
