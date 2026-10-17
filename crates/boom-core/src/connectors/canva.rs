//! Canva connector — list designs, create designs (preset / custom / from a
//! brand template), and export designs via the Canva Connect API.
//!
//! Auth is OAuth2 (authorization-code + PKCE, like the official Canva Connect
//! flow):
//!
//! * [`CanvaConnector::connect_url`] builds the Canva consent URL. The
//!   operator opens it in a browser; Canva redirects to
//!   `http://localhost:18080/api/connectors/canva/callback?code=…&state=…`.
//! * [`CanvaConnector::exchange_code`] trades the `code` (plus the stored
//!   PKCE verifier) for tokens and persists them to
//!   `$BOOM_HOME/connectors/canva.json` (0600).
//! * Access tokens are refreshed automatically — proactively when `expires_at`
//!   is near, and reactively on a `401` from the API.
//!
//! The OAuth client id/secret come from the `BOOM_CANVA_CLIENT_ID` /
//! `BOOM_CANVA_CLIENT_SECRET` env vars (create an integration at
//! https://www.canva.com/developers/ and add the redirect URI above).
//! Tokens are never logged, never embedded in error strings, and never
//! appear in `Debug` output.
//!
//! ## Merge note for the coordinator
//!
//! Drop this file at `crates/boom-core/src/connectors/canva.rs`, declare
//! `pub mod canva;` in `src/connectors/mod.rs`, and register
//! `CanvaConnector::new()` in `ConnectorRegistry::new()` (also extend
//! `ALL_CONNECTOR_IDS`). It uses only the shared contract from the parent
//! module (`Connector`, `ConnectorToken`, `load_token`, `save_token`,
//! `save_extra`, `load_extra`, `delete_token`, `http_client`).

use std::fmt;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use base64::Engine as _;
use serde_json::Value;
use sha2::{Digest, Sha256};

use super::{
    delete_token, http_client, load_extra, load_token, save_extra, save_token, Connector,
    ConnectorToken,
};
use crate::tools::{opt_str, opt_u64, req_str, schema_of, Tool, ToolError, ToolRegistry};

/// Stable connector id (also the token file name: `canva.json`).
pub const CONNECTOR_ID: &str = "canva";

const AUTH_URL: &str = "https://www.canva.com/api/oauth/authorize";
const TOKEN_URL: &str = "https://api.canva.com/rest/v1/oauth/token";
const API_BASE: &str = "https://api.canva.com/rest/v1";
const REDIRECT_URI: &str = "http://localhost:18080/api/connectors/canva/callback";
const SCOPES: &str = "profile:read asset:read design:content:read design:content:write \
     design:meta:read brandtemplate:content:read brandtemplate:meta:read";
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);
/// Refresh the access token this far before it actually expires.
const REFRESH_SKEW_SECS: i64 = 300;
/// How long `canva_export` / template-autofill poll a job before giving up.
const JOB_POLL_DEADLINE: Duration = Duration::from_secs(120);
const JOB_POLL_INTERVAL: Duration = Duration::from_secs(3);

const NOT_CONNECTED_MSG: &str =
    "canva is not connected — open Settings → Connectors to connect";

const NO_CLIENT_MSG: &str = "canva OAuth is not configured: set BOOM_CANVA_CLIENT_ID and \
     BOOM_CANVA_CLIENT_SECRET (from https://www.canva.com/developers/ — create an integration and \
     add http://localhost:18080/api/connectors/canva/callback as a redirect URI), then retry";

// ---------------------------------------------------------------------------
// Pure helpers (unit-testable, no I/O)
// ---------------------------------------------------------------------------

fn now_secs() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// True when the token should be refreshed before use.
fn token_needs_refresh(tok: &ConnectorToken) -> bool {
    match tok.expires_at {
        Some(exp) => exp <= now_secs() + REFRESH_SKEW_SECS,
        None => false,
    }
}

/// Generate a PKCE `code_verifier`: 64 hex chars (two v4 UUIDs) from the OS
/// CSPRNG — within Canva's 43–128 character range.
fn generate_verifier() -> String {
    format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    )
}

/// PKCE `code_challenge` = base64url(SHA256(verifier)).
fn code_challenge(verifier: &str) -> String {
    let digest = Sha256::digest(verifier.as_bytes());
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(digest)
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

/// Build the Canva OAuth2 consent URL (PKCE S256). Pure: easy to test.
fn build_auth_url(client_id: &str, state: &str, challenge: &str) -> String {
    let mut url = reqwest::Url::parse(AUTH_URL).expect("hardcoded auth URL must parse");
    url.query_pairs_mut()
        .append_pair("response_type", "code")
        .append_pair("client_id", client_id)
        .append_pair("redirect_uri", REDIRECT_URI)
        .append_pair("scope", SCOPES)
        .append_pair("state", state)
        .append_pair("code_challenge", challenge)
        .append_pair("code_challenge_method", "S256");
    url.to_string()
}

/// OAuth client credentials from the environment. Clear error when missing —
/// the client secret must never be logged.
fn client_credentials() -> Result<(String, String), String> {
    let id = std::env::var("BOOM_CANVA_CLIENT_ID")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());
    let secret = std::env::var("BOOM_CANVA_CLIENT_SECRET")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());
    match (id, secret) {
        (Some(id), Some(secret)) => Ok((id, secret)),
        _ => Err(NO_CLIENT_MSG.to_string()),
    }
}

/// Human-readable error from a failed token-endpoint call.
/// Never includes tokens — only the status and Canva's error detail.
fn token_endpoint_error(status: u16, data: &Value) -> String {
    let detail = data
        .get("error_description")
        .or_else(|| data.get("error"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let detail: String = detail.chars().take(300).collect();
    format!(
        "canva token request failed (HTTP {status}): {}",
        if detail.is_empty() {
            "no detail"
        } else {
            &detail
        }
    )
}

/// Parse a token-endpoint success body into a [`ConnectorToken`]. `old` is
/// the previous token (refresh token is preserved when the response omits a
/// new one; non-secret extra is carried over).
fn parse_token_response(data: &Value, old: Option<&ConnectorToken>) -> Result<ConnectorToken, String> {
    let access = data
        .get("access_token")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "canva token response did not contain an access_token".to_string())?;
    let refresh = data
        .get("refresh_token")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .or_else(|| {
            old.and_then(|o| o.refresh_token.clone())
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
        });
    let expires_at = data
        .get("expires_in")
        .and_then(Value::as_i64)
        .map(|s| now_secs() + s);
    let mut extra = old
        .map(|o| o.extra.clone())
        .unwrap_or_else(|| serde_json::json!({}));
    if let Some(obj) = extra.as_object_mut() {
        obj.insert(
            "scopes".to_string(),
            Value::String(SCOPES.replace('\n', " ")),
        );
    }
    Ok(ConnectorToken {
        access_token: access.to_string(),
        refresh_token: refresh,
        expires_at,
        extra,
    })
}

// ---------------------------------------------------------------------------
// Connector
// ---------------------------------------------------------------------------

/// Canva connector (OAuth2 + PKCE).
///
/// Holds no secret: the token file is the source of truth and is loaded
/// fresh on every call, so `exchange_code`/`disconnect` take effect
/// immediately for all registered tools.
#[derive(Clone)]
pub struct CanvaConnector {
    api_base: String,
    token_url: String,
}

impl CanvaConnector {
    /// Production constructor: real Canva endpoints.
    pub fn new() -> Self {
        Self::with_endpoints(API_BASE, TOKEN_URL)
    }

    /// Constructor with explicit endpoints — tests drive local mock servers
    /// through this.
    pub fn with_endpoints(api_base: &str, token_url: &str) -> Self {
        Self {
            api_base: api_base.trim_end_matches('/').to_string(),
            token_url: token_url.to_string(),
        }
    }

    fn api_base(&self) -> &str {
        &self.api_base
    }
    fn token_url(&self) -> &str {
        &self.token_url
    }

    // -- token plumbing ----------------------------------------------------

    /// Load the stored token (fresh from disk every call).
    fn load_stored(&self) -> Option<ConnectorToken> {
        load_token(CONNECTOR_ID)
    }

    /// Exchange one refresh token for a new token pair. Returns `None` on
    /// any failure (network, bad response, missing client creds) — callers
    /// fall back to a clear "reconnect" error. Persists on success.
    async fn refresh_now(&self) -> Option<ConnectorToken> {
        let old = self.load_stored()?;
        let rt = old
            .refresh_token
            .as_ref()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())?;
        let (client_id, client_secret) = client_credentials().ok()?;
        let resp = http_client(REQUEST_TIMEOUT)
            .post(self.token_url())
            .form(&[
                ("grant_type", "refresh_token"),
                ("refresh_token", rt.as_str()),
                ("client_id", client_id.as_str()),
                ("client_secret", client_secret.as_str()),
            ])
            .send()
            .await
            .ok()?;
        if !resp.status().is_success() {
            return None;
        }
        let data: Value = resp.json().await.ok()?;
        let tok = parse_token_response(&data, Some(&old)).ok()?;
        save_token(CONNECTOR_ID, &tok).ok()?;
        Some(tok)
    }

    /// A usable access token: stored one, refreshed proactively when near
    /// expiry. Fails honestly when not connected or when refresh fails.
    async fn ensure_token(&self) -> Result<ConnectorToken, ToolError> {
        let tok = self
            .load_stored()
            .filter(|t| t.is_present())
            .ok_or_else(|| ToolError::Other(NOT_CONNECTED_MSG.to_string()))?;
        if token_needs_refresh(&tok) {
            match self.refresh_now().await {
                Some(fresh) => Ok(fresh),
                None => Err(ToolError::Other(
                    "canva access token expired and the refresh failed (the stored refresh \
                     token may be revoked) — reconnect the Canva connector"
                        .to_string(),
                )),
            }
        } else {
            Ok(tok)
        }
    }

    // -- authed HTTP ---------------------------------------------------------

    /// One completed Canva API round-trip.
    async fn raw_request(
        &self,
        method: &str,
        path: &str,
        access_token: &str,
        json: Option<&Value>,
    ) -> Result<ApiResp, ToolError> {
        let url = format!("{}{}", self.api_base(), path);
        let mut req = match method {
            "POST" => self.client_post(&url),
            _ => http_client(REQUEST_TIMEOUT).get(&url),
        };
        req = req.bearer_auth(access_token);
        if let Some(body) = json {
            req = req.json(body);
        }
        let resp = req.send().await.map_err(transport_error)?;
        let status = resp.status().as_u16();
        let text = resp
            .text()
            .await
            .map_err(|_| ToolError::Http("failed to read Canva response body".to_string()))?;
        let body = serde_json::from_str::<Value>(text.trim()).unwrap_or(Value::Null);
        Ok(ApiResp { status, body })
    }

    fn client_post(&self, url: &str) -> reqwest::RequestBuilder {
        http_client(REQUEST_TIMEOUT).post(url)
    }

    /// GET with bearer auth; on 401, refresh once and retry. Never logs the
    /// token.
    async fn authed_get(&self, path: &str) -> Result<ApiResp, ToolError> {
        let tok = self.ensure_token().await?;
        let resp = self.raw_request("GET", path, &tok.access_token, None).await?;
        if resp.status == 401 {
            if let Some(fresh) = self.refresh_now().await {
                return self
                    .raw_request("GET", path, &fresh.access_token, None)
                    .await;
            }
        }
        Ok(resp)
    }

    /// POST with bearer auth; on 401, refresh once and retry.
    async fn authed_post(&self, path: &str, json: &Value) -> Result<ApiResp, ToolError> {
        let tok = self.ensure_token().await?;
        let resp = self
            .raw_request("POST", path, &tok.access_token, Some(json))
            .await?;
        if resp.status == 401 {
            if let Some(fresh) = self.refresh_now().await {
                return self
                    .raw_request("POST", path, &fresh.access_token, Some(json))
                    .await;
            }
        }
        Ok(resp)
    }

    /// Poll a Canva async job (`GET {path}`) until `status` is `success`
    /// (returns the body) or `failed` (returns a clear error), or the
    /// deadline passes.
    async fn poll_job(&self, path: &str, what: &str) -> Result<Value, ToolError> {
        let deadline = Instant::now() + JOB_POLL_DEADLINE;
        loop {
            let resp = self.authed_get(path).await?;
            check_status(&resp, what)?;
            let status = resp
                .body
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or("");
            match status {
                "success" => return Ok(resp.body),
                "failed" => {
                    let detail = resp
                        .body
                        .get("error")
                        .and_then(|e| e.get("message"))
                        .and_then(Value::as_str)
                        .unwrap_or("no detail from Canva");
                    return Err(ToolError::Other(format!(
                        "canva {what} job failed: {detail}"
                    )));
                }
                _ => {
                    if Instant::now() >= deadline {
                        return Err(ToolError::Timeout(JOB_POLL_DEADLINE.as_secs()));
                    }
                    tokio::time::sleep(JOB_POLL_INTERVAL).await;
                }
            }
        }
    }
}

impl Default for CanvaConnector {
    fn default() -> Self {
        Self::new()
    }
}

/// Never print tokens in logs or Debug output.
impl fmt::Debug for CanvaConnector {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("CanvaConnector")
            .field("api_base", &self.api_base)
            .field("connected", &self.is_connected())
            .field("tokens", &"<redacted>")
            .finish()
    }
}

#[async_trait]
impl Connector for CanvaConnector {
    fn id(&self) -> &'static str {
        CONNECTOR_ID
    }
    fn name(&self) -> &'static str {
        "Canva"
    }
    fn description(&self) -> &'static str {
        "Canva Connect API: list designs, create designs (preset, custom size, \
         or from a brand template) and export designs. OAuth2 sign-in."
    }
    fn auth_kind(&self) -> &'static str {
        "oauth2"
    }
    fn connect_url(&self) -> Result<String, String> {
        let (client_id, _secret) = client_credentials()?;
        // PKCE: the verifier is stored for `exchange_code` to consume.
        let verifier = generate_verifier();
        save_extra(CONNECTOR_ID, "pkce_verifier", &verifier)?;
        let state = format!("boom-{}", now_secs());
        Ok(build_auth_url(&client_id, &state, &code_challenge(&verifier)))
    }
    async fn exchange_code(&mut self, code: &str) -> Result<(), String> {
        let code = code.trim();
        if code.is_empty() {
            return Err("empty authorization code".to_string());
        }
        let (client_id, client_secret) = client_credentials()?;
        let verifier = load_extra(CONNECTOR_ID, "pkce_verifier")
            .filter(|s| !s.trim().is_empty())
            .ok_or_else(|| {
                "PKCE verifier missing — open a fresh connect_url and authorize again".to_string()
            })?;
        let resp = http_client(REQUEST_TIMEOUT)
            .post(self.token_url())
            .form(&[
                ("grant_type", "authorization_code"),
                ("code", code),
                ("redirect_uri", REDIRECT_URI),
                ("client_id", client_id.as_str()),
                ("client_secret", client_secret.as_str()),
                ("code_verifier", verifier.as_str()),
            ])
            .send()
            .await
            .map_err(|e| {
                if e.is_timeout() {
                    "connection to Canva timed out".to_string()
                } else {
                    "could not reach the Canva token endpoint".to_string()
                }
            })?;
        let status = resp.status().as_u16();
        let data: Value = resp.json().await.unwrap_or(Value::Null);
        if !(200..300).contains(&status) {
            return Err(token_endpoint_error(status, &data));
        }
        let tok = parse_token_response(&data, None)?;
        save_token(CONNECTOR_ID, &tok)?;
        Ok(())
    }
    fn disconnect(&mut self) -> Result<(), String> {
        delete_token(CONNECTOR_ID)
    }
    fn register_tools(&self, reg: &mut ToolRegistry) {
        reg.register("canva", DesignsTool::new(self.clone()));
        reg.register("canva", CreateTool::new(self.clone()));
        reg.register("canva", ExportTool::new(self.clone()));
    }
}

// ---------------------------------------------------------------------------
// Shared API plumbing
// ---------------------------------------------------------------------------

/// One completed Canva API round-trip.
struct ApiResp {
    status: u16,
    body: Value,
}

/// Generic transport failure message. Deliberately generic: reqwest errors
/// can echo request details, and we never want tokens in an error.
fn transport_error(err: reqwest::Error) -> ToolError {
    if err.is_timeout() {
        ToolError::Timeout(REQUEST_TIMEOUT.as_secs())
    } else if err.is_connect() {
        ToolError::Http("could not reach the Canva API (connection failed)".to_string())
    } else {
        ToolError::Http("Canva request failed".to_string())
    }
}

/// Short `message` from a Canva error body (`{"error": {"message": …}}`).
fn api_message(body: &Value) -> String {
    body.get("error")
        .and_then(|e| e.get("message"))
        .and_then(Value::as_str)
        .map(|s| s.trim().chars().take(300).collect::<String>())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "no detail from Canva".to_string())
}

/// Map a non-2xx Canva response to a `ToolError`. Nothing here ever contains
/// a token.
fn check_status(resp: &ApiResp, what: &str) -> Result<(), ToolError> {
    if (200..300).contains(&resp.status) {
        return Ok(());
    }
    let msg = api_message(&resp.body);
    match resp.status {
        401 => Err(ToolError::Other(format!(
            "Canva rejected the access token (401 Unauthorized): {msg} — reconnect the Canva \
             connector"
        ))),
        403 => Err(ToolError::Other(format!(
            "Canva refused the {what} request (403 Forbidden): {msg} — the integration may lack \
             the required scope"
        ))),
        404 => Err(ToolError::NotFound(format!(
            "Canva resource not found (404): {msg}"
        ))),
        429 => Err(ToolError::Other(
            "Canva rate limit exceeded (429): back off and retry shortly".to_string(),
        )),
        s => Err(ToolError::Http(format!(
            "Canva {what} returned HTTP {s}: {msg}"
        ))),
    }
}

// ---------------------------------------------------------------------------
// Response summarizers (pure — unit-tested against fixtures)
// ---------------------------------------------------------------------------

fn str_field(v: &Value, key: &str) -> String {
    v.get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

/// Unix seconds → RFC3339, empty string when missing/invalid.
fn ts_field(v: &Value, key: &str) -> String {
    use chrono::TimeZone;
    v.get(key)
        .and_then(Value::as_i64)
        .and_then(|s| chrono::Utc.timestamp_opt(s, 0).single())
        .map(|dt| dt.to_rfc3339())
        .unwrap_or_default()
}

/// `{id, title, created_at, updated_at, thumbnail_url, edit_url, view_url}`.
fn summarize_design(v: &Value) -> Value {
    serde_json::json!({
        "id": str_field(v, "id"),
        "title": str_field(v, "title"),
        "created_at": ts_field(v, "created_at"),
        "updated_at": ts_field(v, "updated_at"),
        "thumbnail_url": v.get("thumbnail").and_then(|t| t.get("url")).and_then(Value::as_str),
        "edit_url": v.get("urls").and_then(|u| u.get("edit_url")).and_then(Value::as_str),
        "view_url": v.get("urls").and_then(|u| u.get("view_url")).and_then(Value::as_str),
    })
}

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

/// Validate a Canva design id: non-empty, safe charset only (prevents path
/// injection into the request URL).
fn validate_design_id(raw: &str) -> Result<String, ToolError> {
    let v = raw.trim();
    if v.is_empty() {
        return Err(ToolError::InvalidArgs(
            "design_id must not be empty".to_string(),
        ));
    }
    if v.chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
    {
        Ok(v.to_string())
    } else {
        Err(ToolError::InvalidArgs(format!(
            "invalid design_id '{raw}': only letters, digits, '-' and '_' are allowed"
        )))
    }
}

/// `canva_designs` — `GET /designs`.
pub struct DesignsTool {
    conn: CanvaConnector,
}

impl DesignsTool {
    pub fn new(conn: CanvaConnector) -> Self {
        Self { conn }
    }
}

#[async_trait]
impl Tool for DesignsTool {
    fn name(&self) -> &str {
        "canva_designs"
    }
    fn description(&self) -> &str {
        "List the user's Canva designs (id, title, timestamps, thumbnail, \
         edit/view URLs). Paginated via the continuation token."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[(
                "continuation",
                "string",
                "Pagination token from a previous call (optional)",
            )],
            &[],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let cont = opt_str(&args, "continuation", "").trim();
        let path = if cont.is_empty() {
            "/designs".to_string()
        } else {
            // Continuation tokens are opaque; encode defensively.
            format!("/designs?continuation={}", pct_encode(cont))
        };
        let resp = self.conn.authed_get(&path).await?;
        check_status(&resp, "list designs")?;
        let designs: Vec<Value> = resp
            .body
            .get("items")
            .and_then(Value::as_array)
            .map(|a| a.iter().map(summarize_design).collect())
            .unwrap_or_default();
        Ok(serde_json::json!({
            "designs": designs,
            "count": designs.len(),
            "continuation": resp.body.get("continuation").and_then(Value::as_str),
        }))
    }
}

/// Build the `POST /designs` body for preset/custom creation. Pure.
fn build_create_body(
    title: &str,
    kind: &str,
    preset: &str,
    width: u64,
    height: u64,
) -> Result<Value, ToolError> {
    let design_type = match kind {
        "preset" => {
            if preset.trim().is_empty() {
                return Err(ToolError::InvalidArgs(
                    "preset must not be empty for kind=preset".to_string(),
                ));
            }
            serde_json::json!({"type": "preset", "name": preset.trim()})
        }
        "custom" => {
            if width == 0 || height == 0 {
                return Err(ToolError::InvalidArgs(
                    "width and height must be positive for kind=custom".to_string(),
                ));
            }
            serde_json::json!({"type": "custom", "width": width, "height": height})
        }
        _ => {
            return Err(ToolError::InvalidArgs(format!(
                "invalid kind '{kind}': expected preset, custom or template"
            )))
        }
    };
    Ok(serde_json::json!({ "design_type": design_type, "title": title }))
}

/// `canva_create` — `POST /designs` (preset/custom) or the autofill flow
/// (from a brand template).
pub struct CreateTool {
    conn: CanvaConnector,
}

impl CreateTool {
    pub fn new(conn: CanvaConnector) -> Self {
        Self { conn }
    }
}

#[async_trait]
impl Tool for CreateTool {
    fn name(&self) -> &str {
        "canva_create"
    }
    fn description(&self) -> &str {
        "Create a Canva design: from a preset (e.g. \"Presentation (16:9)\"), \
         a custom width×height, or a brand template (autofill). Returns the \
         new design with edit/view URLs."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("title", "string", "Title of the new design"),
                (
                    "kind",
                    "string",
                    "preset, custom, or template (default preset)",
                ),
                (
                    "preset",
                    "string",
                    "Preset name for kind=preset (default \"Presentation (16:9)\")",
                ),
                ("width", "integer", "Width in px for kind=custom"),
                ("height", "integer", "Height in px for kind=custom"),
                (
                    "template_id",
                    "string",
                    "Brand template id for kind=template",
                ),
                (
                    "template_data",
                    "string",
                    "JSON object of autofill data for kind=template (optional)",
                ),
            ],
            &["title"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let title = req_str(&args, "title")?;
        if title.trim().is_empty() {
            return Err(ToolError::InvalidArgs("title must not be empty".to_string()));
        }
        let kind = opt_str(&args, "kind", "preset").trim().to_lowercase();
        if kind == "template" {
            return self.create_from_template(&args, title.trim()).await;
        }
        let body = build_create_body(
            title.trim(),
            &kind,
            opt_str(&args, "preset", "Presentation (16:9)"),
            opt_u64(&args, "width", 0),
            opt_u64(&args, "height", 0),
        )?;
        let resp = self.conn.authed_post("/designs", &body).await?;
        check_status(&resp, "create design")?;
        Ok(serde_json::json!({ "design": summarize_design(&resp.body) }))
    }
}

impl CreateTool {
    /// Brand-template flow: `POST /autofills` → poll → `GET /designs/{id}`.
    async fn create_from_template(&self, args: &Value, title: &str) -> Result<Value, ToolError> {
        let template_id = opt_str(&args, "template_id", "").trim();
        if template_id.is_empty() {
            return Err(ToolError::InvalidArgs(
                "template_id is required for kind=template".to_string(),
            ));
        }
        let mut body = serde_json::json!({
            "brand_template_id": template_id,
            "title": title,
        });
        let data_raw = opt_str(&args, "template_data", "").trim();
        if !data_raw.is_empty() {
            let data: Value = serde_json::from_str(data_raw).map_err(|_| {
                ToolError::InvalidArgs("template_data must be a JSON object".to_string())
            })?;
            if !data.is_object() {
                return Err(ToolError::InvalidArgs(
                    "template_data must be a JSON object".to_string(),
                ));
            }
            body["data"] = data;
        }
        let resp = self.conn.authed_post("/autofills", &body).await?;
        check_status(&resp, "create autofill job")?;
        let job_id = resp
            .body
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if job_id.is_empty() {
            return Err(ToolError::Other(
                "canva autofill response did not contain a job id".to_string(),
            ));
        }
        let done = self
            .conn
            .poll_job(&format!("/autofills/{job_id}"), "autofill")
            .await?;
        let design_id = done
            .get("result")
            .and_then(|r| r.get("designs"))
            .and_then(Value::as_array)
            .and_then(|a| a.first())
            .and_then(|d| d.get("id"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if design_id.is_empty() {
            return Err(ToolError::Other(
                "canva autofill finished without producing a design".to_string(),
            ));
        }
        let design_id = validate_design_id(&design_id)?;
        let resp = self.conn.authed_get(&format!("/designs/{design_id}")).await?;
        check_status(&resp, "get design")?;
        Ok(serde_json::json!({ "design": summarize_design(&resp.body) }))
    }
}

/// `canva_export` — `POST /exports` → poll → download URLs.
pub struct ExportTool {
    conn: CanvaConnector,
}

impl ExportTool {
    pub fn new(conn: CanvaConnector) -> Self {
        Self { conn }
    }
}

#[async_trait]
impl Tool for ExportTool {
    fn name(&self) -> &str {
        "canva_export"
    }
    fn description(&self) -> &str {
        "Export a Canva design (pdf, png, jpg, pptx, mp4, gif). Starts an \
         export job, waits for it to finish, and returns download URLs."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("design_id", "string", "Id of the design to export"),
                (
                    "format",
                    "string",
                    "Export format: pdf, png, jpg, pptx, mp4, gif",
                ),
                (
                    "pages",
                    "string",
                    "Page selector for multi-page formats, e.g. \"1-3\" (optional)",
                ),
            ],
            &["design_id", "format"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let design_id = validate_design_id(&req_str(&args, "design_id")?)?;
        let format = opt_str(&args, "format", "").trim().to_lowercase();
        let allowed = ["pdf", "png", "jpg", "pptx", "mp4", "gif"];
        if !allowed.contains(&format.as_str()) {
            return Err(ToolError::InvalidArgs(format!(
                "invalid format '{format}': expected one of {}",
                allowed.join(", ")
            )));
        }
        let mut body = serde_json::json!({ "design_id": design_id, "format": format });
        let pages = opt_str(&args, "pages", "").trim();
        if !pages.is_empty() {
            body["pages"] = Value::String(pages.to_string());
        }
        let resp = self.conn.authed_post("/exports", &body).await?;
        check_status(&resp, "create export job")?;
        let job_id = resp
            .body
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if job_id.is_empty() {
            return Err(ToolError::Other(
                "canva export response did not contain a job id".to_string(),
            ));
        }
        let done = self
            .conn
            .poll_job(&format!("/exports/{job_id}"), "export")
            .await?;
        let urls: Vec<String> = done
            .get("urls")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default();
        Ok(serde_json::json!({
            "design_id": design_id,
            "format": format,
            "export_id": job_id,
            "urls": urls,
        }))
    }
}

// ---------------------------------------------------------------------------
// Tests (all offline — fixtures + validation + honest degradation + mock API)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    struct TestHome {
        _lock: tokio::sync::MutexGuard<'static, ()>,
        _dir: tempfile::TempDir,
        old: Option<String>,
        old_client_id: Option<String>,
        old_client_secret: Option<String>,
    }

    impl TestHome {
        async fn new() -> Self {
            let lock = crate::connectors::TEST_ENV_LOCK.lock().await;
            let dir = tempfile::tempdir().unwrap();
            let old = std::env::var("BOOM_HOME").ok();
            std::env::set_var("BOOM_HOME", dir.path());
            let old_client_id = std::env::var("BOOM_CANVA_CLIENT_ID").ok();
            let old_client_secret = std::env::var("BOOM_CANVA_CLIENT_SECRET").ok();
            std::env::set_var("BOOM_CANVA_CLIENT_ID", "test-client-id");
            std::env::set_var("BOOM_CANVA_CLIENT_SECRET", "test-client-secret");
            Self {
                _lock: lock,
                _dir: dir,
                old,
                old_client_id,
                old_client_secret,
            }
        }
    }

    impl Drop for TestHome {
        fn drop(&mut self) {
            match self.old.take() {
                Some(v) => std::env::set_var("BOOM_HOME", v),
                None => std::env::remove_var("BOOM_HOME"),
            }
            match self.old_client_id.take() {
                Some(v) => std::env::set_var("BOOM_CANVA_CLIENT_ID", v),
                None => std::env::remove_var("BOOM_CANVA_CLIENT_ID"),
            }
            match self.old_client_secret.take() {
                Some(v) => std::env::set_var("BOOM_CANVA_CLIENT_SECRET", v),
                None => std::env::remove_var("BOOM_CANVA_CLIENT_SECRET"),
            }
        }
    }

    /// Raw-TCP mock serving a sequence of canned responses, capturing raw
    /// requests. `seq[i]` is the response for the i-th request.
    async fn mock_seq(
        seq: Vec<(u16, &'static str)>,
    ) -> (String, tokio::task::JoinHandle<Vec<String>>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind mock server");
        let addr = listener.local_addr().expect("local addr");
        let handle = tokio::spawn(async move {
            use tokio::io::{AsyncReadExt, AsyncWriteExt};
            let mut requests = Vec::new();
            for (status, body) in seq {
                let (mut sock, _) = listener.accept().await.expect("accept");
                let mut req: Vec<u8> = Vec::new();
                let mut buf = vec![0u8; 8192];
                let mut content_len = 0usize;
                let mut header_done = false;
                loop {
                    let n = sock.read(&mut buf).await.expect("read");
                    if n == 0 {
                        break;
                    }
                    req.extend_from_slice(&buf[..n]);
                    if !header_done {
                        if let Some(pos) = req.windows(4).position(|w| w == b"\r\n\r\n") {
                            header_done = true;
                            let head = String::from_utf8_lossy(&req[..pos + 4]);
                            content_len = head
                                .lines()
                                .find_map(|l| {
                                    let (k, v) = l.split_once(':')?;
                                    k.trim()
                                        .eq_ignore_ascii_case("content-length")
                                        .then(|| v.trim().parse::<usize>().ok())
                                        .flatten()
                                })
                                .unwrap_or(0);
                            let body_start = pos + 4;
                            if req.len() - body_start >= content_len {
                                break;
                            }
                        }
                    } else {
                        let body_start = req
                            .windows(4)
                            .position(|w| w == b"\r\n\r\n")
                            .map(|p| p + 4)
                            .unwrap_or(req.len());
                        if req.len() - body_start >= content_len {
                            break;
                        }
                    }
                }
                requests.push(String::from_utf8_lossy(&req).into_owned());
                let reason = match status {
                    200 => "OK",
                    400 => "Bad Request",
                    401 => "Unauthorized",
                    403 => "Forbidden",
                    404 => "Not Found",
                    _ => "Error",
                };
                let resp = format!(
                    "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n\
                     Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                sock.write_all(resp.as_bytes()).await.expect("write");
            }
            requests
        });
        (format!("http://{addr}"), handle)
    }

    async fn save_test_token() -> TestHome {
        let home = TestHome::new().await;
        save_token(
            CONNECTOR_ID,
            &ConnectorToken {
                access_token: "canva-access-1".to_string(),
                refresh_token: Some("canva-refresh-1".to_string()),
                expires_at: Some(now_secs() + 3600),
                extra: serde_json::json!({}),
            },
        )
        .unwrap();
        home
    }

    const TOKEN_FIXTURE: &str = r#"{"access_token":"canva-access-2","token_type":"Bearer",
        "expires_in":3600,"refresh_token":"canva-refresh-2"}"#;

    const DESIGNS_FIXTURE: &str = r#"{"items":[
      {"id":"DAGAbc123","title":"Pitch Deck","created_at":1760000000,"updated_at":1760000100,
       "thumbnail":{"width":100,"height":60,"url":"https://thumb.example/t.png"},
       "urls":{"edit_url":"https://www.canva.com/api/design/AAA/edit",
               "view_url":"https://www.canva.com/api/design/AAA/view"}}
    ],"continuation":"next-page"}"#;

    const DESIGN_FIXTURE: &str = r#"{"id":"DAGNew1","title":"My Design","created_at":1760000200,
      "updated_at":1760000200,
      "urls":{"edit_url":"https://www.canva.com/api/design/BBB/edit",
              "view_url":"https://www.canva.com/api/design/BBB/view"}}"#;

    #[test]
    fn connector_identity() {
        let c = CanvaConnector::new();
        assert_eq!(c.id(), "canva");
        assert_eq!(c.name(), "Canva");
        assert_eq!(c.auth_kind(), "oauth2");
        assert!(!c.description().is_empty());
    }

    #[test]
    fn pkce_helpers() {
        let v1 = generate_verifier();
        let v2 = generate_verifier();
        assert_ne!(v1, v2, "verifiers must be unique");
        assert!((43..=128).contains(&v1.len()), "len = {}", v1.len());
        let ch = code_challenge(&v1);
        assert_eq!(ch.len(), 43, "SHA256 base64url without padding is 43 chars");
        assert_eq!(ch, code_challenge(&v1), "challenge is deterministic");
        // RFC 7636 Appendix B example.
        assert_eq!(
            code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        );
    }

    #[test]
    fn auth_url_shape() {
        let url = build_auth_url("cid", "st", "chal");
        assert!(url.starts_with("https://www.canva.com/api/oauth/authorize?"));
        for part in [
            "response_type=code",
            "client_id=cid",
            "redirect_uri=",
            "scope=",
            "state=st",
            "code_challenge=chal",
            "code_challenge_method=S256",
        ] {
            assert!(url.contains(part), "missing {part} in {url}");
        }
        assert!(url.contains("design%3Acontent%3Aread") || url.contains("design:content:read"));
    }

    #[tokio::test]
    async fn connect_url_stores_verifier_and_points_at_canva() {
        let _home = TestHome::new().await;
        let c = CanvaConnector::new();
        let url = c.connect_url().unwrap();
        assert!(url.starts_with("https://www.canva.com/api/oauth/authorize?"));
        assert!(url.contains("client_id=test-client-id"));
        assert!(!url.contains("test-client-secret"), "secret leaked in URL: {url}");
        let verifier = load_extra(CONNECTOR_ID, "pkce_verifier").expect("verifier stored");
        assert!((43..=128).contains(&verifier.len()));
        assert!(url.contains(&format!("code_challenge={}", code_challenge(&verifier))));
    }

    #[tokio::test]
    async fn connect_url_fails_clearly_without_client_config() {
        let _home = TestHome::new().await;
        std::env::remove_var("BOOM_CANVA_CLIENT_ID");
        std::env::remove_var("BOOM_CANVA_CLIENT_SECRET");
        let c = CanvaConnector::new();
        let err = c.connect_url().unwrap_err();
        assert!(err.contains("BOOM_CANVA_CLIENT_ID"), "got: {err}");
        assert!(err.contains("BOOM_CANVA_CLIENT_SECRET"), "got: {err}");
    }

    #[test]
    fn schemas_are_valid() {
        let mut reg = ToolRegistry::default();
        CanvaConnector::new().register_tools(&mut reg);
        for name in ["canva_designs", "canva_create", "canva_export"] {
            let tool = reg.get(name).unwrap_or_else(|| panic!("tool '{name}' missing"));
            assert!(!tool.description().is_empty());
            let schema = tool.schema();
            let obj = schema.as_object().expect("schema is object");
            assert_eq!(obj.get("type").and_then(|t| t.as_str()), Some("object"));
            let props = obj
                .get("properties")
                .and_then(|p| p.as_object())
                .expect("schema has properties");
            assert!(!props.is_empty());
            for req in obj
                .get("required")
                .and_then(|r| r.as_array())
                .cloned()
                .unwrap_or_default()
            {
                let key = req.as_str().expect("required entry is string");
                assert!(props.contains_key(key), "required '{key}' missing in {name}");
            }
        }
        assert_eq!(reg.tools_in_toolset("canva").len(), 3);
    }

    #[tokio::test]
    async fn token_roundtrip_and_0600() {
        let _home = TestHome::new().await;
        assert!(!CanvaConnector::new().is_connected());
        let tok = ConnectorToken {
            access_token: "a".to_string(),
            refresh_token: Some("r".to_string()),
            expires_at: Some(now_secs() + 3600),
            extra: serde_json::json!({}),
        };
        save_token(CONNECTOR_ID, &tok).unwrap();
        assert!(CanvaConnector::new().is_connected());
        let loaded = load_token(CONNECTOR_ID).unwrap();
        assert_eq!(loaded.access_token, "a");
        assert_eq!(loaded.refresh_token.as_deref(), Some("r"));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(super::super::token_path(CONNECTOR_ID))
                .unwrap()
                .permissions()
                .mode()
                & 0o777;
            assert_eq!(mode, 0o600, "token file mode = {mode:o}");
        }
        // Expired tokens are not "connected".
        let mut expired = tok.clone();
        expired.expires_at = Some(1);
        save_token(CONNECTOR_ID, &expired).unwrap();
        assert!(!CanvaConnector::new().is_connected());
        delete_token(CONNECTOR_ID).unwrap();
        assert!(!CanvaConnector::new().is_connected());
        delete_token(CONNECTOR_ID).unwrap(); // idempotent
    }

    #[tokio::test]
    async fn exchange_code_ok_stores_tokens() {
        let _home = TestHome::new().await;
        let (token_base, handle) = mock_seq(vec![(200, TOKEN_FIXTURE)]).await;
        save_extra(CONNECTOR_ID, "pkce_verifier", "test-verifier-123").unwrap();
        let mut c = CanvaConnector::with_endpoints(
            "http://127.0.0.1:9",
            &format!("{token_base}/oauth/token"),
        );
        c.exchange_code("auth-code-xyz").await.unwrap();
        assert!(c.is_connected());
        let tok = load_token(CONNECTOR_ID).unwrap();
        assert_eq!(tok.access_token, "canva-access-2");
        assert_eq!(tok.refresh_token.as_deref(), Some("canva-refresh-2"));
        assert!(tok.expires_at.unwrap() > now_secs() + 3500);

        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("POST /oauth/token "), "request: {raw}");
        assert!(raw.contains("grant_type=authorization_code"), "body: {raw}");
        assert!(raw.contains("code_verifier=test-verifier-123"), "body: {raw}");
        assert!(raw.contains("code=auth-code-xyz"), "code missing: {raw}");

        let dbg = format!("{:?}", c);
        assert!(!dbg.contains("canva-access-2"), "token leaked: {dbg}");
        assert!(dbg.contains("<redacted>"));
    }

    #[tokio::test]
    async fn exchange_code_rejects_bad_code_without_saving() {
        let _home = TestHome::new().await;
        let (token_base, _h) = mock_seq(vec![(
            400,
            r#"{"error":"invalid_grant","error_description":"bad code"}"#,
        )])
        .await;
        save_extra(CONNECTOR_ID, "pkce_verifier", "v").unwrap();
        let mut c = CanvaConnector::with_endpoints(
            "http://127.0.0.1:9",
            &format!("{token_base}/oauth/token"),
        );
        let err = c.exchange_code("bad-code").await.unwrap_err();
        assert!(err.contains("400"), "got: {err}");
        assert!(err.contains("bad code"), "got: {err}");
        assert!(!err.contains("test-client-secret"), "secret leaked: {err}");
        assert!(!c.is_connected());
    }

    #[tokio::test]
    async fn exchange_code_needs_verifier() {
        let _home = TestHome::new().await;
        let mut c = CanvaConnector::new();
        let err = c.exchange_code("some-code").await.unwrap_err();
        assert!(err.contains("verifier"), "got: {err}");
    }

    #[tokio::test]
    async fn proactive_refresh_before_expiry() {
        let _home = TestHome::new().await;
        // Token expiring in 60s (< 300s skew) must be refreshed before use.
        let (token_base, _h) = mock_seq(vec![(200, TOKEN_FIXTURE)]).await;
        save_token(
            CONNECTOR_ID,
            &ConnectorToken {
                access_token: "old-access".to_string(),
                refresh_token: Some("old-refresh".to_string()),
                expires_at: Some(now_secs() + 60),
                extra: serde_json::json!({}),
            },
        )
        .unwrap();
        let (api_base, api_h) = mock_seq(vec![(200, DESIGNS_FIXTURE)]).await;
        let c = CanvaConnector::with_endpoints(&api_base, &format!("{token_base}/oauth/token"));
        let tool = DesignsTool::new(c);
        let out = tool.execute(serde_json::json!({})).await.unwrap();
        assert_eq!(out["count"], 1);
        // The API call used the refreshed token.
        let raw = &api_h.await.unwrap()[0];
        assert!(
            raw.to_lowercase().contains("authorization: bearer canva-access-2"),
            "auth: {raw}"
        );
        // And the refreshed token was persisted.
        assert_eq!(
            load_token(CONNECTOR_ID).unwrap().access_token,
            "canva-access-2"
        );
    }

    #[tokio::test]
    async fn reactive_refresh_on_401() {
        let _home = save_test_token().await;
        let (token_base, _h) = mock_seq(vec![(200, TOKEN_FIXTURE)]).await;
        // First API call 401s, retry with the refreshed token succeeds.
        let (api_base, api_h) =
            mock_seq(vec![(401, r#"{"error":{"message":"expired"}}"#), (200, DESIGNS_FIXTURE)])
                .await;
        let c = CanvaConnector::with_endpoints(&api_base, &format!("{token_base}/oauth/token"));
        let tool = DesignsTool::new(c);
        let out = tool.execute(serde_json::json!({})).await.unwrap();
        assert_eq!(out["count"], 1);
        let raws = api_h.await.unwrap();
        assert_eq!(raws.len(), 2);
        assert!(
            raws[1].to_lowercase().contains("authorization: bearer canva-access-2"),
            "retry auth: {}",
            raws[1]
        );
    }

    #[tokio::test]
    async fn not_connected_is_honest() {
        let _home = TestHome::new().await;
        let tool = DesignsTool::new(CanvaConnector::new());
        let err = tool.execute(serde_json::json!({})).await.unwrap_err();
        assert!(err.to_string().contains("not connected"), "got: {err}");
    }

    #[tokio::test]
    async fn designs_lists_and_summarizes() {
        let _home = save_test_token().await;
        let (api_base, handle) = mock_seq(vec![(200, DESIGNS_FIXTURE)]).await;
        let c = CanvaConnector::with_endpoints(&api_base, "http://127.0.0.1:9");
        let tool = DesignsTool::new(c);
        let out = tool
            .execute(serde_json::json!({"continuation": "tok-1"}))
            .await
            .unwrap();
        assert_eq!(out["count"], 1);
        let d = &out["designs"][0];
        assert_eq!(d["id"], "DAGAbc123");
        assert_eq!(d["title"], "Pitch Deck");
        assert!(d["created_at"].as_str().unwrap().starts_with("2025-"));
        assert_eq!(
            d["edit_url"],
            "https://www.canva.com/api/design/AAA/edit"
        );
        assert_eq!(out["continuation"], "next-page");
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("GET /designs?continuation=tok-1 "), "request: {raw}");
    }

    #[tokio::test]
    async fn create_preset_posts_design_type() {
        let _home = save_test_token().await;
        let (api_base, handle) = mock_seq(vec![(200, DESIGN_FIXTURE)]).await;
        let c = CanvaConnector::with_endpoints(&api_base, "http://127.0.0.1:9");
        let tool = CreateTool::new(c);
        let out = tool
            .execute(serde_json::json!({"title": "My Design", "preset": "Doc"}))
            .await
            .unwrap();
        assert_eq!(out["design"]["id"], "DAGNew1");
        assert_eq!(out["design"]["title"], "My Design");
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("POST /designs "), "request: {raw}");
        assert!(raw.contains(r#""type":"preset""#), "body: {raw}");
        assert!(raw.contains(r#""name":"Doc""#), "body: {raw}");
    }

    #[tokio::test]
    async fn create_custom_validates_dimensions() {
        let _home = save_test_token().await;
        let c = CanvaConnector::with_endpoints("http://127.0.0.1:9", "http://127.0.0.1:9");
        let tool = CreateTool::new(c);
        let err = tool
            .execute(serde_json::json!({"title": "X", "kind": "custom"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "got: {err}");
        let err = tool
            .execute(serde_json::json!({"title": "X", "kind": "nope"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "got: {err}");
        let err = tool
            .execute(serde_json::json!({"title": "X", "kind": "template"}))
            .await
            .unwrap_err();
        assert!(err.to_string().contains("template_id"), "got: {err}");
    }

    #[tokio::test]
    async fn create_from_template_runs_autofill_flow() {
        let _home = save_test_token().await;
        let autofill_done = r#"{"id":"job-1","status":"success",
            "result":{"designs":[{"id":"DAGFromTpl"}]}}"#;
        let (api_base, handle) = mock_seq(vec![
            (200, r#"{"id":"job-1","status":"in_progress"}"#),
            (200, autofill_done),
            (200, DESIGN_FIXTURE),
        ])
        .await;
        let c = CanvaConnector::with_endpoints(&api_base, "http://127.0.0.1:9");
        let tool = CreateTool::new(c);
        let out = tool
            .execute(serde_json::json!({
                "title": "Flyer",
                "kind": "template",
                "template_id": "tpl-1",
                "template_data": "{\"name\":\"Sale\"}",
            }))
            .await
            .unwrap();
        assert_eq!(out["design"]["id"], "DAGNew1");
        let raws = handle.await.unwrap();
        assert_eq!(raws.len(), 3);
        assert!(raws[0].contains("POST /autofills "), "req0: {}", raws[0]);
        assert!(raws[0].contains("brand_template_id"), "req0: {}", raws[0]);
        assert!(raws[1].contains("GET /autofills/job-1 "), "req1: {}", raws[1]);
        assert!(raws[2].contains("GET /designs/DAGFromTpl "), "req2: {}", raws[2]);
    }

    #[tokio::test]
    async fn export_polls_until_success() {
        let _home = save_test_token().await;
        let (api_base, handle) = mock_seq(vec![
            (200, r#"{"id":"exp-1","status":"in_progress"}"#),
            (200, r#"{"id":"exp-1","status":"in_progress"}"#),
            (
                200,
                r#"{"id":"exp-1","status":"success","urls":["https://export.example/a.pdf"]}"#,
            ),
        ])
        .await;
        let c = CanvaConnector::with_endpoints(&api_base, "http://127.0.0.1:9");
        let tool = ExportTool::new(c);
        let out = tool
            .execute(serde_json::json!({"design_id": "DAGAbc123", "format": "pdf"}))
            .await
            .unwrap();
        assert_eq!(out["design_id"], "DAGAbc123");
        assert_eq!(out["format"], "pdf");
        assert_eq!(out["export_id"], "exp-1");
        assert_eq!(out["urls"], serde_json::json!(["https://export.example/a.pdf"]));
        let raws = handle.await.unwrap();
        assert_eq!(raws.len(), 3);
        assert!(raws[0].contains("POST /exports "), "req0: {}", raws[0]);
        assert!(raws[0].contains(r#""format":"pdf""#), "req0: {}", raws[0]);
    }

    #[tokio::test]
    async fn export_validates_args() {
        let _home = save_test_token().await;
        let c = CanvaConnector::with_endpoints("http://127.0.0.1:9", "http://127.0.0.1:9");
        let tool = ExportTool::new(c);
        let err = tool
            .execute(serde_json::json!({"design_id": "DAGAbc123", "format": "exe"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "got: {err}");
        let err = tool
            .execute(serde_json::json!({"design_id": "../x", "format": "pdf"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "got: {err}");
    }

    #[tokio::test]
    async fn failed_job_is_a_clear_error() {
        let _home = save_test_token().await;
        let (api_base, _h) = mock_seq(vec![
            (200, r#"{"id":"exp-9","status":"in_progress"}"#),
            (
                200,
                r#"{"id":"exp-9","status":"failed","error":{"message":"render exploded"}}"#,
            ),
        ])
        .await;
        let c = CanvaConnector::with_endpoints(&api_base, "http://127.0.0.1:9");
        let tool = ExportTool::new(c);
        let err = tool
            .execute(serde_json::json!({"design_id": "DAGAbc123", "format": "pdf"}))
            .await
            .unwrap_err();
        assert!(err.to_string().contains("render exploded"), "got: {err}");
    }

    #[test]
    fn pure_helpers() {
        let designs: Value = serde_json::from_str(DESIGNS_FIXTURE).unwrap();
        let s = summarize_design(&designs["items"][0]);
        assert_eq!(s["thumbnail_url"], "https://thumb.example/t.png");
        assert!(s["updated_at"].as_str().unwrap().starts_with("2025-"));

        let body = build_create_body("T", "preset", "Doc", 0, 0).unwrap();
        assert_eq!(body["design_type"]["name"], "Doc");
        let body = build_create_body("T", "custom", "", 1080, 1080).unwrap();
        assert_eq!(body["design_type"]["width"], 1080);
        assert!(build_create_body("T", "custom", "", 0, 0).is_err());
        assert!(build_create_body("T", "bogus", "", 0, 0).is_err());

        assert_eq!(validate_design_id("DAGAbc-123_x").unwrap(), "DAGAbc-123_x");
        assert!(validate_design_id("../x").is_err());
        assert!(validate_design_id("").is_err());

        // Token-response parsing keeps an old refresh token when omitted.
        let old = ConnectorToken {
            access_token: "o".to_string(),
            refresh_token: Some("old-rt".to_string()),
            expires_at: None,
            extra: serde_json::json!({}),
        };
        let tok = parse_token_response(
            &serde_json::json!({"access_token": "n", "expires_in": 100}),
            Some(&old),
        )
        .unwrap();
        assert_eq!(tok.refresh_token.as_deref(), Some("old-rt"));
        assert!(tok.expires_at.unwrap() > now_secs());
        assert!(parse_token_response(&serde_json::json!({}), None).is_err());

        // 401 maps to a reconnect message without secrets.
        let resp = ApiResp {
            status: 401,
            body: serde_json::json!({"error": {"message": "bad"}}),
        };
        let msg = check_status(&resp, "x").unwrap_err().to_string();
        assert!(msg.contains("reconnect"), "got: {msg}");
    }
}
