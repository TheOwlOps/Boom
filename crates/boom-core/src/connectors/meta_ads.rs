//! Meta Ads connector — list ad accounts, campaigns, and ad insights.
//!
//! Wraps the Meta Marketing (Graph) API. Auth is a Meta access token (a user
//! token from the Graph API Explorer or, for production, a system-user token
//! from Meta Business Settings):
//! [`MetaAdsConnector::exchange_code`] treats its `code` argument as the
//! token, verifies it with `GET /v21.0/me`, and stores it in
//! `$BOOM_HOME/connectors/meta_ads.json` (mode 0600) as `access_token`, plus
//! non-secret `extra.user_id` / `extra.user_name`. Every tool call loads the
//! token fresh from that file, so the connector struct itself holds no secret
//! — `Debug` output and error messages can never leak it.
//!
//! NOTE: the connector id is `meta_ads` (not `meta-ads`) because token-file
//! ids are restricted to `[a-z0-9_]+`; tool names (`meta_ads_accounts`, …)
//! match the requested names exactly.
//!
//! ## Merge note for the coordinator
//!
//! Drop this file at `crates/boom-core/src/connectors/meta_ads.rs`, declare
//! `pub mod meta_ads;` in `src/connectors/mod.rs`, and register
//! `MetaAdsConnector::new()` in `ConnectorRegistry::new()` (also extend
//! `ALL_CONNECTOR_IDS`). It uses only the shared contract from the parent
//! module (`Connector`, `ConnectorToken`, `load_token`, `save_token`,
//! `save_extra`, `delete_token`, `http_client`).

use std::fmt;
use std::time::Duration;

use async_trait::async_trait;
use serde_json::Value;

use super::{
    delete_token, http_client, load_token, save_extra, save_token, Connector, ConnectorToken,
};
use crate::tools::{opt_str, opt_u64, req_str, schema_of, Tool, ToolError, ToolRegistry};

/// Stable connector id (also the token file name: `meta_ads.json`).
const CONNECTOR_ID: &str = "meta_ads";
const API_BASE: &str = "https://graph.facebook.com/v21.0";
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);

const CONNECT_INSTRUCTIONS: &str = "Meta Ads uses a Meta access token — POST it to /api/connectors/meta_ads/token.\n\
For testing: https://developers.facebook.com/tools/explorer — create/select a Meta app, add the \
ads_read permission, generate a user token. For production: Meta Business Settings → System users → \
create a system user with the ad account assigned, generate a token with ads_read scope. \
The token is verified with GET /v21.0/me and stored in $BOOM_HOME/connectors/meta_ads.json (mode 0600).";

const NOT_CONNECTED_MSG: &str = "meta ads is not connected: no access token is stored. POST a \
Meta access token to /api/connectors/meta_ads/token first (see the connector's connect instructions).";

/// Meta Ads connector. Holds no secret: the token is loaded from the token
/// file on every call, so `exchange_code`/`disconnect` take effect immediately.
pub struct MetaAdsConnector {
    api_base: String,
}

impl MetaAdsConnector {
    /// Production constructor: real Graph API, token from `$BOOM_HOME`.
    pub fn new() -> Self {
        Self::with_api_base(API_BASE)
    }

    /// Constructor with an explicit API base — tests drive a local mock
    /// server through this.
    pub fn with_api_base(api_base: impl Into<String>) -> Self {
        let api_base: String = api_base.into();
        Self {
            api_base: api_base.trim_end_matches('/').to_string(),
        }
    }

    fn api_base(&self) -> &str {
        &self.api_base
    }
}

impl Default for MetaAdsConnector {
    fn default() -> Self {
        Self::new()
    }
}

/// Never print the token in logs or Debug output — the struct holds no token
/// at all, and this impl makes that a compile-time guarantee.
impl fmt::Debug for MetaAdsConnector {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("MetaAdsConnector")
            .field("api_base", &self.api_base)
            .field("connected", &self.is_connected())
            .field("access_token", &"<redacted>")
            .finish()
    }
}

#[async_trait]
impl Connector for MetaAdsConnector {
    fn id(&self) -> &'static str {
        CONNECTOR_ID
    }
    fn name(&self) -> &'static str {
        "Meta Ads"
    }
    fn description(&self) -> &'static str {
        "Meta Marketing API: list ad accounts, campaigns and performance \
         insights. Authenticates with a Meta access token (ads_read)."
    }
    fn auth_kind(&self) -> &'static str {
        "api_key"
    }
    fn connect_url(&self) -> Result<String, String> {
        // No OAuth: the "code" step consumes the raw access token instead.
        Err(CONNECT_INSTRUCTIONS.to_string())
    }
    async fn exchange_code(&mut self, code: &str) -> Result<(), String> {
        let token = code.trim();
        if token.is_empty() {
            return Err("empty Meta access token".to_string());
        }
        let (user_id, user_name) = verify_token(self.api_base(), token).await?;
        save_token(CONNECTOR_ID, &ConnectorToken::api_key(token))?;
        // User id/name are not secret.
        let _ = save_extra(CONNECTOR_ID, "user_id", &user_id);
        let _ = save_extra(CONNECTOR_ID, "user_name", &user_name);
        Ok(())
    }
    fn disconnect(&mut self) -> Result<(), String> {
        delete_token(CONNECTOR_ID)
    }
    fn register_tools(&self, reg: &mut ToolRegistry) {
        reg.register("meta_ads", AccountsTool::new(self.api_base.clone()));
        reg.register("meta_ads", CampaignsTool::new(self.api_base.clone()));
        reg.register("meta_ads", InsightsTool::new(self.api_base.clone()));
    }
}

// ---------------------------------------------------------------------------
// HTTP transport
// ---------------------------------------------------------------------------

/// One completed Graph API round-trip.
struct ApiResp {
    status: u16,
    body: Value,
}

/// GET against the Graph API with `Authorization: Bearer <token>`. The token
/// travels only in the Authorization header and is never logged.
async fn graph_get(
    api_base: &str,
    token: &str,
    path: &str,
    query: &[(&str, String)],
) -> Result<ApiResp, ToolError> {
    let url = format!("{}{}", api_base, path);
    let resp = http_client(REQUEST_TIMEOUT)
        .get(&url)
        .bearer_auth(token)
        .query(&query)
        .send()
        .await
        .map_err(transport_error)?;
    let status = resp.status().as_u16();
    let text = resp
        .text()
        .await
        .map_err(|_| ToolError::Http("failed to read Meta API response body".to_string()))?;
    let body = serde_json::from_str::<Value>(text.trim()).unwrap_or(Value::Null);
    Ok(ApiResp { status, body })
}

/// Generic transport failure message. Deliberately generic: reqwest errors
/// can echo request details, and we never want the token in an error.
fn transport_error(err: reqwest::Error) -> ToolError {
    if err.is_timeout() {
        ToolError::Timeout(REQUEST_TIMEOUT.as_secs())
    } else if err.is_connect() {
        ToolError::Http("could not reach the Meta Graph API (connection failed)".to_string())
    } else {
        ToolError::Http("Meta API request failed".to_string())
    }
}

/// Short `message` from a Graph error body
/// (`{"error": {"message": …, "code": …}}`).
fn api_message(body: &Value) -> (String, Option<i64>) {
    let err = body.get("error");
    let msg = err
        .and_then(|e| e.get("message"))
        .and_then(Value::as_str)
        .map(|s| s.trim().chars().take(300).collect::<String>())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "no detail from Meta".to_string());
    let code = err.and_then(|e| e.get("code")).and_then(Value::as_i64);
    (msg, code)
}

/// Map a non-2xx Graph response to a `ToolError`. Graph error code 190 means
/// the token is invalid/expired. Nothing here ever contains the token.
fn check_status(resp: &ApiResp) -> Result<(), ToolError> {
    if (200..300).contains(&resp.status) {
        return Ok(());
    }
    let (msg, code) = api_message(&resp.body);
    match (resp.status, code) {
        (_, Some(190)) => Err(ToolError::Other(format!(
            "Meta rejected the access token (Graph error 190): {msg} — the token is invalid or \
             expired; reconnect with a fresh access token"
        ))),
        (400, _) => Err(ToolError::Other(format!("Meta refused the request (400): {msg}"))),
        (403, _) => Err(ToolError::Other(format!(
            "Meta refused the request (403): {msg} — the token may lack the ads_read permission"
        ))),
        (404, _) => Err(ToolError::NotFound(format!(
            "Meta resource not found (404): {msg}"
        ))),
        (429, _) => Err(ToolError::Other(
            "Meta rate limit exceeded (429): back off and retry shortly".to_string(),
        )),
        (s, _) => Err(ToolError::Http(format!("Meta returned HTTP {s}: {msg}"))),
    }
}

/// Load the access token or fail honestly when the connector was never connected.
fn require_token() -> Result<ConnectorToken, ToolError> {
    load_token(CONNECTOR_ID)
        .filter(|t| t.is_present())
        .ok_or_else(|| ToolError::Other(NOT_CONNECTED_MSG.to_string()))
}

/// Verify a candidate token with `GET /me?fields=id,name`. Returns
/// `(id, name)`. Error messages never contain the token.
async fn verify_token(api_base: &str, token: &str) -> Result<(String, String), String> {
    let resp = graph_get(api_base, token, "/me", &[("fields", "id,name".to_string())])
        .await
        .map_err(|e| match e {
            ToolError::Timeout(_) => "connection to the Meta Graph API timed out".to_string(),
            _ => "could not reach the Meta Graph API".to_string(),
        })?;
    match resp.status {
        200 => {
            let id = resp
                .body
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let name = resp
                .body
                .get("name")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            Ok((id, name))
        }
        _ => {
            let (msg, _) = api_message(&resp.body);
            Err(format!(
                "Meta token verification failed (HTTP {}): {msg} — the token was NOT saved",
                resp.status
            ))
        }
    }
}

/// Validate a Meta ad account id: digits only (prevents path injection).
/// Accepts `act_123` or `123` and returns the bare digits.
fn validate_account_id(raw: &str) -> Result<String, ToolError> {
    let v = raw.trim().strip_prefix("act_").unwrap_or(raw.trim());
    if !v.is_empty() && v.chars().all(|c| c.is_ascii_digit()) {
        Ok(v.to_string())
    } else {
        Err(ToolError::InvalidArgs(format!(
            "invalid account_id '{raw}': expected digits, e.g. act_123456789"
        )))
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

/// `{id, name, account_status, currency, timezone}`.
fn summarize_account(v: &Value) -> Value {
    serde_json::json!({
        "id": str_field(v, "id"),
        "name": str_field(v, "name"),
        "account_status": v.get("account_status").and_then(Value::as_i64),
        "currency": str_field(v, "currency"),
        "timezone": str_field(v, "timezone_name"),
    })
}

/// `{id, name, status, objective, effective_status}`.
fn summarize_campaign(v: &Value) -> Value {
    serde_json::json!({
        "id": str_field(v, "id"),
        "name": str_field(v, "name"),
        "status": str_field(v, "status"),
        "objective": str_field(v, "objective"),
        "effective_status": str_field(v, "effective_status"),
    })
}

/// Extract `data[]` from a Graph list response and summarize each item.
fn summarize_list(body: &Value, f: impl Fn(&Value) -> Value) -> Vec<Value> {
    body.get("data")
        .and_then(Value::as_array)
        .map(|a| a.iter().map(f).collect())
        .unwrap_or_default()
}

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

/// `meta_ads_accounts` — `GET /me/adaccounts`.
pub struct AccountsTool {
    api_base: String,
}

impl AccountsTool {
    pub fn new(api_base: String) -> Self {
        Self { api_base }
    }
}

#[async_trait]
impl Tool for AccountsTool {
    fn name(&self) -> &str {
        "meta_ads_accounts"
    }
    fn description(&self) -> &str {
        "List Meta ad accounts visible to the token (id, name, status, \
         currency, timezone)."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[("limit", "integer", "Results per page, max 100 (default 25)")],
            &[],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let tok = require_token()?;
        let limit = opt_u64(&args, "limit", 25).clamp(1, 100);
        let resp = graph_get(
            &self.api_base,
            &tok.access_token,
            "/me/adaccounts",
            &[
                (
                    "fields",
                    "id,name,account_status,currency,timezone_name".to_string(),
                ),
                ("limit", limit.to_string()),
            ],
        )
        .await?;
        check_status(&resp)?;
        let accounts = summarize_list(&resp.body, summarize_account);
        Ok(serde_json::json!({ "accounts": accounts, "count": accounts.len() }))
    }
}

/// `meta_ads_campaigns` — `GET /act_{id}/campaigns`.
pub struct CampaignsTool {
    api_base: String,
}

impl CampaignsTool {
    pub fn new(api_base: String) -> Self {
        Self { api_base }
    }
}

#[async_trait]
impl Tool for CampaignsTool {
    fn name(&self) -> &str {
        "meta_ads_campaigns"
    }
    fn description(&self) -> &str {
        "List campaigns of a Meta ad account (id, name, status, objective, \
         effective status)."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                (
                    "account_id",
                    "string",
                    "Ad account id, e.g. act_123456789",
                ),
                ("limit", "integer", "Results per page, max 100 (default 25)"),
                (
                    "effective_status",
                    "string",
                    "Filter, e.g. ACTIVE, PAUSED, ARCHIVED (optional)",
                ),
            ],
            &["account_id"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let tok = require_token()?;
        let account_id = validate_account_id(&req_str(&args, "account_id")?)?;
        let limit = opt_u64(&args, "limit", 25).clamp(1, 100);
        let mut query = vec![
            (
                "fields",
                "id,name,status,objective,effective_status".to_string(),
            ),
            ("limit", limit.to_string()),
        ];
        let eff = opt_str(&args, "effective_status", "").trim().to_uppercase();
        if !eff.is_empty() {
            query.push(("effective_status", format!("[\"{eff}\"]")));
        }
        let resp = graph_get(
            &self.api_base,
            &tok.access_token,
            &format!("/act_{account_id}/campaigns"),
            &query,
        )
        .await?;
        check_status(&resp)?;
        let campaigns = summarize_list(&resp.body, summarize_campaign);
        Ok(serde_json::json!({
            "account_id": format!("act_{account_id}"),
            "campaigns": campaigns,
            "count": campaigns.len(),
        }))
    }
}

/// `meta_ads_insights` — `GET /act_{id}/insights`.
pub struct InsightsTool {
    api_base: String,
}

impl InsightsTool {
    pub fn new(api_base: String) -> Self {
        Self { api_base }
    }
}

#[async_trait]
impl Tool for InsightsTool {
    fn name(&self) -> &str {
        "meta_ads_insights"
    }
    fn description(&self) -> &str {
        "Ad performance insights for a Meta ad account (spend, impressions, \
         clicks, CTR, CPC) over a date preset or explicit range."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                (
                    "account_id",
                    "string",
                    "Ad account id, e.g. act_123456789",
                ),
                (
                    "date_preset",
                    "string",
                    "Preset range: today, yesterday, last_7d, last_30d, last_90d (default last_30d)",
                ),
                (
                    "since",
                    "string",
                    "Range start YYYY-MM-DD (overrides date_preset when both given)",
                ),
                (
                    "until",
                    "string",
                    "Range end YYYY-MM-DD (overrides date_preset when both given)",
                ),
            ],
            &["account_id"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let tok = require_token()?;
        let account_id = validate_account_id(&req_str(&args, "account_id")?)?;
        let mut query = vec![(
            "fields",
            "spend,impressions,clicks,ctr,cpc,reach".to_string(),
        )];
        let since = opt_str(&args, "since", "").trim();
        let until = opt_str(&args, "until", "").trim();
        if !since.is_empty() || !until.is_empty() {
            for (v, name) in [(since, "since"), (until, "until")] {
                if !v.is_empty() && chrono::NaiveDate::parse_from_str(v, "%Y-%m-%d").is_err() {
                    return Err(ToolError::InvalidArgs(format!(
                        "invalid {name} '{v}': expected YYYY-MM-DD"
                    )));
                }
            }
            if since.is_empty() || until.is_empty() {
                return Err(ToolError::InvalidArgs(
                    "since and until must be given together".to_string(),
                ));
            }
            query.push((
                "time_range",
                format!("{{\"since\":\"{since}\",\"until\":\"{until}\"}}"),
            ));
        } else {
            let preset = opt_str(&args, "date_preset", "last_30d")
                .trim()
                .to_lowercase();
            let allowed = ["today", "yesterday", "last_7d", "last_30d", "last_90d"];
            if !allowed.contains(&preset.as_str()) {
                return Err(ToolError::InvalidArgs(format!(
                    "invalid date_preset '{preset}': expected one of {}",
                    allowed.join(", ")
                )));
            }
            query.push(("date_preset", preset));
        }
        let resp = graph_get(
            &self.api_base,
            &tok.access_token,
            &format!("/act_{account_id}/insights"),
            &query,
        )
        .await?;
        check_status(&resp)?;
        let rows: Vec<Value> = resp
            .body
            .get("data")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        Ok(serde_json::json!({
            "account_id": format!("act_{account_id}"),
            "insights": rows,
            "count": rows.len(),
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
    }

    impl TestHome {
        async fn new() -> Self {
            let lock = crate::connectors::TEST_ENV_LOCK.lock().await;
            let dir = tempfile::tempdir().unwrap();
            let old = std::env::var("BOOM_HOME").ok();
            std::env::set_var("BOOM_HOME", dir.path());
            Self {
                _lock: lock,
                _dir: dir,
                old,
            }
        }
    }

    impl Drop for TestHome {
        fn drop(&mut self) {
            match self.old.take() {
                Some(v) => std::env::set_var("BOOM_HOME", v),
                None => std::env::remove_var("BOOM_HOME"),
            }
        }
    }

    /// Raw-TCP mock: canned JSON body + status, captures raw requests.
    async fn mock_server(
        status: u16,
        body: &'static str,
        count: usize,
    ) -> (String, tokio::task::JoinHandle<Vec<String>>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind mock server");
        let addr = listener.local_addr().expect("local addr");
        let handle = tokio::spawn(async move {
            use tokio::io::{AsyncReadExt, AsyncWriteExt};
            let mut requests = Vec::new();
            for _ in 0..count {
                let (mut sock, _) = listener.accept().await.expect("accept");
                let mut req: Vec<u8> = Vec::new();
                let mut buf = vec![0u8; 8192];
                loop {
                    let n = sock.read(&mut buf).await.expect("read");
                    if n == 0 {
                        break;
                    }
                    req.extend_from_slice(&buf[..n]);
                    if req.windows(4).any(|w| w == b"\r\n\r\n") {
                        break;
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
        save_token(CONNECTOR_ID, &ConnectorToken::api_key("EAATEST")).unwrap();
        home
    }

    const ME_FIXTURE: &str = r#"{"id":"12345","name":"Son Nguyen"}"#;

    const ACCOUNTS_FIXTURE: &str = r#"{"data":[
      {"id":"act_111","name":"Shop - VN","account_status":1,"currency":"VND","timezone_name":"Asia/Ho_Chi_Minh"}
    ]}"#;

    const CAMPAIGNS_FIXTURE: &str = r#"{"data":[
      {"id":"222","name":"Tet Sale","status":"ACTIVE","objective":"SALES","effective_status":"ACTIVE"}
    ]}"#;

    const INSIGHTS_FIXTURE: &str = r#"{"data":[
      {"spend":"1500000","impressions":"120000","clicks":"2400","ctr":"2.0","cpc":"625","reach":"90000",
       "date_start":"2026-09-17","date_stop":"2026-10-16"}
    ]}"#;

    #[test]
    fn connector_identity() {
        let c = MetaAdsConnector::new();
        assert_eq!(c.id(), "meta_ads");
        assert_eq!(c.name(), "Meta Ads");
        assert_eq!(c.auth_kind(), "api_key");
        assert!(!c.description().is_empty());
    }

    #[test]
    fn connect_url_gives_token_instructions() {
        let c = MetaAdsConnector::new();
        let err = c.connect_url().unwrap_err();
        assert!(err.contains("access token"), "got: {err}");
        assert!(err.contains("ads_read"), "got: {err}");
        assert!(err.contains("/api/connectors/meta_ads/token"), "got: {err}");
    }

    #[test]
    fn schemas_are_valid() {
        let mut reg = ToolRegistry::default();
        MetaAdsConnector::new().register_tools(&mut reg);
        for name in ["meta_ads_accounts", "meta_ads_campaigns", "meta_ads_insights"] {
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
        assert_eq!(reg.tools_in_toolset("meta_ads").len(), 3);
    }

    #[tokio::test]
    async fn token_roundtrip_and_0600() {
        let _home = TestHome::new().await;
        assert!(!MetaAdsConnector::new().is_connected());
        save_token(CONNECTOR_ID, &ConnectorToken::api_key("EAATEST")).unwrap();
        assert!(MetaAdsConnector::new().is_connected());
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
        delete_token(CONNECTOR_ID).unwrap();
        assert!(!MetaAdsConnector::new().is_connected());
        delete_token(CONNECTOR_ID).unwrap(); // idempotent
    }

    #[tokio::test]
    async fn exchange_code_ok_verifies_and_stores() {
        let _home = TestHome::new().await;
        let (base, handle) = mock_server(200, ME_FIXTURE, 1).await;
        let mut c = MetaAdsConnector::with_api_base(&base);
        c.exchange_code("EAATEST").await.unwrap();
        assert!(c.is_connected());
        assert_eq!(load_token(CONNECTOR_ID).unwrap().access_token, "EAATEST");
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("GET /me?"), "request: {raw}");
        assert!(raw.to_lowercase().contains("authorization: bearer "), "auth: {raw}");
        let dbg = format!("{:?}", c);
        assert!(!dbg.contains("EAATEST"), "token leaked: {dbg}");
        assert!(dbg.contains("<redacted>"));
    }

    #[tokio::test]
    async fn exchange_code_rejects_bad_token_without_saving() {
        let _home = TestHome::new().await;
        let (base, _h) = mock_server(
            400,
            r#"{"error":{"message":"Invalid OAuth access token","code":190}}"#,
            1,
        )
        .await;
        let mut c = MetaAdsConnector::with_api_base(&base);
        let err = c.exchange_code("EAABAD").await.unwrap_err();
        assert!(err.contains("NOT saved"), "got: {err}");
        assert!(!err.contains("EAABAD"), "token leaked: {err}");
        assert!(!c.is_connected());
    }

    #[tokio::test]
    async fn exchange_code_rejects_empty() {
        let _home = TestHome::new().await;
        let mut c = MetaAdsConnector::new();
        let err = c.exchange_code("  ").await.unwrap_err();
        assert!(err.contains("empty"), "got: {err}");
    }

    #[tokio::test]
    async fn not_connected_is_honest() {
        let _home = TestHome::new().await;
        let tool = AccountsTool::new("http://127.0.0.1:9".to_string());
        let err = tool.execute(serde_json::json!({})).await.unwrap_err();
        assert!(err.to_string().contains("not connected"), "got: {err}");
    }

    #[tokio::test]
    async fn accounts_lists_and_summarizes() {
        let _home = save_test_token().await;
        let (base, handle) = mock_server(200, ACCOUNTS_FIXTURE, 1).await;
        let tool = AccountsTool::new(base);
        let out = tool.execute(serde_json::json!({})).await.unwrap();
        assert_eq!(out["count"], 1);
        assert_eq!(out["accounts"][0]["id"], "act_111");
        assert_eq!(out["accounts"][0]["currency"], "VND");
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("GET /me/adaccounts?"), "request: {raw}");
    }

    #[tokio::test]
    async fn campaigns_lists_and_validates_account_id() {
        let _home = save_test_token().await;
        let (base, handle) = mock_server(200, CAMPAIGNS_FIXTURE, 1).await;
        let tool = CampaignsTool::new(base);
        let out = tool
            .execute(serde_json::json!({"account_id": "act_111"}))
            .await
            .unwrap();
        assert_eq!(out["account_id"], "act_111");
        assert_eq!(out["count"], 1);
        assert_eq!(out["campaigns"][0]["name"], "Tet Sale");
        assert_eq!(out["campaigns"][0]["objective"], "SALES");
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("GET /act_111/campaigns?"), "request: {raw}");

        // Missing / malformed account ids are rejected before any network.
        let tool2 = CampaignsTool::new("http://127.0.0.1:9".to_string());
        let err = tool2
            .execute(serde_json::json!({}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "got: {err}");
        let err = tool2
            .execute(serde_json::json!({"account_id": "../../x"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "got: {err}");
    }

    #[tokio::test]
    async fn insights_with_preset_and_range() {
        let _home = save_test_token().await;
        let (base, handle) = mock_server(200, INSIGHTS_FIXTURE, 2).await;
        let tool = InsightsTool::new(base);
        let out = tool
            .execute(serde_json::json!({"account_id": "111", "date_preset": "last_7d"}))
            .await
            .unwrap();
        assert_eq!(out["count"], 1);
        assert_eq!(out["insights"][0]["spend"], "1500000");
        let out = tool
            .execute(serde_json::json!({
                "account_id": "act_111",
                "since": "2026-09-01",
                "until": "2026-09-30",
            }))
            .await
            .unwrap();
        assert_eq!(out["count"], 1);
        let raws = handle.await.unwrap();
        assert!(raws[0].contains("date_preset=last_7d"), "request: {}", raws[0]);
        assert!(raws[1].contains("time_range="), "request: {}", raws[1]);

        // Bad preset / bad dates rejected.
        let tool2 = InsightsTool::new("http://127.0.0.1:9".to_string());
        let err = tool2
            .execute(serde_json::json!({"account_id": "111", "date_preset": "forever"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "got: {err}");
        let err = tool2
            .execute(serde_json::json!({"account_id": "111", "since": "2026-13-01", "until": "2026-09-30"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "got: {err}");
        let err = tool2
            .execute(serde_json::json!({"account_id": "111", "since": "2026-09-01"}))
            .await
            .unwrap_err();
        assert!(err.to_string().contains("together"), "got: {err}");
    }

    #[tokio::test]
    async fn graph_error_190_maps_to_reauth_message() {
        let _home = save_test_token().await;
        let (base, _h) = mock_server(
            400,
            r#"{"error":{"message":"Invalid OAuth access token","code":190}}"#,
            1,
        )
        .await;
        let err = AccountsTool::new(base)
            .execute(serde_json::json!({}))
            .await
            .unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("Graph error 190"), "got: {msg}");
        assert!(msg.contains("reconnect"), "got: {msg}");
        assert!(!msg.contains("EAATEST"), "token leaked: {msg}");
    }

    #[test]
    fn summarizers_and_validators_are_pure() {
        let accounts: Value = serde_json::from_str(ACCOUNTS_FIXTURE).unwrap();
        let s = summarize_account(&accounts["data"][0]);
        assert_eq!(s["timezone"], "Asia/Ho_Chi_Minh");

        let campaigns: Value = serde_json::from_str(CAMPAIGNS_FIXTURE).unwrap();
        let s = summarize_campaign(&campaigns["data"][0]);
        assert_eq!(s["effective_status"], "ACTIVE");

        assert_eq!(validate_account_id("act_123").unwrap(), "123");
        assert_eq!(validate_account_id("123").unwrap(), "123");
        assert!(validate_account_id("").is_err());
        assert!(validate_account_id("../../x").is_err());
        assert!(validate_account_id("act_12a").is_err());
    }
}
