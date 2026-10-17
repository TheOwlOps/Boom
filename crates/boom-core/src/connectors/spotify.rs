//! Spotify connector — OAuth2 authorization-code flow + playback tools.
//!
//! ## Merge note for the coordinator
//!
//! Drop this file at `crates/boom-core/src/connectors/spotify.rs`, declare
//! `pub mod spotify;` in `src/connectors/mod.rs`, and call
//! `SpotifyConnector::new().register_tools(&mut reg)` from wherever
//! connectors are wired in. Everything the file needs (the `Connector` trait,
//! the token helpers) is defined here so it compiles standalone inside
//! `boom-core` without touching existing files.

use std::fmt;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::sync::Mutex;

use crate::tools::http::client;
use super::Connector;
use crate::tools::{opt_str, opt_u64, req_str, schema_of, ssrf_check_url, Tool, ToolError, ToolRegistry};


// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/// Connector id used for the token file and tool routing.
pub const CONNECTOR_ID: &str = "spotify";

const AUTH_URL: &str = "https://accounts.spotify.com/authorize";
const TOKEN_URL: &str = "https://accounts.spotify.com/api/token";
const API_BASE: &str = "https://api.spotify.com";

/// OAuth2 redirect target. Boom's callback server listens here.
pub const REDIRECT_URI: &str = "http://localhost:18080/api/connectors/spotify/callback";

/// Scopes needed by the four tools below.
pub const SCOPES: &str =
    "user-read-playback-state user-modify-playback-state user-read-currently-playing";

/// All Spotify API calls time out after this.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);

/// Refresh a token this many seconds before it actually expires.
const REFRESH_LEEWAY_SECS: u64 = 60;

const VALID_KINDS: &[&str] = &["track", "album", "artist", "playlist"];

// ---------------------------------------------------------------------------
// Token storage helpers
// ---------------------------------------------------------------------------

/// Persisted OAuth2 material. `extra` carries client credentials when they
/// come from anywhere other than environment variables (see below).
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct SpotifyToken {
    pub access_token: String,
    pub refresh_token: Option<String>,
    /// Unix timestamp at which the access token expires.
    pub expires_at: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub extra: Option<Value>,
}

fn token_path(id: &str) -> PathBuf {
    std::env::var("BOOM_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            dirs::home_dir()
                .map(|h| h.join(".boom"))
                .unwrap_or_else(|| PathBuf::from(".boom"))
        })
        .join("connectors")
        .join(format!("{id}.json"))
}

/// Load the stored token for a connector id (e.g. `spotify.json`).
pub fn load_token(id: &str) -> Result<Option<SpotifyToken>, String> {
    let path = token_path(id);
    if !path.exists() {
        return Ok(None);
    }
    let raw =
        std::fs::read_to_string(&path).map_err(|e| format!("read token file: {e}"))?;
    serde_json::from_str(&raw).map(Some).map_err(|e| format!("parse token file: {e}"))
}

/// Save a token; parent dir is created with the file mode `0600`.
pub fn save_token(id: &str, tok: &SpotifyToken) -> Result<(), String> {
    let path = token_path(id);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("create token dir: {e}"))?;
    }
    let raw = serde_json::to_string_pretty(tok).map_err(|e| format!("encode token: {e}"))?;
    std::fs::write(&path, raw).map_err(|e| format!("write token file: {e}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

/// Remove the stored token (logout).
pub fn delete_token(id: &str) -> Result<(), String> {
    let path = token_path(id);
    if path.exists() {
        std::fs::remove_file(&path).map_err(|e| format!("delete token file: {e}"))?;
    }
    let _ = id;
    Ok(())
}

// ---------------------------------------------------------------------------
// Credentials
// ---------------------------------------------------------------------------

/// Client credentials: env first (`BOOM_SPOTIFY_CLIENT_ID` /
/// `BOOM_SPOTIFY_CLIENT_SECRET`), then the `extra` object in the token file.
fn client_credentials() -> Result<(String, String), String> {
    let id = std::env::var("BOOM_SPOTIFY_CLIENT_ID").ok().filter(|s| !s.is_empty());
    let secret = std::env::var("BOOM_SPOTIFY_CLIENT_SECRET")
        .ok()
        .filter(|s| !s.is_empty());
    if let (Some(id), Some(secret)) = (id, secret) {
        return Ok((id, secret));
    }
    // Fallback: creds embedded in the token file's `extra` object.
    if let Ok(Some(tok)) = load_token(CONNECTOR_ID) {
        if let Some(extra) = &tok.extra {
            let id = extra.get("client_id").and_then(Value::as_str);
            let secret = extra.get("client_secret").and_then(Value::as_str);
            if let (Some(id), Some(secret)) = (id, secret) {
                return Ok((id.to_string(), secret.to_string()));
            }
        }
    }
    Err("Spotify client credentials not configured — set BOOM_SPOTIFY_CLIENT_ID \
         and BOOM_SPOTIFY_CLIENT_SECRET"
        .to_string())
}

fn now_unix() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Minimal percent-encoding for query components (RFC 3986 unreserved set).
fn encode(s: &str) -> String {
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

// ---------------------------------------------------------------------------
// Connector
// ---------------------------------------------------------------------------

/// Spotify connector. Owns the OAuth2 token lifecycle and hands its tools a
/// shared handle so playback calls can auto-refresh the access token.
pub struct SpotifyConnector {
    token: Option<SpotifyToken>,
}

impl Default for SpotifyConnector {
    fn default() -> Self {
        Self::new()
    }
}

impl SpotifyConnector {
    /// Load any persisted token; disconnected until OAuth completes.
    pub fn new() -> Self {
        let token = load_token(CONNECTOR_ID).ok().flatten();
        SpotifyConnector { token }
    }

    /// A valid (auto-refreshed) access token, or an honest "not connected"
    /// style error.
    async fn access_token(&mut self) -> Result<String, String> {
        let tok = self.token.as_mut().ok_or_else(|| {
            "Spotify not connected — call spotify connect_url and complete the OAuth2 flow first"
                .to_string()
        })?;
        if tok.expires_at <= now_unix().saturating_add(REFRESH_LEEWAY_SECS) {
            self.refresh().await?;
        }
        Ok(self
            .token
            .as_ref()
            .map(|t| t.access_token.clone())
            .unwrap_or_default())
    }

    /// Exchange the refresh token for a new access token and persist it.
    async fn refresh(&mut self) -> Result<(), String> {
        let (client_id, client_secret) = client_credentials()?;
        let refresh_token = self
            .token
            .as_ref()
            .and_then(|t| t.refresh_token.clone())
            .ok_or_else(|| "Spotify token expired and no refresh token is available — reconnect".to_string())?;

        let body = format!(
            "grant_type=refresh_token&refresh_token={}",
            encode(&refresh_token)
        );
        let resp = token_post(&client_id, &client_secret, body).await?;
        let new_access = resp
            .get("access_token")
            .and_then(Value::as_str)
            .ok_or_else(|| "Spotify refresh response missing access_token".to_string())?
            .to_string();
        let expires_in = resp.get("expires_in").and_then(Value::as_u64).unwrap_or(3600);

        let mut updated = self.token.clone().unwrap_or_default();
        updated.access_token = new_access;
        // Spotify sometimes omits refresh_token on refresh: keep the old one.
        if let Some(rt) = resp.get("refresh_token").and_then(Value::as_str) {
            updated.refresh_token = Some(rt.to_string());
        }
        updated.expires_at = now_unix().saturating_add(expires_in);
        save_token(CONNECTOR_ID, &updated)?;
        self.token = Some(updated);
        Ok(())
    }

    /// `true` when an access token exists (expiry is checked lazily by
    /// `access_token`, which auto-refreshes).
    fn has_token(&self) -> bool {
        self.token.is_some()
    }
}

impl fmt::Debug for SpotifyConnector {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("SpotifyConnector")
            .field("id", &CONNECTOR_ID)
            .field("connected", &self.has_token())
            .finish_non_exhaustive() // never print token material
    }
}

/// POST form body to the Spotify token endpoint with Basic auth.
async fn token_post(
    client_id: &str,
    client_secret: &str,
    body: String,
) -> Result<Value, String> {
    ssrf_check_url(TOKEN_URL)
        .await
        .map_err(|e| format!("ssrf check failed: {e}"))?;

    let basic = base64::Engine::encode(
        &base64::engine::general_purpose::STANDARD,
        format!("{client_id}:{client_secret}"),
    );
    let resp = client()
        .post(TOKEN_URL)
        .header("Authorization", format!("Basic {basic}"))
        .header("Content-Type", "application/x-www-form-urlencoded")
        .body(body)
        .send()
        .await
        .map_err(|e| format!("token request failed: {e}"))?;

    let status = resp.status().as_u16();
    let text = resp.text().await.unwrap_or_default();
    if !(200..300).contains(&status) {
        return Err(format!("Spotify token endpoint returned {status}: {text}"));
    }
    serde_json::from_str(&text).map_err(|e| format!("parse token response: {e}"))
}

#[async_trait]
impl Connector for SpotifyConnector {
    fn id(&self) -> &'static str {
        CONNECTOR_ID
    }

    fn name(&self) -> &'static str {
        "Spotify"
    }

    fn description(&self) -> &'static str {
        "Search music and control Spotify playback (requires an active device)."
    }

    fn auth_kind(&self) -> &'static str {
        "oauth2"
    }

    fn is_connected(&self) -> bool {
        self.has_token()
    }

    fn connect_url(&self) -> Result<String, String> {
        let (client_id, _) = client_credentials()?;
        let scope = SCOPES.replace(' ', "%20");
        Ok(format!(
            "{AUTH_URL}?client_id={}&response_type=code&redirect_uri={}&scope={scope}",
            encode(&client_id),
            encode(REDIRECT_URI)
        ))
    }

    async fn exchange_code(&mut self, code: &str) -> Result<(), String> {
        let (client_id, client_secret) = client_credentials()?;
        let body = format!(
            "grant_type=authorization_code&code={}&redirect_uri={}",
            encode(code),
            encode(REDIRECT_URI)
        );
        let resp = token_post(&client_id, &client_secret, body).await?;
        let access_token = resp
            .get("access_token")
            .and_then(Value::as_str)
            .ok_or_else(|| "Spotify token response missing access_token".to_string())?
            .to_string();
        let expires_in = resp.get("expires_in").and_then(Value::as_u64).unwrap_or(3600);
        let tok = SpotifyToken {
            access_token,
            refresh_token: resp
                .get("refresh_token")
                .and_then(Value::as_str)
                .map(str::to_string),
            expires_at: now_unix().saturating_add(expires_in),
            extra: None,
        };
        save_token(CONNECTOR_ID, &tok)?;
        self.token = Some(tok);
        Ok(())
    }

    fn disconnect(&mut self) -> Result<(), String> {
        delete_token(CONNECTOR_ID)?;
        self.token = None;
        Ok(())
    }

    fn register_tools(&self, reg: &mut ToolRegistry) {
        let shared = Arc::new(Mutex::new(Self::with_token(self.token.clone())));
        reg.register("music", SpotifySearch { conn: shared.clone() });
        reg.register("music", SpotifyCurrent { conn: shared.clone() });
        reg.register("music", SpotifyPlay { conn: shared.clone() });
        reg.register("music", SpotifyPause { conn: shared });
    }
}

impl SpotifyConnector {
    fn with_token(token: Option<SpotifyToken>) -> Self {
        SpotifyConnector { token }
    }
}

// ---------------------------------------------------------------------------
// Authenticated API calls
// ---------------------------------------------------------------------------

/// Shared handle handed to every tool; auto-refresh is coordinated through
/// the mutex.
type Shared = Arc<Mutex<SpotifyConnector>>;

/// Honest error returned when no token is available.
fn not_connected_err() -> ToolError {
    ToolError::Other(
        "Spotify not connected — run the OAuth2 connect flow first".to_string(),
    )
}

/// Perform an authenticated Spotify Web API request.
///
/// `path` is the path portion (e.g. `/v1/search`). Query pairs are appended
/// verbatim (already encoded or safe). A `204` returns `Value::Null`.
async fn api_call(
    conn: &Shared,
    method: &str,
    path: &str,
    query: &[(&str, &str)],
    body: Option<Value>,
) -> Result<Value, ToolError> {
    let mut url = format!("{API_BASE}{path}");
    if !query.is_empty() {
        let q: Vec<String> = query
            .iter()
            .map(|(k, v)| format!("{}={}", encode(k), encode(v)))
            .collect();
        url.push('?');
        url.push_str(&q.join("&"));
    }

    // SSRF guard before anything goes on the wire (api.spotify.com is public,
    // this just enforces the crate-wide policy).
    ssrf_check_url(&url).await?;

    let token = {
        let mut guard = conn.lock().await;
        guard.access_token().await.map_err(|_| not_connected_err())?
    };

    let req = {
        let c = client();
        let rb = match method {
            "PUT" => c.put(&url),
            "POST" => c.post(&url),
            _ => c.get(&url),
        };
        rb.bearer_auth(token)
            .header("Content-Type", "application/json")
    };
    let req = match body {
        Some(b) => req.json(&b),
        None => req,
    };

    let resp = tokio::time::timeout(REQUEST_TIMEOUT, req.send())
        .await
        .map_err(|_| ToolError::Timeout(REQUEST_TIMEOUT.as_secs()))?
        .map_err(|e| ToolError::Http(format!("request failed: {e}")))?;

    let status = resp.status().as_u16();
    let text = resp
        .text()
        .await
        .map_err(|e| ToolError::Http(format!("read response body: {e}")))?;

    if status == 204 {
        return Ok(Value::Null);
    }
    let json: Value = serde_json::from_str(&text).unwrap_or(Value::Null);

    if status == 401 {
        return Err(ToolError::Http(
            "Spotify auth failed (401) — token was rejected; reconnect the Spotify account"
                .to_string(),
        ));
    }
    if !(200..300).contains(&status) {
        return Err(spotify_http_error(method, path, status, &json, &text));
    }
    Ok(json)
}

/// Map a non-2xx Spotify response to a helpful [`ToolError`]. `NO_ACTIVE_DEVICE`
/// gets the friendly message the spec requires.
fn spotify_http_error(
    _method: &str,
    _path: &str,
    status: u16,
    json: &Value,
    raw: &str,
) -> ToolError {
    let reason = json
        .pointer("/error/reason")
        .and_then(Value::as_str)
        .unwrap_or("");
    let message = json
        .pointer("/error/message")
        .and_then(Value::as_str)
        .unwrap_or("");
    if reason == "NO_ACTIVE_DEVICE" || message.contains("No active device") {
        return ToolError::Other(
            "no active Spotify device — open Spotify on a device first".to_string(),
        );
    }
    let detail = if !message.is_empty() {
        message.to_string()
    } else {
        raw.chars().take(200).collect()
    };
    ToolError::Http(format!("Spotify API {status}: {detail}"))
}

/// Names of artists in a Spotify `artists` array.
fn artist_names(item: &Value) -> Vec<String> {
    item.get("artists")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|x| x.get("name").and_then(Value::as_str))
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

/// `spotify_search` — search the Spotify catalog.
pub struct SpotifySearch {
    conn: Shared,
}

#[async_trait]
impl Tool for SpotifySearch {
    fn name(&self) -> &str {
        "spotify_search"
    }

    fn description(&self) -> &str {
        "Search Spotify for tracks, albums, artists, or playlists. \
         Returns name, artists, URI and link for each hit."
    }

    fn schema(&self) -> Value {
        let mut s = schema_of(
            &[
                ("query", "string", "Search text (song, artist, album …)"),
                (
                    "kind",
                    "string",
                    "What to search: track, album, artist, playlist (default track)",
                ),
                ("limit", "integer", "Max results, 1-20 (default 5)"),
            ],
            &["query"],
        );
        // Extend the generated schema with enum + range info.
        if let Some(props) = s.get_mut("properties") {
            if let Some(kind) = props.get_mut("kind") {
                kind["enum"] = serde_json::json!(VALID_KINDS);
            }
            if let Some(limit) = props.get_mut("limit") {
                limit["minimum"] = serde_json::json!(1);
                limit["maximum"] = serde_json::json!(20);
            }
        }
        s
    }

    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let query = req_str(&args, "query")?;
        let kind = opt_str(&args, "kind", "track");
        if !VALID_KINDS.contains(&kind) {
            return Err(ToolError::InvalidArgs(format!(
                "kind must be one of {}",
                VALID_KINDS.join(", ")
            )));
        }
        let limit = opt_u64(&args, "limit", 5).clamp(1, 20);
        let json = api_call(
            &self.conn,
            "GET",
            "/v1/search",
            &[
                ("q", &query),
                ("type", kind),
                ("limit", &limit.to_string()),
            ],
            None,
        )
        .await?;
        Ok(serde_json::json!({
            "kind": kind,
            "results": parse_search_results(kind, &json),
        }))
    }
}

/// Normalize `/v1/search` response items into `{name, artists, uri, url}`.
fn parse_search_results(kind: &str, body: &Value) -> Vec<Value> {
    body.pointer(&format!("/{kind}s/items"))
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .map(|it| {
                    serde_json::json!({
                        "name": it.get("name").and_then(Value::as_str).unwrap_or(""),
                        "artists": artist_names(it),
                        "uri": it.get("uri").and_then(Value::as_str).unwrap_or(""),
                        "url": it.pointer("/external_urls/spotify")
                                 .and_then(Value::as_str).unwrap_or(""),
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

/// `spotify_current` — what is playing right now.
pub struct SpotifyCurrent {
    conn: Shared,
}

#[async_trait]
impl Tool for SpotifyCurrent {
    fn name(&self) -> &str {
        "spotify_current"
    }

    fn description(&self) -> &str {
        "Get the currently playing Spotify track, or report that nothing is playing."
    }

    fn schema(&self) -> Value {
        schema_of(&[], &[])
    }

    async fn execute(&self, _args: Value) -> Result<Value, ToolError> {
        let json = api_call(&self.conn, "GET", "/v1/me/player/currently-playing", &[], None).await?;
        Ok(parse_current(&json))
    }
}

/// Normalize `/v1/me/player/currently-playing` into `{playing, track, …}`.
/// `204 No Content` arrives as `Value::Null`.
fn parse_current(body: &Value) -> Value {
    if body.is_null() {
        return serde_json::json!({ "playing": false });
    }
    let item = body.get("item");
    let playing = body.get("is_playing").and_then(Value::as_bool).unwrap_or(false);
    match (playing, item) {
        (true, Some(it)) => serde_json::json!({
            "playing": true,
            "track": it.get("name").and_then(Value::as_str).unwrap_or(""),
            "artists": artist_names(it),
            "progress_ms": body.get("progress_ms").and_then(Value::as_u64).unwrap_or(0),
        }),
        _ => serde_json::json!({ "playing": false }),
    }
}

/// `spotify_play` — start/resume playback.
pub struct SpotifyPlay {
    conn: Shared,
}

#[async_trait]
impl Tool for SpotifyPlay {
    fn name(&self) -> &str {
        "spotify_play"
    }

    fn description(&self) -> &str {
        "Start or resume Spotify playback. Pass a track/album/playlist URI to \
         play something specific, and optionally a device_id to target a device."
    }

    fn schema(&self) -> Value {
        schema_of(
            &[
                (
                    "uri",
                    "string",
                    "Spotify URI to play (e.g. spotify:track:…); omitted = resume current",
                ),
                ("device_id", "string", "Target device id (omit = active device)"),
            ],
            &[],
        )
    }

    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let uri = args.get("uri").and_then(Value::as_str).filter(|s| !s.is_empty());
        let device_id = args
            .get("device_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty());

        // Fail fast with the honest no-device error instead of a raw 404.
        let devices = api_call(&self.conn, "GET", "/v1/me/player/devices", &[], None).await?;
        let active = devices
            .get("devices")
            .and_then(Value::as_array)
            .and_then(|ds| {
                ds.iter()
                    .find(|d| d.get("is_active").and_then(Value::as_bool).unwrap_or(false))
                    .map(|d| d.get("name").and_then(Value::as_str).unwrap_or("").to_string())
            });
        if active.is_none() && device_id.is_none() {
            return Err(ToolError::Other(
                "no active Spotify device — open Spotify on a device first".to_string(),
            ));
        }

        let mut query: Vec<(&str, &str)> = Vec::new();
        if let Some(id) = device_id {
            query.push(("device_id", id));
        }
        let body = uri.map(|u| serde_json::json!({ "uris": [u] }));
        api_call(&self.conn, "PUT", "/v1/me/player/play", &query, body).await?;

        let mut out = serde_json::Map::new();
        out.insert("ok".to_string(), Value::Bool(true));
        if let Some(name) = active {
            out.insert("device".to_string(), Value::String(name));
        }
        Ok(Value::Object(out))
    }
}

/// `spotify_pause` — pause playback.
pub struct SpotifyPause {
    conn: Shared,
}

#[async_trait]
impl Tool for SpotifyPause {
    fn name(&self) -> &str {
        "spotify_pause"
    }

    fn description(&self) -> &str {
        "Pause Spotify playback on the active device."
    }

    fn schema(&self) -> Value {
        schema_of(&[], &[])
    }

    async fn execute(&self, _args: Value) -> Result<Value, ToolError> {
        api_call(&self.conn, "PUT", "/v1/me/player/pause", &[], None).await?;
        Ok(serde_json::json!({ "ok": true }))
    }
}

// ---------------------------------------------------------------------------
// Tests (offline)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    /// Env vars are process-global: serialize every test that mutates them.

    /// Isolate the token file: a fresh temp dir per test.
    fn temp_home() -> String {
        let dir = std::env::temp_dir().join(format!(
            "boom-spotify-test-{}-{}",
            std::process::id(),
            now_unix()
        ));
        let _ = std::fs::create_dir_all(&dir);
        dir.to_string_lossy().to_string()
    }

    #[tokio::test]
    async fn connect_url_shape() {
        let _env_guard = crate::connectors::TEST_ENV_LOCK.lock().await;
        let home = temp_home();
        std::env::set_var("BOOM_HOME", &home);
        std::env::set_var("BOOM_SPOTIFY_CLIENT_ID", "abc123");
        std::env::set_var("BOOM_SPOTIFY_CLIENT_SECRET", "shh");
        let c = SpotifyConnector::new();
        let url = c.connect_url().expect("url");
        assert!(url.starts_with("https://accounts.spotify.com/authorize?"));
        assert!(url.contains("client_id=abc123"));
        assert!(url.contains("response_type=code"));
        assert!(url.contains("redirect_uri=http%3A%2F%2Flocalhost%3A18080"));
        assert!(url.contains("scope=user-read-playback-state"));
        assert!(!url.contains(' '));
        std::env::remove_var("BOOM_SPOTIFY_CLIENT_ID");
        std::env::remove_var("BOOM_SPOTIFY_CLIENT_SECRET");
    }

    #[tokio::test]
    async fn connect_url_without_creds_is_honest_error() {
        let _env_guard = crate::connectors::TEST_ENV_LOCK.lock().await;
        let home = temp_home();
        std::env::set_var("BOOM_HOME", &home);
        std::env::remove_var("BOOM_SPOTIFY_CLIENT_ID");
        std::env::remove_var("BOOM_SPOTIFY_CLIENT_SECRET");
        let c = SpotifyConnector::new();
        let err = c.connect_url().expect_err("must fail without creds");
        assert!(err.contains("BOOM_SPOTIFY_CLIENT_ID"));
    }

    #[tokio::test]
    async fn token_file_roundtrip_and_permissions() {
        let _env_guard = crate::connectors::TEST_ENV_LOCK.lock().await;
        let home = temp_home();
        std::env::set_var("BOOM_HOME", &home);
        let tok = SpotifyToken {
            access_token: "a".into(),
            refresh_token: Some("r".into()),
            expires_at: 9_999_999_999,
            extra: None,
        };
        save_token(CONNECTOR_ID, &tok).expect("save");
        let back = load_token(CONNECTOR_ID).expect("load").expect("present");
        assert_eq!(back.access_token, "a");
        assert_eq!(back.refresh_token.as_deref(), Some("r"));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(token_path(CONNECTOR_ID))
                .expect("meta")
                .permissions()
                .mode()
                & 0o777;
            assert_eq!(mode, 0o600, "token file must be 0600");
        }
        delete_token(CONNECTOR_ID).expect("delete");
        assert!(load_token(CONNECTOR_ID).expect("load").is_none());
    }

    #[test]
    fn schemas_are_valid() {
        for t in [
            &SpotifySearch { conn: dummy() } as &dyn Tool,
            &SpotifyCurrent { conn: dummy() },
            &SpotifyPlay { conn: dummy() },
            &SpotifyPause { conn: dummy() },
        ] {
            let s = t.schema();
            assert_eq!(s["type"].as_str(), Some("object"), "{}", t.name());
            assert!(s["properties"].is_object(), "{}", t.name());
            assert!(!t.name().is_empty() && !t.description().is_empty());
        }
        let search_schema = SpotifySearch { conn: dummy() }.schema();
        assert!(search_schema["required"]
            .as_array()
            .map(|r| r.contains(&serde_json::json!("query")))
            .unwrap_or(false));
        let kinds = search_schema["properties"]["kind"]["enum"]
            .as_array()
            .expect("kind enum");
        assert!(kinds.contains(&serde_json::json!("track")));
    }

    #[test]
    fn invalid_kind_is_rejected() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let tool = SpotifySearch { conn: dummy() };
        let err = rt
            .block_on(tool.execute(serde_json::json!({"query": "x", "kind": "podcast"})))
            .expect_err("bad kind must fail");
        assert!(matches!(err, ToolError::InvalidArgs(_)));
    }

    #[test]
    fn parse_search_fixture() {
        let body: Value = serde_json::json!({
            "tracks": { "items": [
                {
                    "name": "Blue Sky",
                    "artists": [{"name": "Alice"}, {"name": "Bob"}],
                    "uri": "spotify:track:111",
                    "external_urls": {"spotify": "https://open.spotify.com/track/111"}
                }
            ]}
        });
        let out = parse_search_results("track", &body);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0]["name"].as_str(), Some("Blue Sky"));
        assert_eq!(out[0]["artists"], serde_json::json!(["Alice", "Bob"]));
        assert_eq!(out[0]["uri"].as_str(), Some("spotify:track:111"));
        assert!(out[0]["url"].as_str().unwrap().contains("open.spotify.com"));
        // Unknown/missing items key degrades to empty, not panic.
        assert!(parse_search_results("track", &serde_json::json!({})).is_empty());
    }

    #[test]
    fn parse_current_fixtures() {
        // 204 → null body
        assert_eq!(parse_current(&Value::Null)["playing"], Value::Bool(false));
        // Playing with item
        let playing: Value = serde_json::json!({
            "is_playing": true,
            "progress_ms": 42000,
            "item": {"name": "Night", "artists": [{"name": "Zed"}]}
        });
        let out = parse_current(&playing);
        assert_eq!(out["playing"], Value::Bool(true));
        assert_eq!(out["track"].as_str(), Some("Night"));
        assert_eq!(out["artists"], serde_json::json!(["Zed"]));
        assert_eq!(out["progress_ms"].as_u64(), Some(42000));
        // Paused → honest false
        let paused: Value = serde_json::json!({"is_playing": false, "item": null});
        assert_eq!(parse_current(&paused)["playing"], Value::Bool(false));
    }

    #[test]
    fn no_device_error_message_is_friendly() {
        let err = spotify_http_error(
            "PUT",
            "/v1/me/player/play",
            404,
            &serde_json::json!({
                "error": {
                    "status": 404,
                    "message": "Player command failed: No active device found",
                    "reason": "NO_ACTIVE_DEVICE"
                }
            }),
            "",
        );
        let msg = err.to_string();
        assert!(
            msg.contains("no active Spotify device") && msg.contains("open Spotify on a device first"),
            "unexpected: {msg}"
        );
    }

    #[allow(clippy::await_holding_lock)]
    #[tokio::test]
    async fn tools_honestly_degrade_when_disconnected() {
        let _env_guard = crate::connectors::TEST_ENV_LOCK.lock().await;
        let home = temp_home();
        std::env::set_var("BOOM_HOME", &home);
        let shared = Arc::new(Mutex::new(SpotifyConnector::new()));
        assert!(!shared.lock().await.is_connected());

        let search = SpotifySearch { conn: shared.clone() };
        let err = search
            .execute(serde_json::json!({"query": "hello"}))
            .await
            .expect_err("must fail when not connected");
        assert!(
            err.to_string().contains("not connected"),
            "unexpected: {err}"
        );

        let current = SpotifyCurrent { conn: shared };
        let err = current
            .execute(serde_json::json!({}))
            .await
            .expect_err("must fail when not connected");
        assert!(err.to_string().contains("not connected"));
    }

    #[test]
    fn debug_redacts_tokens() {
        let c = SpotifyConnector::with_token(Some(SpotifyToken {
            access_token: "super-secret-access".into(),
            refresh_token: Some("super-secret-refresh".into()),
            expires_at: 9_999_999_999,
            extra: None,
        }));
        let dbg = format!("{c:?}");
        assert!(dbg.contains("connected"), "{dbg}");
        assert!(!dbg.contains("super-secret-access"), "{dbg}");
        assert!(!dbg.contains("super-secret-refresh"), "{dbg}");
    }

    fn dummy() -> Shared {
        Arc::new(Mutex::new(SpotifyConnector::with_token(None)))
    }
}
