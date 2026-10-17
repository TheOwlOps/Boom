//! Finances connector — bank account balances, transactions and liabilities via Plaid.
//!
//! Auth is a Plaid credential bundle: [`FinancesConnector::exchange_code`]
//! expects its `code` argument to be a JSON object
//! `{"client_id": "…", "secret": "…", "access_token": "…", "env": "sandbox"}`,
//! verifies it with `POST /accounts/get`, and stores the bundle in
//! `$BOOM_HOME/connectors/finances.json` (mode 0600) as a JSON `access_token`
//! (so every secret stays redacted in `Debug` output). `env` is one of
//! `sandbox` (default), `development`, `production`. The item `access_token`
//! comes from linking a bank account (Plaid Link / the Plaid dashboard);
//! `client_id` + `secret` come from https://dashboard.plaid.com/team/keys.
//! Every tool call loads the bundle fresh from that file, so the connector
//! struct itself holds no secret.
//!
//! ## Merge note for the coordinator
//!
//! Drop this file at `crates/boom-core/src/connectors/finances.rs`, declare
//! `pub mod finances;` in `src/connectors/mod.rs`, and register
//! `FinancesConnector::new()` in `ConnectorRegistry::new()` (also extend
//! `ALL_CONNECTOR_IDS`). It uses only the shared contract from the parent
//! module (`Connector`, `ConnectorToken`, `load_token`, `save_token`,
//! `save_extra`, `delete_token`, `http_client`).

use std::fmt;
use std::time::Duration;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::{delete_token, http_client, load_token, save_extra, save_token, Connector, ConnectorToken};
use crate::tools::{opt_str, schema_of, Tool, ToolError, ToolRegistry};

/// Stable connector id (also the token file name: `finances.json`).
const CONNECTOR_ID: &str = "finances";
const PLAID_VERSION: &str = "2020-09-14";
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);

const CONNECT_INSTRUCTIONS: &str = "Plaid uses a credential bundle — POST a JSON object to /api/connectors/finances/token:\n\
{\"client_id\": \"…\", \"secret\": \"…\", \"access_token\": \"…\", \"env\": \"sandbox\"}.\n\
Get client_id + secret at https://dashboard.plaid.com/team/keys (API keys). The item access_token \
comes from linking a bank account (Plaid Link in your own app, or the dashboard's sandbox items). \
\"env\" is sandbox (default), development, or production — start with sandbox. \
The bundle is verified with POST /accounts/get and stored in $BOOM_HOME/connectors/finances.json (mode 0600).";

const NOT_CONNECTED_MSG: &str = "finances is not connected: no Plaid credentials are stored. POST \
{\"client_id\": \"…\", \"secret\": \"…\", \"access_token\": \"…\", \"env\": \"sandbox\"} to \
/api/connectors/finances/token first (see the connector's connect instructions).";

/// Credential bundle stored (as JSON) in `ConnectorToken.access_token` so
/// every secret stays redacted in `Debug` output.
#[derive(Debug, Clone, Serialize, Deserialize)]
struct PlaidCreds {
    client_id: String,
    secret: String,
    access_token: String,
    env: String,
}

impl PlaidCreds {
    /// Base URL for the configured environment. `env` is validated at parse
    /// time, so this can never produce an arbitrary host.
    fn base_url(&self) -> String {
        format!("https://{}.plaid.com", self.env)
    }
}

/// Finances connector. Holds no secret: credentials are loaded from the token
/// file on every call, so `exchange_code`/`disconnect` take effect immediately.
pub struct FinancesConnector;

impl FinancesConnector {
    pub fn new() -> Self {
        Self
    }
}

impl Default for FinancesConnector {
    fn default() -> Self {
        Self::new()
    }
}

/// Never print credentials in logs or Debug output.
impl fmt::Debug for FinancesConnector {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("FinancesConnector")
            .field("connected", &self.is_connected())
            .field("credentials", &"<redacted>")
            .finish()
    }
}

#[async_trait]
impl Connector for FinancesConnector {
    fn id(&self) -> &'static str {
        CONNECTOR_ID
    }
    fn name(&self) -> &'static str {
        "Finances"
    }
    fn description(&self) -> &'static str {
        "Plaid API: account balances, transactions and liabilities for linked \
         bank accounts. Authenticates with a Plaid credential bundle \
         (client_id + secret + item access token)."
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
        let _item = verify_creds(&creds.base_url(), &creds).await?;
        let bundle = serde_json::to_string(&creds)
            .map_err(|e| format!("cannot serialize credentials: {e}"))?;
        save_token(CONNECTOR_ID, &ConnectorToken::api_key(&bundle))?;
        // Environment is not secret.
        let _ = save_extra(CONNECTOR_ID, "env", &creds.env);
        Ok(())
    }
    fn disconnect(&mut self) -> Result<(), String> {
        delete_token(CONNECTOR_ID)
    }
    fn register_tools(&self, reg: &mut ToolRegistry) {
        reg.register("finances", AccountsTool::new());
        reg.register("finances", TransactionsTool::new());
        reg.register("finances", LiabilitiesTool::new());
    }
}

// ---------------------------------------------------------------------------
// Credential plumbing
// ---------------------------------------------------------------------------

/// Parse the credential bundle the user POSTs as the "code". Never echoes
/// secrets in errors.
fn parse_bundle(code: &str) -> Result<PlaidCreds, String> {
    let v: Value = serde_json::from_str(code.trim()).map_err(|_| {
        "invalid credentials: expected a JSON object {\"client_id\": \"…\", \"secret\": \"…\", \
         \"access_token\": \"…\", \"env\": \"sandbox\"}"
            .to_string()
    })?;
    let field = |name: &str| {
        v.get(name)
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| format!("invalid credentials: missing \"{name}\""))
            .map(str::to_string)
    };
    let env = v
        .get("env")
        .and_then(Value::as_str)
        .map(|s| s.trim().to_lowercase())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "sandbox".to_string());
    if !["sandbox", "development", "production"].contains(&env.as_str()) {
        return Err(format!(
            "invalid credentials: \"env\" must be sandbox, development or production (got '{env}')"
        ));
    }
    Ok(PlaidCreds {
        client_id: field("client_id")?,
        secret: field("secret")?,
        access_token: field("access_token")?,
        env,
    })
}

/// Load the credential bundle or fail honestly when not connected.
fn require_creds() -> Result<PlaidCreds, ToolError> {
    let tok = load_token(CONNECTOR_ID)
        .filter(|t| t.is_present())
        .ok_or_else(|| ToolError::Other(NOT_CONNECTED_MSG.to_string()))?;
    serde_json::from_str::<PlaidCreds>(&tok.access_token).map_err(|_| {
        ToolError::Other("plaid credentials are corrupt — reconnect the connector".to_string())
    })
}

// ---------------------------------------------------------------------------
// HTTP transport
// ---------------------------------------------------------------------------

/// One completed Plaid API round-trip.
struct ApiResp {
    status: u16,
    body: Value,
}

/// POST a JSON body to the Plaid API. `base` is `https://{env}.plaid.com`;
/// tests point it at a local mock server. Secrets travel only in the JSON
/// body and are never logged.
async fn plaid_post(base: &str, path: &str, body: &Value) -> Result<ApiResp, ToolError> {
    let url = format!("{}{}", base.trim_end_matches('/'), path);
    let resp = http_client(REQUEST_TIMEOUT)
        .post(&url)
        .header("Plaid-Version", PLAID_VERSION)
        .json(body)
        .send()
        .await
        .map_err(transport_error)?;
    let status = resp.status().as_u16();
    let text = resp
        .text()
        .await
        .map_err(|_| ToolError::Http("failed to read Plaid response body".to_string()))?;
    let body = serde_json::from_str::<Value>(text.trim()).unwrap_or(Value::Null);
    Ok(ApiResp { status, body })
}

/// Generic transport failure message. Deliberately generic: reqwest errors
/// can echo request details, and we never want secrets in an error.
fn transport_error(err: reqwest::Error) -> ToolError {
    if err.is_timeout() {
        ToolError::Timeout(REQUEST_TIMEOUT.as_secs())
    } else if err.is_connect() {
        ToolError::Http("could not reach the Plaid API (connection failed)".to_string())
    } else {
        ToolError::Http("Plaid request failed".to_string())
    }
}

/// The auth envelope every Plaid request carries.
fn auth_body(creds: &PlaidCreds) -> Value {
    serde_json::json!({
        "client_id": creds.client_id,
        "secret": creds.secret,
        "access_token": creds.access_token,
    })
}

/// Map a non-2xx Plaid response to a `ToolError`. Plaid error bodies look
/// like `{"error_type": …, "error_code": …, "error_message": …}` — the message
/// never contains secrets, only the status and Plaid's detail are surfaced.
fn check_status(resp: &ApiResp) -> Result<(), ToolError> {
    if (200..300).contains(&resp.status) {
        return Ok(());
    }
    let code = resp
        .body
        .get("error_code")
        .and_then(Value::as_str)
        .unwrap_or("");
    let msg = resp
        .body
        .get("error_message")
        .and_then(Value::as_str)
        .map(|s| s.trim().chars().take(300).collect::<String>())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "no detail from Plaid".to_string());
    match code {
        "INVALID_API_KEYS" => Err(ToolError::Other(format!(
            "Plaid rejected the client_id/secret (INVALID_API_KEYS): {msg} — reconnect with \
             fresh Plaid API keys"
        ))),
        "INVALID_ACCESS_TOKEN" => Err(ToolError::Other(format!(
            "Plaid rejected the item access token (INVALID_ACCESS_TOKEN): {msg} — relink the \
             bank account to get a fresh access token"
        ))),
        "ITEM_LOGIN_REQUIRED" => Err(ToolError::Other(format!(
            "Plaid needs the item re-authenticated (ITEM_LOGIN_REQUIRED): {msg} — update the \
             login via Plaid Link, then reconnect"
        ))),
        _ => Err(ToolError::Http(format!(
            "Plaid returned HTTP {} ({code}): {msg}",
            resp.status
        ))),
    }
}

/// Verify candidate credentials with `POST /accounts/get`. Returns the item
/// id. Error messages never contain secrets. `base` is the Plaid base URL;
/// tests pass a mock server URL.
async fn verify_creds(base: &str, creds: &PlaidCreds) -> Result<String, String> {
    let resp = plaid_post(base, "/accounts/get", &auth_body(creds))
        .await
        .map_err(|e| match e {
            ToolError::Timeout(_) => "connection to Plaid timed out".to_string(),
            _ => "could not reach the Plaid API".to_string(),
        })?;
    match resp.status {
        200 => Ok(resp
            .body
            .get("item")
            .and_then(|i| i.get("item_id"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()),
        _ => {
            let code = resp
                .body
                .get("error_code")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            Err(format!(
                "Plaid credential verification failed (HTTP {}, {code}): the credentials were \
                 NOT saved",
                resp.status
            ))
        }
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

/// `{account_id, name, official_name?, type, subtype, balances{available, current, currency}}`.
fn summarize_account(v: &Value) -> Value {
    let b = v.get("balances").unwrap_or(&Value::Null);
    serde_json::json!({
        "account_id": str_field(v, "account_id"),
        "name": str_field(v, "name"),
        "official_name": v.get("official_name").and_then(Value::as_str),
        "type": str_field(v, "type"),
        "subtype": str_field(v, "subtype"),
        "balances": {
            "available": b.get("available").and_then(Value::as_f64),
            "current": b.get("current").and_then(Value::as_f64),
            "currency": b.get("iso_currency_code").and_then(Value::as_str),
        },
    })
}

/// `{date, merchant_name?, name, amount, currency, category[], pending}`.
/// Plaid amounts are positive = money out, negative = money in.
fn summarize_transaction(v: &Value) -> Value {
    let category: Vec<String> = v
        .get("personal_finance_category")
        .and_then(|c| c.get("primary"))
        .and_then(Value::as_str)
        .map(|p| vec![p.to_string()])
        .unwrap_or_default();
    serde_json::json!({
        "date": str_field(v, "date"),
        "merchant_name": v.get("merchant_name").and_then(Value::as_str),
        "name": str_field(v, "name"),
        "amount": v.get("amount").and_then(Value::as_f64).unwrap_or(0.0),
        "currency": v.get("iso_currency_code").and_then(Value::as_str),
        "category": category,
        "pending": v.get("pending").and_then(Value::as_bool).unwrap_or(false),
    })
}

/// `{type, account_id, balance_current, apr?, next_payment_due?, last_payment_amount?}`
/// per liability entry.
fn summarize_liability(kind: &str, v: &Value) -> Value {
    serde_json::json!({
        "type": kind,
        "account_id": str_field(v, "account_id"),
        "balance_current": v.get("balances").and_then(|b| b.get("current")).and_then(Value::as_f64),
        "apr": v.get("aprs").and_then(Value::as_array).and_then(|a| a.first())
            .and_then(|a| a.get("apr")).and_then(Value::as_f64),
        "next_payment_due": v.get("next_payment_due_date").and_then(Value::as_str),
        "last_payment_amount": v.get("last_payment_amount").and_then(Value::as_f64),
    })
}

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

/// Shared base-URL plumbing: production base from creds, mock override in tests.
trait BaseUrl {
    #[cfg(test)]
    fn mock_base(&self) -> Option<&str>;
    fn base(&self, creds: &PlaidCreds) -> String {
        #[cfg(test)]
        if let Some(m) = self.mock_base() {
            return m.to_string();
        }
        #[cfg(not(test))]
        let _ = creds;
        creds.base_url()
    }
}

/// `plaid_accounts` — `POST /accounts/get`.
pub struct AccountsTool {
    #[cfg(test)]
    mock_base: Option<String>,
}

impl AccountsTool {
    pub fn new() -> Self {
        Self {
            #[cfg(test)]
            mock_base: None,
        }
    }
    #[cfg(test)]
    pub fn with_mock_base(mock_base: &str) -> Self {
        Self {
            mock_base: Some(mock_base.trim_end_matches('/').to_string()),
        }
    }
}

impl Default for AccountsTool {
    fn default() -> Self {
        Self::new()
    }
}

impl BaseUrl for AccountsTool {
    #[cfg(test)]
    fn mock_base(&self) -> Option<&str> {
        self.mock_base.as_deref()
    }
}

#[async_trait]
impl Tool for AccountsTool {
    fn name(&self) -> &str {
        "plaid_accounts"
    }
    fn description(&self) -> &str {
        "List Plaid-linked bank accounts with balances (account id, name, \
         type, available/current balance, currency)."
    }
    fn schema(&self) -> Value {
        schema_of(&[], &[])
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let _ = args;
        let creds = require_creds()?;
        let resp = plaid_post(&self.base(&creds), "/accounts/get", &auth_body(&creds)).await?;
        check_status(&resp)?;
        let accounts: Vec<Value> = resp
            .body
            .get("accounts")
            .and_then(Value::as_array)
            .map(|a| a.iter().map(summarize_account).collect())
            .unwrap_or_default();
        Ok(serde_json::json!({ "accounts": accounts, "count": accounts.len() }))
    }
}

/// Validate a YYYY-MM-DD date arg.
fn validate_date(raw: &str, arg: &str) -> Result<chrono::NaiveDate, ToolError> {
    chrono::NaiveDate::parse_from_str(raw.trim(), "%Y-%m-%d").map_err(|_| {
        ToolError::InvalidArgs(format!("invalid {arg} '{raw}': expected YYYY-MM-DD"))
    })
}

/// `plaid_transactions` — `POST /transactions/get`.
pub struct TransactionsTool {
    #[cfg(test)]
    mock_base: Option<String>,
}

impl TransactionsTool {
    pub fn new() -> Self {
        Self {
            #[cfg(test)]
            mock_base: None,
        }
    }
    #[cfg(test)]
    pub fn with_mock_base(mock_base: &str) -> Self {
        Self {
            mock_base: Some(mock_base.trim_end_matches('/').to_string()),
        }
    }
}

impl Default for TransactionsTool {
    fn default() -> Self {
        Self::new()
    }
}

impl BaseUrl for TransactionsTool {
    #[cfg(test)]
    fn mock_base(&self) -> Option<&str> {
        self.mock_base.as_deref()
    }
}

#[async_trait]
impl Tool for TransactionsTool {
    fn name(&self) -> &str {
        "plaid_transactions"
    }
    fn description(&self) -> &str {
        "List Plaid transactions in a date range (date, merchant, amount, \
         category, pending). Positive amounts are money out. Defaults to the \
         last 30 days."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                (
                    "start_date",
                    "string",
                    "Range start YYYY-MM-DD (default: 30 days ago)",
                ),
                (
                    "end_date",
                    "string",
                    "Range end YYYY-MM-DD (default: today)",
                ),
            ],
            &[],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let creds = require_creds()?;
        let today = chrono::Utc::now().date_naive();
        let end = match opt_str(&args, "end_date", "").trim() {
            "" => today,
            s => validate_date(s, "end_date")?,
        };
        let start = match opt_str(&args, "start_date", "").trim() {
            "" => end - chrono::Duration::days(30),
            s => validate_date(s, "start_date")?,
        };
        if start > end {
            return Err(ToolError::InvalidArgs(
                "start_date must not be after end_date".to_string(),
            ));
        }
        if (end - start).num_days() > 730 {
            return Err(ToolError::InvalidArgs(
                "date range must not exceed 730 days".to_string(),
            ));
        }
        let mut body = auth_body(&creds);
        body["start_date"] = Value::String(start.format("%Y-%m-%d").to_string());
        body["end_date"] = Value::String(end.format("%Y-%m-%d").to_string());
        let resp = plaid_post(&self.base(&creds), "/transactions/get", &body).await?;
        check_status(&resp)?;
        let transactions: Vec<Value> = resp
            .body
            .get("transactions")
            .and_then(Value::as_array)
            .map(|a| a.iter().map(summarize_transaction).collect())
            .unwrap_or_default();
        Ok(serde_json::json!({
            "start_date": start.format("%Y-%m-%d").to_string(),
            "end_date": end.format("%Y-%m-%d").to_string(),
            "total_transactions": resp.body.get("total_transactions").and_then(Value::as_u64).unwrap_or(0),
            "transactions": transactions,
            "count": transactions.len(),
        }))
    }
}

/// `plaid_liabilities` — `POST /liabilities/get`.
pub struct LiabilitiesTool {
    #[cfg(test)]
    mock_base: Option<String>,
}

impl LiabilitiesTool {
    pub fn new() -> Self {
        Self {
            #[cfg(test)]
            mock_base: None,
        }
    }
    #[cfg(test)]
    pub fn with_mock_base(mock_base: &str) -> Self {
        Self {
            mock_base: Some(mock_base.trim_end_matches('/').to_string()),
        }
    }
}

impl Default for LiabilitiesTool {
    fn default() -> Self {
        Self::new()
    }
}

impl BaseUrl for LiabilitiesTool {
    #[cfg(test)]
    fn mock_base(&self) -> Option<&str> {
        self.mock_base.as_deref()
    }
}

#[async_trait]
impl Tool for LiabilitiesTool {
    fn name(&self) -> &str {
        "plaid_liabilities"
    }
    fn description(&self) -> &str {
        "List Plaid liabilities: credit cards, mortgages, student and auto \
         loans (balance, APR, next payment)."
    }
    fn schema(&self) -> Value {
        schema_of(&[], &[])
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let _ = args;
        let creds = require_creds()?;
        let resp = plaid_post(&self.base(&creds), "/liabilities/get", &auth_body(&creds)).await?;
        check_status(&resp)?;
        let mut out = Vec::new();
        if let Some(liab) = resp.body.get("liabilities") {
            for (kind, key) in [
                ("credit", "credit"),
                ("mortgage", "mortgage"),
                ("student", "student"),
                ("auto", "auto"),
            ] {
                if let Some(arr) = liab.get(key).and_then(Value::as_array) {
                    out.extend(arr.iter().map(|v| summarize_liability(kind, v)));
                }
            }
        }
        Ok(serde_json::json!({ "liabilities": out, "count": out.len() }))
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

    fn test_creds() -> PlaidCreds {
        PlaidCreds {
            client_id: "cid_test".to_string(),
            secret: "sec_test".to_string(),
            access_token: "access_test".to_string(),
            env: "sandbox".to_string(),
        }
    }

    async fn save_test_creds() -> TestHome {
        let home = TestHome::new().await;
        let bundle = serde_json::to_string(&test_creds()).unwrap();
        save_token(CONNECTOR_ID, &ConnectorToken::api_key(&bundle)).unwrap();
        home
    }

    const ACCOUNTS_FIXTURE: &str = r#"{"accounts":[
      {"account_id":"acc1","name":"Checking","official_name":"Everyday Checking",
       "type":"depository","subtype":"checking",
       "balances":{"available":1250.5,"current":1300.0,"iso_currency_code":"USD"}},
      {"account_id":"acc2","name":"Visa","type":"credit","subtype":"credit card",
       "balances":{"available":null,"current":420.75,"iso_currency_code":"USD"}}
    ],"item":{"item_id":"item1"}}"#;

    const TRANSACTIONS_FIXTURE: &str = r#"{"transactions":[
      {"date":"2026-10-16","merchant_name":"Highlands Coffee","name":"HIGHLANDS COFFEE",
       "amount":4.5,"iso_currency_code":"USD",
       "personal_finance_category":{"primary":"FOOD_AND_DRINK"},"pending":false}
    ],"total_transactions":1}"#;

    const LIABILITIES_FIXTURE: &str = r#"{"liabilities":{
      "credit":[{"account_id":"acc2","balances":{"current":420.75},
                 "aprs":[{"apr":19.99}],"next_payment_due_date":"2026-11-01",
                 "last_payment_amount":100.0}],
      "mortgage":[],"student":[],"auto":[]
    }}"#;

    #[test]
    fn connector_identity() {
        let c = FinancesConnector::new();
        assert_eq!(c.id(), "finances");
        assert_eq!(c.name(), "Finances");
        assert_eq!(c.auth_kind(), "api_key");
        assert!(!c.description().is_empty());
    }

    #[test]
    fn connect_url_gives_bundle_instructions() {
        let c = FinancesConnector::new();
        let err = c.connect_url().unwrap_err();
        assert!(err.contains("client_id"), "got: {err}");
        assert!(err.contains("sandbox"), "got: {err}");
        assert!(err.contains("/api/connectors/finances/token"), "got: {err}");
    }

    #[test]
    fn schemas_are_valid() {
        let mut reg = ToolRegistry::default();
        FinancesConnector::new().register_tools(&mut reg);
        for name in ["plaid_accounts", "plaid_transactions", "plaid_liabilities"] {
            let tool = reg.get(name).unwrap_or_else(|| panic!("tool '{name}' missing"));
            assert!(!tool.description().is_empty());
            let schema = tool.schema();
            let obj = schema.as_object().expect("schema is object");
            assert_eq!(obj.get("type").and_then(|t| t.as_str()), Some("object"));
        }
        assert_eq!(reg.tools_in_toolset("finances").len(), 3);
    }

    #[tokio::test]
    async fn token_roundtrip_and_0600() {
        let _home = TestHome::new().await;
        assert!(!FinancesConnector::new().is_connected());
        let bundle = serde_json::to_string(&test_creds()).unwrap();
        save_token(CONNECTOR_ID, &ConnectorToken::api_key(&bundle)).unwrap();
        assert!(FinancesConnector::new().is_connected());
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
        assert!(!FinancesConnector::new().is_connected());
        delete_token(CONNECTOR_ID).unwrap(); // idempotent
    }

    #[test]
    fn bundle_parsing() {
        let c = parse_bundle(
            r#"{"client_id":"a","secret":"b","access_token":"c"}"#,
        )
        .unwrap();
        assert_eq!(c.env, "sandbox");
        assert_eq!(test_creds().base_url(), "https://sandbox.plaid.com");
        let c = parse_bundle(
            r#"{"client_id":"a","secret":"b","access_token":"c","env":"production"}"#,
        )
        .unwrap();
        assert_eq!(c.env, "production");
        assert!(parse_bundle(r#"{"client_id":"a","secret":"b"}"#).is_err());
        assert!(parse_bundle(r#"{"client_id":"a","secret":"b","access_token":"c","env":"mars"}"#).is_err());
        assert!(parse_bundle("nope").is_err());
    }

    #[tokio::test]
    async fn verify_ok_and_redacted() {
        let _home = TestHome::new().await;
        let (base, handle) = mock_server(200, ACCOUNTS_FIXTURE, 1).await;
        let creds = test_creds();
        let item = verify_creds(&base, &creds).await.unwrap();
        assert_eq!(item, "item1");
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("POST /accounts/get "), "request: {raw}");
        assert!(raw.to_lowercase().contains("plaid-version: 2020-09-14"), "header: {raw}");

        let bundle = serde_json::to_string(&creds).unwrap();
        save_token(CONNECTOR_ID, &ConnectorToken::api_key(&bundle)).unwrap();
        let dbg = format!("{:?}", FinancesConnector::new());
        assert!(!dbg.contains("sec_test"), "secret leaked: {dbg}");
        assert!(!dbg.contains("access_test"), "token leaked: {dbg}");
        assert!(dbg.contains("<redacted>"));
    }

    #[tokio::test]
    async fn verify_rejects_bad_creds_without_saving() {
        let _home = TestHome::new().await;
        let (base, _h) = mock_server(
            400,
            r#"{"error_type":"INVALID_INPUT","error_code":"INVALID_API_KEYS","error_message":"bad keys"}"#,
            1,
        )
        .await;
        let err = verify_creds(&base, &test_creds()).await.unwrap_err();
        assert!(err.contains("NOT saved"), "got: {err}");
        assert!(!err.contains("sec_test"), "secret leaked: {err}");
        assert!(!FinancesConnector::new().is_connected());
    }

    #[tokio::test]
    async fn exchange_code_rejects_invalid_bundle_without_network() {
        let _home = TestHome::new().await;
        let mut c = FinancesConnector::new();
        let err = c.exchange_code("nope").await.unwrap_err();
        assert!(err.contains("JSON"), "got: {err}");
        assert!(!c.is_connected());
    }

    #[tokio::test]
    async fn not_connected_is_honest() {
        let _home = TestHome::new().await;
        let err = AccountsTool::new()
            .execute(serde_json::json!({}))
            .await
            .unwrap_err();
        assert!(err.to_string().contains("not connected"), "got: {err}");
    }

    #[tokio::test]
    async fn accounts_lists_and_summarizes() {
        let _home = save_test_creds().await;
        let (base, handle) = mock_server(200, ACCOUNTS_FIXTURE, 1).await;
        let tool = AccountsTool::with_mock_base(&base);
        let out = tool.execute(serde_json::json!({})).await.unwrap();
        assert_eq!(out["count"], 2);
        assert_eq!(out["accounts"][0]["name"], "Checking");
        assert_eq!(out["accounts"][0]["balances"]["available"], 1250.5);
        assert_eq!(out["accounts"][0]["balances"]["currency"], "USD");
        assert_eq!(out["accounts"][1]["type"], "credit");
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("POST /accounts/get "), "request: {raw}");
        // Secrets are in the POST body, never in the URL — and the mock
        // request line itself carries no secret.
        let request_line = raw.lines().next().unwrap_or("");
        assert!(!request_line.contains("sec_test"), "secret in URL: {request_line}");
    }

    #[tokio::test]
    async fn transactions_lists_and_validates_dates() {
        let _home = save_test_creds().await;
        let (base, handle) = mock_server(200, TRANSACTIONS_FIXTURE, 1).await;
        let tool = TransactionsTool::with_mock_base(&base);
        let out = tool
            .execute(serde_json::json!({"start_date": "2026-10-01", "end_date": "2026-10-16"}))
            .await
            .unwrap();
        assert_eq!(out["count"], 1);
        assert_eq!(out["total_transactions"], 1);
        assert_eq!(out["transactions"][0]["merchant_name"], "Highlands Coffee");
        assert_eq!(out["transactions"][0]["amount"], 4.5);
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("POST /transactions/get "), "request: {raw}");

        let tool2 = TransactionsTool::with_mock_base("http://127.0.0.1:9");
        let err = tool2
            .execute(serde_json::json!({"start_date": "2026-10-16", "end_date": "2026-10-01"}))
            .await
            .unwrap_err();
        assert!(err.to_string().contains("start_date"), "got: {err}");
        let err = tool2
            .execute(serde_json::json!({"start_date": "not-a-date"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "got: {err}");
    }

    #[tokio::test]
    async fn liabilities_lists() {
        let _home = save_test_creds().await;
        let (base, handle) = mock_server(200, LIABILITIES_FIXTURE, 1).await;
        let tool = LiabilitiesTool::with_mock_base(&base);
        let out = tool.execute(serde_json::json!({})).await.unwrap();
        assert_eq!(out["count"], 1);
        assert_eq!(out["liabilities"][0]["type"], "credit");
        assert_eq!(out["liabilities"][0]["apr"], 19.99);
        assert_eq!(out["liabilities"][0]["next_payment_due"], "2026-11-01");
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("POST /liabilities/get "), "request: {raw}");
    }

    #[tokio::test]
    async fn plaid_error_codes_map_to_clear_messages() {
        let _home = save_test_creds().await;
        let (base, _h) = mock_server(
            400,
            r#"{"error_type":"INVALID_INPUT","error_code":"INVALID_API_KEYS","error_message":"bad keys"}"#,
            1,
        )
        .await;
        let err = AccountsTool::with_mock_base(&base)
            .execute(serde_json::json!({}))
            .await
            .unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("INVALID_API_KEYS"), "got: {msg}");
        assert!(msg.contains("reconnect"), "got: {msg}");
        assert!(!msg.contains("sec_test"), "secret leaked: {msg}");
    }

    #[test]
    fn summarizers_are_pure() {
        let accounts: Value = serde_json::from_str(ACCOUNTS_FIXTURE).unwrap();
        let s = summarize_account(&accounts["accounts"][0]);
        assert_eq!(s["balances"]["current"], 1300.0);

        let txs: Value = serde_json::from_str(TRANSACTIONS_FIXTURE).unwrap();
        let s = summarize_transaction(&txs["transactions"][0]);
        assert_eq!(s["category"], serde_json::json!(["FOOD_AND_DRINK"]));

        let liab: Value = serde_json::from_str(LIABILITIES_FIXTURE).unwrap();
        let s = summarize_liability("credit", &liab["liabilities"]["credit"][0]);
        assert_eq!(s["balance_current"], 420.75);

        assert!(validate_date("2026-10-17", "d").is_ok());
        assert!(validate_date("17/10/2026", "d").is_err());
    }
}
