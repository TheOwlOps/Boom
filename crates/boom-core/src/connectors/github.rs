//! GitHub connector — list repositories, read issues, and search the GitHub REST API.
//!
//! Auth is a personal access token (PAT): [`GitHubConnector::exchange_code`]
//! treats its `code` argument as the PAT, verifies it with `GET /user`, and
//! stores it in `$BOOM_HOME/connectors/github.json` (mode 0600) as
//! `access_token`, together with `extra.login`. Every tool call loads the
//! token fresh from that file, so the connector struct itself holds no
//! secret — `Debug` output and error messages can never leak it.
//!
//! Rate limits are handled honestly: a 403 with `x-ratelimit-remaining: 0`
//! surfaces a clear "GitHub rate limit exceeded" message instead of a
//! generic failure.
//!
//! ## Merge note for the coordinator
//!
//! Drop this file at `crates/boom-core/src/connectors/github.rs` and declare
//! `pub mod github;` in `src/connectors/mod.rs`. Everything it needs (the
//! `Connector` trait, the token helpers) is defined in the "Shared connector
//! contract" section below so it compiles standalone inside `boom-core`
//! without touching existing files. If a shared `Connector` trait / token
//! helpers have already landed in a parent module, delete that section and
//! `use super::{Connector, delete_token, load_token, save_token};` instead —
//! the shapes are identical, except this file's token document carries the
//! PAT under `access_token` (per the connector contract) plus an `extra` map.

use std::fmt;
use std::path::PathBuf;
use std::time::Duration;

use async_trait::async_trait;
use serde_json::Value;

use crate::tools::{opt_str, opt_u64, req_str, schema_of, Tool, ToolError, ToolRegistry};
use super::Connector;


// ---------------------------------------------------------------------------
// Token persistence helpers
// ---------------------------------------------------------------------------
//
// Contract: tokens live in `$BOOM_HOME/connectors/<id>.json` written with
// mode 0600. `$BOOM_HOME` defaults to `~/.boom`.

fn boom_home() -> PathBuf {
    if let Ok(dir) = std::env::var("BOOM_HOME") {
        let dir = dir.trim();
        if !dir.is_empty() {
            return PathBuf::from(dir);
        }
    }
    dirs::home_dir()
        .map(|h| h.join(".boom"))
        .unwrap_or_else(|| PathBuf::from(".boom"))
}

fn token_path(id: &str) -> PathBuf {
    boom_home().join("connectors").join(format!("{id}.json"))
}

/// Read the token document, degrading to an empty object on any problem.
fn read_doc(id: &str) -> Value {
    std::fs::read_to_string(token_path(id))
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .filter(|v| v.is_object())
        .unwrap_or_else(|| serde_json::json!({}))
}

/// Atomic 0600 write of the token document.
fn write_doc(id: &str, doc: &Value) -> Result<(), String> {
    let path = token_path(id);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("cannot create connector dir: {e}"))?;
    }
    let body = serde_json::to_string_pretty(doc)
        .map_err(|e| format!("cannot serialize token file: {e}"))?;
    // Write temp + chmod 0600 + rename, mirroring
    // `server::state::atomic_write_secret`.
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, body.as_bytes()).map_err(|e| format!("cannot write token file: {e}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600))
            .map_err(|e| format!("cannot chmod token file: {e}"))?;
    }
    std::fs::rename(&tmp, &path).map_err(|e| format!("cannot write token file: {e}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))
            .map_err(|e| format!("cannot chmod token file: {e}"))?;
    }
    Ok(())
}

/// Load the stored token for connector `id`. Returns `None` when the file is
/// absent or unreadable — never an error. Reads `access_token` (the contract
/// field), falling back to `token` for older documents.
pub fn load_token(id: &str) -> Option<String> {
    let doc = read_doc(id);
    doc.get("access_token")
        .or_else(|| doc.get("token"))
        .and_then(Value::as_str)
        .filter(|t| !t.trim().is_empty())
        .map(str::to_string)
}

/// Persist the token for connector `id` (file mode 0600). Existing `extra`
/// fields are preserved.
pub fn save_token(id: &str, token: &str) -> Result<(), String> {
    let mut doc = read_doc(id);
    doc["id"] = Value::String(id.to_string());
    doc["access_token"] = Value::String(token.to_string());
    // Drop any legacy `token` field so there is a single source of truth.
    if let Some(obj) = doc.as_object_mut() {
        obj.remove("token");
    }
    write_doc(id, &doc)
}

/// Store one `extra` field (e.g. the verified GitHub login) without
/// disturbing the saved token.
pub fn save_extra(id: &str, key: &str, value: &str) -> Result<(), String> {
    let mut doc = read_doc(id);
    let mut extra = doc
        .get("extra")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    extra.insert(key.to_string(), Value::String(value.to_string()));
    doc["extra"] = Value::Object(extra);
    write_doc(id, &doc)
}

/// Read one `extra` field back.
pub fn load_extra(id: &str, key: &str) -> Option<String> {
    read_doc(id)
        .get("extra")
        .and_then(|e| e.get(key))
        .and_then(Value::as_str)
        .map(str::to_string)
}

/// Delete the stored token for connector `id`. Missing file is fine.
pub fn delete_token(id: &str) -> Result<(), String> {
    match std::fs::remove_file(token_path(id)) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(format!("cannot delete token file: {e}")),
    }
}

// ---------------------------------------------------------------------------
// GitHub connector
// ---------------------------------------------------------------------------

const CONNECTOR_ID: &str = "github";
const API_BASE: &str = "https://api.github.com";
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);

const CONNECT_INSTRUCTIONS: &str = "GitHub uses a personal access token — POST it to /api/connectors/github/token.\n\
Create one at https://github.com/settings/tokens : Settings → Developer settings → \
Personal access tokens → Tokens (classic) → Generate new token (classic). \
Required scopes: `repo` (to list private repositories) and `read:user` (to verify the token). \
The token is verified with GET /user and stored in $BOOM_HOME/connectors/github.json (mode 0600).";

/// GitHub REST API connector. Holds no secret: the PAT is loaded from the
/// token file on every call, so `exchange_code`/`disconnect` take effect
/// immediately for all registered tools.
pub struct GitHubConnector {
    api_base: String,
}

impl GitHubConnector {
    /// Production constructor: real GitHub API, token from `$BOOM_HOME`.
    pub fn new() -> Self {
        Self::with_api_base(API_BASE)
    }

    /// Constructor with an explicit API base — tests drive a local mock
    /// server through this.
    pub fn with_api_base(api_base: impl Into<String>) -> Self {
        Self {
            api_base: api_base.into(),
        }
    }

    fn api_base(&self) -> &str {
        &self.api_base
    }
}

impl Default for GitHubConnector {
    fn default() -> Self {
        Self::new()
    }
}

/// Never print the token in logs or Debug output — the struct holds no
/// token at all, and this impl makes that a compile-time guarantee.
impl fmt::Debug for GitHubConnector {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("GitHubConnector")
            .field("api_base", &self.api_base)
            .field("connected", &self.is_connected())
            .field("access_token", &"<redacted>")
            .finish()
    }
}

#[async_trait]
impl Connector for GitHubConnector {
    fn id(&self) -> &'static str {
        CONNECTOR_ID
    }
    fn name(&self) -> &'static str {
        "GitHub"
    }
    fn description(&self) -> &'static str {
        "GitHub REST API: list your repositories, read issues, and search \
         repositories, issues and code. Authenticates with a personal access token."
    }
    fn auth_kind(&self) -> &'static str {
        "api_key"
    }
    fn connect_url(&self) -> Result<String, String> {
        // No OAuth: the "code" step consumes the raw PAT instead.
        Err(CONNECT_INSTRUCTIONS.to_string())
    }
    async fn exchange_code(&mut self, code: &str) -> Result<(), String> {
        let pat = code.trim();
        if pat.is_empty() {
            return Err("empty personal access token".to_string());
        }
        let login = verify_pat(self.api_base(), pat).await?;
        save_token(CONNECTOR_ID, pat)?;
        save_extra(CONNECTOR_ID, "login", &login)?;
        Ok(())
    }
    fn disconnect(&mut self) -> Result<(), String> {
        delete_token(CONNECTOR_ID)
    }
    fn register_tools(&self, reg: &mut ToolRegistry) {
        reg.register("github", ReposTool::new(self.api_base.clone()));
        reg.register("github", IssuesTool::new(self.api_base.clone()));
        reg.register("github", SearchTool::new(self.api_base.clone()));
    }
}

// ---------------------------------------------------------------------------
// HTTP transport
// ---------------------------------------------------------------------------

/// Outcome of one GitHub API call, with the rate-limit header we care about.
struct GhResponse {
    status: u16,
    rate_remaining: Option<u64>,
    body: Value,
}

fn http_client() -> Result<reqwest::Client, ToolError> {
    reqwest::Client::builder()
        .timeout(REQUEST_TIMEOUT)
        .user_agent("boom")
        .build()
        .map_err(|e| ToolError::Http(format!("failed to build HTTP client: {e}")))
}

/// GET against the GitHub API with the standard headers
/// (`Authorization: Bearer <PAT>`, `Accept: application/vnd.github+json`,
/// `X-GitHub-Api-Version: 2022-11-28`). The token travels only in the
/// Authorization header and is never logged.
async fn gh_get(
    api_base: &str,
    token: &str,
    path: &str,
    query: &[(&str, String)],
) -> Result<GhResponse, ToolError> {
    let url = format!("{}{}", api_base.trim_end_matches('/'), path);
    let resp = http_client()?
        .get(&url)
        .bearer_auth(token)
        .header("Accept", "application/vnd.github+json")
        .header("X-GitHub-Api-Version", "2022-11-28")
        .query(&query)
        .send()
        .await
        .map_err(transport_error)?;
    let status = resp.status().as_u16();
    let rate_remaining = resp
        .headers()
        .get("x-ratelimit-remaining")
        .and_then(|v| v.to_str().ok())
        .and_then(|s| s.trim().parse::<u64>().ok());
    let text = resp
        .text()
        .await
        .map_err(|_| ToolError::Http("failed to read GitHub response body".to_string()))?;
    let body = serde_json::from_str::<Value>(text.trim()).unwrap_or(Value::Null);
    Ok(GhResponse {
        status,
        rate_remaining,
        body,
    })
}

/// Generic transport failure message. Deliberately generic: reqwest errors
/// can echo request details, and we never want the token in an error.
fn transport_error(err: reqwest::Error) -> ToolError {
    if err.is_timeout() {
        ToolError::Timeout(30)
    } else if err.is_connect() {
        ToolError::Http("could not reach the GitHub API (connection failed)".to_string())
    } else {
        ToolError::Http("GitHub request failed".to_string())
    }
}

/// Short `message` from a GitHub error body (`{"message": "..."}`).
fn api_message(body: &Value) -> &str {
    body.get("message")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("")
}

/// Map a non-2xx GitHub response to a `ToolError`. A 403 with
/// `x-ratelimit-remaining: 0` gets an explicit rate-limit message; nothing
/// here ever contains the token.
fn check_status(resp: &GhResponse) -> Result<(), ToolError> {
    if (200..300).contains(&resp.status) {
        return Ok(());
    }
    let msg = api_message(&resp.body);
    let detail = if msg.is_empty() {
        "no detail from GitHub".to_string()
    } else {
        msg.to_string()
    };
    match resp.status {
        401 => Err(ToolError::Other(
            "GitHub rejected the token (401 Unauthorized): it is invalid, expired or revoked — \
             reconnect with a fresh personal access token"
                .to_string(),
        )),
        403 if resp.rate_remaining == Some(0) || msg.to_lowercase().contains("rate limit") => {
            Err(ToolError::Other(
                "GitHub rate limit exceeded (x-ratelimit-remaining=0): wait for the rate-limit \
                 window to reset before retrying"
                    .to_string(),
            ))
        }
        403 => Err(ToolError::Other(format!(
            "GitHub refused the request (403 Forbidden): {detail}"
        ))),
        404 => Err(ToolError::NotFound(format!(
            "GitHub resource not found (404): {detail}"
        ))),
        s => Err(ToolError::Http(format!("GitHub returned HTTP {s}: {detail}"))),
    }
}

/// Load the PAT or fail honestly when the connector was never connected.
fn require_token() -> Result<String, ToolError> {
    load_token(CONNECTOR_ID).ok_or_else(|| {
        ToolError::Other(
            "GitHub is not connected: no personal access token is stored. Paste a token via \
             the token endpoint first (see the connector's connect instructions)."
                .to_string(),
        )
    })
}

/// Verify a candidate PAT with `GET /user`. Returns the account login on
/// success. Error messages never contain the token.
async fn verify_pat(api_base: &str, pat: &str) -> Result<String, String> {
    let resp = gh_get(api_base, pat, "/user", &[]).await.map_err(|e| match e {
        ToolError::Timeout(_) => "connection to GitHub timed out".to_string(),
        _ => "could not reach the GitHub API".to_string(),
    })?;
    match resp.status {
        200 => resp
            .body
            .get("login")
            .and_then(Value::as_str)
            .map(str::to_string)
            .ok_or_else(|| "GitHub /user response did not contain a login".to_string()),
        401 => Err("GitHub rejected the personal access token (401 Unauthorized): it is invalid \
                    or revoked — the token was NOT saved"
            .to_string()),
        403 if resp.rate_remaining == Some(0) => Err(
            "GitHub rate limit exceeded (x-ratelimit-remaining=0): try again later".to_string(),
        ),
        s => Err(format!(
            "GitHub token verification failed (HTTP {s}): the token was NOT saved"
        )),
    }
}

// ---------------------------------------------------------------------------
// Argument validation
// ---------------------------------------------------------------------------

/// Validate a string arg against an allow-list (case-insensitive).
fn validate_enum(raw: &str, allowed: &[&str], arg: &str) -> Result<String, ToolError> {
    let v = raw.trim().to_lowercase();
    if allowed.contains(&v.as_str()) {
        Ok(v)
    } else {
        Err(ToolError::InvalidArgs(format!(
            "invalid {arg} '{raw}': expected one of {}",
            allowed.join(", ")
        )))
    }
}

/// Validate a GitHub owner/repo path segment: non-empty, safe charset only
/// (prevents path injection into the request URL).
fn validate_name(raw: &str, arg: &str) -> Result<String, ToolError> {
    let v = raw.trim();
    if v.is_empty() {
        return Err(ToolError::InvalidArgs(format!(
            "missing required argument '{arg}'"
        )));
    }
    if v.chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == '.')
    {
        Ok(v.to_string())
    } else {
        Err(ToolError::InvalidArgs(format!(
            "invalid {arg} '{raw}': only letters, digits, '-', '_' and '.' are allowed"
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

/// `{full_name, description, private, html_url, updated_at}`.
fn summarize_repo(v: &Value) -> Value {
    serde_json::json!({
        "full_name": str_field(v, "full_name"),
        "description": v.get("description").and_then(Value::as_str),
        "private": v.get("private").and_then(Value::as_bool).unwrap_or(false),
        "html_url": str_field(v, "html_url"),
        "updated_at": str_field(v, "updated_at"),
    })
}

/// `{number, title, state, html_url, user_login, labels[]}`.
fn summarize_issue(v: &Value) -> Value {
    let labels: Vec<String> = v
        .get("labels")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|l| l.get("name").and_then(Value::as_str).map(str::to_string))
                .collect()
        })
        .unwrap_or_default();
    serde_json::json!({
        "number": v.get("number").and_then(Value::as_u64).unwrap_or(0),
        "title": str_field(v, "title"),
        "state": str_field(v, "state"),
        "html_url": str_field(v, "html_url"),
        "user_login": v.get("user").and_then(|u| u.get("login")).and_then(Value::as_str).unwrap_or(""),
        "labels": labels,
    })
}

/// Everything after `/repos/` in a GitHub API URL: `owner/repo`.
fn repo_from_api_url(url: &str) -> String {
    url.split("/repos/").nth(1).unwrap_or("").to_string()
}

/// Per-kind search item summaries.
fn summarize_search_item(kind: &str, v: &Value) -> Value {
    match kind {
        "issues" => serde_json::json!({
            "number": v.get("number").and_then(Value::as_u64).unwrap_or(0),
            "title": str_field(v, "title"),
            "state": str_field(v, "state"),
            "html_url": str_field(v, "html_url"),
            "repository": repo_from_api_url(&str_field(v, "repository_url")),
        }),
        "code" => serde_json::json!({
            "name": str_field(v, "name"),
            "path": str_field(v, "path"),
            "html_url": str_field(v, "html_url"),
            "repository": v.get("repository").map(|r| str_field(r, "full_name")).unwrap_or_default(),
        }),
        _ => serde_json::json!({
            "full_name": str_field(v, "full_name"),
            "description": v.get("description").and_then(Value::as_str),
            "html_url": str_field(v, "html_url"),
            "stargazers_count": v.get("stargazers_count").and_then(Value::as_u64).unwrap_or(0),
            "language": v.get("language").and_then(Value::as_str),
        }),
    }
}

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

/// `github_repos` — `GET /user/repos`.
pub struct ReposTool {
    api_base: String,
}

impl ReposTool {
    pub fn new(api_base: String) -> Self {
        Self { api_base }
    }
}

#[async_trait]
impl Tool for ReposTool {
    fn name(&self) -> &str {
        "github_repos"
    }
    fn description(&self) -> &str {
        "List the authenticated user's GitHub repositories (full_name, description, \
         visibility, URL, last update)."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                (
                    "visibility",
                    "string",
                    "Filter repositories: all, public, or private (default all)",
                ),
                (
                    "per_page",
                    "integer",
                    "Results per page, max 100 (default 30)",
                ),
                (
                    "sort",
                    "string",
                    "Sort by: created, updated, pushed, or full_name (default full_name)",
                ),
            ],
            &[],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let token = require_token()?;
        let visibility = validate_enum(
            opt_str(&args, "visibility", "all"),
            &["all", "public", "private"],
            "visibility",
        )?;
        let per_page = opt_u64(&args, "per_page", 30).clamp(1, 100);
        let sort = validate_enum(
            opt_str(&args, "sort", "full_name"),
            &["created", "updated", "pushed", "full_name"],
            "sort",
        )?;
        let resp = gh_get(
            &self.api_base,
            &token,
            "/user/repos",
            &[
                ("visibility", visibility),
                ("per_page", per_page.to_string()),
                ("sort", sort),
            ],
        )
        .await?;
        check_status(&resp)?;
        let repos: Vec<Value> = resp
            .body
            .as_array()
            .map(|a| a.iter().map(summarize_repo).collect())
            .unwrap_or_default();
        Ok(serde_json::json!({ "repos": repos, "count": repos.len() }))
    }
}

/// `github_issues` — `GET /repos/{owner}/{repo}/issues`.
pub struct IssuesTool {
    api_base: String,
}

impl IssuesTool {
    pub fn new(api_base: String) -> Self {
        Self { api_base }
    }
}

#[async_trait]
impl Tool for IssuesTool {
    fn name(&self) -> &str {
        "github_issues"
    }
    fn description(&self) -> &str {
        "List issues of a GitHub repository (number, title, state, URL, author, labels)."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("owner", "string", "Repository owner (user or org)"),
                ("repo", "string", "Repository name"),
                (
                    "state",
                    "string",
                    "Issue state: open, closed, or all (default open)",
                ),
                (
                    "per_page",
                    "integer",
                    "Results per page, max 100 (default 30)",
                ),
            ],
            &["owner", "repo"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let token = require_token()?;
        let owner = validate_name(&req_str(&args, "owner")?, "owner")?;
        let repo = validate_name(&req_str(&args, "repo")?, "repo")?;
        let state = validate_enum(opt_str(&args, "state", "open"), &["open", "closed", "all"], "state")?;
        let per_page = opt_u64(&args, "per_page", 30).clamp(1, 100);
        let resp = gh_get(
            &self.api_base,
            &token,
            &format!("/repos/{owner}/{repo}/issues"),
            &[("state", state), ("per_page", per_page.to_string())],
        )
        .await?;
        check_status(&resp)?;
        let issues: Vec<Value> = resp
            .body
            .as_array()
            .map(|a| a.iter().map(summarize_issue).collect())
            .unwrap_or_default();
        Ok(serde_json::json!({
            "repository": format!("{owner}/{repo}"),
            "issues": issues,
            "count": issues.len(),
        }))
    }
}

/// `github_search` — `GET /search/{repositories,issues,code}`.
pub struct SearchTool {
    api_base: String,
}

impl SearchTool {
    pub fn new(api_base: String) -> Self {
        Self { api_base }
    }
}

#[async_trait]
impl Tool for SearchTool {
    fn name(&self) -> &str {
        "github_search"
    }
    fn description(&self) -> &str {
        "Search GitHub repositories, issues, or code. Returns total_count plus \
         a short summary per hit."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                (
                    "query",
                    "string",
                    "GitHub search query (same syntax as github.com/search)",
                ),
                (
                    "kind",
                    "string",
                    "What to search: repositories, issues, or code (default repositories)",
                ),
                (
                    "per_page",
                    "integer",
                    "Results per page, max 50 (default 10)",
                ),
            ],
            &["query"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let token = require_token()?;
        let query = req_str(&args, "query")?;
        if query.trim().is_empty() {
            return Err(ToolError::InvalidArgs(
                "query must not be empty".to_string(),
            ));
        }
        let kind = validate_enum(
            opt_str(&args, "kind", "repositories"),
            &["repositories", "issues", "code"],
            "kind",
        )?;
        let per_page = opt_u64(&args, "per_page", 10).clamp(1, 50);
        let resp = gh_get(
            &self.api_base,
            &token,
            &format!("/search/{kind}"),
            &[("q", query), ("per_page", per_page.to_string())],
        )
        .await?;
        check_status(&resp)?;
        let items: Vec<Value> = resp
            .body
            .get("items")
            .and_then(Value::as_array)
            .map(|a| a.iter().map(|v| summarize_search_item(&kind, v)).collect())
            .unwrap_or_default();
        Ok(serde_json::json!({
            "kind": kind,
            "total_count": resp.body.get("total_count").and_then(Value::as_u64).unwrap_or(0),
            "items": items,
        }))
    }
}

// ---------------------------------------------------------------------------
// Tests (all offline — fixtures + validation + honest degradation + mock API)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // -- fixtures ------------------------------------------------------------

    const REPOS_FIXTURE: &str = r#"[
      {"id":1,"full_name":"octocat/Hello-World","description":"My first repo",
       "private":false,"html_url":"https://github.com/octocat/Hello-World",
       "updated_at":"2026-10-01T12:00:00Z"},
      {"id":2,"full_name":"octocat/Spoon-Knife","description":null,
       "private":true,"html_url":"https://github.com/octocat/Spoon-Knife",
       "updated_at":"2026-09-15T08:30:00Z"}
    ]"#;

    const ISSUES_FIXTURE: &str = r#"[
      {"id":10,"number":42,"title":"Bug: crash on startup","state":"open",
       "html_url":"https://github.com/octocat/Hello-World/issues/42",
       "user":{"login":"octocat"},
       "labels":[{"name":"bug"},{"name":"urgent"}]},
      {"id":11,"number":43,"title":"Docs typo","state":"open",
       "html_url":"https://github.com/octocat/Hello-World/issues/43",
       "user":{"login":"hubot"},"labels":[]}
    ]"#;

    const SEARCH_REPOS_FIXTURE: &str = r#"{
      "total_count": 2, "incomplete_results": false,
      "items": [
        {"full_name":"rust-lang/rust","description":"Empowering everyone to build reliable software",
         "html_url":"https://github.com/rust-lang/rust","stargazers_count":100000,"language":"Rust"}
      ]
    }"#;

    const SEARCH_ISSUES_FIXTURE: &str = r#"{
      "total_count": 1, "incomplete_results": false,
      "items": [
        {"number":7,"title":"Fix it","state":"open",
         "html_url":"https://github.com/o/r/issues/7",
         "repository_url":"https://api.github.com/repos/o/r"}
      ]
    }"#;

    const SEARCH_CODE_FIXTURE: &str = r#"{
      "total_count": 1, "incomplete_results": false,
      "items": [
        {"name":"lib.rs","path":"src/lib.rs",
         "html_url":"https://github.com/o/r/blob/main/src/lib.rs",
         "repository":{"full_name":"o/r"}}
      ]
    }"#;

    // -- BOOM_HOME sandbox ---------------------------------------------------
    // Tests that touch the token file serialize on a process-global lock and
    // point BOOM_HOME at a fresh temp dir, restoring the env var afterwards.

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

    // -- mock HTTP server (stands in for api.github.com) ---------------------
    // Same shape as server::channels' mock: raw TCP, canned response with
    // custom headers, captures the full raw request for header assertions.

    async fn mock_server(
        status: u16,
        extra_headers: &[(&str, &str)],
        body: &'static str,
        count: usize,
    ) -> (String, tokio::task::JoinHandle<Vec<String>>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind mock server");
        let addr = listener.local_addr().expect("local addr");
        let headers: Vec<(String, String)> = extra_headers
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        let handle = tokio::spawn(async move {
            use tokio::io::{AsyncReadExt, AsyncWriteExt};
            let mut requests = Vec::new();
            for _ in 0..count {
                let (mut sock, _) = listener.accept().await.expect("accept");
                let mut req: Vec<u8> = Vec::new();
                let mut buf = vec![0u8; 8192];
                let header_end = loop {
                    let n = sock.read(&mut buf).await.expect("read");
                    if n == 0 {
                        break None;
                    }
                    req.extend_from_slice(&buf[..n]);
                    if let Some(pos) = req
                        .windows(4)
                        .position(|w| w == b"\r\n\r\n")
                        .map(|p| p + 4)
                    {
                        break Some(pos);
                    }
                };
                if let Some(end) = header_end {
                    let head = String::from_utf8_lossy(&req[..end]).into_owned();
                    let content_len = head
                        .lines()
                        .find_map(|l| {
                            let (k, v) = l.split_once(':')?;
                            k.trim()
                                .eq_ignore_ascii_case("content-length")
                                .then(|| v.trim().parse::<usize>().ok())
                                .flatten()
                        })
                        .unwrap_or(0);
                    while req.len() < end + content_len {
                        let n = sock.read(&mut buf).await.expect("read body");
                        if n == 0 {
                            break;
                        }
                        req.extend_from_slice(&buf[..n]);
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
                let mut resp = format!(
                    "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
                );
                for (k, v) in &headers {
                    resp.push_str(&format!("{k}: {v}\r\n"));
                }
                resp.push_str(&format!(
                    "Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                ));
                sock.write_all(resp.as_bytes()).await.expect("write");
            }
            requests
        });
        (format!("http://{addr}"), handle)
    }

    // -- contract ------------------------------------------------------------

    #[test]
    fn connector_identity() {
        let c = GitHubConnector::new();
        assert_eq!(c.id(), "github");
        assert_eq!(c.name(), "GitHub");
        assert_eq!(c.auth_kind(), "api_key");
        assert!(!c.description().is_empty());
    }

    #[test]
    fn connect_url_gives_pat_instructions() {
        let c = GitHubConnector::new();
        let err = c.connect_url().unwrap_err();
        assert!(err.contains("personal access token"), "got: {err}");
        assert!(err.contains("github.com/settings/tokens"), "got: {err}");
        assert!(err.contains("repo"), "got: {err}");
        assert!(err.contains("read:user"), "got: {err}");
    }

    // -- schemas -------------------------------------------------------------

    #[test]
    fn schemas_are_valid() {
        let mut reg = ToolRegistry::default();
        GitHubConnector::new().register_tools(&mut reg);
        for name in ["github_repos", "github_issues", "github_search"] {
            let tool = reg.get(name).unwrap_or_else(|| panic!("tool '{name}' missing"));
            assert!(!tool.description().is_empty(), "empty description for {name}");
            let schema = tool.schema();
            let obj = schema
                .as_object()
                .unwrap_or_else(|| panic!("schema of {name} is not an object"));
            assert_eq!(obj.get("type").and_then(|t| t.as_str()), Some("object"));
            let props = obj
                .get("properties")
                .and_then(|p| p.as_object())
                .unwrap_or_else(|| panic!("schema of {name} lacks properties"));
            assert!(!props.is_empty(), "schema of {name} has no properties");
            for req in obj
                .get("required")
                .and_then(|r| r.as_array())
                .cloned()
                .unwrap_or_default()
            {
                let key = req.as_str().expect("required entry not a string");
                assert!(
                    props.contains_key(key),
                    "required arg '{key}' of {name} missing from properties"
                );
            }
        }
        // Toolset grouping.
        let in_set = reg.tools_in_toolset("github");
        assert_eq!(in_set.len(), 3, "toolset 'github': {in_set:?}");
    }

    // -- token helpers -------------------------------------------------------

    #[tokio::test]
    async fn token_helpers_roundtrip() {
        let _home = TestHome::new().await;
        assert!(load_token("github").is_none());
        save_token("github", "ghp_TEST").unwrap();
        assert_eq!(load_token("github").as_deref(), Some("ghp_TEST"));
        save_extra("github", "login", "octocat").unwrap();
        assert_eq!(load_extra("github", "login").as_deref(), Some("octocat"));
        // Token survives the extra write.
        assert_eq!(load_token("github").as_deref(), Some("ghp_TEST"));

        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(token_path("github"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777;
            assert_eq!(mode, 0o600, "token file mode = {mode:o}");
        }

        delete_token("github").unwrap();
        assert!(load_token("github").is_none());
        delete_token("github").unwrap(); // idempotent
    }

    // -- exchange (mock) -----------------------------------------------------

    const USER_FIXTURE: &str = r#"{"login":"octocat","id":1,"type":"User"}"#;

    #[tokio::test]
    async fn exchange_code_ok_stores_token_and_login() {
        let _home = TestHome::new().await;
        let (base, handle) = mock_server(200, &[], USER_FIXTURE, 1).await;
        let mut c = GitHubConnector::with_api_base(&base);
        assert!(!c.is_connected());
        c.exchange_code("ghp_VALIDTOKEN123").await.unwrap();
        assert!(c.is_connected());
        assert_eq!(load_token("github").as_deref(), Some("ghp_VALIDTOKEN123"));
        assert_eq!(load_extra("github", "login").as_deref(), Some("octocat"));

        // Request carried the standard GitHub headers…
        let requests = handle.await.unwrap();
        let raw = &requests[0];
        assert!(raw.contains("GET /user "), "request line: {raw}");
        assert!(
            raw.to_lowercase()
                .contains("accept: application/vnd.github+json"),
            "accept header: {raw}"
        );
        assert!(
            raw.to_lowercase().contains("x-github-api-version: 2022-11-28"),
            "api version header: {raw}"
        );
        assert!(
            raw.to_lowercase().contains("authorization: bearer ghp_validtoken123"),
            "auth header: {raw}"
        );

        // …and Debug never leaks the token.
        let dbg = format!("{:?}", c);
        assert!(!dbg.contains("ghp_VALIDTOKEN123"), "token leaked: {dbg}");
        assert!(dbg.contains("<redacted>"), "got: {dbg}");
    }

    #[tokio::test]
    async fn exchange_code_rejects_bad_token() {
        let _home = TestHome::new().await;
        let (base, _handle) =
            mock_server(401, &[], r#"{"message":"Bad credentials"}"#, 1).await;
        let mut c = GitHubConnector::with_api_base(&base);
        let err = c.exchange_code("ghp_WRONG").await.unwrap_err();
        assert!(err.contains("401"), "got: {err}");
        assert!(err.contains("NOT saved"), "got: {err}");
        assert!(!err.contains("ghp_WRONG"), "token leaked in error: {err}");
        assert!(!c.is_connected());
        assert!(load_token("github").is_none());
    }

    #[tokio::test]
    async fn exchange_code_empty_pat_is_rejected_without_network() {
        let _home = TestHome::new().await;
        let mut c = GitHubConnector::new();
        let err = c.exchange_code("   ").await.unwrap_err();
        assert!(err.contains("empty"), "got: {err}");
    }

    #[tokio::test]
    async fn disconnect_clears_token() {
        let _home = TestHome::new().await;
        let (base, _handle) = mock_server(200, &[], USER_FIXTURE, 1).await;
        let mut c = GitHubConnector::with_api_base(&base);
        c.exchange_code("ghp_X").await.unwrap();
        assert!(c.is_connected());
        c.disconnect().unwrap();
        assert!(!c.is_connected());
        assert!(load_token("github").is_none());
        c.disconnect().unwrap(); // idempotent
    }

    // -- honest degradation --------------------------------------------------

    #[tokio::test]
    async fn not_connected_is_honest() {
        let _home = TestHome::new().await;
        let tool = ReposTool::new("http://127.0.0.1:9".to_string());
        let err = tool
            .execute(serde_json::json!({}))
            .await
            .unwrap_err();
        assert!(err.to_string().contains("not connected"), "got: {err}");
    }

    // -- repos tool (mock) ---------------------------------------------------

    async fn connected_tool_home(pat: &str) -> TestHome {
        let home = TestHome::new().await;
        save_token("github", pat).unwrap();
        home
    }

    #[tokio::test]
    async fn repos_lists_and_maps_fields() {
        let _home = connected_tool_home("ghp_T").await;
        let (base, handle) = mock_server(200, &[], REPOS_FIXTURE, 1).await;
        let tool = ReposTool::new(base);
        let out = tool.execute(serde_json::json!({})).await.unwrap();
        assert_eq!(out["count"], 2);
        let r0 = &out["repos"][0];
        assert_eq!(r0["full_name"], "octocat/Hello-World");
        assert_eq!(r0["description"], "My first repo");
        assert_eq!(r0["private"], false);
        assert_eq!(r0["html_url"], "https://github.com/octocat/Hello-World");
        assert_eq!(r0["updated_at"], "2026-10-01T12:00:00Z");
        // Null description stays null; private flag maps.
        assert!(out["repos"][1]["description"].is_null());
        assert_eq!(out["repos"][1]["private"], true);
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("GET /user/repos?"), "request: {raw}");
        assert!(raw.contains("visibility=all"), "request: {raw}");
    }

    #[tokio::test]
    async fn repos_clamps_per_page_to_100() {
        let _home = connected_tool_home("ghp_T").await;
        let (base, handle) = mock_server(200, &[], "[]", 1).await;
        let tool = ReposTool::new(base);
        tool.execute(serde_json::json!({"per_page": 500}))
            .await
            .unwrap();
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("per_page=100"), "request: {raw}");
        assert!(!raw.contains("per_page=500"), "request: {raw}");
    }

    #[tokio::test]
    async fn repos_rejects_bad_args() {
        let _home = connected_tool_home("ghp_T").await;
        let tool = ReposTool::new("http://127.0.0.1:9".to_string());
        for args in [
            serde_json::json!({"visibility": "everyone"}),
            serde_json::json!({"sort": "stars"}),
        ] {
            let err = tool.execute(args).await.unwrap_err();
            assert!(
                matches!(err, ToolError::InvalidArgs(_)),
                "expected InvalidArgs, got: {err}"
            );
        }
    }

    // -- issues tool (mock) --------------------------------------------------

    #[tokio::test]
    async fn issues_lists_and_maps_fields() {
        let _home = connected_tool_home("ghp_T").await;
        let (base, handle) = mock_server(200, &[], ISSUES_FIXTURE, 1).await;
        let tool = IssuesTool::new(base);
        let out = tool
            .execute(serde_json::json!({"owner": "octocat", "repo": "Hello-World"}))
            .await
            .unwrap();
        assert_eq!(out["repository"], "octocat/Hello-World");
        assert_eq!(out["count"], 2);
        let i0 = &out["issues"][0];
        assert_eq!(i0["number"], 42);
        assert_eq!(i0["title"], "Bug: crash on startup");
        assert_eq!(i0["state"], "open");
        assert_eq!(i0["user_login"], "octocat");
        assert_eq!(i0["labels"], serde_json::json!(["bug", "urgent"]));
        assert_eq!(out["issues"][1]["labels"], serde_json::json!([]));
        let raw = &handle.await.unwrap()[0];
        assert!(
            raw.contains("GET /repos/octocat/Hello-World/issues?"),
            "request: {raw}"
        );
    }

    #[tokio::test]
    async fn issues_rejects_bad_args() {
        let _home = connected_tool_home("ghp_T").await;
        let tool = IssuesTool::new("http://127.0.0.1:9".to_string());
        // Missing required args.
        let err = tool
            .execute(serde_json::json!({"owner": "octocat"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "got: {err}");
        // Bad state.
        let err = tool
            .execute(serde_json::json!({"owner": "o", "repo": "r", "state": "bogus"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "got: {err}");
        // Path injection rejected.
        let err = tool
            .execute(serde_json::json!({"owner": "../etc", "repo": "r"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "got: {err}");
    }

    // -- search tool (mock) --------------------------------------------------

    #[tokio::test]
    async fn search_repositories_summarizes() {
        let _home = connected_tool_home("ghp_T").await;
        let (base, handle) = mock_server(200, &[], SEARCH_REPOS_FIXTURE, 1).await;
        let tool = SearchTool::new(base);
        let out = tool
            .execute(serde_json::json!({"query": "language:rust stars:>50000"}))
            .await
            .unwrap();
        assert_eq!(out["kind"], "repositories");
        assert_eq!(out["total_count"], 2);
        let item = &out["items"][0];
        assert_eq!(item["full_name"], "rust-lang/rust");
        assert_eq!(item["stargazers_count"], 100000);
        assert_eq!(item["language"], "Rust");
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("GET /search/repositories?"), "request: {raw}");
        assert!(raw.contains("q=language%3Arust"), "request: {raw}");
    }

    #[tokio::test]
    async fn search_issues_extracts_repo() {
        let _home = connected_tool_home("ghp_T").await;
        let (base, _handle) = mock_server(200, &[], SEARCH_ISSUES_FIXTURE, 1).await;
        let tool = SearchTool::new(base);
        let out = tool
            .execute(serde_json::json!({"query": "crash", "kind": "issues"}))
            .await
            .unwrap();
        let item = &out["items"][0];
        assert_eq!(item["number"], 7);
        assert_eq!(item["repository"], "o/r");
    }

    #[tokio::test]
    async fn search_code_summarizes() {
        let _home = connected_tool_home("ghp_T").await;
        let (base, _handle) = mock_server(200, &[], SEARCH_CODE_FIXTURE, 1).await;
        let tool = SearchTool::new(base);
        let out = tool
            .execute(serde_json::json!({"query": "fn main", "kind": "code"}))
            .await
            .unwrap();
        let item = &out["items"][0];
        assert_eq!(item["name"], "lib.rs");
        assert_eq!(item["path"], "src/lib.rs");
        assert_eq!(item["repository"], "o/r");
    }

    #[tokio::test]
    async fn search_clamps_per_page_to_50() {
        let _home = connected_tool_home("ghp_T").await;
        let (base, handle) = mock_server(200, &[], SEARCH_REPOS_FIXTURE, 1).await;
        let tool = SearchTool::new(base);
        tool.execute(serde_json::json!({"query": "x", "per_page": 99}))
            .await
            .unwrap();
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("per_page=50"), "request: {raw}");
    }

    #[tokio::test]
    async fn search_rejects_bad_args() {
        let _home = connected_tool_home("ghp_T").await;
        let tool = SearchTool::new("http://127.0.0.1:9".to_string());
        let err = tool
            .execute(serde_json::json!({"kind": "repositories"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "got: {err}");
        let err = tool
            .execute(serde_json::json!({"query": "x", "kind": "commits"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "got: {err}");
    }

    // -- rate limits & status mapping (mock) ---------------------------------

    #[tokio::test]
    async fn rate_limit_exhausted_gives_clear_message() {
        let _home = connected_tool_home("ghp_T").await;
        let (base, _handle) = mock_server(
            403,
            &[("x-ratelimit-remaining", "0")],
            r#"{"message":"API rate limit exceeded for user."}"#,
            1,
        )
        .await;
        let tool = ReposTool::new(base);
        let err = tool.execute(serde_json::json!({})).await.unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("GitHub rate limit exceeded"), "got: {msg}");
        assert!(!msg.contains("ghp_T"), "token leaked: {msg}");
    }

    #[tokio::test]
    async fn forbidden_without_rate_limit_is_not_a_rate_limit_error() {
        let _home = connected_tool_home("ghp_T").await;
        let (base, _handle) = mock_server(
            403,
            &[("x-ratelimit-remaining", "59")],
            r#"{"message":"Resource not accessible by integration"}"#,
            1,
        )
        .await;
        let tool = ReposTool::new(base);
        let err = tool.execute(serde_json::json!({})).await.unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("refused"), "got: {msg}");
        assert!(!msg.contains("rate limit exceeded"), "got: {msg}");
    }

    #[tokio::test]
    async fn not_found_maps_cleanly() {
        let _home = connected_tool_home("ghp_T").await;
        let (base, _handle) =
            mock_server(404, &[], r#"{"message":"Not Found"}"#, 1).await;
        let tool = IssuesTool::new(base);
        let err = tool
            .execute(serde_json::json!({"owner": "o", "repo": "nope"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::NotFound(_)), "got: {err}");
    }

    // -- pure parse units ----------------------------------------------------

    #[test]
    fn fixtures_parse_to_expected_summaries() {
        let repos: Value = serde_json::from_str(REPOS_FIXTURE).unwrap();
        let s = summarize_repo(&repos[0]);
        assert_eq!(s["full_name"], "octocat/Hello-World");
        assert_eq!(s["private"], false);

        let issues: Value = serde_json::from_str(ISSUES_FIXTURE).unwrap();
        let s = summarize_issue(&issues[0]);
        assert_eq!(s["user_login"], "octocat");
        assert_eq!(s["labels"], serde_json::json!(["bug", "urgent"]));

        let sr: Value = serde_json::from_str(SEARCH_REPOS_FIXTURE).unwrap();
        let s = summarize_search_item("repositories", &sr["items"][0]);
        assert_eq!(s["stargazers_count"], 100000);

        let si: Value = serde_json::from_str(SEARCH_ISSUES_FIXTURE).unwrap();
        let s = summarize_search_item("issues", &si["items"][0]);
        assert_eq!(s["repository"], "o/r");

        let sc: Value = serde_json::from_str(SEARCH_CODE_FIXTURE).unwrap();
        let s = summarize_search_item("code", &sc["items"][0]);
        assert_eq!(s["repository"], "o/r");

        assert_eq!(repo_from_api_url("https://api.github.com/repos/a/b"), "a/b");
        assert_eq!(repo_from_api_url("garbage"), "");
    }

    #[test]
    fn validation_units() {
        assert_eq!(
            validate_enum("Open", &["open", "closed", "all"], "state").unwrap(),
            "open"
        );
        assert!(validate_enum("x", &["open"], "state").is_err());
        assert_eq!(validate_name("octocat", "owner").unwrap(), "octocat");
        assert_eq!(validate_name("my-repo.js", "repo").unwrap(), "my-repo.js");
        assert!(validate_name("", "owner").is_err());
        assert!(validate_name("../x", "owner").is_err());
        assert!(validate_name("a/b", "repo").is_err());
    }

    #[test]
    fn check_status_units() {
        let ok = GhResponse {
            status: 200,
            rate_remaining: Some(60),
            body: Value::Null,
        };
        assert!(check_status(&ok).is_ok());

        let rl = GhResponse {
            status: 403,
            rate_remaining: Some(0),
            body: serde_json::json!({"message": "API rate limit exceeded"}),
        };
        let msg = check_status(&rl).unwrap_err().to_string();
        assert!(msg.contains("GitHub rate limit exceeded"), "got: {msg}");

        // 403 with a rate-limit message but no header also counts.
        let rl2 = GhResponse {
            status: 403,
            rate_remaining: None,
            body: serde_json::json!({"message": "rate limit exceeded"}),
        };
        assert!(check_status(&rl2)
            .unwrap_err()
            .to_string()
            .contains("GitHub rate limit exceeded"));

        let denied = GhResponse {
            status: 403,
            rate_remaining: Some(10),
            body: serde_json::json!({"message": "nope"}),
        };
        assert!(check_status(&denied)
            .unwrap_err()
            .to_string()
            .contains("refused"));

        let unauth = GhResponse {
            status: 401,
            rate_remaining: None,
            body: Value::Null,
        };
        assert!(check_status(&unauth)
            .unwrap_err()
            .to_string()
            .contains("401"));
    }
}
