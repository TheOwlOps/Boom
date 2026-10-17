//! Shopify connector — list products, orders, and customers of a store.
//!
//! Auth is a Shopify Admin API access token (`shpat_…`, from a custom app in
//! the store admin) plus the store's myshopify.com domain:
//! [`ShopifyConnector::exchange_code`] expects its `code` argument to be a JSON
//! object `{"store": "…", "access_token": "…"}`, verifies it with
//! `GET /admin/api/2024-10/shop.json`, and stores the bundle in
//! `$BOOM_HOME/connectors/shopify.json` (mode 0600) as a JSON `access_token`
//! (so the token stays redacted in `Debug` output), plus the non-secret store
//! domain in `extra.store`. Every tool call loads the token fresh from that
//! file, so the connector struct itself holds no secret.
//!
//! ## Merge note for the coordinator
//!
//! Drop this file at `crates/boom-core/src/connectors/shopify.rs`, declare
//! `pub mod shopify;` in `src/connectors/mod.rs`, and register
//! `ShopifyConnector::new()` in `ConnectorRegistry::new()` (also extend
//! `ALL_CONNECTOR_IDS`). It uses only the shared contract from the parent
//! module (`Connector`, `ConnectorToken`, `load_token`, `save_token`,
//! `save_extra`, `delete_token`, `http_client`).

use std::fmt;
use std::time::Duration;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::{
    delete_token, http_client, load_token, save_extra, save_token, Connector, ConnectorToken,
};
use crate::tools::{opt_str, opt_u64, schema_of, Tool, ToolError, ToolRegistry};

/// Stable connector id (also the token file name: `shopify.json`).
const CONNECTOR_ID: &str = "shopify";
const API_VERSION: &str = "2024-10";
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);

const CONNECT_INSTRUCTIONS: &str = "Shopify uses an Admin API access token — POST a JSON object to /api/connectors/shopify/token:\n\
{\"store\": \"your-store\", \"access_token\": \"shpat_…\"}.\n\
Create the token in the store admin: Settings → Apps and sales channels → Develop apps → \
Create an app → Admin API access scopes (read_products, read_orders, read_customers) → Install → \
copy the Admin API access token. \"store\" may be \"your-store\" or \"your-store.myshopify.com\". \
The token is verified with GET /admin/api/2024-10/shop.json and stored in \
$BOOM_HOME/connectors/shopify.json (mode 0600).";

const NOT_CONNECTED_MSG: &str = "shopify is not connected: no store credentials are stored. POST \
{\"store\": \"…\", \"access_token\": \"…\"} to /api/connectors/shopify/token first (see the \
connector's connect instructions).";

/// Credential bundle stored (as JSON) in `ConnectorToken.access_token` so
/// the Admin API token stays redacted in `Debug` output.
#[derive(Debug, Clone, Serialize, Deserialize)]
struct ShopifyCreds {
    store: String,
    access_token: String,
}

/// Shopify connector. Holds no secret: credentials are loaded from the token
/// file on every call, so `exchange_code`/`disconnect` take effect immediately.
pub struct ShopifyConnector {
    api_version: String,
}

impl ShopifyConnector {
    /// Production constructor: real Shopify API, token from `$BOOM_HOME`.
    pub fn new() -> Self {
        Self::with_api_version(API_VERSION)
    }

    /// Constructor with an explicit API version — tests drive a local mock
    /// server through this.
    pub fn with_api_version(api_version: impl Into<String>) -> Self {
        Self {
            api_version: api_version.into(),
        }
    }

    fn api_version(&self) -> &str {
        &self.api_version
    }
}

impl Default for ShopifyConnector {
    fn default() -> Self {
        Self::new()
    }
}

/// Never print credentials in logs or Debug output.
impl fmt::Debug for ShopifyConnector {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ShopifyConnector")
            .field("api_version", &self.api_version)
            .field("connected", &self.is_connected())
            .field("credentials", &"<redacted>")
            .finish()
    }
}

#[async_trait]
impl Connector for ShopifyConnector {
    fn id(&self) -> &'static str {
        CONNECTOR_ID
    }
    fn name(&self) -> &'static str {
        "Shopify"
    }
    fn description(&self) -> &'static str {
        "Shopify Admin API: list products, orders and customers of your store. \
         Authenticates with a store Admin API access token."
    }
    fn auth_kind(&self) -> &'static str {
        "api_key"
    }
    fn connect_url(&self) -> Result<String, String> {
        // No OAuth: the "code" step consumes the JSON credential bundle instead.
        Err(CONNECT_INSTRUCTIONS.to_string())
    }
    async fn exchange_code(&mut self, code: &str) -> Result<(), String> {
        let creds = parse_bundle(code)?;
        let base = format!(
            "https://{}/admin/api/{}",
            creds.store,
            self.api_version()
        );
        let _shop_name = verify_creds(&base, &creds).await?;
        let bundle = serde_json::to_string(&creds)
            .map_err(|e| format!("cannot serialize credentials: {e}"))?;
        save_token(CONNECTOR_ID, &ConnectorToken::api_key(&bundle))?;
        // Store domain is not secret (it is the public store URL).
        let _ = save_extra(CONNECTOR_ID, "store", &creds.store);
        Ok(())
    }
    fn disconnect(&mut self) -> Result<(), String> {
        delete_token(CONNECTOR_ID)
    }
    fn register_tools(&self, reg: &mut ToolRegistry) {
        reg.register("shopify", ProductsTool::new(self.api_version.clone()));
        reg.register("shopify", OrdersTool::new(self.api_version.clone()));
        reg.register("shopify", CustomersTool::new(self.api_version.clone()));
    }
}

// ---------------------------------------------------------------------------
// Credential plumbing
// ---------------------------------------------------------------------------

/// Parse the `{"store": …, "access_token": …}` bundle the user POSTs as the
/// "code". Never echoes the token in errors.
fn parse_bundle(code: &str) -> Result<ShopifyCreds, String> {
    let v: Value = serde_json::from_str(code.trim())
        .map_err(|_| "invalid credentials: expected a JSON object \
                      {\"store\": \"your-store\", \"access_token\": \"shpat_…\"}".to_string())?;
    let store = v
        .get("store")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "invalid credentials: missing \"store\"".to_string())?;
    let access_token = v
        .get("access_token")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "invalid credentials: missing \"access_token\"".to_string())?;
    Ok(ShopifyCreds {
        store: normalize_store(store)?,
        access_token: access_token.to_string(),
    })
}

/// Accept `your-store` or `your-store.myshopify.com`, return the full host.
/// Strict charset so a crafted value can never become an arbitrary URL.
fn normalize_store(raw: &str) -> Result<String, String> {
    let v = raw.trim().to_lowercase();
    let name = v
        .strip_suffix(".myshopify.com")
        .unwrap_or(&v)
        .trim_matches('.');
    if name.is_empty()
        || name.len() > 63
        || !name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-')
    {
        return Err(format!(
            "invalid store '{raw}': expected \"your-store\" or \"your-store.myshopify.com\""
        ));
    }
    Ok(format!("{name}.myshopify.com"))
}

/// Load the credential bundle or fail honestly when not connected.
fn require_creds() -> Result<ShopifyCreds, ToolError> {
    let tok = load_token(CONNECTOR_ID)
        .filter(|t| t.is_present())
        .ok_or_else(|| ToolError::Other(NOT_CONNECTED_MSG.to_string()))?;
    serde_json::from_str::<ShopifyCreds>(&tok.access_token).map_err(|_| {
        ToolError::Other(
            "shopify credentials are corrupt — reconnect the connector".to_string(),
        )
    })
}

// ---------------------------------------------------------------------------
// HTTP transport
// ---------------------------------------------------------------------------

/// One completed Shopify API round-trip.
struct ApiResp {
    status: u16,
    body: Value,
}

/// GET against the store's Admin API with the `X-Shopify-Access-Token`
/// header. `base` is the API root (`https://{store}/admin/api/{version}`);
/// tests point it at a local mock server. The token travels only in the
/// header and is never logged.
async fn shopify_get(
    base: &str,
    creds: &ShopifyCreds,
    path: &str,
    query: &[(&str, String)],
) -> Result<ApiResp, ToolError> {
    let url = format!("{}{}", base.trim_end_matches('/'), path);
    let resp = http_client(REQUEST_TIMEOUT)
        .get(&url)
        .header("X-Shopify-Access-Token", &creds.access_token)
        .query(&query)
        .send()
        .await
        .map_err(transport_error)?;
    let status = resp.status().as_u16();
    let text = resp
        .text()
        .await
        .map_err(|_| ToolError::Http("failed to read Shopify response body".to_string()))?;
    let body = serde_json::from_str::<Value>(text.trim()).unwrap_or(Value::Null);
    Ok(ApiResp { status, body })
}

/// Generic transport failure message. Deliberately generic: reqwest errors
/// can echo request details, and we never want the token in an error.
fn transport_error(err: reqwest::Error) -> ToolError {
    if err.is_timeout() {
        ToolError::Timeout(REQUEST_TIMEOUT.as_secs())
    } else if err.is_connect() {
        ToolError::Http("could not reach the Shopify API (connection failed)".to_string())
    } else {
        ToolError::Http("Shopify request failed".to_string())
    }
}

/// Map a non-2xx Shopify response to a `ToolError`. Nothing here ever
/// contains the access token.
fn check_status(resp: &ApiResp) -> Result<(), ToolError> {
    if (200..300).contains(&resp.status) {
        return Ok(());
    }
    let msg = resp
        .body
        .get("errors")
        .map(|e| e.to_string())
        .unwrap_or_else(|| "no detail from Shopify".to_string());
    let msg: String = msg.chars().take(300).collect();
    match resp.status {
        401 | 403 => Err(ToolError::Other(format!(
            "Shopify rejected the access token (HTTP {}): {} — the token is invalid, revoked or \
             lacks the required scope; reconnect with a fresh Admin API access token",
            resp.status, msg
        ))),
        404 => Err(ToolError::NotFound(format!(
            "Shopify resource not found (404): {msg}"
        ))),
        429 => Err(ToolError::Other(
            "Shopify rate limit exceeded (429): back off and retry shortly".to_string(),
        )),
        s => Err(ToolError::Http(format!("Shopify returned HTTP {s}: {msg}"))),
    }
}

/// Verify candidate credentials with `GET {base}/shop.json`. Returns the
/// shop name. Error messages never contain the token. `base` is the API root
/// (`https://{store}/admin/api/{version}`); tests pass a mock server URL.
async fn verify_creds(base: &str, creds: &ShopifyCreds) -> Result<String, String> {
    let resp = shopify_get(base, creds, "/shop.json", &[])
        .await
        .map_err(|e| match e {
            ToolError::Timeout(_) => "connection to Shopify timed out".to_string(),
            _ => "could not reach the Shopify API".to_string(),
        })?;
    match resp.status {
        200 => Ok(resp
            .body
            .get("shop")
            .and_then(|s| s.get("name"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()),
        401 | 403 => Err("Shopify rejected the credentials (401/403): the access token is \
                          invalid, revoked or lacks the required scopes — the credentials were \
                          NOT saved"
            .to_string()),
        s => Err(format!(
            "Shopify credential verification failed (HTTP {s}): the credentials were NOT saved"
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

/// `{id, title, status, product_type, price_range, variants_count}`.
fn summarize_product(v: &Value) -> Value {
    let prices: Vec<f64> = v
        .get("variants")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|x| x.get("price").and_then(Value::as_str))
                .filter_map(|p| p.parse::<f64>().ok())
                .collect()
        })
        .unwrap_or_default();
    let price_range = match (prices.iter().cloned().fold(None::<f64>, |a, b| {
        Some(a.map_or(b, |x: f64| x.min(b)))
    }), prices.iter().cloned().fold(None::<f64>, |a, b| {
        Some(a.map_or(b, |x: f64| x.max(b)))
    })) {
        (Some(lo), Some(hi)) => serde_json::json!({"min": lo, "max": hi}),
        _ => Value::Null,
    };
    serde_json::json!({
        "id": v.get("id").and_then(Value::as_u64).unwrap_or(0),
        "title": str_field(v, "title"),
        "status": str_field(v, "status"),
        "product_type": str_field(v, "product_type"),
        "price_range": price_range,
        "variants_count": v.get("variants").and_then(Value::as_array).map(|a| a.len()).unwrap_or(0),
    })
}

/// `{id, name, created_at, financial_status, fulfillment_status, total_price, currency, email}`.
fn summarize_order(v: &Value) -> Value {
    serde_json::json!({
        "id": v.get("id").and_then(Value::as_u64).unwrap_or(0),
        "name": str_field(v, "name"),
        "created_at": str_field(v, "created_at"),
        "financial_status": str_field(v, "financial_status"),
        "fulfillment_status": v.get("fulfillment_status").and_then(Value::as_str),
        "total_price": str_field(v, "total_price"),
        "currency": str_field(v, "currency"),
        "email": v.get("email").and_then(Value::as_str),
    })
}

/// `{id, first_name, last_name, email, orders_count, total_spent}`.
fn summarize_customer(v: &Value) -> Value {
    serde_json::json!({
        "id": v.get("id").and_then(Value::as_u64).unwrap_or(0),
        "first_name": str_field(v, "first_name"),
        "last_name": str_field(v, "last_name"),
        "email": v.get("email").and_then(Value::as_str),
        "orders_count": v.get("orders_count").and_then(Value::as_u64).unwrap_or(0),
        "total_spent": str_field(v, "total_spent"),
    })
}

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

/// `shopify_products` — `GET /admin/api/{v}/products.json`.
pub struct ProductsTool {
    api_version: String,
    #[cfg(test)]
    mock_base: Option<String>,
}

impl ProductsTool {
    pub fn new(api_version: String) -> Self {
        Self {
            api_version,
            #[cfg(test)]
            mock_base: None,
        }
    }

    /// Test-only constructor: point the tool at a mock server root.
    #[cfg(test)]
    pub fn with_mock_base(mock_base: &str, api_version: &str) -> Self {
        Self {
            api_version: api_version.to_string(),
            mock_base: Some(mock_base.trim_end_matches('/').to_string()),
        }
    }

    fn base(&self, creds: &ShopifyCreds) -> String {
        #[cfg(test)]
        if let Some(m) = &self.mock_base {
            return m.clone();
        }
        format!("https://{}/admin/api/{}", creds.store, self.api_version)
    }
}

#[async_trait]
impl Tool for ProductsTool {
    fn name(&self) -> &str {
        "shopify_products"
    }
    fn description(&self) -> &str {
        "List products of the connected Shopify store (id, title, status, \
         price range, variant count)."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("limit", "integer", "Results per page, max 250 (default 50)"),
                (
                    "status",
                    "string",
                    "Filter by status: active, archived, draft (optional)",
                ),
                ("title", "string", "Filter by product title substring (optional)"),
            ],
            &[],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let creds = require_creds()?;
        let limit = opt_u64(&args, "limit", 50).clamp(1, 250);
        let mut query = vec![
            ("limit", limit.to_string()),
            (
                "fields",
                "id,title,status,product_type,variants".to_string(),
            ),
        ];
        let status = opt_str(&args, "status", "").trim().to_lowercase();
        if !status.is_empty() {
            let allowed = ["active", "archived", "draft"];
            if !allowed.contains(&status.as_str()) {
                return Err(ToolError::InvalidArgs(format!(
                    "invalid status '{status}': expected one of {}",
                    allowed.join(", ")
                )));
            }
            query.push(("status", status));
        }
        let title = opt_str(&args, "title", "").trim();
        if !title.is_empty() {
            query.push(("title", title.to_string()));
        }
        let resp = shopify_get(&self.base(&creds), &creds, "/products.json", &query).await?;
        check_status(&resp)?;
        let products: Vec<Value> = resp
            .body
            .get("products")
            .and_then(Value::as_array)
            .map(|a| a.iter().map(summarize_product).collect())
            .unwrap_or_default();
        Ok(serde_json::json!({
            "store": creds.store,
            "products": products,
            "count": products.len(),
        }))
    }
}

/// `shopify_orders` — `GET /admin/api/{v}/orders.json`.
pub struct OrdersTool {
    api_version: String,
    #[cfg(test)]
    mock_base: Option<String>,
}

impl OrdersTool {
    pub fn new(api_version: String) -> Self {
        Self {
            api_version,
            #[cfg(test)]
            mock_base: None,
        }
    }

    /// Test-only constructor: point the tool at a mock server root.
    #[cfg(test)]
    pub fn with_mock_base(mock_base: &str, api_version: &str) -> Self {
        Self {
            api_version: api_version.to_string(),
            mock_base: Some(mock_base.trim_end_matches('/').to_string()),
        }
    }

    fn base(&self, creds: &ShopifyCreds) -> String {
        #[cfg(test)]
        if let Some(m) = &self.mock_base {
            return m.clone();
        }
        format!("https://{}/admin/api/{}", creds.store, self.api_version)
    }
}

#[async_trait]
impl Tool for OrdersTool {
    fn name(&self) -> &str {
        "shopify_orders"
    }
    fn description(&self) -> &str {
        "List orders of the connected Shopify store (id, name, financial and \
         fulfillment status, total, customer email)."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("limit", "integer", "Results per page, max 250 (default 50)"),
                (
                    "status",
                    "string",
                    "Order status: open, closed, cancelled, any (default any)",
                ),
                (
                    "financial_status",
                    "string",
                    "Filter by financial status, e.g. paid, pending, refunded (optional)",
                ),
            ],
            &[],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let creds = require_creds()?;
        let limit = opt_u64(&args, "limit", 50).clamp(1, 250);
        let status = opt_str(&args, "status", "any").trim().to_lowercase();
        let allowed = ["open", "closed", "cancelled", "any"];
        if !allowed.contains(&status.as_str()) {
            return Err(ToolError::InvalidArgs(format!(
                "invalid status '{status}': expected one of {}",
                allowed.join(", ")
            )));
        }
        let mut query = vec![
            ("limit", limit.to_string()),
            ("status", status),
            (
                "fields",
                "id,name,created_at,financial_status,fulfillment_status,total_price,currency,email"
                    .to_string(),
            ),
        ];
        let fin = opt_str(&args, "financial_status", "").trim().to_lowercase();
        if !fin.is_empty() {
            query.push(("financial_status", fin));
        }
        let resp = shopify_get(&self.base(&creds), &creds, "/orders.json", &query).await?;
        check_status(&resp)?;
        let orders: Vec<Value> = resp
            .body
            .get("orders")
            .and_then(Value::as_array)
            .map(|a| a.iter().map(summarize_order).collect())
            .unwrap_or_default();
        Ok(serde_json::json!({
            "store": creds.store,
            "orders": orders,
            "count": orders.len(),
        }))
    }
}

/// `shopify_customers` — `GET /admin/api/{v}/customers.json`.
pub struct CustomersTool {
    api_version: String,
    #[cfg(test)]
    mock_base: Option<String>,
}

impl CustomersTool {
    pub fn new(api_version: String) -> Self {
        Self {
            api_version,
            #[cfg(test)]
            mock_base: None,
        }
    }

    /// Test-only constructor: point the tool at a mock server root.
    #[cfg(test)]
    pub fn with_mock_base(mock_base: &str, api_version: &str) -> Self {
        Self {
            api_version: api_version.to_string(),
            mock_base: Some(mock_base.trim_end_matches('/').to_string()),
        }
    }

    fn base(&self, creds: &ShopifyCreds) -> String {
        #[cfg(test)]
        if let Some(m) = &self.mock_base {
            return m.clone();
        }
        format!("https://{}/admin/api/{}", creds.store, self.api_version)
    }
}

#[async_trait]
impl Tool for CustomersTool {
    fn name(&self) -> &str {
        "shopify_customers"
    }
    fn description(&self) -> &str {
        "List customers of the connected Shopify store (id, name, email, \
         orders count, total spent)."
    }
    fn schema(&self) -> Value {
        schema_of(&[("limit", "integer", "Results per page, max 250 (default 50)")], &[])
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let creds = require_creds()?;
        let limit = opt_u64(&args, "limit", 50).clamp(1, 250);
        let query = vec![
            ("limit", limit.to_string()),
            (
                "fields",
                "id,first_name,last_name,email,orders_count,total_spent".to_string(),
            ),
        ];
        let resp =
            shopify_get(&self.base(&creds), &creds, "/customers.json", &query).await?;
        check_status(&resp)?;
        let customers: Vec<Value> = resp
            .body
            .get("customers")
            .and_then(Value::as_array)
            .map(|a| a.iter().map(summarize_customer).collect())
            .unwrap_or_default();
        Ok(serde_json::json!({
            "store": creds.store,
            "customers": customers,
            "count": customers.len(),
        }))
    }
}

// ---------------------------------------------------------------------------
// Tests (all offline — fixtures + validation + honest degradation + mock API)
// ---------------------------------------------------------------------------

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

    fn test_creds() -> ShopifyCreds {
        ShopifyCreds {
            store: "acme.myshopify.com".to_string(),
            access_token: "shpat_TEST".to_string(),
        }
    }

    async fn save_test_creds() -> TestHome {
        let home = TestHome::new().await;
        let bundle = serde_json::to_string(&test_creds()).unwrap();
        save_token(CONNECTOR_ID, &ConnectorToken::api_key(&bundle)).unwrap();
        home
    }

    const SHOP_FIXTURE: &str = r#"{"shop":{"id":1,"name":"Acme","myshopify_domain":"acme.myshopify.com"}}"#;

    const PRODUCTS_FIXTURE: &str = r#"{"products":[
      {"id":1,"title":"T-Shirt","status":"active","product_type":"Apparel",
       "variants":[{"price":"19.99"},{"price":"24.99"}]},
      {"id":2,"title":"Mug","status":"draft","product_type":"Home",
       "variants":[{"price":"9.50"}]}
    ]}"#;

    const ORDERS_FIXTURE: &str = r##"{"orders":[
      {"id":100,"name":"#1001","created_at":"2026-10-16T10:00:00+07:00",
       "financial_status":"paid","fulfillment_status":"fulfilled",
       "total_price":"44.98","currency":"USD","email":"a@example.com"}
    ]}"##;

    const CUSTOMERS_FIXTURE: &str = r#"{"customers":[
      {"id":7,"first_name":"Son","last_name":"Nguyen","email":"son@example.com",
       "orders_count":3,"total_spent":"120.00"}
    ]}"#;

    #[test]
    fn connector_identity() {
        let c = ShopifyConnector::new();
        assert_eq!(c.id(), "shopify");
        assert_eq!(c.name(), "Shopify");
        assert_eq!(c.auth_kind(), "api_key");
        assert!(!c.description().is_empty());
    }

    #[test]
    fn connect_url_gives_bundle_instructions() {
        let c = ShopifyConnector::new();
        let err = c.connect_url().unwrap_err();
        assert!(err.contains("access_token"), "got: {err}");
        assert!(err.contains("shpat_"), "got: {err}");
        assert!(err.contains("/api/connectors/shopify/token"), "got: {err}");
    }

    #[test]
    fn schemas_are_valid() {
        let mut reg = ToolRegistry::default();
        ShopifyConnector::new().register_tools(&mut reg);
        for name in ["shopify_products", "shopify_orders", "shopify_customers"] {
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
        }
        assert_eq!(reg.tools_in_toolset("shopify").len(), 3);
    }

    #[tokio::test]
    async fn token_roundtrip_and_0600() {
        let _home = TestHome::new().await;
        assert!(!ShopifyConnector::new().is_connected());
        let bundle = serde_json::to_string(&test_creds()).unwrap();
        save_token(CONNECTOR_ID, &ConnectorToken::api_key(&bundle)).unwrap();
        assert!(ShopifyConnector::new().is_connected());
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
        assert!(!ShopifyConnector::new().is_connected());
        delete_token(CONNECTOR_ID).unwrap(); // idempotent
    }

    #[test]
    fn bundle_parsing_and_store_normalization() {
        let c = parse_bundle(r#"{"store":"acme","access_token":"shpat_X"}"#).unwrap();
        assert_eq!(c.store, "acme.myshopify.com");
        let c = parse_bundle(r#"{"store":"acme.myshopify.com","access_token":"shpat_X"}"#).unwrap();
        assert_eq!(c.store, "acme.myshopify.com");
        assert!(parse_bundle(r#"{"store":"acme"}"#).is_err());
        assert!(parse_bundle(r#"{"access_token":"x"}"#).is_err());
        assert!(parse_bundle("not json").is_err());
        assert!(normalize_store("ACME").unwrap() == "acme.myshopify.com");
        assert!(normalize_store("../evil").is_err());
        assert!(normalize_store("a/b.myshopify.com").is_err());
        assert!(normalize_store("").is_err());
    }

    #[tokio::test]
    async fn verify_ok_returns_shop_name_and_redacts() {
        let _home = TestHome::new().await;
        let (base, handle) = mock_server(200, SHOP_FIXTURE, 1).await;
        let creds = test_creds();
        let name = verify_creds(&base, &creds).await.unwrap();
        assert_eq!(name, "Acme");
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("GET /shop.json "), "request: {raw}");
        assert!(
            raw.to_lowercase().contains("x-shopify-access-token: shpat_test"),
            "header: {raw}"
        );
        // Token must not appear in the request line / URL.
        let request_line = raw.lines().next().unwrap_or("");
        assert!(!request_line.contains("shpat_TEST"), "token in URL: {request_line}");

        // Full exchange-equivalent save keeps the bundle redacted in Debug.
        let bundle = serde_json::to_string(&creds).unwrap();
        save_token(CONNECTOR_ID, &ConnectorToken::api_key(&bundle)).unwrap();
        let dbg = format!("{:?}", ShopifyConnector::new());
        assert!(!dbg.contains("shpat_TEST"), "token leaked: {dbg}");
        assert!(dbg.contains("<redacted>"));
    }

    #[tokio::test]
    async fn verify_rejects_bad_creds_without_saving() {
        let _home = TestHome::new().await;
        let (base, _h) = mock_server(401, r#"{"errors":"Invalid API key"}"#, 1).await;
        let err = verify_creds(&base, &test_creds()).await.unwrap_err();
        assert!(err.contains("401"), "got: {err}");
        assert!(err.contains("NOT saved"), "got: {err}");
        assert!(!err.contains("shpat_TEST"), "token leaked: {err}");
        assert!(!ShopifyConnector::new().is_connected());
    }

    #[tokio::test]
    async fn exchange_code_rejects_invalid_bundle_without_network() {
        let _home = TestHome::new().await;
        let mut c = ShopifyConnector::new();
        let err = c.exchange_code("not json").await.unwrap_err();
        assert!(err.contains("JSON"), "got: {err}");
        assert!(!c.is_connected());
    }

    #[tokio::test]
    async fn not_connected_is_honest() {
        let _home = TestHome::new().await;
        let tool = ProductsTool::new(API_VERSION.to_string());
        let err = tool.execute(serde_json::json!({})).await.unwrap_err();
        assert!(err.to_string().contains("not connected"), "got: {err}");
    }

    #[tokio::test]
    async fn products_lists_and_summarizes() {
        let _home = save_test_creds().await;
        let (base, handle) = mock_server(200, PRODUCTS_FIXTURE, 1).await;
        let tool = ProductsTool::with_mock_base(&base, API_VERSION);
        let out = tool.execute(serde_json::json!({})).await.unwrap();
        assert_eq!(out["store"], "acme.myshopify.com");
        assert_eq!(out["count"], 2);
        assert_eq!(out["products"][0]["title"], "T-Shirt");
        assert_eq!(out["products"][0]["price_range"]["min"], 19.99);
        assert_eq!(out["products"][0]["price_range"]["max"], 24.99);
        assert_eq!(out["products"][0]["variants_count"], 2);
        assert_eq!(out["products"][1]["status"], "draft");
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("GET /products.json?"), "request: {raw}");
        assert!(raw.contains("limit=50"), "request: {raw}");
    }

    #[tokio::test]
    async fn products_rejects_bad_status() {
        let _home = save_test_creds().await;
        let tool = ProductsTool::with_mock_base("http://127.0.0.1:9", API_VERSION);
        let err = tool
            .execute(serde_json::json!({"status": "bogus"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "got: {err}");
    }

    #[tokio::test]
    async fn orders_lists_and_summarizes() {
        let _home = save_test_creds().await;
        let (base, handle) = mock_server(200, ORDERS_FIXTURE, 1).await;
        let tool = OrdersTool::with_mock_base(&base, API_VERSION);
        let out = tool
            .execute(serde_json::json!({"financial_status": "paid"}))
            .await
            .unwrap();
        assert_eq!(out["count"], 1);
        assert_eq!(out["orders"][0]["name"], "#1001");
        assert_eq!(out["orders"][0]["financial_status"], "paid");
        assert_eq!(out["orders"][0]["total_price"], "44.98");
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("financial_status=paid"), "request: {raw}");
        assert!(raw.contains("status=any"), "request: {raw}");
    }

    #[tokio::test]
    async fn orders_rejects_bad_status() {
        let _home = save_test_creds().await;
        let tool = OrdersTool::with_mock_base("http://127.0.0.1:9", API_VERSION);
        let err = tool
            .execute(serde_json::json!({"status": "bogus"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "got: {err}");
    }

    #[tokio::test]
    async fn customers_lists_and_summarizes() {
        let _home = save_test_creds().await;
        let (base, handle) = mock_server(200, CUSTOMERS_FIXTURE, 1).await;
        let tool = CustomersTool::with_mock_base(&base, API_VERSION);
        let out = tool.execute(serde_json::json!({})).await.unwrap();
        assert_eq!(out["count"], 1);
        assert_eq!(out["customers"][0]["email"], "son@example.com");
        assert_eq!(out["customers"][0]["orders_count"], 3);
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("GET /customers.json?"), "request: {raw}");
    }

    #[tokio::test]
    async fn auth_failure_is_clear_and_redacted() {
        let _home = save_test_creds().await;
        let (base, _h) = mock_server(403, r#"{"errors":"forbidden"}"#, 1).await;
        let err = ProductsTool::with_mock_base(&base, API_VERSION)
            .execute(serde_json::json!({}))
            .await
            .unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("rejected the access token"), "got: {msg}");
        assert!(!msg.contains("shpat_TEST"), "token leaked: {msg}");
    }

    #[test]
    fn summarizers_are_pure() {
        let products: Value = serde_json::from_str(PRODUCTS_FIXTURE).unwrap();
        let s = summarize_product(&products["products"][0]);
        assert_eq!(s["title"], "T-Shirt");
        assert_eq!(s["variants_count"], 2);

        let orders: Value = serde_json::from_str(ORDERS_FIXTURE).unwrap();
        let s = summarize_order(&orders["orders"][0]);
        assert_eq!(s["name"], "#1001");

        let customers: Value = serde_json::from_str(CUSTOMERS_FIXTURE).unwrap();
        let s = summarize_customer(&customers["customers"][0]);
        assert_eq!(s["first_name"], "Son");
    }
}
