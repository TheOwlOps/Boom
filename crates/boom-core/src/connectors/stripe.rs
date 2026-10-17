//! Stripe connector — list customers, payment intents, and invoices.
//!
//! Auth is a Stripe secret API key (`sk_live_…` / `sk_test_…` / restricted
//! keys): [`StripeConnector::exchange_code`] treats its `code` argument as
//! the key, verifies it with `GET /v1/account`, and stores it in
//! `$BOOM_HOME/connectors/stripe.json` (mode 0600) as `access_token`, plus
//! non-secret `extra.account_id` / `extra.livemode`. Every tool call loads
//! the token fresh from that file, so the connector struct itself holds no
//! secret — `Debug` output and error messages can never leak it.
//!
//! ## Merge note for the coordinator
//!
//! Drop this file at `crates/boom-core/src/connectors/stripe.rs`, declare
//! `pub mod stripe;` in `src/connectors/mod.rs`, and register
//! `StripeConnector::new()` in `ConnectorRegistry::new()` (also extend
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
use crate::tools::{opt_str, opt_u64, schema_of, Tool, ToolError, ToolRegistry};

/// Stable connector id (also the token file name: `stripe.json`).
const CONNECTOR_ID: &str = "stripe";
const API_BASE: &str = "https://api.stripe.com/v1";
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);

const CONNECT_INSTRUCTIONS: &str = "Stripe uses a secret API key — POST it to /api/connectors/stripe/token.\n\
Create one at https://dashboard.stripe.com/apikeys : Developers → API keys → Create secret key.\n\
Use a test-mode key (sk_test_…) while experimenting. The key is verified with GET /v1/account \
and stored in $BOOM_HOME/connectors/stripe.json (mode 0600).";

const NOT_CONNECTED_MSG: &str = "stripe is not connected: no API key is stored. POST a Stripe \
secret key to /api/connectors/stripe/token first (see the connector's connect instructions).";

/// Stripe connector. Holds no secret: the key is loaded from the token file
/// on every call, so `exchange_code`/`disconnect` take effect immediately.
pub struct StripeConnector {
    api_base: String,
}

impl StripeConnector {
    /// Production constructor: real Stripe API, token from `$BOOM_HOME`.
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

impl Default for StripeConnector {
    fn default() -> Self {
        Self::new()
    }
}

/// Never print the key in logs or Debug output — the struct holds no key at
/// all, and this impl makes that a compile-time guarantee.
impl fmt::Debug for StripeConnector {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("StripeConnector")
            .field("api_base", &self.api_base)
            .field("connected", &self.is_connected())
            .field("api_key", &"<redacted>")
            .finish()
    }
}

#[async_trait]
impl Connector for StripeConnector {
    fn id(&self) -> &'static str {
        CONNECTOR_ID
    }
    fn name(&self) -> &'static str {
        "Stripe"
    }
    fn description(&self) -> &'static str {
        "Stripe API: list customers, payment intents and invoices. \
         Authenticates with a Stripe secret API key."
    }
    fn auth_kind(&self) -> &'static str {
        "api_key"
    }
    fn connect_url(&self) -> Result<String, String> {
        // No OAuth: the "code" step consumes the raw secret key instead.
        Err(CONNECT_INSTRUCTIONS.to_string())
    }
    async fn exchange_code(&mut self, code: &str) -> Result<(), String> {
        let key = code.trim();
        if key.is_empty() {
            return Err("empty Stripe API key".to_string());
        }
        let (account_id, livemode) = verify_key(self.api_base(), key).await?;
        save_token(CONNECTOR_ID, &ConnectorToken::api_key(key))?;
        let _ = save_extra(CONNECTOR_ID, "account_id", &account_id);
        let _ = save_extra(CONNECTOR_ID, "livemode", if livemode { "true" } else { "false" });
        Ok(())
    }
    fn disconnect(&mut self) -> Result<(), String> {
        delete_token(CONNECTOR_ID)
    }
    fn register_tools(&self, reg: &mut ToolRegistry) {
        reg.register("stripe", CustomersTool::new(self.api_base.clone()));
        reg.register("stripe", PaymentsTool::new(self.api_base.clone()));
        reg.register("stripe", InvoicesTool::new(self.api_base.clone()));
    }
}

// ---------------------------------------------------------------------------
// HTTP transport
// ---------------------------------------------------------------------------

/// One completed Stripe API round-trip.
struct ApiResp {
    status: u16,
    body: Value,
}

/// GET against the Stripe API with `Authorization: Bearer <key>`. The key
/// travels only in the Authorization header and is never logged.
async fn stripe_get(
    api_base: &str,
    key: &str,
    path: &str,
    query: &[(&str, String)],
) -> Result<ApiResp, ToolError> {
    let url = format!("{}{}", api_base, path);
    let resp = http_client(REQUEST_TIMEOUT)
        .get(&url)
        .bearer_auth(key)
        .query(&query)
        .send()
        .await
        .map_err(transport_error)?;
    let status = resp.status().as_u16();
    let text = resp
        .text()
        .await
        .map_err(|_| ToolError::Http("failed to read Stripe response body".to_string()))?;
    let body = serde_json::from_str::<Value>(text.trim()).unwrap_or(Value::Null);
    Ok(ApiResp { status, body })
}

/// Generic transport failure message. Deliberately generic: reqwest errors
/// can echo request details, and we never want the key in an error.
fn transport_error(err: reqwest::Error) -> ToolError {
    if err.is_timeout() {
        ToolError::Timeout(REQUEST_TIMEOUT.as_secs())
    } else if err.is_connect() {
        ToolError::Http("could not reach the Stripe API (connection failed)".to_string())
    } else {
        ToolError::Http("Stripe request failed".to_string())
    }
}

/// Short `message` from a Stripe error body (`{"error": {"message": …}}`).
fn api_message(body: &Value) -> String {
    body.get("error")
        .and_then(|e| e.get("message"))
        .and_then(Value::as_str)
        .map(|s| s.trim().chars().take(300).collect::<String>())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "no detail from Stripe".to_string())
}

/// Map a non-2xx Stripe response to a `ToolError`. Nothing here ever
/// contains the API key.
fn check_status(resp: &ApiResp) -> Result<(), ToolError> {
    if (200..300).contains(&resp.status) {
        return Ok(());
    }
    let msg = api_message(&resp.body);
    match resp.status {
        401 => Err(ToolError::Other(format!(
            "Stripe rejected the API key (401 Unauthorized): {msg} — the key is invalid or \
             revoked; reconnect with a fresh secret key"
        ))),
        429 => Err(ToolError::Other(
            "Stripe rate limit exceeded (429): back off and retry shortly".to_string(),
        )),
        404 => Err(ToolError::NotFound(format!(
            "Stripe resource not found (404): {msg}"
        ))),
        s => Err(ToolError::Http(format!("Stripe returned HTTP {s}: {msg}"))),
    }
}

/// Load the API key or fail honestly when the connector was never connected.
fn require_token() -> Result<ConnectorToken, ToolError> {
    load_token(CONNECTOR_ID)
        .filter(|t| t.is_present())
        .ok_or_else(|| ToolError::Other(NOT_CONNECTED_MSG.to_string()))
}

/// Verify a candidate key with `GET /v1/account`. Returns
/// `(account_id, livemode)` on success. Error messages never contain the key.
async fn verify_key(api_base: &str, key: &str) -> Result<(String, bool), String> {
    let resp = stripe_get(api_base, key, "/account", &[])
        .await
        .map_err(|e| match e {
            ToolError::Timeout(_) => "connection to Stripe timed out".to_string(),
            _ => "could not reach the Stripe API".to_string(),
        })?;
    match resp.status {
        200 => {
            let id = resp
                .body
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let livemode = resp
                .body
                .get("livemode")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            Ok((id, livemode))
        }
        401 => Err("Stripe rejected the API key (401 Unauthorized): it is invalid or revoked — \
                    the key was NOT saved"
            .to_string()),
        s => Err(format!(
            "Stripe key verification failed (HTTP {s}): the key was NOT saved"
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

/// Unix seconds → RFC3339, empty string when missing/invalid.
fn ts_field(v: &Value, key: &str) -> String {
    use chrono::TimeZone;
    v.get(key)
        .and_then(Value::as_i64)
        .and_then(|s| chrono::Utc.timestamp_opt(s, 0).single())
        .map(|dt| dt.to_rfc3339())
        .unwrap_or_default()
}

/// `{id, email, name, created, balance, currency, livemode?}`.
fn summarize_customer(v: &Value) -> Value {
    serde_json::json!({
        "id": str_field(v, "id"),
        "email": v.get("email").and_then(Value::as_str),
        "name": v.get("name").and_then(Value::as_str),
        "created": ts_field(v, "created"),
        "balance": v.get("balance").and_then(Value::as_i64).unwrap_or(0),
        "currency": v.get("currency").and_then(Value::as_str),
    })
}

/// `{id, amount, currency, status, created, receipt_email?}` — amounts are in
/// the currency's smallest unit (e.g. cents).
fn summarize_payment(v: &Value) -> Value {
    serde_json::json!({
        "id": str_field(v, "id"),
        "amount": v.get("amount").and_then(Value::as_i64).unwrap_or(0),
        "currency": str_field(v, "currency"),
        "status": str_field(v, "status"),
        "created": ts_field(v, "created"),
        "receipt_email": v.get("receipt_email").and_then(Value::as_str),
    })
}

/// `{id, number, customer_email, amount_due, currency, status, created, due_date?}`.
fn summarize_invoice(v: &Value) -> Value {
    serde_json::json!({
        "id": str_field(v, "id"),
        "number": v.get("number").and_then(Value::as_str),
        "customer_email": v.get("customer_email").and_then(Value::as_str),
        "amount_due": v.get("amount_due").and_then(Value::as_i64).unwrap_or(0),
        "currency": str_field(v, "currency"),
        "status": str_field(v, "status"),
        "created": ts_field(v, "created"),
    })
}

/// Extract `data[]` from a Stripe list response and summarize each item.
fn summarize_list(body: &Value, f: impl Fn(&Value) -> Value) -> Vec<Value> {
    body.get("data")
        .and_then(Value::as_array)
        .map(|a| a.iter().map(f).collect())
        .unwrap_or_default()
}

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

/// `stripe_customers` — `GET /v1/customers`.
pub struct CustomersTool {
    api_base: String,
}

impl CustomersTool {
    pub fn new(api_base: String) -> Self {
        Self { api_base }
    }
}

#[async_trait]
impl Tool for CustomersTool {
    fn name(&self) -> &str {
        "stripe_customers"
    }
    fn description(&self) -> &str {
        "List Stripe customers (id, email, name, created, balance). Paginated, \
         newest first."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("limit", "integer", "Results per page, max 100 (default 20)"),
                (
                    "email",
                    "string",
                    "Filter by exact customer email (optional)",
                ),
            ],
            &[],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let tok = require_token()?;
        let limit = opt_u64(&args, "limit", 20).clamp(1, 100);
        let mut query = vec![("limit", limit.to_string())];
        let email = opt_str(&args, "email", "").trim();
        if !email.is_empty() {
            query.push(("email", email.to_string()));
        }
        let resp = stripe_get(&self.api_base, &tok.access_token, "/customers", &query).await?;
        check_status(&resp)?;
        let customers = summarize_list(&resp.body, summarize_customer);
        Ok(serde_json::json!({ "customers": customers, "count": customers.len() }))
    }
}

/// `stripe_payments` — `GET /v1/payment_intents`.
pub struct PaymentsTool {
    api_base: String,
}

impl PaymentsTool {
    pub fn new(api_base: String) -> Self {
        Self { api_base }
    }
}

#[async_trait]
impl Tool for PaymentsTool {
    fn name(&self) -> &str {
        "stripe_payments"
    }
    fn description(&self) -> &str {
        "List Stripe payment intents (id, amount, currency, status, created). \
         Amounts are in the currency's smallest unit (e.g. cents)."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("limit", "integer", "Results per page, max 100 (default 20)"),
                (
                    "customer",
                    "string",
                    "Filter by Stripe customer id (optional)",
                ),
            ],
            &[],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let tok = require_token()?;
        let limit = opt_u64(&args, "limit", 20).clamp(1, 100);
        let mut query = vec![("limit", limit.to_string())];
        let customer = opt_str(&args, "customer", "").trim();
        if !customer.is_empty() {
            query.push(("customer", customer.to_string()));
        }
        let resp =
            stripe_get(&self.api_base, &tok.access_token, "/payment_intents", &query).await?;
        check_status(&resp)?;
        let payments = summarize_list(&resp.body, summarize_payment);
        Ok(serde_json::json!({ "payments": payments, "count": payments.len() }))
    }
}

/// `stripe_invoices` — `GET /v1/invoices`.
pub struct InvoicesTool {
    api_base: String,
}

impl InvoicesTool {
    pub fn new(api_base: String) -> Self {
        Self { api_base }
    }
}

#[async_trait]
impl Tool for InvoicesTool {
    fn name(&self) -> &str {
        "stripe_invoices"
    }
    fn description(&self) -> &str {
        "List Stripe invoices (id, number, customer email, amount_due, status, \
         created)."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("limit", "integer", "Results per page, max 100 (default 20)"),
                (
                    "status",
                    "string",
                    "Filter by status: draft, open, paid, uncollectible, void (optional)",
                ),
            ],
            &[],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let tok = require_token()?;
        let limit = opt_u64(&args, "limit", 20).clamp(1, 100);
        let mut query = vec![("limit", limit.to_string())];
        let status = opt_str(&args, "status", "").trim().to_lowercase();
        if !status.is_empty() {
            let allowed = ["draft", "open", "paid", "uncollectible", "void"];
            if !allowed.contains(&status.as_str()) {
                return Err(ToolError::InvalidArgs(format!(
                    "invalid status '{status}': expected one of {}",
                    allowed.join(", ")
                )));
            }
            query.push(("status", status));
        }
        let resp = stripe_get(&self.api_base, &tok.access_token, "/invoices", &query).await?;
        check_status(&resp)?;
        let invoices = summarize_list(&resp.body, summarize_invoice);
        Ok(serde_json::json!({ "invoices": invoices, "count": invoices.len() }))
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
                    404 => "Not Found",
                    429 => "Too Many Requests",
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

    async fn save_test_key(key: &str) -> TestHome {
        let home = TestHome::new().await;
        save_token(CONNECTOR_ID, &ConnectorToken::api_key(key)).unwrap();
        home
    }

    const ACCOUNT_FIXTURE: &str =
        r#"{"id":"acct_123","business_profile":{"name":"Acme"},"livemode":false}"#;

    const CUSTOMERS_FIXTURE: &str = r#"{"object":"list","data":[
      {"id":"cus_1","email":"a@example.com","name":"Alice","created":1760000000,
       "balance":0,"currency":"usd"},
      {"id":"cus_2","email":null,"name":null,"created":1760000100,
       "balance":-500,"currency":"usd"}
    ]}"#;

    const PAYMENTS_FIXTURE: &str = r#"{"object":"list","data":[
      {"id":"pi_1","amount":2000,"currency":"usd","status":"succeeded",
       "created":1760000200,"receipt_email":"a@example.com"}
    ]}"#;

    const INVOICES_FIXTURE: &str = r#"{"object":"list","data":[
      {"id":"in_1","number":"INV-001","customer_email":"a@example.com",
       "amount_due":5000,"currency":"usd","status":"open","created":1760000300}
    ]}"#;

    #[test]
    fn connector_identity() {
        let c = StripeConnector::new();
        assert_eq!(c.id(), "stripe");
        assert_eq!(c.name(), "Stripe");
        assert_eq!(c.auth_kind(), "api_key");
        assert!(!c.description().is_empty());
    }

    #[test]
    fn connect_url_gives_key_instructions() {
        let c = StripeConnector::new();
        let err = c.connect_url().unwrap_err();
        assert!(err.contains("secret API key"), "got: {err}");
        assert!(err.contains("dashboard.stripe.com/apikeys"), "got: {err}");
        assert!(err.contains("/api/connectors/stripe/token"), "got: {err}");
    }

    #[test]
    fn schemas_are_valid() {
        let mut reg = ToolRegistry::default();
        StripeConnector::new().register_tools(&mut reg);
        for name in ["stripe_customers", "stripe_payments", "stripe_invoices"] {
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
        assert_eq!(reg.tools_in_toolset("stripe").len(), 3);
    }

    #[tokio::test]
    async fn token_roundtrip_and_0600() {
        let _home = TestHome::new().await;
        assert!(!StripeConnector::new().is_connected());
        save_token(CONNECTOR_ID, &ConnectorToken::api_key("sk_test_123")).unwrap();
        assert!(StripeConnector::new().is_connected());
        let loaded = load_token(CONNECTOR_ID).unwrap();
        assert_eq!(loaded.access_token, "sk_test_123");
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
        assert!(!StripeConnector::new().is_connected());
        delete_token(CONNECTOR_ID).unwrap(); // idempotent
    }

    #[tokio::test]
    async fn exchange_code_ok_verifies_and_stores() {
        let _home = TestHome::new().await;
        let (base, handle) = mock_server(200, ACCOUNT_FIXTURE, 1).await;
        let mut c = StripeConnector::with_api_base(&format!("{base}/v1"));
        c.exchange_code("sk_test_ABC").await.unwrap();
        assert!(c.is_connected());
        assert_eq!(load_token(CONNECTOR_ID).unwrap().access_token, "sk_test_ABC");
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("GET /v1/account "), "request: {raw}");
        // Bearer header present, key never in the URL.
        assert!(raw.to_lowercase().contains("authorization: bearer "), "auth: {raw}");
        assert!(!raw.split_whitespace().nth(1).unwrap_or("").contains("sk_test_ABC"));
        let dbg = format!("{:?}", c);
        assert!(!dbg.contains("sk_test_ABC"), "key leaked: {dbg}");
        assert!(dbg.contains("<redacted>"));
    }

    #[tokio::test]
    async fn exchange_code_rejects_bad_key_without_saving() {
        let _home = TestHome::new().await;
        let (base, _h) = mock_server(
            401,
            r#"{"error":{"message":"Invalid API Key provided","type":"invalid_request_error"}}"#,
            1,
        )
        .await;
        let mut c = StripeConnector::with_api_base(&format!("{base}/v1"));
        let err = c.exchange_code("sk_test_WRONG").await.unwrap_err();
        assert!(err.contains("401"), "got: {err}");
        assert!(err.contains("NOT saved"), "got: {err}");
        assert!(!err.contains("sk_test_WRONG"), "key leaked: {err}");
        assert!(!c.is_connected());
    }

    #[tokio::test]
    async fn exchange_code_rejects_empty() {
        let _home = TestHome::new().await;
        let mut c = StripeConnector::new();
        let err = c.exchange_code("  ").await.unwrap_err();
        assert!(err.contains("empty"), "got: {err}");
    }

    #[tokio::test]
    async fn not_connected_is_honest() {
        let _home = TestHome::new().await;
        let tool = CustomersTool::new("http://127.0.0.1:9".to_string());
        let err = tool.execute(serde_json::json!({})).await.unwrap_err();
        assert!(err.to_string().contains("not connected"), "got: {err}");
    }

    #[tokio::test]
    async fn customers_lists_and_summarizes() {
        let _home = save_test_key("sk_test_T").await;
        let (base, handle) = mock_server(200, CUSTOMERS_FIXTURE, 1).await;
        let tool = CustomersTool::new(format!("{base}/v1"));
        let out = tool.execute(serde_json::json!({})).await.unwrap();
        assert_eq!(out["count"], 2);
        assert_eq!(out["customers"][0]["id"], "cus_1");
        assert_eq!(out["customers"][0]["email"], "a@example.com");
        assert!(out["customers"][0]["created"].as_str().unwrap().contains("2025"));
        assert!(out["customers"][1]["email"].is_null());
        assert_eq!(out["customers"][1]["balance"], -500);
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("GET /v1/customers?"), "request: {raw}");
        assert!(raw.contains("limit=20"), "request: {raw}");
    }

    #[tokio::test]
    async fn customers_clamps_limit() {
        let _home = save_test_key("sk_test_T").await;
        let (base, handle) = mock_server(200, r#"{"data":[]}"#, 1).await;
        CustomersTool::new(format!("{base}/v1"))
            .execute(serde_json::json!({"limit": 500}))
            .await
            .unwrap();
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("limit=100"), "request: {raw}");
    }

    #[tokio::test]
    async fn payments_lists() {
        let _home = save_test_key("sk_test_T").await;
        let (base, handle) = mock_server(200, PAYMENTS_FIXTURE, 1).await;
        let tool = PaymentsTool::new(format!("{base}/v1"));
        let out = tool
            .execute(serde_json::json!({"customer": "cus_1"}))
            .await
            .unwrap();
        assert_eq!(out["count"], 1);
        assert_eq!(out["payments"][0]["id"], "pi_1");
        assert_eq!(out["payments"][0]["amount"], 2000);
        assert_eq!(out["payments"][0]["status"], "succeeded");
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("customer=cus_1"), "request: {raw}");
    }

    #[tokio::test]
    async fn invoices_lists_and_validates_status() {
        let _home = save_test_key("sk_test_T").await;
        let (base, handle) = mock_server(200, INVOICES_FIXTURE, 1).await;
        let tool = InvoicesTool::new(format!("{base}/v1"));
        let out = tool
            .execute(serde_json::json!({"status": "open"}))
            .await
            .unwrap();
        assert_eq!(out["count"], 1);
        assert_eq!(out["invoices"][0]["number"], "INV-001");
        assert_eq!(out["invoices"][0]["status"], "open");
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("status=open"), "request: {raw}");

        let err = InvoicesTool::new("http://127.0.0.1:9".to_string())
            .execute(serde_json::json!({"status": "bogus"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "got: {err}");
    }

    #[tokio::test]
    async fn auth_failure_maps_to_clear_error_without_key() {
        let _home = save_test_key("sk_test_T").await;
        let (base, _h) = mock_server(
            401,
            r#"{"error":{"message":"Invalid API Key provided"}}"#,
            1,
        )
        .await;
        let err = CustomersTool::new(format!("{base}/v1"))
            .execute(serde_json::json!({}))
            .await
            .unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("rejected the API key"), "got: {msg}");
        assert!(!msg.contains("sk_test_T"), "key leaked: {msg}");
    }

    #[tokio::test]
    async fn rate_limit_maps_cleanly() {
        let _home = save_test_key("sk_test_T").await;
        let (base, _h) = mock_server(429, r#"{"error":{"message":"Too many requests"}}"#, 1).await;
        let err = CustomersTool::new(format!("{base}/v1"))
            .execute(serde_json::json!({}))
            .await
            .unwrap_err();
        assert!(err.to_string().contains("rate limit"), "got: {err}");
    }

    #[test]
    fn summarizers_are_pure() {
        let customers: Value = serde_json::from_str(CUSTOMERS_FIXTURE).unwrap();
        let s = summarize_customer(&customers["data"][0]);
        assert_eq!(s["email"], "a@example.com");
        assert!(s["created"].as_str().unwrap().starts_with("2025-"));

        let payments: Value = serde_json::from_str(PAYMENTS_FIXTURE).unwrap();
        let s = summarize_payment(&payments["data"][0]);
        assert_eq!(s["amount"], 2000);

        let invoices: Value = serde_json::from_str(INVOICES_FIXTURE).unwrap();
        let s = summarize_invoice(&invoices["data"][0]);
        assert_eq!(s["amount_due"], 5000);

        assert_eq!(ts_field(&serde_json::json!({}), "created"), "");
    }
}
