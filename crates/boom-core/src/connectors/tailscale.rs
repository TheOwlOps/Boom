//! Tailscale connector — list devices in a tailnet and summarize tailnet status.
//!
//! Auth is a Tailscale API access token (from https://login.tailscale.com/admin/settings/keys,
//! "Generate OAuth client" is NOT needed — a plain API access token under "API access tokens"):
//! [`TailscaleConnector::exchange_code`] treats its `code` argument as the token, verifies it
//! with `GET /api/v2/tailnets`, and stores it in `$BOOM_HOME/connectors/tailscale.json`
//! (mode 0600) as `access_token`, plus the default tailnet name in non-secret
//! `extra.tailnet`. Every tool call loads the token fresh from that file, so the connector
//! struct itself holds no secret — `Debug` output and error messages can never leak it.
//!
//! ## Merge note for the coordinator
//!
//! Drop this file at `crates/boom-core/src/connectors/tailscale.rs`, declare
//! `pub mod tailscale;` in `src/connectors/mod.rs`, and register
//! `TailscaleConnector::new()` in `ConnectorRegistry::new()` (also extend
//! `ALL_CONNECTOR_IDS`). It uses only the shared contract from the parent
//! module (`Connector`, `ConnectorToken`, `load_token`, `save_token`,
//! `save_extra`, `load_extra`, `delete_token`, `http_client`).

use std::collections::BTreeMap;
use std::fmt;
use std::time::Duration;

use async_trait::async_trait;
use serde_json::Value;

use super::{
    delete_token, http_client, load_extra, load_token, save_extra, save_token, Connector,
    ConnectorToken,
};
use crate::tools::{opt_str, schema_of, Tool, ToolError, ToolRegistry};

/// Stable connector id (also the token file name: `tailscale.json`).
const CONNECTOR_ID: &str = "tailscale";
const API_BASE: &str = "https://api.tailscale.com/api/v2";
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);

const CONNECT_INSTRUCTIONS: &str = "Tailscale uses an API access token — POST it to /api/connectors/tailscale/token.\n\
Create one at https://login.tailscale.com/admin/settings/keys : scroll to \"API access tokens\" → \
Generate access token (it starts with tskey-api-…). The token is verified with GET /api/v2/tailnets \
and stored in $BOOM_HOME/connectors/tailscale.json (mode 0600).";

const NOT_CONNECTED_MSG: &str = "tailscale is not connected: no API token is stored. POST a \
Tailscale API access token to /api/connectors/tailscale/token first (see the connector's \
connect instructions).";

/// Tailscale connector. Holds no secret: the token is loaded from the token
/// file on every call, so `exchange_code`/`disconnect` take effect immediately.
pub struct TailscaleConnector {
    api_base: String,
}

impl TailscaleConnector {
    /// Production constructor: real Tailscale API, token from `$BOOM_HOME`.
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

impl Default for TailscaleConnector {
    fn default() -> Self {
        Self::new()
    }
}

/// Never print the token in logs or Debug output — the struct holds no token
/// at all, and this impl makes that a compile-time guarantee.
impl fmt::Debug for TailscaleConnector {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("TailscaleConnector")
            .field("api_base", &self.api_base)
            .field("connected", &self.is_connected())
            .field("api_token", &"<redacted>")
            .finish()
    }
}

#[async_trait]
impl Connector for TailscaleConnector {
    fn id(&self) -> &'static str {
        CONNECTOR_ID
    }
    fn name(&self) -> &'static str {
        "Tailscale"
    }
    fn description(&self) -> &'static str {
        "Tailscale API: list devices in your tailnet and summarize tailnet \
         status (OS mix, users, exit nodes). Authenticates with a Tailscale \
         API access token."
    }
    fn auth_kind(&self) -> &'static str {
        "api_key"
    }
    fn connect_url(&self) -> Result<String, String> {
        // No OAuth: the "code" step consumes the raw API token instead.
        Err(CONNECT_INSTRUCTIONS.to_string())
    }
    async fn exchange_code(&mut self, code: &str) -> Result<(), String> {
        let token = code.trim();
        if token.is_empty() {
            return Err("empty Tailscale API token".to_string());
        }
        let default_tailnet = verify_token(self.api_base(), token).await?;
        save_token(CONNECTOR_ID, &ConnectorToken::api_key(token))?;
        // Tailnet names are not secret (they appear in URLs and DNS).
        let _ = save_extra(CONNECTOR_ID, "tailnet", &default_tailnet);
        Ok(())
    }
    fn disconnect(&mut self) -> Result<(), String> {
        delete_token(CONNECTOR_ID)
    }
    fn register_tools(&self, reg: &mut ToolRegistry) {
        reg.register("tailscale", DevicesTool::new(self.api_base.clone()));
        reg.register("tailscale", StatusTool::new(self.api_base.clone()));
    }
}

// ---------------------------------------------------------------------------
// HTTP transport
// ---------------------------------------------------------------------------

/// One completed Tailscale API round-trip.
struct ApiResp {
    status: u16,
    body: Value,
}

/// GET against the Tailscale API with `Bearer` auth. The token travels only
/// in the Authorization header and is never logged.
async fn ts_get(api_base: &str, token: &str, path: &str) -> Result<ApiResp, ToolError> {
    let url = format!("{}{}", api_base, path);
    let resp = http_client(REQUEST_TIMEOUT)
        .get(&url)
        .bearer_auth(token)
        .send()
        .await
        .map_err(transport_error)?;
    let status = resp.status().as_u16();
    let text = resp
        .text()
        .await
        .map_err(|_| ToolError::Http("failed to read Tailscale response body".to_string()))?;
    let body = serde_json::from_str::<Value>(text.trim()).unwrap_or(Value::Null);
    Ok(ApiResp { status, body })
}

/// Generic transport failure message. Deliberately generic: reqwest errors
/// can echo request details, and we never want the token in an error.
fn transport_error(err: reqwest::Error) -> ToolError {
    if err.is_timeout() {
        ToolError::Timeout(REQUEST_TIMEOUT.as_secs())
    } else if err.is_connect() {
        ToolError::Http("could not reach the Tailscale API (connection failed)".to_string())
    } else {
        ToolError::Http("Tailscale request failed".to_string())
    }
}

/// Short `message` from a Tailscale error body (`{"message": …}` or
/// `{"error": …}`).
fn api_message(body: &Value) -> String {
    body.get("message")
        .or_else(|| body.get("error"))
        .and_then(Value::as_str)
        .map(|s| s.trim().chars().take(300).collect::<String>())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "no detail from Tailscale".to_string())
}

/// Map a non-2xx Tailscale response to a `ToolError`. Nothing here ever
/// contains the API token.
fn check_status(resp: &ApiResp) -> Result<(), ToolError> {
    if (200..300).contains(&resp.status) {
        return Ok(());
    }
    let msg = api_message(&resp.body);
    match resp.status {
        401 | 403 => Err(ToolError::Other(format!(
            "Tailscale rejected the API token (HTTP {}): {} — the token is invalid, expired or \
             lacks the required scope; reconnect with a fresh API access token",
            resp.status, msg
        ))),
        404 => Err(ToolError::NotFound(format!(
            "Tailscale resource not found (404): {msg}"
        ))),
        429 => Err(ToolError::Other(
            "Tailscale rate limit exceeded (429): back off and retry shortly".to_string(),
        )),
        s => Err(ToolError::Http(format!("Tailscale returned HTTP {s}: {msg}"))),
    }
}

/// Load the API token or fail honestly when the connector was never connected.
fn require_token() -> Result<ConnectorToken, ToolError> {
    load_token(CONNECTOR_ID)
        .filter(|t| t.is_present())
        .ok_or_else(|| ToolError::Other(NOT_CONNECTED_MSG.to_string()))
}

/// A tailnet name is a DNS-ish label: letters, digits, dots, dashes and
/// underscores only (prevents path injection into the request URL).
fn validate_tailnet(raw: &str) -> Result<String, ToolError> {
    let v = raw.trim();
    if v.is_empty() {
        return Err(ToolError::InvalidArgs(
            "tailnet must not be empty".to_string(),
        ));
    }
    if v.len() <= 253
        && v.chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '.' || c == '-' || c == '_')
    {
        Ok(v.to_string())
    } else {
        Err(ToolError::InvalidArgs(format!(
            "invalid tailnet '{raw}': only letters, digits, '.', '-' and '_' are allowed"
        )))
    }
}

/// Resolve the tailnet: explicit arg wins, otherwise the stored default from
/// connect time.
fn resolve_tailnet(arg: &str) -> Result<String, ToolError> {
    let v = arg.trim();
    if !v.is_empty() {
        return validate_tailnet(v);
    }
    load_extra(CONNECTOR_ID, "tailnet")
        .filter(|s| !s.trim().is_empty())
        .map(|s| validate_tailnet(&s))
        .unwrap_or_else(|| {
            Err(ToolError::Other(
                "no tailnet given and none was stored at connect time — pass the 'tailnet' \
                 argument (e.g. your tailnet's domain name) or reconnect"
                    .to_string(),
            ))
        })
}

/// Verify a candidate token with `GET /api/v2/tailnets`. Returns the first
/// tailnet name as the default. Error messages never contain the token.
async fn verify_token(api_base: &str, token: &str) -> Result<String, String> {
    let resp = ts_get(api_base, token, "/tailnets")
        .await
        .map_err(|e| match e {
            ToolError::Timeout(_) => "connection to the Tailscale API timed out".to_string(),
            _ => "could not reach the Tailscale API".to_string(),
        })?;
    match resp.status {
        200 => resp
            .body
            .get("tailnets")
            .and_then(Value::as_array)
            .and_then(|a| a.first())
            .and_then(|t| t.get("name").or_else(|| t.get("displayName")))
            .and_then(Value::as_str)
            .map(str::to_string)
            .ok_or_else(|| "Tailscale /tailnets response did not contain a tailnet".to_string()),
        401 | 403 => Err(format!(
            "Tailscale rejected the API token (HTTP {}): it is invalid or expired — the token \
             was NOT saved",
            resp.status
        )),
        s => Err(format!(
            "Tailscale token verification failed (HTTP {s}): the token was NOT saved"
        )),
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

/// `{hostname, name, addresses[], os, user, last_seen, is_exit_node, key_expiry_disabled?}`.
fn summarize_device(v: &Value) -> Value {
    let addresses: Vec<String> = v
        .get("addresses")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    serde_json::json!({
        "hostname": str_field(v, "hostname"),
        "name": str_field(v, "name"),
        "addresses": addresses,
        "os": str_field(v, "os"),
        "user": v.get("user").and_then(|u| u.get("displayName")).and_then(Value::as_str).unwrap_or(""),
        "last_seen": str_field(v, "lastSeen"),
        "is_exit_node": v.get("isExitNode").and_then(Value::as_bool).unwrap_or(false),
        "key_expiry_disabled": v.get("keyExpiryDisabled").and_then(Value::as_bool).unwrap_or(false),
    })
}

/// Aggregate a device list into a status summary.
fn summarize_status(tailnet: &str, devices: &[Value]) -> Value {
    let mut by_os: BTreeMap<String, u64> = BTreeMap::new();
    let mut users: BTreeMap<String, u64> = BTreeMap::new();
    let mut exit_nodes = 0u64;
    let mut key_expiry_disabled = 0u64;
    for d in devices {
        let os = str_field(d, "os");
        *by_os.entry(if os.is_empty() { "unknown".into() } else { os }).or_insert(0) += 1;
        let user = d
            .get("user")
            .and_then(|u| u.get("displayName"))
            .and_then(Value::as_str)
            .unwrap_or("unknown");
        *users.entry(user.to_string()).or_insert(0) += 1;
        if d.get("isExitNode").and_then(Value::as_bool).unwrap_or(false) {
            exit_nodes += 1;
        }
        if d.get("keyExpiryDisabled").and_then(Value::as_bool).unwrap_or(false) {
            key_expiry_disabled += 1;
        }
    }
    serde_json::json!({
        "tailnet": tailnet,
        "total_devices": devices.len(),
        "by_os": by_os,
        "users": users,
        "exit_nodes": exit_nodes,
        "key_expiry_disabled": key_expiry_disabled,
    })
}

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

/// `tailscale_devices` — `GET /api/v2/tailnet/{tailnet}/devices`.
pub struct DevicesTool {
    api_base: String,
}

impl DevicesTool {
    pub fn new(api_base: String) -> Self {
        Self { api_base }
    }
}

#[async_trait]
impl Tool for DevicesTool {
    fn name(&self) -> &str {
        "tailscale_devices"
    }
    fn description(&self) -> &str {
        "List devices in a Tailscale tailnet (hostname, addresses, OS, user, \
         last seen, exit-node flag)."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[(
                "tailnet",
                "string",
                "Tailnet name (defaults to the tailnet stored at connect time)",
            )],
            &[],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let tok = require_token()?;
        let tailnet = resolve_tailnet(opt_str(&args, "tailnet", ""))?;
        let resp = ts_get(
            &self.api_base,
            &tok.access_token,
            &format!("/tailnet/{tailnet}/devices?fields=all"),
        )
        .await?;
        check_status(&resp)?;
        let devices: Vec<Value> = resp
            .body
            .get("devices")
            .and_then(Value::as_array)
            .map(|a| a.iter().map(summarize_device).collect())
            .unwrap_or_default();
        Ok(serde_json::json!({
            "tailnet": tailnet,
            "devices": devices,
            "count": devices.len(),
        }))
    }
}

/// `tailscale_status` — same device list, aggregated into a status summary.
pub struct StatusTool {
    api_base: String,
}

impl StatusTool {
    pub fn new(api_base: String) -> Self {
        Self { api_base }
    }
}

#[async_trait]
impl Tool for StatusTool {
    fn name(&self) -> &str {
        "tailscale_status"
    }
    fn description(&self) -> &str {
        "Summarize a Tailscale tailnet: device count, OS mix, users, exit \
         nodes, key-expiry-disabled count."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[(
                "tailnet",
                "string",
                "Tailnet name (defaults to the tailnet stored at connect time)",
            )],
            &[],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let tok = require_token()?;
        let tailnet = resolve_tailnet(opt_str(&args, "tailnet", ""))?;
        let resp = ts_get(
            &self.api_base,
            &tok.access_token,
            &format!("/tailnet/{tailnet}/devices?fields=default"),
        )
        .await?;
        check_status(&resp)?;
        let devices: Vec<Value> = resp
            .body
            .get("devices")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        Ok(summarize_status(&tailnet, &devices))
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

    async fn save_test_token(token: &str) -> TestHome {
        let home = TestHome::new().await;
        save_token(CONNECTOR_ID, &ConnectorToken::api_key(token)).unwrap();
        home
    }

    const TAILNETS_FIXTURE: &str =
        r#"{"tailnets":[{"name":"example.ts.net","displayName":"example.ts.net"}]}"#;

    const DEVICES_FIXTURE: &str = r#"{"devices":[
      {"hostname":"laptop","name":"laptop.example.ts.net","addresses":["100.64.0.1"],
       "os":"linux","user":{"displayName":"son@example.com"},"lastSeen":"2026-10-17T05:00:00Z",
       "isExitNode":false,"keyExpiryDisabled":true},
      {"hostname":"phone","name":"phone.example.ts.net","addresses":["100.64.0.2","fd7a::2"],
       "os":"android","user":{"displayName":"son@example.com"},"lastSeen":"2026-10-17T05:01:00Z",
       "isExitNode":true,"keyExpiryDisabled":false}
    ]}"#;

    #[test]
    fn connector_identity() {
        let c = TailscaleConnector::new();
        assert_eq!(c.id(), "tailscale");
        assert_eq!(c.name(), "Tailscale");
        assert_eq!(c.auth_kind(), "api_key");
        assert!(!c.description().is_empty());
    }

    #[test]
    fn connect_url_gives_token_instructions() {
        let c = TailscaleConnector::new();
        let err = c.connect_url().unwrap_err();
        assert!(err.contains("API access token"), "got: {err}");
        assert!(err.contains("login.tailscale.com"), "got: {err}");
        assert!(err.contains("/api/connectors/tailscale/token"), "got: {err}");
    }

    #[test]
    fn schemas_are_valid() {
        let mut reg = ToolRegistry::default();
        TailscaleConnector::new().register_tools(&mut reg);
        for name in ["tailscale_devices", "tailscale_status"] {
            let tool = reg.get(name).unwrap_or_else(|| panic!("tool '{name}' missing"));
            assert!(!tool.description().is_empty());
            let schema = tool.schema();
            let obj = schema.as_object().expect("schema is object");
            assert_eq!(obj.get("type").and_then(|t| t.as_str()), Some("object"));
            let props = obj
                .get("properties")
                .and_then(|p| p.as_object())
                .expect("schema has properties");
            assert!(props.contains_key("tailnet"));
        }
        assert_eq!(reg.tools_in_toolset("tailscale").len(), 2);
    }

    #[tokio::test]
    async fn token_roundtrip_and_0600() {
        let _home = TestHome::new().await;
        assert!(!TailscaleConnector::new().is_connected());
        save_token(CONNECTOR_ID, &ConnectorToken::api_key("tskey-api-123")).unwrap();
        assert!(TailscaleConnector::new().is_connected());
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
        assert!(!TailscaleConnector::new().is_connected());
        delete_token(CONNECTOR_ID).unwrap(); // idempotent
    }

    #[tokio::test]
    async fn exchange_code_ok_verifies_and_stores() {
        let _home = TestHome::new().await;
        let (base, handle) = mock_server(200, TAILNETS_FIXTURE, 1).await;
        let mut c = TailscaleConnector::with_api_base(&format!("{base}/api/v2"));
        c.exchange_code("tskey-api-ABC").await.unwrap();
        assert!(c.is_connected());
        assert_eq!(
            load_token(CONNECTOR_ID).unwrap().access_token,
            "tskey-api-ABC"
        );
        assert_eq!(
            load_extra(CONNECTOR_ID, "tailnet").as_deref(),
            Some("example.ts.net")
        );
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("GET /api/v2/tailnets "), "request: {raw}");
        assert!(raw.to_lowercase().contains("authorization: bearer "), "auth: {raw}");
        let dbg = format!("{:?}", c);
        assert!(!dbg.contains("tskey-api-ABC"), "token leaked: {dbg}");
        assert!(dbg.contains("<redacted>"));
    }

    #[tokio::test]
    async fn exchange_code_rejects_bad_token_without_saving() {
        let _home = TestHome::new().await;
        let (base, _h) = mock_server(401, r#"{"message":"invalid token"}"#, 1).await;
        let mut c = TailscaleConnector::with_api_base(&format!("{base}/api/v2"));
        let err = c.exchange_code("tskey-api-WRONG").await.unwrap_err();
        assert!(err.contains("401"), "got: {err}");
        assert!(err.contains("NOT saved"), "got: {err}");
        assert!(!err.contains("tskey-api-WRONG"), "token leaked: {err}");
        assert!(!c.is_connected());
    }

    #[tokio::test]
    async fn exchange_code_rejects_empty() {
        let _home = TestHome::new().await;
        let mut c = TailscaleConnector::new();
        let err = c.exchange_code("  ").await.unwrap_err();
        assert!(err.contains("empty"), "got: {err}");
    }

    #[tokio::test]
    async fn not_connected_is_honest() {
        let _home = TestHome::new().await;
        let tool = DevicesTool::new("http://127.0.0.1:9".to_string());
        let err = tool.execute(serde_json::json!({})).await.unwrap_err();
        assert!(err.to_string().contains("not connected"), "got: {err}");
    }

    #[tokio::test]
    async fn devices_lists_using_stored_tailnet() {
        let _home = save_test_token("tskey-api-T").await;
        save_extra(CONNECTOR_ID, "tailnet", "example.ts.net").unwrap();
        let (base, handle) = mock_server(200, DEVICES_FIXTURE, 1).await;
        let tool = DevicesTool::new(format!("{base}/api/v2"));
        let out = tool.execute(serde_json::json!({})).await.unwrap();
        assert_eq!(out["tailnet"], "example.ts.net");
        assert_eq!(out["count"], 2);
        assert_eq!(out["devices"][0]["hostname"], "laptop");
        assert_eq!(out["devices"][0]["addresses"], serde_json::json!(["100.64.0.1"]));
        assert_eq!(out["devices"][1]["os"], "android");
        assert!(out["devices"][1]["is_exit_node"].as_bool().unwrap());
        let raw = &handle.await.unwrap()[0];
        assert!(
            raw.contains("GET /api/v2/tailnet/example.ts.net/devices?fields=all "),
            "request: {raw}"
        );
    }

    #[tokio::test]
    async fn devices_rejects_bad_tailnet() {
        let _home = save_test_token("tskey-api-T").await;
        let tool = DevicesTool::new("http://127.0.0.1:9".to_string());
        let err = tool
            .execute(serde_json::json!({"tailnet": "../evil"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "got: {err}");
    }

    #[tokio::test]
    async fn devices_requires_tailnet_when_none_stored() {
        let _home = save_test_token("tskey-api-T").await;
        let tool = DevicesTool::new("http://127.0.0.1:9".to_string());
        let err = tool.execute(serde_json::json!({})).await.unwrap_err();
        assert!(err.to_string().contains("no tailnet"), "got: {err}");
    }

    #[tokio::test]
    async fn status_aggregates() {
        let _home = save_test_token("tskey-api-T").await;
        save_extra(CONNECTOR_ID, "tailnet", "example.ts.net").unwrap();
        let (base, handle) = mock_server(200, DEVICES_FIXTURE, 1).await;
        let tool = StatusTool::new(format!("{base}/api/v2"));
        let out = tool.execute(serde_json::json!({})).await.unwrap();
        assert_eq!(out["total_devices"], 2);
        assert_eq!(out["by_os"]["linux"], 1);
        assert_eq!(out["by_os"]["android"], 1);
        assert_eq!(out["exit_nodes"], 1);
        assert_eq!(out["key_expiry_disabled"], 1);
        assert_eq!(out["users"]["son@example.com"], 2);
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("fields=default"), "request: {raw}");
    }

    #[tokio::test]
    async fn auth_failure_is_clear_and_redacted() {
        let _home = save_test_token("tskey-api-T").await;
        save_extra(CONNECTOR_ID, "tailnet", "example.ts.net").unwrap();
        let (base, _h) = mock_server(403, r#"{"message":"forbidden"}"#, 1).await;
        let err = DevicesTool::new(format!("{base}/api/v2"))
            .execute(serde_json::json!({}))
            .await
            .unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("rejected the API token"), "got: {msg}");
        assert!(!msg.contains("tskey-api-T"), "token leaked: {msg}");
    }

    #[test]
    fn summarizers_are_pure() {
        let devices: Value = serde_json::from_str(DEVICES_FIXTURE).unwrap();
        let arr = devices["devices"].as_array().unwrap();
        let s = summarize_device(&arr[0]);
        assert_eq!(s["hostname"], "laptop");
        assert_eq!(s["user"], "son@example.com");
        let st = summarize_status("example.ts.net", arr);
        assert_eq!(st["total_devices"], 2);
        assert_eq!(st["by_os"]["linux"], 1);

        assert_eq!(validate_tailnet("example.ts.net").unwrap(), "example.ts.net");
        assert!(validate_tailnet("../x").is_err());
        assert!(validate_tailnet("").is_err());
        assert!(validate_tailnet("a/b").is_err());
    }
}
