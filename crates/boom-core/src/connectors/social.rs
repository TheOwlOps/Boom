//! Social connector — one connector for Facebook, Instagram and Threads,
//! all through the Meta Graph API.
//!
//! Target path: `crates/boom-core/src/connectors/social.rs`
//! (registered from `crates/boom-core/src/connectors/mod.rs` by the coordinator).
//!
//! # Auth
//! Standard Meta OAuth2 authorization-code flow with manual paste (no local
//! callback server in the core yet):
//!
//! 1. Create an app at <https://developers.facebook.com/apps>, add the
//!    **Facebook Login** product, and put the redirect URI (default
//!    `https://www.facebook.com/connect/login_success.html`, overridable via
//!    `BOOM_META_REDIRECT_URI`) into *Valid OAuth Redirect URIs*.
//! 2. Set `BOOM_META_CLIENT_ID` / `BOOM_META_CLIENT_SECRET` from the app
//!    dashboard (App ID / App Secret).
//! 3. Open the URL from [`Connector::connect_url`], authorize, and copy the
//!    `code` out of the redirected URL.
//! 4. Boom calls [`Connector::exchange_code`] with that code. The short-lived
//!    token is automatically upgraded to a 60-day long-lived token whenever
//!    the client secret is available.
//!
//! **Threads** uses its own OAuth flow (different authorize/token hosts and
//! scopes). Build that URL with [`build_threads_connect_url`] (see REPORT.md),
//! complete it in the browser, then exchange the resulting code with the same
//! `exchange_code` — the connector tries the Facebook token endpoint first
//! and the Threads endpoint second, so the right provider is picked
//! automatically (`BOOM_META_PROVIDER=threads` forces Threads-first).
//!
//! The token is stored at `$BOOM_HOME/connectors/social.json` (mode 0600)
//! via the shared `super::` helpers. `extra` carries
//! `{provider, user_id, name, client_id, obtained_at}`. Tokens are never
//! logged; the shared [`ConnectorToken`]'s `Debug` redacts the secret
//! (asserted in the tests below).
//!
//! A transient `BOOM_META_ACCESS_TOKEN` env var (plus optional
//! `BOOM_META_PROVIDER` / `BOOM_META_USER_ID`) is honoured when no token file
//! exists — useful for headless setups. It is never persisted.
//!
//! # Scope (deliberately narrow)
//! The three tools only ever touch **your own** account and Pages: read your
//! profile, read your own recent posts, publish as you / your Page. They
//! cannot read anyone else's feed, search users, or manage comments.
//!
//! # Assumptions about `super`
//! (the framework worker owns `connectors/mod.rs`):
//! * `super::{Connector, ConnectorToken, load_token, save_token, delete_token}`
//!   exist with the signatures from the shared contract.
//! * `ConnectorToken { access_token: String, refresh_token: Option<String>,
//!   expires_at: Option<i64>, extra: serde_json::Value }`.
//! * `load_token` / `save_token` honour `$BOOM_HOME` (env) for the file dir,
//!   and `load_token` returns `Ok(None)` when the file is absent.

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use serde_json::Value;

use super::{delete_token, load_token, save_token, Connector, ConnectorToken};
use crate::tools::http::client;
use crate::tools::{opt_u64, req_str, schema_of, ssrf_check_url, Tool, ToolError};

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/// Connector id: token file name under `$BOOM_HOME/connectors/`.
const CONNECTOR_ID: &str = "social";

const FB_GRAPH: &str = "https://graph.facebook.com/v18.0";
const FB_OAUTH_DIALOG: &str = "https://www.facebook.com/v18.0/dialog/oauth";
const FB_TOKEN_URL: &str = "https://graph.facebook.com/v18.0/oauth/access_token";
const THREADS_GRAPH: &str = "https://graph.threads.net/v1.0";
#[allow(dead_code)]
const THREADS_OAUTH_DIALOG: &str = "https://www.threads.net/oauth/authorize";
const THREADS_TOKEN_URL: &str = "https://graph.threads.net/oauth/access_token";
const THREADS_LONG_LIVED_URL: &str = "https://graph.threads.net/access_token";

/// Scopes requested by the Facebook/Instagram OAuth URL.
const SCOPES: &str =
    "pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish";
/// Scopes for the Threads OAuth URL (separate flow — see REPORT.md).
#[allow(dead_code)]
const THREADS_SCOPES: &str = "threads_basic,threads_content_publish";

/// Every Meta HTTP call (OAuth + Graph) gets this deadline.
const HTTP_TIMEOUT: Duration = Duration::from_secs(30);

/// Honest-degrade message returned by every tool while not connected.
const NOT_CONNECTED_MSG: &str = "social is not connected — open Settings → Connectors to connect";

/// Threads text posts are limited to 500 characters by the API.
const THREADS_MAX_CHARS: usize = 500;

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

fn now_secs() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// Minimal percent-encoder for OAuth query/form values (no extra deps).
fn urlencode(s: &str) -> String {
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

/// Which Meta network a tool call targets.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SocialProvider {
    Facebook,
    Instagram,
    Threads,
}

impl SocialProvider {
    fn from_str(s: &str) -> Result<Self, ToolError> {
        match s.to_ascii_lowercase().as_str() {
            "facebook" | "fb" => Ok(Self::Facebook),
            "instagram" | "ig" => Ok(Self::Instagram),
            "threads" => Ok(Self::Threads),
            other => Err(ToolError::InvalidArgs(format!(
                "unknown provider '{other}' — expected \"facebook\", \"instagram\" or \"threads\""
            ))),
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::Facebook => "facebook",
            Self::Instagram => "instagram",
            Self::Threads => "threads",
        }
    }
}

/// Meta App ID: env first, then the `extra.client_id` saved by a previous
/// exchange (keeps `connect_url` working if the env var is later removed).
fn client_id() -> Option<String> {
    if let Ok(v) = std::env::var("BOOM_META_CLIENT_ID") {
        if !v.trim().is_empty() {
            return Some(v);
        }
    }
    load_token(CONNECTOR_ID)
        .and_then(|t| {
            t.extra
                .get("client_id")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
}

/// Meta App Secret: env only — never persisted to disk.
fn client_secret() -> Option<String> {
    std::env::var("BOOM_META_CLIENT_SECRET")
        .ok()
        .filter(|v| !v.trim().is_empty())
}

/// Redirect URI registered in the Meta app ("Valid OAuth Redirect URIs").
/// The default shows the authorization code in the URL bar for manual copy.
fn redirect_uri() -> String {
    std::env::var("BOOM_META_REDIRECT_URI")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .unwrap_or_else(|| "https://www.facebook.com/connect/login_success.html".to_string())
}

/// Pure constructor for the Facebook/Instagram OAuth consent URL.
pub(crate) fn build_connect_url(client_id: &str, redirect: &str) -> String {
    format!(
        "{FB_OAUTH_DIALOG}?client_id={}&redirect_uri={}&response_type=code&scope={}",
        urlencode(client_id),
        urlencode(redirect),
        urlencode(SCOPES),
    )
}

/// Pure constructor for the Threads OAuth consent URL (separate flow —
/// different host, scopes and token endpoint).
#[allow(dead_code)]
pub(crate) fn build_threads_connect_url(client_id: &str, redirect: &str) -> String {
    format!(
        "{THREADS_OAUTH_DIALOG}?client_id={}&redirect_uri={}&response_type=code&scope={}",
        urlencode(client_id),
        urlencode(redirect),
        urlencode(THREADS_SCOPES),
    )
}

/// Loaded token; honest-degrade when absent. A transient
/// `BOOM_META_ACCESS_TOKEN` env var is honoured when no token file exists.
fn stored_token() -> Result<ConnectorToken, ToolError> {
    if let Some(tok) = load_token(CONNECTOR_ID) {
        if !tok.access_token.trim().is_empty() {
            return Ok(tok);
        }
    }
    if let Ok(v) = std::env::var("BOOM_META_ACCESS_TOKEN") {
        if !v.trim().is_empty() {
            let provider =
                std::env::var("BOOM_META_PROVIDER").unwrap_or_else(|_| "facebook".to_string());
            let user_id = std::env::var("BOOM_META_USER_ID").unwrap_or_default();
            return Ok(ConnectorToken {
                access_token: v,
                refresh_token: None,
                expires_at: None,
                extra: serde_json::json!({
                    "provider": provider,
                    "user_id": user_id,
                    "transient": true,
                }),
            });
        }
    }
    Err(ToolError::Other(NOT_CONNECTED_MSG.to_string()))
}

fn token_user_id(tok: &ConnectorToken) -> Result<String, ToolError> {
    tok.extra
        .get("user_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .ok_or_else(|| {
            ToolError::Other("social token has no user id stored — reconnect the connector".to_string())
        })
}

/// Clamp the `limit` arg into the documented 1–25 range.
fn normalize_limit(v: u64) -> u64 {
    v.clamp(1, 25)
}

// ---------------------------------------------------------------------------
// Graph API plumbing
// ---------------------------------------------------------------------------

/// Meta's error envelope → honest ToolError. Code 190 (bad/expired token)
/// points at reconnecting instead of retrying. Never includes the token.
fn meta_http_error(status: u16, json: &Value) -> ToolError {
    let err = json.get("error");
    let message = err
        .and_then(|e| e.get("message"))
        .and_then(Value::as_str)
        .unwrap_or("unknown Meta API error");
    let code = err.and_then(|e| e.get("code")).and_then(Value::as_i64);
    let code_str = code.map(|c| format!(", code {c}")).unwrap_or_default();
    if code == Some(190) {
        return ToolError::Other(format!(
            "Meta: invalid or expired access token — reconnect the Social connector (Settings → Connectors). Detail: {message}"
        ));
    }
    ToolError::Http(format!(
        "Meta API error (HTTP {status}{code_str}): {message}"
    ))
}

/// OAuth token-endpoint error envelope → String.
fn oauth_error(kind: &str, json: &Value) -> String {
    let desc = json
        .get("error_description")
        .or_else(|| json.get("error").and_then(|e| e.get("message")))
        .or_else(|| json.get("error"))
        .and_then(Value::as_str)
        .unwrap_or("unknown OAuth error");
    format!("Meta {kind} OAuth failed: {desc}")
}

async fn parse_graph_response(resp: reqwest::Response) -> Result<Value, ToolError> {
    let status = resp.status().as_u16();
    let text = resp
        .text()
        .await
        .map_err(|e| ToolError::Http(format!("reading Meta response failed: {e}")))?;
    let json: Value = serde_json::from_str(&text).map_err(|e| {
        let preview: String = text.chars().take(200).collect();
        ToolError::Http(format!("Meta API returned non-JSON ({e}): {preview}"))
    })?;
    if !(200..300).contains(&status) {
        return Err(meta_http_error(status, &json));
    }
    Ok(json)
}

/// GET `base + path` with query params + access token. SSRF-guarded (same
/// guard as the other tools), 30s timeout.
async fn graph_get(
    base: &str,
    path: &str,
    params: &[(&str, &str)],
    token: &str,
) -> Result<Value, ToolError> {
    let mut all: Vec<(String, String)> = params
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();
    all.push(("access_token".to_string(), token.to_string()));
    let query = all
        .iter()
        .map(|(k, v)| format!("{}={}", urlencode(k), urlencode(v)))
        .collect::<Vec<_>>()
        .join("&");
    let url = format!("{base}{path}?{query}");
    ssrf_check_url(&url).await?;
    let resp = client()
        .get(&url)
        .timeout(HTTP_TIMEOUT)
        .send()
        .await
        .map_err(|e| ToolError::Http(format!("Meta API request failed: {e}")))?;
    parse_graph_response(resp).await
}

/// POST form-encoded `base + path`, access token as a query param.
async fn graph_post(
    base: &str,
    path: &str,
    form: &[(&str, &str)],
    token: &str,
) -> Result<Value, ToolError> {
    let url = format!("{base}{path}?access_token={}", urlencode(token));
    ssrf_check_url(&url).await?;
    let body = form
        .iter()
        .map(|(k, v)| format!("{}={}", urlencode(k), urlencode(v)))
        .collect::<Vec<_>>()
        .join("&");
    let resp = client()
        .post(&url)
        .timeout(HTTP_TIMEOUT)
        .header("content-type", "application/x-www-form-urlencoded")
        .body(body)
        .send()
        .await
        .map_err(|e| ToolError::Http(format!("Meta API request failed: {e}")))?;
    parse_graph_response(resp).await
}

// ---------------------------------------------------------------------------
// Pure response parsers (unit-testable, no network)
// ---------------------------------------------------------------------------

/// (id, display name) from a profile response. Threads uses `username`;
/// Facebook/Instagram prefer `name` with a `username` fallback.
fn parse_profile(_provider: SocialProvider, v: &Value) -> Result<(String, String), ToolError> {
    let id = v
        .get("id")
        .and_then(Value::as_str)
        .ok_or_else(|| ToolError::Http("profile response has no 'id'".to_string()))?;
    let name = v
        .get("name")
        .or_else(|| v.get("username"))
        .and_then(Value::as_str)
        .unwrap_or("");
    Ok((id.to_string(), name.to_string()))
}

#[derive(Debug)]
struct PostView {
    id: String,
    text: String,
    created: String,
    permalink: String,
}

/// Normalize a posts/media list into a uniform view. Items without an `id`
/// are skipped; missing text/date/link become empty strings (never an error).
fn parse_post_list(provider: SocialProvider, v: &Value) -> Result<Vec<PostView>, ToolError> {
    let data = v
        .get("data")
        .and_then(Value::as_array)
        .ok_or_else(|| ToolError::Http("posts response has no 'data' array".to_string()))?;
    let mut out = Vec::with_capacity(data.len());
    for item in data {
        let id = item
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if id.is_empty() {
            continue;
        }
        let (text, created, permalink) = match provider {
            SocialProvider::Facebook => (
                item.get("message").and_then(Value::as_str).unwrap_or(""),
                item
                    .get("created_time")
                    .and_then(Value::as_str)
                    .unwrap_or(""),
                item
                    .get("permalink_url")
                    .and_then(Value::as_str)
                    .unwrap_or(""),
            ),
            SocialProvider::Instagram => (
                item.get("caption").and_then(Value::as_str).unwrap_or(""),
                item.get("timestamp").and_then(Value::as_str).unwrap_or(""),
                item.get("permalink").and_then(Value::as_str).unwrap_or(""),
            ),
            SocialProvider::Threads => (
                item.get("text").and_then(Value::as_str).unwrap_or(""),
                item.get("timestamp").and_then(Value::as_str).unwrap_or(""),
                item.get("permalink").and_then(Value::as_str).unwrap_or(""),
            ),
        };
        out.push(PostView {
            id,
            text: text.to_string(),
            created: created.to_string(),
            permalink: permalink.to_string(),
        });
    }
    Ok(out)
}

struct PageInfo {
    id: String,
    name: String,
    /// Page access token from `/me/accounts` — required for publishing as
    /// the Page (Meta rejects a bare user token on `/{page-id}/feed`).
    access_token: Option<String>,
}

fn parse_pages(v: &Value) -> Result<Vec<PageInfo>, ToolError> {
    let data = v
        .get("data")
        .and_then(Value::as_array)
        .ok_or_else(|| ToolError::Http("pages response has no 'data' array".to_string()))?;
    let mut out = Vec::with_capacity(data.len());
    for p in data {
        if let Some(id) = p.get("id").and_then(Value::as_str) {
            out.push(PageInfo {
                id: id.to_string(),
                name: p
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                access_token: p
                    .get("access_token")
                    .and_then(Value::as_str)
                    .map(str::to_string),
            });
        }
    }
    Ok(out)
}

/// (ig_user_id, username) pairs linked to the user's Pages.
fn parse_linked_ig(v: &Value) -> Vec<(String, String)> {
    let mut out = Vec::new();
    if let Some(pages) = v.get("data").and_then(Value::as_array) {
        for p in pages {
            if let Some(ig) = p.get("instagram_business_account") {
                if let Some(id) = ig.get("id").and_then(Value::as_str) {
                    let username = ig
                        .get("username")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string();
                    out.push((id.to_string(), username));
                }
            }
        }
    }
    out
}

/// Pick a Page id: an explicit hint wins (and must match); a single page is
/// auto-picked; anything else is an honest, actionable error — never a silent
/// guess. `purpose` reads like "post as" / "read posts from".
fn select_page(
    pages: &[PageInfo],
    hint: Option<&str>,
    purpose: &str,
) -> Result<String, ToolError> {
    if let Some(h) = hint.map(str::trim).filter(|s| !s.is_empty()) {
        return pages
            .iter()
            .find(|p| p.id == h)
            .map(|p| p.id.clone())
            .ok_or_else(|| {
                ToolError::InvalidArgs(format!(
                    "page_id '{h}' is not among your Pages ({})",
                    pages
                        .iter()
                        .map(|p| format!("{} ({})", p.name, p.id))
                        .collect::<Vec<_>>()
                        .join(", ")
                ))
            });
    }
    match pages {
        [] => Err(ToolError::Other(
            "no Facebook Pages found for this account — create a Page, or grant the \
             'pages_manage_posts' / 'pages_read_engagement' permissions, then reconnect"
                .to_string(),
        )),
        [p] => Ok(p.id.clone()),
        _ => Err(ToolError::Other(format!(
            "multiple Facebook Pages found ({}); pass \"page_id\" to choose which one to {purpose}",
            pages
                .iter()
                .map(|p| format!("{} ({})", p.name, p.id))
                .collect::<Vec<_>>()
                .join(", ")
        ))),
    }
}

// ---------------------------------------------------------------------------
// Page / Instagram account resolution
// ---------------------------------------------------------------------------

/// (page_id, page_access_token) for publishing. Meta requires the Page's own
/// access token on `/{page-id}/feed` — a bare user token is rejected — so we
/// always look the page up in `/me/accounts` to get its token.
async fn resolve_page_for_post(
    tok: &ConnectorToken,
    hint: Option<&str>,
) -> Result<(String, String), ToolError> {
    let hint = hint
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .or_else(|| {
            tok.extra
                .get("page_id")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_string)
        })
        .or_else(|| {
            std::env::var("BOOM_META_PAGE_ID")
                .ok()
                .filter(|v| !v.trim().is_empty())
        });
    let v = graph_get(
        FB_GRAPH,
        "/me/accounts",
        &[("fields", "id,name,access_token"), ("limit", "25")],
        &tok.access_token,
    )
    .await?;
    let pages = parse_pages(&v)?;
    let id = select_page(&pages, hint.as_deref(), "post as")?;
    let page = pages
        .iter()
        .find(|p| p.id == id)
        .expect("selected page comes from the same list");
    // Fall back to the user token if Meta omitted the page token; the API
    // will then fail honestly with a clear permission error.
    let page_token = page
        .access_token
        .clone()
        .filter(|t| !t.is_empty())
        .unwrap_or_else(|| tok.access_token.clone());
    Ok((id, page_token))
}

/// Instagram Business/Creator account id: explicit arg → stored extra →
/// env → auto-discover from the Pages' linked accounts.
async fn resolve_ig_user_id(
    tok: &ConnectorToken,
    hint: Option<&str>,
) -> Result<String, ToolError> {
    if let Some(h) = hint.map(str::trim).filter(|s| !s.is_empty()) {
        return Ok(h.to_string());
    }
    if let Some(id) = tok
        .extra
        .get("ig_user_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        return Ok(id.to_string());
    }
    if let Ok(id) = std::env::var("BOOM_META_IG_USER_ID") {
        if !id.trim().is_empty() {
            return Ok(id);
        }
    }
    let v = graph_get(
        FB_GRAPH,
        "/me/accounts",
        &[
            ("fields", "instagram_business_account{id,username},name"),
            ("limit", "25"),
        ],
        &tok.access_token,
    )
    .await?;
    let linked = parse_linked_ig(&v);
    match linked.first() {
        Some((id, _)) => Ok(id.clone()),
        None => Err(ToolError::Other(
            "no Instagram Business/Creator account is linked to your Facebook Pages — \
             link one at business.facebook.com, or pass \"ig_user_id\" / set BOOM_META_IG_USER_ID"
                .to_string(),
        )),
    }
}

/// Where Facebook reads come from: a Page when one is configured or
/// unambiguous, otherwise the user's own profile posts (`/{user-id}/posts` —
/// note this needs the `user_posts` permission, which requires Meta app
/// review; the API error says so honestly when it is missing).
enum FacebookReadTarget {
    Page(String),
    Me,
}

async fn facebook_read_target(tok: &ConnectorToken) -> Result<FacebookReadTarget, ToolError> {
    let hint = tok
        .extra
        .get("page_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .or_else(|| {
            std::env::var("BOOM_META_PAGE_ID")
                .ok()
                .filter(|v| !v.trim().is_empty())
        });
    let v = graph_get(
        FB_GRAPH,
        "/me/accounts",
        &[("fields", "id,name"), ("limit", "25")],
        &tok.access_token,
    )
    .await?;
    let pages = parse_pages(&v)?;
    if pages.is_empty() && hint.is_none() {
        return Ok(FacebookReadTarget::Me);
    }
    select_page(&pages, hint.as_deref(), "read posts from")
        .map(FacebookReadTarget::Page)
}

// ---------------------------------------------------------------------------
// Connector
// ---------------------------------------------------------------------------

/// Social connector. Stateless — the token is reloaded from the token file on
/// every call so concurrent processes see fresh credentials.
#[derive(Debug, Default)]
pub struct SocialConnector;

impl SocialConnector {
    pub fn new() -> Self {
        Self
    }

    /// Exchange the authorization `code` at one token endpoint
    /// (`kind` = "facebook" | "threads"), upgrade to a 60-day long-lived
    /// token when the client secret is available, probe `/me` to verify, and
    /// return the token ready to store.
    async fn exchange_with(
        &self,
        kind: &str,
        code: &str,
        cid: &str,
        secret: Option<&str>,
        redirect: &str,
    ) -> Result<ConnectorToken, String> {
        let token_url = if kind == "threads" {
            THREADS_TOKEN_URL
        } else {
            FB_TOKEN_URL
        };
        let mut form: Vec<(&str, &str)> = vec![
            ("client_id", cid),
            ("code", code),
            ("redirect_uri", redirect),
        ];
        if kind == "threads" {
            // Threads requires grant_type=authorization_code in the body.
            form.push(("grant_type", "authorization_code"));
        }
        ssrf_check_url(token_url)
            .await
            .map_err(|e| e.to_string())?;
        let mut body = form
            .iter()
            .map(|(k, v)| format!("{}={}", urlencode(k), urlencode(v)))
            .collect::<Vec<_>>()
            .join("&");
        if let Some(s) = secret {
            body.push_str(&format!("&client_secret={}", urlencode(s)));
        }
        let resp = client()
            .post(token_url)
            .timeout(HTTP_TIMEOUT)
            .header("content-type", "application/x-www-form-urlencoded")
            .body(body)
            .send()
            .await
            .map_err(|e| format!("token exchange request failed: {e}"))?;
        let status = resp.status();
        let json: Value = resp
            .json()
            .await
            .map_err(|e| format!("token exchange: bad JSON: {e}"))?;
        if !status.is_success() {
            return Err(oauth_error(kind, &json));
        }
        let access = json
            .get("access_token")
            .and_then(Value::as_str)
            .ok_or_else(|| "token exchange: response has no access_token".to_string())?
            .to_string();
        let short_expires = json.get("expires_in").and_then(Value::as_i64);

        // Upgrade to a 60-day long-lived token when we have the client
        // secret; otherwise keep the short-lived one honestly.
        let (access, expires_at) = match self
            .upgrade_long_lived(kind, &access, cid, secret)
            .await
        {
            Ok((a, e)) => (a, e),
            Err(_) => (access, short_expires.map(|s| now_secs() + s)),
        };

        // Probe /me so we only store a verified token with a known user id.
        let (user_id, name) = self.probe_identity(kind, &access).await?;
        let provider_label = if kind == "threads" {
            "threads".to_string()
        } else {
            let hint = std::env::var("BOOM_META_PROVIDER")
                .unwrap_or_default()
                .to_ascii_lowercase();
            if hint == "instagram" {
                "instagram".to_string()
            } else {
                "facebook".to_string()
            }
        };
        Ok(ConnectorToken {
            access_token: access,
            refresh_token: None,
            expires_at,
            extra: serde_json::json!({
                "provider": provider_label,
                "user_id": user_id,
                "name": name,
                "client_id": cid,
                "obtained_at": now_secs(),
            }),
        })
    }

    /// Exchange a short-lived token for a 60-day long-lived one.
    /// Returns (access_token, expires_at). Requires the client secret.
    async fn upgrade_long_lived(
        &self,
        kind: &str,
        short: &str,
        cid: &str,
        secret: Option<&str>,
    ) -> Result<(String, Option<i64>), String> {
        let secret =
            secret.ok_or_else(|| "no client secret — skipping long-lived upgrade".to_string())?;
        let url = if kind == "threads" {
            format!(
                "{THREADS_LONG_LIVED_URL}?grant_type=th_exchange_token&client_secret={}&access_token={}",
                urlencode(secret),
                urlencode(short)
            )
        } else {
            format!(
                "{FB_TOKEN_URL}?grant_type=fb_exchange_token&client_id={}&client_secret={}&fb_exchange_token={}",
                urlencode(cid),
                urlencode(secret),
                urlencode(short)
            )
        };
        ssrf_check_url(&url).await.map_err(|e| e.to_string())?;
        let resp = client()
            .get(&url)
            .timeout(HTTP_TIMEOUT)
            .send()
            .await
            .map_err(|e| format!("long-lived exchange failed: {e}"))?;
        let status = resp.status();
        let json: Value = resp
            .json()
            .await
            .map_err(|e| format!("long-lived exchange: bad JSON: {e}"))?;
        if !status.is_success() {
            return Err(oauth_error(kind, &json));
        }
        let access = json
            .get("access_token")
            .and_then(Value::as_str)
            .ok_or_else(|| "long-lived exchange: no access_token".to_string())?
            .to_string();
        let expires_at = json
            .get("expires_in")
            .and_then(Value::as_i64)
            .map(|s| now_secs() + s);
        Ok((access, expires_at))
    }

    /// `GET /me` with the fresh token — proves the token works and learns
    /// the user id + display name. Fails honestly otherwise.
    async fn probe_identity(
        &self,
        kind: &str,
        access: &str,
    ) -> Result<(String, String), String> {
        let (base, fields) = if kind == "threads" {
            (THREADS_GRAPH, "id,username")
        } else {
            (FB_GRAPH, "id,name")
        };
        let url = format!("{base}/me?fields={fields}&access_token={}", urlencode(access));
        ssrf_check_url(&url).await.map_err(|e| e.to_string())?;
        let resp = client()
            .get(&url)
            .timeout(HTTP_TIMEOUT)
            .send()
            .await
            .map_err(|e| format!("identity probe failed: {e}"))?;
        let status = resp.status();
        let json: Value = resp
            .json()
            .await
            .map_err(|e| format!("identity probe: bad JSON: {e}"))?;
        if !status.is_success() {
            return Err(format!(
                "token obtained but the /me probe failed: {}",
                oauth_error(kind, &json)
            ));
        }
        let id = json
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if id.is_empty() {
            return Err(
                "token obtained but /me returned no id — check the token's permissions".to_string(),
            );
        }
        let name = json
            .get("name")
            .or_else(|| json.get("username"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        Ok((id, name))
    }
}

#[async_trait]
impl Connector for SocialConnector {
    fn id(&self) -> &'static str {
        CONNECTOR_ID
    }

    fn name(&self) -> &'static str {
        "Social (Meta)"
    }

    fn description(&self) -> &'static str {
        "Facebook, Instagram and Threads through the Meta Graph API (OAuth2). \
         Connect: set BOOM_META_CLIENT_ID / BOOM_META_CLIENT_SECRET from a Meta app \
         (developers.facebook.com, add Facebook Login), open the URL from connect_url(), \
         authorize, then exchange the code. For Threads, complete the Threads OAuth flow \
         instead and exchange that code — the provider is detected automatically. \
         Tools only ever touch YOUR OWN account and Pages: read your profile, read your \
         own recent posts, publish as you / your Page. They cannot read anyone else's \
         feed. Facebook publishes to one of your Pages (page access token resolved \
         automatically). Instagram needs an image (media_url) and a linked \
         Business/Creator account — text-only Instagram posts are impossible via the API."
    }

    fn auth_kind(&self) -> &'static str {
        "oauth2"
    }

    fn is_connected(&self) -> bool {
        match load_token(CONNECTOR_ID) {
            Some(t) => {
                if t.access_token.trim().is_empty() {
                    return false;
                }
                match t.expires_at {
                    // An expired token is reported as disconnected so the UI
                    // offers reconnect instead of failing calls.
                    Some(exp) => now_secs() < exp - 60,
                    // No expiry info — the API 401 path reports it honestly.
                    None => true,
                }
            }
            _ => std::env::var("BOOM_META_ACCESS_TOKEN")
                .map(|v| !v.trim().is_empty())
                .unwrap_or(false),
        }
    }

    fn connect_url(&self) -> Result<String, String> {
        let cid = client_id().ok_or_else(|| {
            "Meta app not configured: set BOOM_META_CLIENT_ID and BOOM_META_CLIENT_SECRET \
             (create an app at https://developers.facebook.com/apps and add Facebook Login), \
             then retry. See the social connector docs."
                .to_string()
        })?;
        Ok(build_connect_url(&cid, &redirect_uri()))
    }

    async fn exchange_code(&mut self, code: &str) -> Result<(), String> {
        let code = code.trim();
        if code.is_empty() {
            return Err("authorization code is empty".to_string());
        }
        let cid = client_id().ok_or_else(|| {
            "Meta app not configured: set BOOM_META_CLIENT_ID and BOOM_META_CLIENT_SECRET \
             (create an app at https://developers.facebook.com/apps, add Facebook Login). \
             See the social connector docs."
                .to_string()
        })?;
        let secret = client_secret();
        let redirect = redirect_uri();
        let hint = std::env::var("BOOM_META_PROVIDER")
            .unwrap_or_default()
            .to_ascii_lowercase();
        // Threads codes only work at graph.threads.net; Facebook/Instagram
        // codes only at graph.facebook.com. Try in hint order.
        let order: &[&str] = if hint == "threads" {
            &["threads", "facebook"]
        } else {
            &["facebook", "threads"]
        };
        let mut last_err = String::new();
        for kind in order {
            match self
                .exchange_with(kind, code, &cid, secret.as_deref(), &redirect)
                .await
            {
                Ok(token) => {
                    save_token(CONNECTOR_ID, &token)
                        .map_err(|e| format!("failed saving social token: {e}"))?;
                    return Ok(());
                }
                Err(e) => last_err = e,
            }
        }
        Err(format!(
            "code exchange failed (tried the facebook and threads token endpoints): {last_err}"
        ))
    }

    fn disconnect(&mut self) -> Result<(), String> {
        delete_token(CONNECTOR_ID).map_err(|e| format!("failed deleting social token: {e}"))?;
        Ok(())
    }

    fn register_tools(&self, reg: &mut crate::tools::ToolRegistry) {
        reg.register("social", SocialProfile);
        reg.register("social", SocialRead);
        reg.register("social", SocialPost);
    }
}

// ---------------------------------------------------------------------------
// social_profile
// ---------------------------------------------------------------------------

pub struct SocialProfile;

#[async_trait]
impl Tool for SocialProfile {
    fn name(&self) -> &str {
        "social_profile"
    }
    fn description(&self) -> &str {
        "Read YOUR OWN Facebook/Instagram/Threads profile (id, name/username). \
         provider: \"facebook\" | \"instagram\" | \"threads\". \
         Only your own account — this tool cannot look up anyone else."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[(
                "provider",
                "string",
                "Which network: \"facebook\", \"instagram\" or \"threads\"",
            )],
            &["provider"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let provider = SocialProvider::from_str(&req_str(&args, "provider")?)?;
        let tok = stored_token()?;
        let (id, name) = match provider {
            SocialProvider::Facebook => {
                let v = graph_get(FB_GRAPH, "/me", &[("fields", "id,name")], &tok.access_token).await?;
                parse_profile(provider, &v)?
            }
            SocialProvider::Instagram => {
                let ig = resolve_ig_user_id(&tok, None).await?;
                let v = graph_get(
                    FB_GRAPH,
                    &format!("/{ig}"),
                    &[("fields", "id,username,name")],
                    &tok.access_token,
                )
                .await?;
                parse_profile(provider, &v)?
            }
            SocialProvider::Threads => {
                let v = graph_get(
                    THREADS_GRAPH,
                    "/me",
                    &[("fields", "id,username")],
                    &tok.access_token,
                )
                .await?;
                parse_profile(provider, &v)?
            }
        };
        Ok(serde_json::json!({"provider": provider.as_str(), "id": id, "name": name}))
    }
}

// ---------------------------------------------------------------------------
// social_read
// ---------------------------------------------------------------------------

pub struct SocialRead;

#[async_trait]
impl Tool for SocialRead {
    fn name(&self) -> &str {
        "social_read"
    }
    fn description(&self) -> &str {
        "Read YOUR OWN recent posts (your profile or your Facebook Page). \
         provider: \"facebook\" | \"instagram\" | \"threads\"; limit 1-25 (default 5). \
         Only your own content — other people's feeds are not accessible."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                (
                    "provider",
                    "string",
                    "Which network: \"facebook\", \"instagram\" or \"threads\"",
                ),
                (
                    "limit",
                    "integer",
                    "How many recent posts to return (1-25, default 5)",
                ),
            ],
            &["provider"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let provider = SocialProvider::from_str(&req_str(&args, "provider")?)?;
        let limit = normalize_limit(opt_u64(&args, "limit", 5));
        let tok = stored_token()?;
        let limit_s = limit.to_string();
        let posts = match provider {
            SocialProvider::Facebook => {
                let path = match facebook_read_target(&tok).await? {
                    FacebookReadTarget::Page(pid) => format!("/{pid}/posts"),
                    FacebookReadTarget::Me => {
                        let uid = token_user_id(&tok).unwrap_or_else(|_| "me".to_string());
                        format!("/{uid}/posts")
                    }
                };
                let v = graph_get(
                    FB_GRAPH,
                    &path,
                    &[
                        ("fields", "id,message,created_time,permalink_url"),
                        ("limit", limit_s.as_str()),
                    ],
                    &tok.access_token,
                )
                .await?;
                parse_post_list(provider, &v)?
            }
            SocialProvider::Instagram => {
                let ig = resolve_ig_user_id(&tok, None).await?;
                let v = graph_get(
                    FB_GRAPH,
                    &format!("/{ig}/media"),
                    &[
                        ("fields", "id,caption,media_type,timestamp,permalink"),
                        ("limit", limit_s.as_str()),
                    ],
                    &tok.access_token,
                )
                .await?;
                parse_post_list(provider, &v)?
            }
            SocialProvider::Threads => {
                let v = graph_get(
                    THREADS_GRAPH,
                    "/me/threads",
                    &[("fields", "id,text,timestamp,permalink"), ("limit", limit_s.as_str())],
                    &tok.access_token,
                )
                .await?;
                parse_post_list(provider, &v)?
            }
        };
        Ok(serde_json::json!({
            "provider": provider.as_str(),
            "count": posts.len(),
            "items": posts.iter().map(|p| serde_json::json!({
                "id": p.id,
                "text": p.text,
                "created": p.created,
                "permalink": p.permalink,
            })).collect::<Vec<_>>(),
        }))
    }
}

// ---------------------------------------------------------------------------
// social_post
// ---------------------------------------------------------------------------

pub struct SocialPost;

#[async_trait]
impl Tool for SocialPost {
    fn name(&self) -> &str {
        "social_post"
    }
    fn description(&self) -> &str {
        "Publish a post as yourself / your Page. provider: \"facebook\" | \"instagram\" | \"threads\"; \
         text is required. Facebook posts to one of YOUR Pages (pass \"page_id\", or it is \
         auto-detected when you manage exactly one Page — the Page access token is resolved \
         automatically). Instagram REQUIRES \"media_url\" (the API has no text-only posts; your \
         text becomes the caption) and a linked Instagram Business/Creator account (or \
         \"ig_user_id\"). Threads publishes text posts (max 500 characters). Nothing is posted \
         without this tool being called."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                (
                    "provider",
                    "string",
                    "Which network: \"facebook\", \"instagram\" or \"threads\"",
                ),
                (
                    "text",
                    "string",
                    "Post text (used as the caption on Instagram)",
                ),
                (
                    "page_id",
                    "string",
                    "Facebook Page to post as (optional; auto-detected when you manage exactly one Page)",
                ),
                (
                    "ig_user_id",
                    "string",
                    "Instagram Business/Creator account id (optional; auto-detected from your linked Pages)",
                ),
                (
                    "media_url",
                    "string",
                    "Public image URL — REQUIRED for Instagram (text-only posts are not supported by the Instagram API)",
                ),
            ],
            &["provider", "text"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let provider = SocialProvider::from_str(&req_str(&args, "provider")?)?;
        let text = req_str(&args, "text")?;
        if text.trim().is_empty() {
            return Err(ToolError::InvalidArgs("text must not be empty".into()));
        }
        // Provider-specific arg validation BEFORE any token use, so bad args
        // fail fast and offline.
        let media_url: Option<String> = match provider {
            SocialProvider::Instagram => Some(
                args.get("media_url")
                    .and_then(Value::as_str)
                    .filter(|s| !s.trim().is_empty())
                    .ok_or_else(|| {
                        ToolError::InvalidArgs(
                            "Instagram requires \"media_url\" — the Instagram API does not support \
                             text-only posts; your text is used as the caption"
                                .to_string(),
                        )
                    })?
                    .to_string(),
            ),
            _ => args
                .get("media_url")
                .and_then(Value::as_str)
                .map(str::to_string),
        };
        if provider == SocialProvider::Threads && text.chars().count() > THREADS_MAX_CHARS {
            return Err(ToolError::InvalidArgs(format!(
                "Threads posts are limited to {THREADS_MAX_CHARS} characters (got {})",
                text.chars().count()
            )));
        }
        let tok = stored_token()?;
        match provider {
            SocialProvider::Facebook => {
                let (page_id, page_token) =
                    resolve_page_for_post(&tok, args.get("page_id").and_then(Value::as_str)).await?;
                let v = graph_post(
                    FB_GRAPH,
                    &format!("/{page_id}/feed"),
                    &[("message", text.as_str())],
                    &page_token,
                )
                .await?;
                let id = v
                    .get("id")
                    .and_then(Value::as_str)
                    .ok_or_else(|| ToolError::Http("post response has no 'id'".to_string()))?;
                Ok(serde_json::json!({"provider": "facebook", "page_id": page_id, "id": id}))
            }
            SocialProvider::Instagram => {
                let media_url = media_url.expect("validated above");
                let ig = resolve_ig_user_id(&tok, args.get("ig_user_id").and_then(Value::as_str)).await?;
                // Step 1: create the media container.
                let c = graph_post(
                    FB_GRAPH,
                    &format!("/{ig}/media"),
                    &[("image_url", media_url.as_str()), ("caption", text.as_str())],
                    &tok.access_token,
                )
                .await?;
                let container = c
                    .get("id")
                    .and_then(Value::as_str)
                    .ok_or_else(|| {
                        ToolError::Http("media container response has no 'id'".to_string())
                    })?;
                // Step 2: publish the container.
                let p = graph_post(
                    FB_GRAPH,
                    &format!("/{ig}/media_publish"),
                    &[("creation_id", container)],
                    &tok.access_token,
                )
                .await?;
                let id = p
                    .get("id")
                    .and_then(Value::as_str)
                    .ok_or_else(|| ToolError::Http("publish response has no 'id'".to_string()))?;
                Ok(serde_json::json!({
                    "provider": "instagram",
                    "ig_user_id": ig,
                    "container_id": container,
                    "id": id,
                }))
            }
            SocialProvider::Threads => {
                // Step 1: create the text container.
                let c = graph_post(
                    THREADS_GRAPH,
                    "/me/threads",
                    &[("media_type", "TEXT"), ("text", text.as_str())],
                    &tok.access_token,
                )
                .await?;
                let container = c
                    .get("id")
                    .and_then(Value::as_str)
                    .ok_or_else(|| {
                        ToolError::Http("threads container response has no 'id'".to_string())
                    })?;
                // Step 2: publish it.
                let p = graph_post(
                    THREADS_GRAPH,
                    "/me/threads_publish",
                    &[("creation_id", container)],
                    &tok.access_token,
                )
                .await?;
                let id = p
                    .get("id")
                    .and_then(Value::as_str)
                    .ok_or_else(|| {
                        ToolError::Http("threads publish response has no 'id'".to_string())
                    })?;
                Ok(serde_json::json!({
                    "provider": "threads",
                    "container_id": container,
                    "id": id,
                }))
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Tests (offline — no network; pure parsers, arg validation, honest degrade)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex as StdMutex;

    // Serialize tests that mutate process-global env vars.
    static ENV_LOCK: StdMutex<()> = StdMutex::new(());

    struct EnvGuard {
        saved: Vec<(String, Option<String>)>,
    }

    impl EnvGuard {
        fn set(pairs: &[(&str, &str)]) -> Self {
            let saved = pairs
                .iter()
                .map(|(k, _)| (k.to_string(), std::env::var(k).ok()))
                .collect();
            for (k, v) in pairs {
                std::env::set_var(k, v);
            }
            EnvGuard { saved }
        }
        fn unset(keys: &[&str]) -> Self {
            let saved = keys
                .iter()
                .map(|k| (k.to_string(), std::env::var(k).ok()))
                .collect();
            for k in keys {
                std::env::remove_var(k);
            }
            EnvGuard { saved }
        }
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            for (k, v) in &self.saved {
                match v {
                    Some(val) => std::env::set_var(k, val),
                    None => std::env::remove_var(k),
                }
            }
        }
    }

    /// Point BOOM_HOME at an empty temp dir so token lookups see "not
    /// connected" deterministically. Assumes `super::` helpers read
    /// `$BOOM_HOME/connectors/social.json`.
    fn isolated_home() -> (tempfile::TempDir, EnvGuard) {
        let dir = tempfile::tempdir().expect("tempdir");
        let guard = EnvGuard::set(&[("BOOM_HOME", dir.path().to_str().unwrap())]);
        (dir, guard)
    }

    fn no_token_env() -> EnvGuard {
        EnvGuard::unset(&["BOOM_META_ACCESS_TOKEN"])
    }

    #[test]
    fn connector_identity() {
        let c = SocialConnector::new();
        assert_eq!(c.id(), "social");
        assert_eq!(c.name(), "Social (Meta)");
        assert_eq!(c.auth_kind(), "oauth2");
        assert!(!c.description().is_empty());
    }

    #[test]
    fn connect_url_shape() {
        let _lock = ENV_LOCK.lock().unwrap();
        let _guard = EnvGuard::set(&[("BOOM_META_CLIENT_ID", "test-app-123")]);
        let url = SocialConnector::new()
            .connect_url()
            .expect("connect_url with env client id");
        assert!(
            url.starts_with("https://www.facebook.com/v18.0/dialog/oauth?"),
            "url={url}"
        );
        assert!(url.contains("client_id=test-app-123"), "url={url}");
        assert!(url.contains("response_type=code"), "url={url}");
        assert!(url.contains("redirect_uri="), "url={url}");
        for scope in [
            "pages_read_engagement",
            "pages_manage_posts",
            "instagram_basic",
            "instagram_content_publish",
        ] {
            assert!(url.contains(&urlencode(scope)), "scope {scope} missing in {url}");
        }
    }

    #[test]
    fn threads_connect_url_shape() {
        let url = build_threads_connect_url("app-1", "https://example.com/cb");
        assert!(
            url.starts_with("https://www.threads.net/oauth/authorize?"),
            "url={url}"
        );
        assert!(url.contains("client_id=app-1"), "url={url}");
        assert!(url.contains("response_type=code"), "url={url}");
        assert!(url.contains("threads_basic"), "url={url}");
        assert!(url.contains("threads_content_publish"), "url={url}");
        assert!(url.contains("redirect_uri=https%3A%2F%2Fexample.com%2Fcb"), "url={url}");
    }

    #[test]
    fn connect_url_errors_without_client_id() {
        let _lock = ENV_LOCK.lock().unwrap();
        let (_dir, _home) = isolated_home(); // no token file to fall back to
        let _guard = EnvGuard::unset(&["BOOM_META_CLIENT_ID", "BOOM_META_ACCESS_TOKEN"]);
        let err = SocialConnector::new().connect_url().unwrap_err();
        assert!(err.contains("BOOM_META_CLIENT_ID"), "err={err}");
    }

    #[allow(clippy::await_holding_lock)]
    #[tokio::test]
    async fn tools_degrade_honestly_when_not_connected() {
        let _lock = ENV_LOCK.lock().unwrap();
        let (_dir, _home) = isolated_home();
        let _guard = no_token_env();

        let err = SocialProfile
            .execute(serde_json::json!({"provider": "facebook"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::Other(_)), "err={err}");
        assert!(err.to_string().contains("not connected"), "err={err}");

        let err = SocialRead
            .execute(serde_json::json!({"provider": "threads"}))
            .await
            .unwrap_err();
        assert!(err.to_string().contains("not connected"), "err={err}");

        let err = SocialPost
            .execute(serde_json::json!({"provider": "facebook", "text": "hi"}))
            .await
            .unwrap_err();
        assert!(err.to_string().contains("not connected"), "err={err}");

        // Unknown provider is rejected on arg validation, before any token check.
        let err = SocialProfile
            .execute(serde_json::json!({"provider": "myspace"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "err={err}");

        assert!(!SocialConnector::new().is_connected());
    }

    #[allow(clippy::await_holding_lock)]
    #[tokio::test]
    async fn token_roundtrip_through_shared_helpers() {
        let _lock = ENV_LOCK.lock().unwrap();
        let (_dir, _home) = isolated_home();
        let _guard = no_token_env();
        let tok = ConnectorToken {
            access_token: "tok-abc".to_string(),
            refresh_token: None,
            expires_at: Some(9_999_999_999),
            extra: serde_json::json!({"provider": "threads", "user_id": "42"}),
        };
        save_token(CONNECTOR_ID, &tok).expect("save");
        assert!(SocialConnector::new().is_connected());
        let loaded = load_token(CONNECTOR_ID).expect("load");
        assert_eq!(loaded.access_token, "tok-abc");
        assert_eq!(loaded.extra["provider"].as_str(), Some("threads"));
        assert_eq!(loaded.extra["user_id"].as_str(), Some("42"));
        SocialConnector::new().disconnect().expect("disconnect");
        assert!(!SocialConnector::new().is_connected());
    }

    #[test]
    fn schemas_are_valid() {
        let check = |tool: &dyn Tool, required: &[&str]| {
            let s = tool.schema();
            assert_eq!(s["type"], "object", "{}", tool.name());
            assert_eq!(s["additionalProperties"], false, "{}", tool.name());
            let req: Vec<&str> = s["required"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap())
                .collect();
            for r in required {
                assert!(req.contains(r), "{} missing required {r}", tool.name());
                assert!(
                    s["properties"][r]["type"].is_string(),
                    "{} prop {r}",
                    tool.name()
                );
            }
            for r in req {
                assert!(
                    s["properties"][r].is_object(),
                    "{} required {r} not in properties",
                    tool.name()
                );
            }
        };
        check(&SocialProfile, &["provider"]);
        check(&SocialRead, &["provider"]);
        check(&SocialPost, &["provider", "text"]);
        // Optional args are documented.
        assert!(
            SocialRead.schema()["properties"]["limit"]["description"]
                .as_str()
                .unwrap()
                .contains("25"),
            "limit should document the 25 cap"
        );
        assert!(
            SocialPost.schema()["properties"]["media_url"]["description"]
                .as_str()
                .unwrap()
                .contains("REQUIRED"),
            "media_url should flag the Instagram requirement"
        );
    }

    #[test]
    fn provider_from_str() {
        assert_eq!(
            SocialProvider::from_str("facebook").unwrap(),
            SocialProvider::Facebook
        );
        assert_eq!(
            SocialProvider::from_str("FB").unwrap(),
            SocialProvider::Facebook
        );
        assert_eq!(
            SocialProvider::from_str("instagram").unwrap(),
            SocialProvider::Instagram
        );
        assert_eq!(
            SocialProvider::from_str("ig").unwrap(),
            SocialProvider::Instagram
        );
        assert_eq!(
            SocialProvider::from_str("THREADS").unwrap(),
            SocialProvider::Threads
        );
        let err = SocialProvider::from_str("zalo").unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "err={err}");
        assert!(err.to_string().contains("zalo"), "err={err}");
    }

    #[test]
    fn normalize_limit_clamps() {
        assert_eq!(normalize_limit(5), 5);
        assert_eq!(normalize_limit(0), 1);
        assert_eq!(normalize_limit(25), 25);
        assert_eq!(normalize_limit(99), 25);
    }

    #[test]
    fn parse_profile_fixtures() {
        let fb = serde_json::json!({"id": "123", "name": "Sơn Nguyễn"});
        let (id, name) = parse_profile(SocialProvider::Facebook, &fb).unwrap();
        assert_eq!(id, "123");
        assert_eq!(name, "Sơn Nguyễn");

        let ig = serde_json::json!({"id": "456", "username": "son.ig", "name": "Son"});
        let (id, name) = parse_profile(SocialProvider::Instagram, &ig).unwrap();
        assert_eq!(id, "456");
        assert_eq!(name, "Son");

        let th = serde_json::json!({"id": "789", "username": "son_threads"});
        let (id, name) = parse_profile(SocialProvider::Threads, &th).unwrap();
        assert_eq!(id, "789");
        assert_eq!(name, "son_threads");

        let err =
            parse_profile(SocialProvider::Facebook, &serde_json::json!({"name": "noid"}))
                .unwrap_err();
        assert!(err.to_string().contains("id"), "err={err}");
    }

    #[test]
    fn parse_post_list_fixtures() {
        let fb = serde_json::json!({"data": [
            {"id": "p1", "message": "Hello", "created_time": "2026-10-16T10:00:00+0000",
             "permalink_url": "https://fb.com/p1"},
            {"id": "p2", "created_time": "2026-10-16T11:00:00+0000"},
        ]});
        let posts = parse_post_list(SocialProvider::Facebook, &fb).unwrap();
        assert_eq!(posts.len(), 2);
        assert_eq!(posts[0].text, "Hello");
        assert_eq!(posts[0].permalink, "https://fb.com/p1");
        assert_eq!(posts[1].text, ""); // missing message → empty, not an error

        let ig = serde_json::json!({"data": [
            {"id": "m1", "caption": "Sunset #photo", "media_type": "IMAGE",
             "timestamp": "2026-10-15T08:00:00+0000", "permalink": "https://ig.com/m1"},
        ]});
        let posts = parse_post_list(SocialProvider::Instagram, &ig).unwrap();
        assert_eq!(posts.len(), 1);
        assert_eq!(posts[0].text, "Sunset #photo");
        assert_eq!(posts[0].created, "2026-10-15T08:00:00+0000");

        let th = serde_json::json!({"data": [
            {"id": "t1", "text": "gm threads", "timestamp": "2026-10-17T01:00:00+0000",
             "permalink": "https://threads.com/t1"},
        ]});
        let posts = parse_post_list(SocialProvider::Threads, &th).unwrap();
        assert_eq!(posts[0].text, "gm threads");

        let err =
            parse_post_list(SocialProvider::Facebook, &serde_json::json!({"oops": 1}))
                .unwrap_err();
        assert!(err.to_string().contains("data"), "err={err}");
    }

    #[test]
    fn parse_pages_and_linked_ig_fixtures() {
        let v = serde_json::json!({"data": [
            {"id": "111", "name": "Shop A", "access_token": "page-tok-1",
             "instagram_business_account": {"id": "ig1", "username": "shop.a"}},
            {"id": "222", "name": "Shop B"},
        ]});
        let pages = parse_pages(&v).unwrap();
        assert_eq!(pages.len(), 2);
        assert_eq!(pages[0].id, "111");
        assert_eq!(pages[0].access_token.as_deref(), Some("page-tok-1"));
        assert_eq!(pages[1].access_token, None);

        let linked = parse_linked_ig(&v);
        assert_eq!(linked, vec![("ig1".to_string(), "shop.a".to_string())]);

        let empty = parse_linked_ig(&serde_json::json!({"data": []}));
        assert!(empty.is_empty());
    }

    fn two_pages() -> Vec<PageInfo> {
        vec![
            PageInfo {
                id: "111".into(),
                name: "Shop A".into(),
                access_token: Some("page-tok-1".into()),
            },
            PageInfo {
                id: "222".into(),
                name: "Shop B".into(),
                access_token: None,
            },
        ]
    }

    #[test]
    fn select_page_honest_errors() {
        // No pages at all.
        let err = select_page(&[], None, "post as").unwrap_err();
        assert!(err.to_string().contains("no Facebook Pages"), "err={err}");

        // Ambiguous without a hint: list the choices, don't guess.
        let pages = two_pages();
        let err = select_page(&pages, None, "post as").unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("Shop A"), "msg={msg}");
        assert!(msg.contains("Shop B"), "msg={msg}");
        assert!(msg.contains("page_id"), "msg={msg}");

        // Hint that matches nothing.
        let err = select_page(&pages, Some("999"), "post as").unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "err={err}");
        assert!(err.to_string().contains("999"), "err={err}");

        // Happy paths.
        assert_eq!(select_page(&pages, Some("222"), "post as").unwrap(), "222");
        assert_eq!(select_page(&pages[..1], None, "read posts from").unwrap(), "111");
    }

    #[allow(clippy::await_holding_lock)]
    #[tokio::test]
    async fn instagram_post_requires_media_url() {
        // Arg validation happens before the token check, so this is offline-safe.
        let _lock = ENV_LOCK.lock().unwrap();
        let (_dir, _home) = isolated_home();
        let _guard = no_token_env();
        let err = SocialPost
            .execute(serde_json::json!({"provider": "instagram", "text": "hello"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "err={err}");
        assert!(err.to_string().contains("media_url"), "err={err}");
    }

    #[allow(clippy::await_holding_lock)]
    #[tokio::test]
    async fn threads_post_rejects_overlong_text() {
        let _lock = ENV_LOCK.lock().unwrap();
        let (_dir, _home) = isolated_home();
        let _guard = no_token_env();
        let long = "x".repeat(501);
        let err = SocialPost
            .execute(serde_json::json!({"provider": "threads", "text": long}))
            .await
            .unwrap_err();
        // Length is validated before the token check too.
        assert!(matches!(err, ToolError::InvalidArgs(_)), "err={err}");
        assert!(err.to_string().contains("500"), "err={err}");
    }

    #[allow(clippy::await_holding_lock)]
    #[tokio::test]
    async fn post_rejects_empty_text() {
        let _lock = ENV_LOCK.lock().unwrap();
        let (_dir, _home) = isolated_home();
        let _guard = no_token_env();
        let err = SocialPost
            .execute(serde_json::json!({"provider": "facebook", "text": "   "}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "err={err}");
    }

    #[test]
    fn meta_error_envelope_mapping() {
        // Code 190 → reconnect guidance, never a raw retry.
        let expired =
            serde_json::json!({"error": {"message": "Invalid OAuth access token.", "code": 190}});
        let err = meta_http_error(400, &expired);
        assert!(matches!(err, ToolError::Other(_)), "err={err}");
        assert!(err.to_string().contains("reconnect"), "err={err}");

        // Ordinary API error keeps the message + code.
        let other =
            serde_json::json!({"error": {"message": "Unsupported post request.", "code": 100}});
        let err = meta_http_error(400, &other);
        assert!(matches!(err, ToolError::Http(_)), "err={err}");
        let msg = err.to_string();
        assert!(msg.contains("Unsupported post request"), "msg={msg}");
        assert!(msg.contains("100"), "msg={msg}");
    }

    #[test]
    fn oauth_error_parsing() {
        let j = serde_json::json!({"error": "invalid_grant", "error_description": "Code expired"});
        assert!(oauth_error("facebook", &j).contains("Code expired"));
        let j2 = serde_json::json!({"error": {"message": "Bad code", "code": 100}});
        assert!(oauth_error("threads", &j2).contains("Bad code"));
    }

    #[test]
    fn token_debug_must_not_leak_access_token() {
        // Contract requirement: the shared `ConnectorToken`'s Debug impl
        // redacts secrets.
        let tok = ConnectorToken {
            access_token: "secret-access-token-xyz".to_string(),
            refresh_token: None,
            expires_at: Some(9_999_999_999),
            extra: serde_json::json!({"provider": "facebook", "user_id": "123"}),
        };
        let dbg = format!("{tok:?}");
        assert!(!dbg.contains("secret-access-token-xyz"), "Debug leaks! {dbg}");
    }

    #[allow(clippy::await_holding_lock)]
    #[tokio::test]
    async fn exchange_code_validates_input_offline() {
        let _lock = ENV_LOCK.lock().unwrap();
        let (_dir, _home) = isolated_home();
        let _guard = EnvGuard::unset(&["BOOM_META_CLIENT_ID", "BOOM_META_ACCESS_TOKEN"]);
        let mut c = SocialConnector::new();
        let err = c.exchange_code("   ").await.unwrap_err();
        assert!(err.contains("empty"), "err={err}");
        // No client id and no stored token → fails before any network.
        let err = c.exchange_code("somecode").await.unwrap_err();
        assert!(err.contains("BOOM_META_CLIENT_ID"), "err={err}");
    }
}
