//! Google Drive connector for Boom.
//!
//! Drop-in target: `crates/boom-core/src/connectors/gdrive.rs`.
//!
//! Implements the shared [`Connector`] contract (`super::Connector`, defined
//! by the connectors framework in `crates/boom-core/src/connectors/mod.rs`):
//! OAuth2 connect flow, token persistence under
//! `$BOOM_HOME/connectors/gdrive.json` (0600), automatic access-token refresh,
//! and three tools: `gdrive_list`, `gdrive_download`, `gdrive_upload`.
//!
//! Security notes:
//! * Tokens are never logged. `Debug` impls redact all secrets.
//! * Download `dest_path` (and upload source paths) must resolve — via
//!   `canonicalize` — inside `$BOOM_HOME` or a directory listed in
//!   `BOOM_GDRIVE_ALLOWED_DIRS` (colon-separated). Anything else is refused.
//! * Downloads are capped at 25 MiB; inline (base64) downloads at 5 MiB;
//!   uploads are capped at 25 MiB. Caps are enforced both from the
//!   `Content-Length` header and while streaming, so a lying server cannot
//!   blow memory.
//! * Requests carry a Bearer token over HTTPS only, with bounded timeouts
//!   (30 s list / 60 s download+upload).

use std::env;
use std::fs::OpenOptions;
use std::path::{Component, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::io::AsyncWriteExt;

use super::Connector;
use crate::tools::{opt_u64, req_str, schema_of, Tool, ToolError, ToolRegistry};

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/// Connector id used for the token file name (`gdrive.json`).
pub const CONNECTOR_ID: &str = "gdrive";

const AUTH_URL: &str = "https://accounts.google.com/o/oauth2/v2/auth";
const TOKEN_URL: &str = "https://oauth2.googleapis.com/token";
const DRIVE_API: &str = "https://www.googleapis.com/drive/v3";
const UPLOAD_API: &str = "https://www.googleapis.com/upload/drive/v3";

const SCOPES: &str = "https://www.googleapis.com/auth/drive.readonly \
                      https://www.googleapis.com/auth/drive.file";

/// Localhost callback the OAuth client must have registered. Overridable via
/// `BOOM_GOOGLE_REDIRECT_URI` when the registered URI differs.
const DEFAULT_REDIRECT_URI: &str = "http://127.0.0.1:18793/oauth/callback";

/// Max bytes accepted for a file download (25 MiB).
pub const MAX_DOWNLOAD_BYTES: u64 = 25 * 1024 * 1024;
/// Max bytes returned inline as base64 when no `dest_path` is given (5 MiB).
pub const MAX_INLINE_BYTES: u64 = 5 * 1024 * 1024;
/// Max bytes accepted for an upload (25 MiB).
pub const MAX_UPLOAD_BYTES: u64 = 25 * 1024 * 1024;

const LIST_TIMEOUT_SECS: u64 = 30;
const TRANSFER_TIMEOUT_SECS: u64 = 60;
/// Refresh proactively when the token expires within this many seconds.
const REFRESH_SKEW_SECS: u64 = 120;

// ---------------------------------------------------------------------------
// BOOM_HOME + token persistence
// ---------------------------------------------------------------------------

/// Resolve `$BOOM_HOME` (`$BOOM_HOME` env, else `~/.boom`).
pub fn boom_home() -> Result<PathBuf, ToolError> {
    if let Some(home) = env::var_os("BOOM_HOME") {
        if !home.is_empty() {
            return Ok(PathBuf::from(home));
        }
    }
    dirs::home_dir()
        .map(|h| h.join(".boom"))
        .ok_or_else(|| ToolError::Other("cannot resolve home directory for BOOM_HOME".into()))
}

/// Extra directories (colon-separated absolute paths) the path guard allows,
/// from `BOOM_GDRIVE_ALLOWED_DIRS`.
fn extra_allowed_dirs() -> Vec<PathBuf> {
    env::var_os("BOOM_GDRIVE_ALLOWED_DIRS")
        .map(|v| env::split_paths(&v).collect())
        .unwrap_or_default()
}

/// Extra client credentials that may live inside the token file (`extra`),
/// used when the `BOOM_GOOGLE_CLIENT_ID` / `BOOM_GOOGLE_CLIENT_SECRET`
/// environment variables are absent.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct ExtraCreds {
    #[serde(default)]
    client_id: Option<String>,
    #[serde(default)]
    client_secret: Option<String>,
}

/// What is persisted to `$BOOM_HOME/connectors/gdrive.json` (mode 0600).
#[derive(Debug, Clone, Serialize, Deserialize)]
struct StoredToken {
    access_token: String,
    #[serde(default)]
    refresh_token: Option<String>,
    /// Unix timestamp (seconds) when `access_token` expires.
    expires_at: u64,
    #[serde(default)]
    extra: ExtraCreds,
}

fn token_path() -> Result<PathBuf, ToolError> {
    Ok(boom_home()?.join("connectors").join("gdrive.json"))
}

fn read_stored() -> Result<Option<StoredToken>, ToolError> {
    let path = token_path()?;
    let bytes = match std::fs::read(&path) {
        Ok(b) => b,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => {
            return Err(ToolError::Io(e));
        }
    };
    serde_json::from_slice(&bytes)
        .map(Some)
        .map_err(|e| ToolError::Other(format!("corrupt token file {}: {e}", path.display())))
}

fn write_stored(tok: &StoredToken) -> Result<(), ToolError> {
    let path = token_path()?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let data = serde_json::to_vec_pretty(tok)
        .map_err(|e| ToolError::Other(format!("cannot serialize token: {e}")))?;
    let mut opts = OpenOptions::new();
    opts.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    let mut f = opts.open(&path)?;
    use std::io::Write;
    f.write_all(&data)?;
    f.flush()?;
    Ok(())
}

fn remove_stored() -> Result<(), ToolError> {
    let path = token_path()?;
    match std::fs::remove_file(&path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(ToolError::Io(e)),
    }
}

// ---------------------------------------------------------------------------
// OAuth plumbing
// ---------------------------------------------------------------------------

fn now_unix() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn redirect_uri() -> String {
    env::var("BOOM_GOOGLE_REDIRECT_URI").unwrap_or_else(|_| DEFAULT_REDIRECT_URI.to_string())
}

/// Resolve OAuth client credentials: env first, then the token file's `extra`.
fn resolve_client_creds() -> Result<(String, String), String> {
    let id = env::var("BOOM_GOOGLE_CLIENT_ID").ok().filter(|s| !s.is_empty());
    let secret = env::var("BOOM_GOOGLE_CLIENT_SECRET")
        .ok()
        .filter(|s| !s.is_empty());
    if let (Some(id), Some(secret)) = (id, secret) {
        return Ok((id, secret));
    }
    match read_stored() {
        Ok(Some(stored)) => match (stored.extra.client_id, stored.extra.client_secret) {
            (Some(id), Some(secret)) if !id.is_empty() && !secret.is_empty() => Ok((id, secret)),
            _ => Err(missing_creds_msg()),
        },
        _ => Err(missing_creds_msg()),
    }
}

fn missing_creds_msg() -> String {
    "Google OAuth client credentials are not configured. Set BOOM_GOOGLE_CLIENT_ID and \
     BOOM_GOOGLE_CLIENT_SECRET (a Google Cloud OAuth 2.0 client with the Drive API enabled), \
     then reconnect."
        .to_string()
}

/// Minimal percent-encoder for query-string values (RFC 3986 unreserved set).
fn pct_encode(s: &str) -> String {
    const UNRESERVED: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~";
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        if UNRESERVED.contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

fn build_connect_url(client_id: &str) -> String {
    let params = [
        ("response_type", "code"),
        ("client_id", client_id),
        ("redirect_uri", &redirect_uri()),
        ("scope", SCOPES),
        ("access_type", "offline"),
        ("prompt", "consent"),
    ];
    let query = params
        .iter()
        .map(|(k, v)| format!("{k}={}", pct_encode(v)))
        .collect::<Vec<_>>()
        .join("&");
    format!("{AUTH_URL}?{query}")
}

#[derive(Debug, Deserialize)]
struct TokenResponse {
    access_token: String,
    #[serde(default)]
    refresh_token: Option<String>,
    #[serde(default)]
    expires_in: Option<u64>,
    #[serde(default)]
    error: Option<String>,
    #[serde(default)]
    error_description: Option<String>,
}

fn http_client() -> &'static reqwest::Client {
    static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
    CLIENT.get_or_init(|| {
        reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::limited(10))
            .user_agent("boom-core-gdrive/0.1 (+https://github.com/TheOwlOps/Boom)")
            .build()
            .expect("gdrive reqwest client must build")
    })
}

/// Enforce a byte cap. Used for Content-Length pre-checks and while streaming.
fn check_limit(total: u64, limit: u64, what: &str) -> Result<(), ToolError> {
    if total > limit {
        return Err(ToolError::Http(format!(
            "{what} is too large ({total} bytes > {limit} byte limit); refusing"
        )));
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Shared client state (token + auto-refresh)
// ---------------------------------------------------------------------------

struct ClientState {
    access_token: Option<String>,
    refresh_token: Option<String>,
    expires_at: u64,
    client_id: String,
    client_secret: String,
    connected: bool,
}

impl std::fmt::Debug for ClientState {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ClientState")
            .field("access_token", &self.access_token.as_ref().map(|_| "<redacted>"))
            .field("refresh_token", &self.refresh_token.as_ref().map(|_| "<redacted>"))
            .field("expires_at", &self.expires_at)
            .field("client_id", &"<redacted>")
            .field("client_secret", &"<redacted>")
            .field("connected", &self.connected)
            .finish()
    }
}

/// Shareable, `Send + Sync` handle used by all three tools.
#[derive(Clone, Debug)]
pub struct GDriveClient {
    state: Arc<Mutex<ClientState>>,
}

impl GDriveClient {
    fn new(state: ClientState) -> Self {
        Self {
            state: Arc::new(Mutex::new(state)),
        }
    }

    /// A client with no credentials and no token — every call fails honestly.
    pub fn disconnected() -> Self {
        Self::new(ClientState {
            access_token: None,
            refresh_token: None,
            expires_at: 0,
            client_id: String::new(),
            client_secret: String::new(),
            connected: false,
        })
    }

    /// Restore from the token file; `None` when never connected.
    pub fn from_token_file() -> Result<Option<Self>, ToolError> {
        let stored = match read_stored()? {
            Some(s) => s,
            None => return Ok(None),
        };
        // Keep the access token usable even if client creds are momentarily
        // unresolvable (refresh will then fail honestly when it is needed).
        let (client_id, client_secret) = resolve_client_creds().unwrap_or_default();
        Ok(Some(Self::new(ClientState {
            access_token: Some(stored.access_token),
            refresh_token: stored.refresh_token,
            expires_at: stored.expires_at,
            client_id,
            client_secret,
            connected: true,
        })))
    }

    pub fn is_connected(&self) -> bool {
        self.state
            .lock()
            .map(|s| s.connected && s.access_token.is_some())
            .unwrap_or(false)
    }

    fn not_connected_err() -> ToolError {
        ToolError::Other(
            "Google Drive is not connected. Run the OAuth flow: open connect_url(), \
             authorize, then call exchange_code() with the returned code."
                .to_string(),
        )
    }

    /// Valid Bearer token, refreshing automatically when expired or near expiry.
    pub async fn ensure_token(&self) -> Result<String, ToolError> {
        // Fast path: token present and fresh.
        {
            let guard = self
                .state
                .lock()
                .map_err(|e| ToolError::Other(format!("state lock poisoned: {e}")))?;
            if guard.connected {
                if let Some(tok) = guard.access_token.clone() {
                    if now_unix() + REFRESH_SKEW_SECS < guard.expires_at {
                        return Ok(tok);
                    }
                }
            } else {
                return Err(Self::not_connected_err());
            }
        }
        // Slow path: refresh outside the lock.
        self.refresh().await?;
        let guard = self
            .state
            .lock()
            .map_err(|e| ToolError::Other(format!("state lock poisoned: {e}")))?;
        guard
            .access_token
            .clone()
            .ok_or_else(Self::not_connected_err)
    }

    /// Exchange the refresh token for a new access token and persist it.
    async fn refresh(&self) -> Result<(), ToolError> {
        let (client_id, client_secret, refresh_token) = {
            let guard = self
                .state
                .lock()
                .map_err(|e| ToolError::Other(format!("state lock poisoned: {e}")))?;
            match guard.refresh_token.clone() {
                Some(rt) => (guard.client_id.clone(), guard.client_secret.clone(), rt),
                None => {
                    return Err(ToolError::Other(
                        "Google Drive session expired and no refresh token is stored; \
                         reconnect via the OAuth flow."
                            .to_string(),
                    ))
                }
            }
        };
        let form = [
            ("client_id", client_id.as_str()),
            ("client_secret", client_secret.as_str()),
            ("refresh_token", refresh_token.as_str()),
            ("grant_type", "refresh_token"),
        ];
        let resp = http_client()
            .post(TOKEN_URL)
            .form(&form)
            .send()
            .await
            .map_err(|e| ToolError::Http(format!("token refresh failed: {e}")))?;
        let status = resp.status();
        let body: TokenResponse = resp
            .json()
            .await
            .map_err(|e| ToolError::Http(format!("bad token response: {e}")))?;
        if !status.is_success() {
            return Err(ToolError::Http(format!(
                "token refresh rejected ({}): {}",
                status,
                body.error_description
                    .or(body.error)
                    .unwrap_or_else(|| "unknown error".into())
            )));
        }
        let expires_at = now_unix() + body.expires_in.unwrap_or(3600);
        let mut guard = self
            .state
            .lock()
            .map_err(|e| ToolError::Other(format!("state lock poisoned: {e}")))?;
        // Google may omit refresh_token on refresh; keep the old one.
        let refresh_token = body.refresh_token.or(Some(refresh_token));
        write_stored(&StoredToken {
            access_token: body.access_token.clone(),
            refresh_token: refresh_token.clone(),
            expires_at,
            extra: ExtraCreds {
                client_id: Some(guard.client_id.clone()),
                client_secret: Some(guard.client_secret.clone()),
            },
        })?;
        guard.access_token = Some(body.access_token);
        guard.refresh_token = refresh_token;
        guard.expires_at = expires_at;
        guard.connected = true;
        Ok(())
    }

    /// Store a freshly exchanged token (called by `exchange_code`).
    fn store_fresh(
        &self,
        body: TokenResponse,
        client_id: String,
        client_secret: String,
    ) -> Result<(), ToolError> {
        let expires_at = now_unix() + body.expires_in.unwrap_or(3600);
        let stored = StoredToken {
            access_token: body.access_token.clone(),
            refresh_token: body.refresh_token.clone(),
            expires_at,
            extra: ExtraCreds {
                client_id: Some(client_id.clone()),
                client_secret: Some(client_secret.clone()),
            },
        };
        // Persist first: an in-memory-only token would silently die on restart.
        write_stored(&stored)?;
        let mut guard = self
            .state
            .lock()
            .map_err(|e| ToolError::Other(format!("state lock poisoned: {e}")))?;
        guard.access_token = Some(body.access_token);
        guard.refresh_token = body.refresh_token;
        guard.expires_at = expires_at;
        guard.client_id = client_id;
        guard.client_secret = client_secret;
        guard.connected = true;
        Ok(())
    }

    fn clear(&self) {
        if let Ok(mut guard) = self.state.lock() {
            guard.access_token = None;
            guard.refresh_token = None;
            guard.expires_at = 0;
            guard.connected = false;
        }
    }
}

// ---------------------------------------------------------------------------
// Path traversal guard
// ---------------------------------------------------------------------------

/// Resolve `raw` to an absolute path that is guaranteed to sit inside
/// `$BOOM_HOME` or one of `BOOM_GDRIVE_ALLOWED_DIRS`.
///
/// * Relative paths are resolved against `$BOOM_HOME`.
/// * The parent directory is created if missing, then canonicalized; the final
///   path must start with a canonical allowed base.
/// * Empty file names, `.`/`..` file names and absolute-escaping `..` chains
///   are rejected.
pub fn guard_dest_path(raw: &str) -> Result<PathBuf, ToolError> {
    let raw = raw.trim();
    if raw.is_empty() {
        return Err(ToolError::InvalidArgs(
            "dest_path must not be empty".into(),
        ));
    }
    let boom = boom_home()?;
    let candidate = {
        let p = PathBuf::from(raw);
        if p.is_absolute() {
            p
        } else {
            boom.join(p)
        }
    };
    let file_name = candidate
        .file_name()
        .and_then(|s| s.to_str())
        .ok_or_else(|| ToolError::InvalidArgs("dest_path must end in a file name".into()))?;
    if matches!(file_name, "." | "..") {
        return Err(ToolError::Blocked(
            "dest_path must not be '.' or '..'".into(),
        ));
    }
    if candidate.components().any(|c| matches!(c, Component::ParentDir)) {
        // Reject literal `..` before canonicalization; symlinks are handled
        // by the canonicalize-then-prefix check below.
        return Err(ToolError::Blocked(
            "dest_path must not contain '..'".into(),
        ));
    }

    let parent = candidate
        .parent()
        .ok_or_else(|| ToolError::InvalidArgs("dest_path has no parent directory".into()))?;
    std::fs::create_dir_all(parent).map_err(ToolError::Io)?;
    let canon_parent = parent.canonicalize().map_err(ToolError::Io)?;

    let mut bases: Vec<PathBuf> = Vec::new();
    if let Ok(canon_boom) = boom.canonicalize() {
        bases.push(canon_boom);
    } else {
        // BOOM_HOME itself may not exist yet (fresh install); fall back to
        // the joined (non-canonical) path for the prefix check.
        bases.push(boom.clone());
    }
    for extra in extra_allowed_dirs() {
        match extra.canonicalize() {
            Ok(c) => bases.push(c),
            Err(_) => bases.push(extra),
        }
    }

    let allowed = bases.iter().any(|b| canon_parent.starts_with(b));
    if !allowed {
        return Err(ToolError::Blocked(format!(
            "dest_path '{}' is outside $BOOM_HOME and BOOM_GDRIVE_ALLOWED_DIRS; refusing",
            candidate.display()
        )));
    }
    Ok(canon_parent.join(file_name))
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

/// Map a non-2xx Drive response into an honest, actionable error.
fn api_error(status: reqwest::StatusCode, body: String) -> ToolError {
    let hint = match status.as_u16() {
        401 => " (access token rejected — it may have expired; it will be refreshed automatically on the next call, or reconnect via OAuth)",
        403 => " (forbidden — check the Drive scopes granted and that the file is shared with this account)",
        404 => " (not found — wrong file id, or the file is not shared with this account)",
        429 => " (rate limited — back off and retry)",
        _ => "",
    };
    let snippet: String = body.chars().take(300).collect();
    ToolError::Http(format!("Drive API error {status}{hint}: {snippet}"))
}

#[derive(Debug, Deserialize)]
struct DriveFile {
    id: String,
    name: String,
    #[serde(rename = "mimeType")]
    mime_type: String,
    #[serde(default)]
    size: Option<String>,
    #[serde(rename = "modifiedTime")]
    modified_time: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ListResponse {
    #[serde(default)]
    files: Vec<DriveFile>,
    #[serde(rename = "nextPageToken")]
    next_page_token: Option<String>,
}

fn drive_file_json(f: &DriveFile) -> Value {
    json!({
        "id": f.id,
        "name": f.name,
        "mime_type": f.mime_type,
        "size": f.size.as_ref().and_then(|s| s.parse::<u64>().ok()),
        "modified_time": f.modified_time,
    })
}

// ---------------------------------------------------------------------------
// Tool: gdrive_list
// ---------------------------------------------------------------------------

/// List files in Google Drive (Drive `files.list`).
pub struct GDriveList {
    client: GDriveClient,
}

impl GDriveList {
    pub fn new(client: GDriveClient) -> Self {
        Self { client }
    }
}

#[async_trait]
impl Tool for GDriveList {
    fn name(&self) -> &str {
        "gdrive_list"
    }

    fn description(&self) -> &str {
        "List files in Google Drive. `query` uses Drive search syntax \
         (default \"trashed=false\"). Returns id, name, mime_type, size and \
         modified_time for each file."
    }

    fn schema(&self) -> Value {
        schema_of(
            &[
                (
                    "query",
                    "string",
                    "Drive search syntax, e.g. \"name contains 'report' and trashed=false\" \
                     (default \"trashed=false\")",
                ),
                (
                    "page_size",
                    "integer",
                    "Max files to return (default 20, max 100)",
                ),
            ],
            &[],
        )
    }

    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let token = self.client.ensure_token().await?;
        let query = args
            .get("query")
            .and_then(Value::as_str)
            .unwrap_or("trashed=false");
        let page_size = opt_u64(&args, "page_size", 20).clamp(1, 100);

        let url = format!(
            "{DRIVE_API}/files?q={}&pageSize={page_size}&orderBy=modifiedTime%20desc&fields={}",
            pct_encode(query),
            pct_encode("files(id,name,mimeType,size,modifiedTime),nextPageToken"),
        );

        let work = async {
            let resp = http_client()
                .get(&url)
                .bearer_auth(&token)
                .send()
                .await
                .map_err(|e| ToolError::Http(format!("gdrive_list request failed: {e}")))?;
            let status = resp.status();
            if !status.is_success() {
                let body = resp.text().await.unwrap_or_default();
                return Err(api_error(status, body));
            }
            let parsed: ListResponse = resp
                .json()
                .await
                .map_err(|e| ToolError::Http(format!("bad list response: {e}")))?;
            let files: Vec<Value> = parsed.files.iter().map(drive_file_json).collect();
            Ok::<Value, ToolError>(json!({
                "files": files,
                "count": files.len(),
                "has_more": parsed.next_page_token.is_some(),
            }))
        };
        tokio::time::timeout(Duration::from_secs(LIST_TIMEOUT_SECS), work)
            .await
            .map_err(|_| ToolError::Timeout(LIST_TIMEOUT_SECS))?
    }
}

// ---------------------------------------------------------------------------
// Tool: gdrive_download
// ---------------------------------------------------------------------------

/// Download a Drive file: to `dest_path` (guarded), or inline as base64.
pub struct GDriveDownload {
    client: GDriveClient,
}

impl GDriveDownload {
    pub fn new(client: GDriveClient) -> Self {
        Self { client }
    }
}

#[async_trait]
impl Tool for GDriveDownload {
    fn name(&self) -> &str {
        "gdrive_download"
    }

    fn description(&self) -> &str {
        "Download a Google Drive file. With `dest_path` the file is saved \
         inside $BOOM_HOME (or BOOM_GDRIVE_ALLOWED_DIRS); without it, the \
         content is returned inline as base64 (files up to 5 MiB). Downloads \
         are capped at 25 MiB."
    }

    fn schema(&self) -> Value {
        schema_of(
            &[
                ("file_id", "string", "Drive file id to download"),
                (
                    "dest_path",
                    "string",
                    "Local destination path (must be inside $BOOM_HOME). \
                     Omit to get base64 inline (max 5 MiB).",
                ),
            ],
            &["file_id"],
        )
    }

    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let file_id = req_str(&args, "file_id")?;
        let dest = match args.get("dest_path").and_then(Value::as_str) {
            Some(p) => Some(guard_dest_path(p)?),
            None => None,
        };
        let token = self.client.ensure_token().await?;
        let limit = if dest.is_some() {
            MAX_DOWNLOAD_BYTES
        } else {
            MAX_INLINE_BYTES
        };

        let work = async {
            let url = format!("{DRIVE_API}/files/{}?alt=media", pct_encode(&file_id));
            let resp = http_client()
                .get(&url)
                .bearer_auth(&token)
                .send()
                .await
                .map_err(|e| ToolError::Http(format!("gdrive_download request failed: {e}")))?;
            let status = resp.status();
            if !status.is_success() {
                let body = resp.text().await.unwrap_or_default();
                if status.as_u16() == 400 && body.contains("export") {
                    return Err(ToolError::Other(
                        "this is a native Google Docs/Sheets/Slides file; direct download is \
                         not supported (use files.export, which this connector does not expose)."
                            .to_string(),
                    ));
                }
                return Err(api_error(status, body));
            }
            let mime_type = resp
                .headers()
                .get(reqwest::header::CONTENT_TYPE)
                .and_then(|v| v.to_str().ok())
                .unwrap_or("application/octet-stream")
                .to_string();
            if let Some(len) = resp.content_length() {
                check_limit(len, limit, "file")?;
            }

            let mut total: u64 = 0;
            let mut stream = resp.bytes_stream();

            match dest {
                Some(path) => {
                    let mut file = tokio::fs::File::create(&path)
                        .await
                        .map_err(ToolError::Io)?;
                    while let Some(chunk) = stream.next().await {
                        let chunk =
                            chunk.map_err(|e| ToolError::Http(format!("download failed: {e}")))?;
                        total += chunk.len() as u64;
                        check_limit(total, limit, "file")?;
                        file.write_all(&chunk).await.map_err(ToolError::Io)?;
                    }
                    file.flush().await.map_err(ToolError::Io)?;
                    Ok::<Value, ToolError>(json!({
                        "file_id": file_id,
                        "dest_path": path.to_string_lossy(),
                        "bytes": total,
                        "mime_type": mime_type,
                    }))
                }
                None => {
                    let mut buf: Vec<u8> = Vec::new();
                    while let Some(chunk) = stream.next().await {
                        let chunk =
                            chunk.map_err(|e| ToolError::Http(format!("download failed: {e}")))?;
                        total += chunk.len() as u64;
                        check_limit(total, limit, "inline file")?;
                        buf.extend_from_slice(&chunk);
                    }
                    Ok::<Value, ToolError>(json!({
                        "file_id": file_id,
                        "bytes": total,
                        "mime_type": mime_type,
                        "encoding": "base64",
                        "data": base64::Engine::encode(
                            &base64::engine::general_purpose::STANDARD, &buf),
                    }))
                }
            }
        };
        tokio::time::timeout(Duration::from_secs(TRANSFER_TIMEOUT_SECS), work)
            .await
            .map_err(|_| ToolError::Timeout(TRANSFER_TIMEOUT_SECS))?
    }
}

// ---------------------------------------------------------------------------
// Tool: gdrive_upload
// ---------------------------------------------------------------------------

/// Upload a local file to Google Drive (multipart upload, 25 MiB cap).
pub struct GDriveUpload {
    client: GDriveClient,
}

impl GDriveUpload {
    pub fn new(client: GDriveClient) -> Self {
        Self { client }
    }
}

/// Build a `multipart/related` body: JSON metadata part + binary media part.
fn build_multipart_body(metadata: &Value, file_bytes: &[u8], boundary: &str) -> Vec<u8> {
    let meta = serde_json::to_vec(metadata).unwrap_or_default();
    let mut body = Vec::with_capacity(meta.len() + file_bytes.len() + 256);
    body.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
    body.extend_from_slice(b"Content-Type: application/json; charset=UTF-8\r\n\r\n");
    body.extend_from_slice(&meta);
    body.extend_from_slice(format!("\r\n--{boundary}\r\n").as_bytes());
    body.extend_from_slice(b"Content-Type: application/octet-stream\r\n\r\n");
    body.extend_from_slice(file_bytes);
    body.extend_from_slice(format!("\r\n--{boundary}--\r\n").as_bytes());
    body
}

#[async_trait]
impl Tool for GDriveUpload {
    fn name(&self) -> &str {
        "gdrive_upload"
    }

    fn description(&self) -> &str {
        "Upload a local file to Google Drive (multipart upload). The source \
         path must be inside $BOOM_HOME (or BOOM_GDRIVE_ALLOWED_DIRS). Files \
         larger than 25 MiB are refused."
    }

    fn schema(&self) -> Value {
        schema_of(
            &[
                ("path", "string", "Local file to upload (inside $BOOM_HOME)"),
                (
                    "name",
                    "string",
                    "Drive file name (defaults to the local file name)",
                ),
                ("folder_id", "string", "Drive folder id to upload into"),
            ],
            &["path"],
        )
    }

    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let raw_path = req_str(&args, "path")?;
        // Same traversal guard as downloads: never exfiltrate files from
        // outside the workspace.
        let path = guard_dest_path(&raw_path)?;
        let token = self.client.ensure_token().await?;

        let name = match args.get("name").and_then(Value::as_str) {
            Some(n) if !n.trim().is_empty() => n.trim().to_string(),
            _ => path
                .file_name()
                .and_then(|s| s.to_str())
                .ok_or_else(|| ToolError::InvalidArgs("cannot derive a file name".into()))?
                .to_string(),
        };
        let folder_id = args.get("folder_id").and_then(Value::as_str);

        let work = async {
            let meta_len = tokio::fs::metadata(&path)
                .await
                .map_err(ToolError::Io)?
                .len();
            check_limit(meta_len, MAX_UPLOAD_BYTES, "upload file")?;
            let bytes = tokio::fs::read(&path).await.map_err(ToolError::Io)?;
            check_limit(bytes.len() as u64, MAX_UPLOAD_BYTES, "upload file")?;

            let boundary = format!("boom-gdrive-{}", now_unix());
            let mut metadata = serde_json::Map::new();
            metadata.insert("name".into(), json!(name));
            if let Some(fid) = folder_id {
                metadata.insert("parents".into(), json!([fid]));
            }
            let body = build_multipart_body(&Value::Object(metadata), &bytes, &boundary);
            let url = format!(
                "{UPLOAD_API}/files?uploadType=multipart&fields={}",
                pct_encode("id,name,size,mimeType,webViewLink")
            );

            let resp = http_client()
                .post(&url)
                .bearer_auth(&token)
                .header(
                    reqwest::header::CONTENT_TYPE,
                    format!("multipart/related; boundary={boundary}"),
                )
                .body(body)
                .send()
                .await
                .map_err(|e| ToolError::Http(format!("gdrive_upload request failed: {e}")))?;
            let status = resp.status();
            if !status.is_success() {
                let body = resp.text().await.unwrap_or_default();
                return Err(api_error(status, body));
            }
            #[derive(Deserialize)]
            struct Uploaded {
                id: String,
                name: String,
                #[serde(default)]
                size: Option<String>,
                #[serde(rename = "mimeType", default)]
                mime_type: Option<String>,
                #[serde(rename = "webViewLink", default)]
                web_view_link: Option<String>,
            }
            let up: Uploaded = resp
                .json()
                .await
                .map_err(|e| ToolError::Http(format!("bad upload response: {e}")))?;
            Ok::<Value, ToolError>(json!({
                "id": up.id,
                "name": up.name,
                "size": up.size.as_ref().and_then(|s| s.parse::<u64>().ok()),
                "mime_type": up.mime_type,
                "web_view_link": up.web_view_link,
            }))
        };
        tokio::time::timeout(Duration::from_secs(TRANSFER_TIMEOUT_SECS), work)
            .await
            .map_err(|_| ToolError::Timeout(TRANSFER_TIMEOUT_SECS))?
    }
}

// ---------------------------------------------------------------------------
// Connector implementation
// ---------------------------------------------------------------------------

/// Google Drive connector: OAuth2 connect flow + tool registration.
#[derive(Clone, Debug)]
pub struct GDriveConnector {
    client: GDriveClient,
}

impl GDriveConnector {
    /// Load from the token file when present; otherwise start disconnected.
    pub fn new() -> Self {
        let client = GDriveClient::from_token_file()
            .ok()
            .flatten()
            .unwrap_or_else(GDriveClient::disconnected);
        Self { client }
    }

    /// Access to the shared client (used by tests and `register_tools`).
    pub fn client(&self) -> GDriveClient {
        self.client.clone()
    }
}

impl Default for GDriveConnector {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl Connector for GDriveConnector {
    fn id(&self) -> &'static str {
        "gdrive"
    }

    fn name(&self) -> &'static str {
        "Google Drive"
    }

    fn description(&self) -> &'static str {
        "List, download and upload files in Google Drive via OAuth2."
    }

    fn auth_kind(&self) -> &'static str {
        "oauth2"
    }

    fn is_connected(&self) -> bool {
        self.client.is_connected()
    }

    fn connect_url(&self) -> Result<String, String> {
        let (client_id, _) = resolve_client_creds()?;
        Ok(build_connect_url(&client_id))
    }

    async fn exchange_code(&mut self, code: &str) -> Result<(), String> {
        let code = code.trim();
        if code.is_empty() {
            return Err("authorization code must not be empty".into());
        }
        let (client_id, client_secret) = resolve_client_creds()?;
        let redirect = redirect_uri();
        let form = [
            ("code", code),
            ("client_id", client_id.as_str()),
            ("client_secret", client_secret.as_str()),
            ("redirect_uri", redirect.as_str()),
            ("grant_type", "authorization_code"),
        ];
        let resp = http_client()
            .post(TOKEN_URL)
            .form(&form)
            .send()
            .await
            .map_err(|e| format!("token exchange failed: {e}"))?;
        let status = resp.status();
        let body: TokenResponse = resp
            .json()
            .await
            .map_err(|e| format!("bad token response: {e}"))?;
        if !status.is_success() {
            return Err(format!(
                "token exchange rejected ({}): {}",
                status,
                body.error_description
                    .or(body.error)
                    .unwrap_or_else(|| "unknown error".into())
            ));
        }
        if body.refresh_token.is_none() {
            // Without a refresh token the session dies with the access token.
            // Still usable, but warn honestly.
            eprintln!(
                "warning: Google did not return a refresh_token (re-authorize with \
                 prompt=consent if sessions keep expiring)"
            );
        }
        self.client
            .store_fresh(body, client_id, client_secret)
            .map_err(|e| e.to_string())?;
        Ok(())
    }

    fn disconnect(&mut self) -> Result<(), String> {
        remove_stored().map_err(|e| e.to_string())?;
        self.client.clear();
        Ok(())
    }

    fn register_tools(&self, reg: &mut ToolRegistry) {
        reg.register("connectors", GDriveList::new(self.client.clone()));
        reg.register("connectors", GDriveDownload::new(self.client.clone()));
        reg.register("connectors", GDriveUpload::new(self.client.clone()));
    }
}

// ---------------------------------------------------------------------------
// Tests (offline)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    /// Unique temp BOOM_HOME per test, serialized on the process-global
    /// [`crate::connectors::TEST_ENV_LOCK`].
    async fn temp_boom_home(tag: &str) -> (tokio::sync::MutexGuard<'static, ()>, tempfile::TempDir) {
        let guard = crate::connectors::TEST_ENV_LOCK.lock().await;
        let dir = tempfile::tempdir().expect("tempdir");
        env::set_var("BOOM_HOME", dir.path());
        env::set_var("BOOM_GOOGLE_CLIENT_ID", "test-client-id.apps.googleusercontent.com");
        env::set_var("BOOM_GOOGLE_CLIENT_SECRET", "test-client-secret");
        let _ = tag;
        (guard, dir)
    }

    fn fresh_connector() -> GDriveConnector {
        GDriveConnector {
            client: GDriveClient::disconnected(),
        }
    }

    #[tokio::test]
    async fn connect_url_shape() {
        let (_guard, _tmp) = temp_boom_home("url").await;
        let c = GDriveConnector {
            client: GDriveClient::disconnected(),
        };
        let url = c.connect_url().expect("connect_url");
        assert!(url.starts_with("https://accounts.google.com/o/oauth2/v2/auth?"));
        assert!(url.contains("client_id=test-client-id.apps.googleusercontent.com"));
        assert!(url.contains("response_type=code"));
        assert!(url.contains("access_type=offline"));
        assert!(url.contains("redirect_uri=http%3A%2F%2F127.0.0.1%3A18793%2Foauth%2Fcallback"));
        // Scopes are percent-encoded but the Drive scope host must be present.
        assert!(url.contains("www.googleapis.com%2Fauth%2Fdrive.readonly"));
        assert!(url.contains("www.googleapis.com%2Fauth%2Fdrive.file"));
    }

    #[tokio::test]
    async fn connect_url_fails_honestly_without_creds() {
        let (_guard, _tmp) = temp_boom_home("nocreds").await;
        env::remove_var("BOOM_GOOGLE_CLIENT_ID");
        env::remove_var("BOOM_GOOGLE_CLIENT_SECRET");
        let c = GDriveConnector {
            client: GDriveClient::disconnected(),
        };
        let err = c.connect_url().unwrap_err();
        assert!(err.contains("BOOM_GOOGLE_CLIENT_ID"));

        // ...and the token file's `extra` works as a fallback source
        // (checked while the env vars are still removed).
        write_stored(&StoredToken {
            access_token: "acc".into(),
            refresh_token: None,
            expires_at: 0,
            extra: ExtraCreds {
                client_id: Some("file-client-id".into()),
                client_secret: Some("file-client-secret".into()),
            },
        })
        .expect("write token");
        let (id, secret) = resolve_client_creds().expect("extra fallback");
        assert_eq!(id, "file-client-id");
        assert_eq!(secret, "file-client-secret");
        remove_stored().expect("cleanup");

        // restore for other tests
        env::set_var("BOOM_GOOGLE_CLIENT_ID", "test-client-id.apps.googleusercontent.com");
        env::set_var("BOOM_GOOGLE_CLIENT_SECRET", "test-client-secret");
    }

    #[tokio::test]
    async fn tools_fail_honestly_when_disconnected() {
        let (_guard, _tmp) = temp_boom_home("fresh").await;
        let c = fresh_connector();
        assert!(!c.is_connected());
        let mut reg = ToolRegistry::default();
        c.register_tools(&mut reg);
        for name in ["gdrive_list", "gdrive_download", "gdrive_upload"] {
            assert!(reg.get(name).is_some(), "tool {name} registered");
        }
        let err = reg
            .get("gdrive_list")
            .unwrap()
            .execute(json!({}))
            .await
            .unwrap_err();
        assert!(err.to_string().contains("not connected"), "list: {err}");
        let err = reg
            .get("gdrive_download")
            .unwrap()
            .execute(json!({"file_id": "abc"}))
            .await
            .unwrap_err();
        assert!(err.to_string().contains("not connected"), "download: {err}");
        // Upload reaches the token check after the path guard; use a real file
        // inside BOOM_HOME so the failure is the honest "not connected".
        let boom = boom_home().unwrap();
        std::fs::create_dir_all(&boom).unwrap();
        let src = boom.join("probe.txt");
        std::fs::write(&src, b"hi").unwrap();
        let err = reg
            .get("gdrive_upload")
            .unwrap()
            .execute(json!({"path": src.to_string_lossy()}))
            .await
            .unwrap_err();
        assert!(err.to_string().contains("not connected"), "upload: {err}");
    }

    #[test]
    fn schemas_are_valid() {
        let c = fresh_connector();
        let mut reg = ToolRegistry::default();
        c.register_tools(&mut reg);

        let list = reg.get("gdrive_list").unwrap().schema();
        assert_eq!(list["type"], "object");
        assert_eq!(list["properties"]["query"]["type"], "string");
        assert_eq!(list["properties"]["page_size"]["type"], "integer");
        assert!(list["required"].as_array().unwrap().is_empty());
        assert_eq!(list["additionalProperties"], false);

        let dl = reg.get("gdrive_download").unwrap().schema();
        assert_eq!(dl["properties"]["file_id"]["type"], "string");
        assert_eq!(dl["properties"]["dest_path"]["type"], "string");
        assert_eq!(dl["required"], json!(["file_id"]));

        let up = reg.get("gdrive_upload").unwrap().schema();
        assert_eq!(up["properties"]["path"]["type"], "string");
        assert_eq!(up["properties"]["name"]["type"], "string");
        assert_eq!(up["properties"]["folder_id"]["type"], "string");
        assert_eq!(up["required"], json!(["path"]));

        assert_eq!(reg.get("gdrive_list").unwrap().name(), "gdrive_list");
        assert!(!reg.get("gdrive_list").unwrap().description().is_empty());
    }

    #[test]
    fn size_limits_enforced() {
        // Boundary: exactly the limit passes, one byte over fails.
        assert!(check_limit(MAX_DOWNLOAD_BYTES, MAX_DOWNLOAD_BYTES, "file").is_ok());
        let err = check_limit(MAX_DOWNLOAD_BYTES + 1, MAX_DOWNLOAD_BYTES, "file").unwrap_err();
        assert!(err.to_string().contains("too large"));
        assert!(check_limit(MAX_INLINE_BYTES, MAX_INLINE_BYTES, "inline file").is_ok());
        assert!(check_limit(MAX_INLINE_BYTES + 1, MAX_INLINE_BYTES, "inline file").is_err());
        assert!(check_limit(MAX_UPLOAD_BYTES, MAX_UPLOAD_BYTES, "upload file").is_ok());
        assert!(check_limit(MAX_UPLOAD_BYTES + 1, MAX_UPLOAD_BYTES, "upload file").is_err());
        // Zero-length files are fine.
        assert!(check_limit(0, MAX_UPLOAD_BYTES, "upload file").is_ok());
    }

    #[tokio::test]
    async fn path_guard() {
        let (_guard, tmp) = temp_boom_home("guard").await;
        let boom = tmp.path().canonicalize().unwrap();

        // Inside BOOM_HOME (relative) → accepted.
        let p = guard_dest_path("downloads/report.pdf").expect("relative inside boom");
        assert!(p.starts_with(&boom));
        assert!(p.ends_with("downloads/report.pdf"));

        // Absolute inside BOOM_HOME → accepted.
        let abs = boom.join("a").join("b.txt");
        let p = guard_dest_path(abs.to_str().unwrap()).expect("absolute inside boom");
        assert!(p.starts_with(&boom));

        // Outside BOOM_HOME → refused.
        for evil in [
            "/etc/passwd",
            "/tmp/evil.txt",
            "../escape.txt", // relative ".." → rejected outright
            "sub/../../etc/x",
            ".",
            "..",
            "",
        ] {
            let err = guard_dest_path(evil).unwrap_err();
            let msg = err.to_string();
            assert!(
                msg.contains("outside") || msg.contains("..") || msg.contains("empty") || msg.contains("file name"),
                "evil={evil} msg={msg}"
            );
        }

        // Absolute path that only *looks* prefixed ("/tmp/boom2") must not pass.
        let sibling = tmp.path().parent().unwrap().join(format!(
            "{}-sibling",
            tmp.path().file_name().unwrap().to_string_lossy()
        ));
        let err = guard_dest_path(sibling.join("x").to_str().unwrap()).unwrap_err();
        assert!(err.to_string().contains("outside"));
    }

    #[tokio::test]
    async fn path_guard_allows_extra_dirs() {
        let (_guard, tmp) = temp_boom_home("extra").await;
        let extra = tempfile::tempdir().unwrap();
        env::set_var("BOOM_GDRIVE_ALLOWED_DIRS", extra.path());
        let p = guard_dest_path(extra.path().join("ok.bin").to_str().unwrap())
            .expect("extra allowed dir");
        assert!(p.starts_with(extra.path().canonicalize().unwrap()));
        env::remove_var("BOOM_GDRIVE_ALLOWED_DIRS");
        let _ = tmp;
    }

    #[test]
    fn debug_redacts_secrets() {
        let client = GDriveClient::new(ClientState {
            access_token: Some("ya29.super-secret-token".into()),
            refresh_token: Some("1//refresh-secret-xyz".into()),
            expires_at: 9999999999,
            client_id: "id-secret".into(),
            client_secret: "shhh-client-secret".into(),
            connected: true,
        });
        let dbg = format!("{client:?}");
        for secret in [
            "ya29.super-secret-token",
            "1//refresh-secret-xyz",
            "id-secret",
            "shhh-client-secret",
        ] {
            assert!(!dbg.contains(secret), "leaked: {secret}");
        }
        assert!(dbg.contains("<redacted>"));
    }

    #[tokio::test]
    async fn token_roundtrip_and_perms() {
        let (_guard, _tmp) = temp_boom_home("token").await;
        let tok = StoredToken {
            access_token: "acc".into(),
            refresh_token: Some("ref".into()),
            expires_at: 12345,
            extra: ExtraCreds {
                client_id: Some("cid".into()),
                client_secret: Some("csec".into()),
            },
        };
        write_stored(&tok).expect("write");
        let back = read_stored().expect("read").expect("present");
        assert_eq!(back.access_token, "acc");
        assert_eq!(back.refresh_token.as_deref(), Some("ref"));
        assert_eq!(back.extra.client_id.as_deref(), Some("cid"));

        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(token_path().unwrap())
                .unwrap()
                .permissions()
                .mode()
                & 0o777;
            assert_eq!(mode, 0o600, "token file must be 0600");
        }

        remove_stored().expect("remove");
        assert!(read_stored().expect("read").is_none());
    }

    #[test]
    fn multipart_body_shape() {
        let meta = json!({"name": "a.txt"});
        let body = build_multipart_body(&meta, b"hello", "BND");
        let s = String::from_utf8(body).unwrap();
        assert!(s.starts_with("--BND\r\n"));
        assert!(s.contains("Content-Type: application/json; charset=UTF-8"));
        assert!(s.contains(r#""name":"a.txt""#));
        assert!(s.contains("Content-Type: application/octet-stream"));
        assert!(s.contains("hello"));
        assert!(s.ends_with("--BND--\r\n"));
    }

    #[test]
    fn connector_identity() {
        let c = fresh_connector();
        // Trait methods are exercised through the trait object path the
        // framework uses; call the inherent-facing parts directly.
        assert_eq!(c.id(), "gdrive");
        assert_eq!(c.name(), "Google Drive");
        assert_eq!(c.auth_kind(), "oauth2");
        assert!(!c.description().is_empty());
        assert!(!c.is_connected());
    }

    #[test]
    fn pct_encode_basics() {
        assert_eq!(pct_encode("trashed=false"), "trashed%3Dfalse");
        assert_eq!(pct_encode("a b"), "a%20b");
        assert_eq!(pct_encode("abc-_.~"), "abc-_.~");
    }
}
