//! External-service connectors (Gmail, Calendar, Drive, GitHub, Spotify, …): OAuth2 / API-key
//! integrations exposed to the agent as tools and to the desktop UI as
//! connect/disconnect HTTP endpoints.
//!
//! Merge target: `crates/boom-core/src/connectors/mod.rs` (see MERGE_PLAN.md).
//! Each connector lives in `crates/boom-core/src/connectors/<id>.rs` and is
//! constructed exactly once in [`ConnectorRegistry::new`].
//!
//! ## Storage
//!
//! One JSON file per connector at `$BOOM_HOME/connectors/<id>.json`
//! (default `~/.boom/connectors/<id>.json`), written atomically (temp file +
//! rename) with mode `0600`. Connector ids are restricted to `[a-z0-9_]+`
//! so a malicious id can never escape the directory via `token_path`.
//!
//! ## Security contract (every connector MUST obey)
//!
//! * Tokens NEVER appear in logs, error messages, or `Debug` output.
//!   [`ConnectorToken`] has a hand-written `Debug` impl that redacts secrets;
//!   `exchange_code` / tool errors must not interpolate tokens either.
//! * `extra` is for non-secret metadata only (scopes, token type, …).
//! * Every HTTP call has a timeout — build clients with [`http_client`] or
//!   set `.timeout(..)` explicitly.
//! * `exchange_code` persists the token itself via [`save_token`] on success
//!   (the token value never travels back up the call stack).

use std::path::{Path, PathBuf};
use std::time::Duration;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// Connector modules
// ---------------------------------------------------------------------------

// One module per connector worker. `flights` holds two connectors
// (FlightAware + Duffel); every other module holds one.
pub mod canva;
pub mod finances;
pub mod flights;
pub mod gcalendar;
pub mod gdrive;
pub mod github;
pub mod gmail;
pub mod health;
pub mod meta_ads;
pub mod places;
pub mod shopify;
pub mod social;
pub mod spotify;
pub mod stripe;
pub mod tailscale;
pub mod tickets;

// ---------------------------------------------------------------------------
// Token storage
// ---------------------------------------------------------------------------

/// Stored credential for one connector.
///
/// `Debug` is implemented by hand and redacts both token values — never
/// derive `Debug` on a struct that holds this.
#[derive(Clone, Default, Serialize, Deserialize)]
pub struct ConnectorToken {
    pub access_token: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub refresh_token: Option<String>,
    /// Unix epoch seconds at which `access_token` expires, if known.
    #[serde(default)]
    pub expires_at: Option<i64>,
    /// Non-secret metadata only (token type, scopes, …). NEVER put secrets here.
    #[serde(default)]
    pub extra: serde_json::Value,
}

impl std::fmt::Debug for ConnectorToken {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ConnectorToken")
            .field("access_token", &"<redacted>")
            .field(
                "refresh_token",
                &self.refresh_token.as_ref().map(|_| "<redacted>"),
            )
            .field("expires_at", &self.expires_at)
            .field("extra", &self.extra)
            .finish()
    }
}

impl ConnectorToken {
    /// Build a token from a raw API key (for `"api_key"` connectors).
    pub fn api_key(key: &str) -> Self {
        Self {
            access_token: key.trim().to_string(),
            ..Default::default()
        }
    }

    /// True when an access token value is present (non-blank).
    pub fn is_present(&self) -> bool {
        !self.access_token.trim().is_empty()
    }

    /// True when `expires_at` is set and lies in the past (60s clock skew).
    pub fn is_expired(&self) -> bool {
        match self.expires_at {
            Some(ts) => {
                let now = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs() as i64)
                    .unwrap_or(0);
                ts < now - 60
            }
            None => false,
        }
    }
}

/// Directory holding one `<id>.json` token file per connector.
pub fn connectors_dir() -> PathBuf {
    let base = std::env::var("BOOM_HOME")
        .map(|v| v.trim().to_string())
        .ok()
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            dirs::home_dir()
                .map(|h| h.join(".boom"))
                .unwrap_or_else(|| PathBuf::from(".boom"))
        });
    base.join("connectors")
}

/// Connector ids are `[a-z0-9_]+`: `token_path` can never escape `connectors_dir`.
fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 64
        && id.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
}

pub fn token_path(id: &str) -> PathBuf {
    connectors_dir().join(format!("{id}.json"))
}

/// Load the stored token, or `None` when the connector was never connected
/// (or the file is unreadable / corrupt — treated as not connected).
pub fn load_token(id: &str) -> Option<ConnectorToken> {
    if !valid_id(id) {
        return None;
    }
    let bytes = std::fs::read(token_path(id)).ok()?;
    serde_json::from_slice(&bytes).ok()
}

#[cfg(unix)]
fn write_secret_file(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::io::Write;
    use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
    // Created 0600 from the start: no window where the secret is group-readable.
    let mut f = std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(path)?;
    f.write_all(bytes)?;
    f.sync_all()?;
    drop(f);
    // Re-apply after callers rename(): rename can reset the mode on some filesystems.
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))
}

#[cfg(not(unix))]
fn write_secret_file(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    std::fs::write(path, bytes)
}

/// Store the token atomically (write temp + chmod 0600 + rename) so a crash
/// mid-write never leaves a half-written credential file.
pub fn save_token(id: &str, tok: &ConnectorToken) -> Result<(), String> {
    if !valid_id(id) {
        return Err(format!("invalid connector id: {id}"));
    }
    let dir = connectors_dir();
    std::fs::create_dir_all(&dir).map_err(|e| format!("cannot create connectors dir: {e}"))?;
    let text =
        serde_json::to_string_pretty(tok).map_err(|e| format!("cannot serialize token: {e}"))?;
    let tmp = dir.join(format!(".{id}.{}.tmp", std::process::id()));
    write_secret_file(&tmp, text.as_bytes())
        .map_err(|e| format!("cannot write token file: {e}"))?;
    let final_path = token_path(id);
    std::fs::rename(&tmp, &final_path).map_err(|e| format!("cannot install token file: {e}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(
            &final_path,
            std::fs::Permissions::from_mode(0o600),
        );
    }
    Ok(())
}

/// Store one non-secret `extra` field (e.g. the verified account login)
/// without disturbing the saved token.
pub fn save_extra(id: &str, key: &str, value: &str) -> Result<(), String> {
    if !valid_id(id) {
        return Err(format!("invalid connector id: {id}"));
    }
    let mut tok = load_token(id).unwrap_or_default();
    let mut extra = tok.extra.as_object().cloned().unwrap_or_default();
    extra.insert(key.to_string(), serde_json::Value::String(value.to_string()));
    tok.extra = serde_json::Value::Object(extra);
    save_token(id, &tok)
}

/// Read one `extra` field back.
pub fn load_extra(id: &str, key: &str) -> Option<String> {
    load_token(id)?
        .extra
        .get(key)
        .and_then(|v| v.as_str())
        .map(str::to_string)
}

/// Delete the stored token. Idempotent: deleting a connector that was never
/// connected (or whose file is already gone) is `Ok`.
pub fn delete_token(id: &str) -> Result<(), String> {
    if !valid_id(id) {
        return Err(format!("invalid connector id: {id}"));
    }
    match std::fs::remove_file(token_path(id)) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(format!("cannot delete token file: {e}")),
    }
}

// ---------------------------------------------------------------------------
// Shared HTTP helper
// ---------------------------------------------------------------------------

/// Build an HTTP client for connector API calls. Every connector HTTP call
/// MUST have a timeout — use this helper (or set `.timeout(..)` yourself).
pub fn http_client(timeout: Duration) -> reqwest::Client {
    reqwest::Client::builder()
        .timeout(timeout)
        .user_agent("boom-core-connectors/0.1 (+https://github.com/TheOwlOps/Boom)")
        .build()
        .expect("reqwest client must build")
}

// ---------------------------------------------------------------------------
// Test isolation
// ---------------------------------------------------------------------------

/// Serializes every test that mutates process-global environment
/// (`BOOM_HOME`, `BOOM_*_CLIENT_ID` / `BOOM_*_CLIENT_SECRET`). The token
/// helpers read these on every call, so per-module locks are insufficient —
/// one process-global lock for all connector tests.
///
/// It is a `tokio::sync::Mutex` (not `std::sync::Mutex`) because tests hold
/// it across `.await` points (mock HTTP servers); a blocking mutex held
/// across an await could starve the executor when many tests contend.
#[cfg(test)]
pub(crate) static TEST_ENV_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

// ---------------------------------------------------------------------------
// Connector trait
// ---------------------------------------------------------------------------

/// One external-service integration. Implemented once per connector in
/// `crates/boom-core/src/connectors/<id>.rs`.
///
/// Implementors MUST:
/// * keep `id()` stable forever — it is the token file name and the URL path
///   segment (`/api/connectors/{id}/…`) and the tool name prefix;
/// * return `auth_kind()` as exactly `"oauth2"` or `"api_key"`;
/// * never include token values in `Err` strings, logs, or tool outputs;
/// * persist the token inside `exchange_code` via [`save_token`] on success;
/// * make `connect_url` fail with a *clear, actionable* message when the
///   connector cannot start a flow (e.g. missing OAuth client id, or an
///   `"api_key"` connector telling the user where to get a key).
#[async_trait]
pub trait Connector: Send + Sync {
    fn id(&self) -> &'static str;
    fn name(&self) -> &'static str;
    fn description(&self) -> &'static str;
    /// `"oauth2"` or `"api_key"`.
    fn auth_kind(&self) -> &'static str;
    /// True when a usable token is stored (default: token file exists and
    /// is not expired). Override only if "usable" needs a live check.
    fn is_connected(&self) -> bool {
        match load_token(self.id()) {
            Some(t) => !t.access_token.trim().is_empty() && !t.is_expired(),
            None => false,
        }
    }
    /// Start the connect flow.
    ///
    /// * `"oauth2"`: return the provider authorization URL the user must open.
    ///   Fail clearly when the OAuth client id/secret is not configured.
    /// * `"api_key"`: return `Err` with instructions telling the user where to
    ///   get a key and to `POST {"access_token": "…"}` to
    ///   `/api/connectors/{id}/token`.
    fn connect_url(&self) -> Result<String, String>;
    /// Finish the OAuth2 flow: exchange `code` for tokens and persist them
    /// with [`save_token`] — the token value must not be returned. For
    /// `"api_key"` connectors return `Err` directing the user to the
    /// `access_token` body form instead.
    async fn exchange_code(&mut self, code: &str) -> Result<(), String>;
    /// Remove the stored credential. Idempotent — `Ok` when already gone.
    /// Also drop any in-memory cached client/token.
    fn disconnect(&mut self) -> Result<(), String>;
    /// Human instructions shown next to `connect_url` in the API response.
    fn instructions(&self) -> String {
        match self.auth_kind() {
            "oauth2" => format!(
                "Open the connect_url in a browser and authorize Boom, then POST the \
                 returned code as {{\"code\": \"…\"}} to /api/connectors/{}/token.",
                self.id()
            ),
            _ => format!(
                "Get an API key from the {} provider dashboard, then POST it as \
                 {{\"access_token\": \"…\"}} to /api/connectors/{}/token.",
                self.name(),
                self.id()
            ),
        }
    }
    /// Add this connector's tools to the agent's tool registry. Tool names
    /// MUST be prefixed with `{id}_` (e.g. `gmail_search`) and registered
    /// under the toolset named `{id}`. Tools must fail with a clear
    /// "not connected" message (never a bare 401) when no token is stored.
    fn register_tools(&self, reg: &mut crate::tools::ToolRegistry);
}

/// One row of `GET /api/connectors`.
#[derive(Debug, Clone, Serialize)]
pub struct ConnectorSummary {
    pub id: String,
    pub name: String,
    pub description: String,
    pub auth_kind: String,
    pub connected: bool,
}

// ---------------------------------------------------------------------------
// Registry
// ---------------------------------------------------------------------------

/// Canonical connector ids — exactly the 17 modules constructed by
/// [`ConnectorRegistry::new`]. Keep in sync with the `pub mod` list above
/// and the constructor calls in `new()`.
pub const ALL_CONNECTOR_IDS: [&str; 17] = [
    "gmail",
    "gcalendar",
    "gdrive",
    "github",
    "spotify",
    "social",
    "places",
    "flightaware",
    "duffel",
    "ticketmaster",
    "health",
    "canva",
    "finances",
    "meta_ads",
    "shopify",
    "stripe",
    "tailscale",
];

/// Owns every connector. Constructed once per process (see
/// `server::routes::connectors`) and once at startup for tool registration.
pub struct ConnectorRegistry {
    connectors: Vec<Box<dyn Connector>>,
}

impl ConnectorRegistry {
    /// Registry with all 17 connectors (each module's `<Id>Connector::new()`).
    pub fn new() -> Self {
        let mut reg = Self::empty();
        reg.register(Box::new(gmail::GmailConnector::new()));
        reg.register(Box::new(gcalendar::GCalendarConnector::new()));
        reg.register(Box::new(gdrive::GDriveConnector::new()));
        reg.register(Box::new(github::GitHubConnector::new()));
        reg.register(Box::new(spotify::SpotifyConnector::new()));
        reg.register(Box::new(social::SocialConnector::new()));
        reg.register(Box::new(places::PlacesConnector::new()));
        reg.register(Box::new(flights::FlightAwareConnector::new()));
        reg.register(Box::new(flights::DuffelConnector::new()));
        reg.register(Box::new(tickets::TicketmasterConnector::new()));
        reg.register(Box::new(health::HealthConnector::new()));
        reg.register(Box::new(canva::CanvaConnector::new()));
        reg.register(Box::new(finances::FinancesConnector::new()));
        reg.register(Box::new(meta_ads::MetaAdsConnector::new()));
        reg.register(Box::new(shopify::ShopifyConnector::new()));
        reg.register(Box::new(stripe::StripeConnector::new()));
        reg.register(Box::new(tailscale::TailscaleConnector::new()));
        debug_assert_eq!(reg.connectors.len(), ALL_CONNECTOR_IDS.len());
        reg
    }

    /// Empty registry — tests and embedders that register connectors manually.
    pub fn empty() -> Self {
        Self {
            connectors: Vec::new(),
        }
    }

    /// Add one connector. Duplicate ids are a programmer error.
    pub fn register(&mut self, c: Box<dyn Connector>) {
        debug_assert!(
            !self.connectors.iter().any(|e| e.id() == c.id()),
            "duplicate connector id"
        );
        self.connectors.push(c);
    }

    /// Ids in registration order.
    pub fn all_ids(&self) -> Vec<&'static str> {
        self.connectors.iter().map(|c| c.id()).collect()
    }

    /// Summaries for `GET /api/connectors`.
    pub fn list(&self) -> Vec<ConnectorSummary> {
        self.connectors
            .iter()
            .map(|c| ConnectorSummary {
                id: c.id().to_string(),
                name: c.name().to_string(),
                description: c.description().to_string(),
                auth_kind: c.auth_kind().to_string(),
                connected: c.is_connected(),
            })
            .collect()
    }

    pub fn get(&self, id: &str) -> Option<&(dyn Connector + '_)> {
        self.connectors.iter().find(|c| c.id() == id).map(|c| &**c)
    }

    pub fn get_mut(&mut self, id: &str) -> Option<&mut (dyn Connector + '_)> {
        // NOTE: written as a loop, not `.find(..).map(|c| &mut **c)` — the
        // closure boundary pins the trait object's 'static bound and the
        // invariant `&mut` then refuses to shrink it to the borrow lifetime.
        for c in self.connectors.iter_mut() {
            if c.id() == id {
                return Some(&mut **c);
            }
        }
        None
    }

    /// Register every connector's tools into the agent tool registry.
    /// Called once at startup (see MERGE_PLAN.md); tools of unconnected
    /// connectors stay registered but answer "not connected" when run.
    pub fn register_all_tools(&self, reg: &mut crate::tools::ToolRegistry) {
        for c in &self.connectors {
            c.register_tools(reg);
        }
    }
}

impl Default for ConnectorRegistry {
    fn default() -> Self {
        Self::new()
    }
}

// ---------------------------------------------------------------------------
// Tests (offline — no network)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    /// Point BOOM_HOME at a fresh temp dir; returns the guard + tempdir
    /// (tempdir must stay alive for the test's duration). Serialized on the
    /// process-global [`super::TEST_ENV_LOCK`].
    async fn temp_boom_home() -> (tokio::sync::MutexGuard<'static, ()>, tempfile::TempDir) {
        let guard = super::TEST_ENV_LOCK.lock().await;
        let dir = tempfile::tempdir().expect("tempdir");
        std::env::set_var("BOOM_HOME", dir.path());
        (guard, dir)
    }

    fn sample_token() -> ConnectorToken {
        ConnectorToken {
            access_token: "sekrit-access-123".to_string(),
            refresh_token: Some("sekrit-refresh-456".to_string()),
            expires_at: Some(9_999_999_999),
            extra: serde_json::json!({"token_type": "Bearer", "scope": "repo"}),
        }
    }

    // -- minimal Connector stub for registry tests --------------------------

    struct StubConnector {
        id: &'static str,
        auth_kind: &'static str,
    }

    #[async_trait]
    impl Connector for StubConnector {
        fn id(&self) -> &'static str {
            self.id
        }
        fn name(&self) -> &'static str {
            "Stub"
        }
        fn description(&self) -> &'static str {
            "test stub"
        }
        fn auth_kind(&self) -> &'static str {
            self.auth_kind
        }
        fn connect_url(&self) -> Result<String, String> {
            if self.auth_kind == "oauth2" {
                match std::env::var("BOOM_TEST_OAUTH_CLIENT_ID") {
                    Ok(v) if !v.trim().is_empty() => {
                        Ok("https://provider.example/oauth/authorize?client_id=set".to_string())
                    }
                    _ => Err(
                        "oauth2 client_id is not configured: set BOOM_TEST_OAUTH_CLIENT_ID \
                         (the connector's documented env var) before connecting"
                            .to_string(),
                    ),
                }
            } else {
                Err(format!(
                    "stub uses api_key auth: POST {{\"access_token\": \"…\"}} to \
                     /api/connectors/{}/token",
                    self.id
                ))
            }
        }
        async fn exchange_code(&mut self, _code: &str) -> Result<(), String> {
            Err("stub: no real OAuth exchange".to_string())
        }
        fn disconnect(&mut self) -> Result<(), String> {
            delete_token(self.id)
        }
        fn register_tools(&self, _reg: &mut crate::tools::ToolRegistry) {}
    }

    // -- token storage ------------------------------------------------------

    #[tokio::test]
    async fn token_save_load_roundtrip() {
        let (_guard, _tmp) = temp_boom_home().await;
        let tok = sample_token();
        save_token("github", &tok).expect("save");
        let loaded = load_token("github").expect("load");
        assert_eq!(loaded.access_token, tok.access_token);
        assert_eq!(loaded.refresh_token, tok.refresh_token);
        assert_eq!(loaded.expires_at, tok.expires_at);
        assert_eq!(loaded.extra, tok.extra);

        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(token_path("github"))
                .expect("stat")
                .permissions()
                .mode()
                & 0o777;
            assert_eq!(mode, 0o600, "token file must be 0600");
        }
        // No temp file left behind.
        let leftovers: Vec<_> = std::fs::read_dir(connectors_dir())
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().ends_with(".tmp"))
            .collect();
        assert!(leftovers.is_empty(), "atomic write left tmp files");
    }

    #[tokio::test]
    async fn token_delete_is_idempotent() {
        let (_guard, _tmp) = temp_boom_home().await;
        delete_token("github").expect("delete of missing file is Ok");
        save_token("github", &sample_token()).expect("save");
        assert!(load_token("github").is_some());
        delete_token("github").expect("delete");
        assert!(load_token("github").is_none(), "token gone after delete");
        delete_token("github").expect("second delete is Ok");
    }

    #[tokio::test]
    async fn token_ids_cannot_escape_dir() {
        let (_guard, _tmp) = temp_boom_home().await;
        assert!(save_token("../evil", &sample_token()).is_err());
        assert!(save_token("a/b", &sample_token()).is_err());
        assert!(save_token("", &sample_token()).is_err());
        assert!(load_token("../evil").is_none());
        assert!(delete_token("../../x").is_err());
    }

    #[test]
    fn token_debug_redacts_secrets() {
        let dbg = format!("{:?}", sample_token());
        assert!(!dbg.contains("sekrit-access-123"), "access token leaked in Debug");
        assert!(!dbg.contains("sekrit-refresh-456"), "refresh token leaked in Debug");
        assert!(dbg.contains("<redacted>"), "Debug should show redaction marker");
    }

    #[tokio::test]
    async fn is_connected_respects_token_state() {
        let (_guard, _tmp) = temp_boom_home().await;
        let c = github::GitHubConnector::new();
        assert!(!c.is_connected(), "not connected before any token");

        let mut expired = sample_token();
        expired.expires_at = Some(1); // 1970
        save_token("github", &expired).expect("save");
        assert!(!c.is_connected(), "expired token is not connected");

        save_token("github", &sample_token()).expect("save");
        assert!(c.is_connected(), "valid token is connected");

        delete_token("github").expect("delete");
        assert!(!c.is_connected(), "not connected after disconnect");
    }

    // -- registry -----------------------------------------------------------

    #[test]
    fn registry_lists_all_ids() {
        // The real registry: every connector module's constructor must work
        // without network access.
        let reg = ConnectorRegistry::new();
        let ids = reg.all_ids();
        assert_eq!(
            ids.len(),
            ALL_CONNECTOR_IDS.len(),
            "registry must list every connector id"
        );
        for want in ALL_CONNECTOR_IDS {
            assert!(ids.contains(&want), "missing connector id: {want}");
        }
        let summaries = reg.list();
        assert_eq!(summaries.len(), ALL_CONNECTOR_IDS.len());
        for s in &summaries {
            assert!(!s.id.is_empty() && !s.name.is_empty());
            assert!(
                s.auth_kind == "oauth2" || s.auth_kind == "api_key",
                "bad auth_kind for {}",
                s.id
            );
        }
    }

    #[test]
    fn registry_get_and_get_mut() {
        let mut reg = ConnectorRegistry::new();
        assert!(reg.get("gmail").is_some());
        assert!(reg.get("nope").is_none());
        assert!(reg.get_mut("spotify").is_some());
        assert!(reg.get_mut("nope").is_none());
        // disconnect through get_mut is idempotent
        reg.get_mut("gmail")
            .unwrap()
            .disconnect()
            .expect("disconnect");
    }

    #[tokio::test]
    async fn connect_url_errors_clearly_without_client_id() {
        let (_guard, _tmp) = temp_boom_home().await;
        std::env::remove_var("BOOM_TEST_OAUTH_CLIENT_ID");
        let stub = StubConnector {
            id: "oauth_stub",
            auth_kind: "oauth2",
        };
        let err = stub.connect_url().expect_err("must fail without client_id");
        assert!(
            err.contains("client_id"),
            "error must name the missing client_id, got: {err}"
        );
        assert!(
            !err.contains("sekrit"),
            "error must never contain secret material"
        );
    }

    #[test]
    fn api_key_connect_url_points_to_token_endpoint() {
        let gh = github::GitHubConnector::new();
        let err = gh.connect_url().expect_err("api_key connectors have no URL");
        assert!(
            err.contains("/api/connectors/github/token"),
            "must direct the user to the token endpoint, got: {err}"
        );
    }
}
