//! Boom desktop backend: built-in tools and marketplace catalog.
//!
//! This module implements the pure request/response logic for three endpoints:
//!
//! * `GET  /api/tools`          -> [`list_tools()`]
//! * `POST /api/tools/run`      -> [`run_tool()`]
//! * `GET  /api/marketplace`     -> [`list_marketplace()`]
//!
//! The functions intentionally depend only on `std`, `serde` and `serde_json`
//! (no axum, no new dependencies). An axum router — e.g.
//! `apps/desktop/src-tauri/src/backend/mod.rs` — can wrap them directly:
//!
//! ```ignore
//! async fn get_tools() -> axum::Json<serde_json::Value> {
//!     axum::Json(tools::list_tools())
//! }
//!
//! async fn post_run_tool(
//!     axum::Json(body): axum::Json<tools::RunToolRequest>,
//! ) -> axum::Json<serde_json::Value> {
//!     axum::Json(tools::run_tool(&body.tool, &body.args))
//! }
//! ```
//!
//! # Security model
//!
//! * Every tool name is checked against a fixed whitelist
//!   ([`is_allowed_tool`]); unknown tools are rejected before any work happens.
//! * File operations sanitize the requested path ([`sanitize_path`]):
//!   `..` components, NUL bytes and empty paths are rejected. Reads are
//!   additionally capped at [`MAX_READ_BYTES`]; writes are capped at
//!   [`MAX_WRITE_BYTES`].
//! * `shell_run` never spawns a shell. The command must be a bare binary name
//!   from [`SHELL_WHITELIST`], and every argument is scanned for shell
//!   metacharacters ([`contains_shell_meta`]) before `std::process::Command`
//!   is invoked directly.
//! * The calculator parses arithmetic with a small hand-written recursive
//!   descent parser — there is no `eval` of any kind.

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fmt;
use std::io;
use std::path::{Component, Path, PathBuf};
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/// Maximum number of bytes returned by `read_file`.
const MAX_READ_BYTES: u64 = 100 * 1024;

/// Maximum number of bytes accepted by `write_file` / `append_file`.
const MAX_WRITE_BYTES: usize = 1024 * 1024;

/// Commands allowed for `shell_run`. Executed directly via
/// `std::process::Command` — never through a shell.
const SHELL_WHITELIST: &[&str] = &["ls", "pwd", "echo", "cat", "date"];

/// Characters that have a special meaning in POSIX shells. Any `shell_run`
/// argument containing one of these is rejected outright.
const SHELL_META: &[char] = &[
    ';', '|', '&', '$', '`', '>', '<', '(', ')', '{', '}', '*', '?', '~', '#', '!', '\\', '"', '\'',
];

// ---------------------------------------------------------------------------
// Public request/response shapes
// ---------------------------------------------------------------------------

/// Body of `POST /api/tools/run`.
#[derive(Debug, Clone, Deserialize)]
pub struct RunToolRequest {
    pub tool: String,
    pub args: Value,
}

/// Metadata describing one built-in tool, as returned by `GET /api/tools`.
#[derive(Debug, Clone, Serialize)]
pub struct ToolInfo {
    pub name: &'static str,
    pub description: &'static str,
    pub category: &'static str,
    /// A simple JSON Schema describing the tool's `args`.
    pub schema: Value,
}

/// One marketplace catalog entry, as returned by `GET /api/marketplace`.
#[derive(Debug, Clone, Serialize)]
pub struct MarketplaceItem {
    pub id: &'static str,
    pub name: &'static str,
    pub description: &'static str,
    /// Either `"plugin"` or `"tool"`.
    pub kind: &'static str,
    pub category: &'static str,
}

// ---------------------------------------------------------------------------
// Tool registry
// ---------------------------------------------------------------------------

/// Static registry of every built-in tool. The whitelist for
/// `POST /api/tools/run` is derived from this table, so a tool cannot be
/// executed unless it is listed here.
fn tool_registry() -> Vec<ToolInfo> {
    vec![
        ToolInfo {
            name: "read_file",
            description: "Read a text file and return its contents (max 100KB).",
            category: "filesystem",
            schema: json!({
                "type": "object",
                "properties": { "path": { "type": "string", "description": "Path of the file to read" } },
                "required": ["path"]
            }),
        },
        ToolInfo {
            name: "write_file",
            description: "Write text to a file, creating parent directories as needed (max 1MB).",
            category: "filesystem",
            schema: json!({
                "type": "object",
                "properties": {
                    "path": { "type": "string", "description": "Path of the file to write" },
                    "content": { "type": "string", "description": "Text content to write" }
                },
                "required": ["path", "content"]
            }),
        },
        ToolInfo {
            name: "append_file",
            description:
                "Append text to the end of a file, creating it if missing (max 1MB total).",
            category: "filesystem",
            schema: json!({
                "type": "object",
                "properties": {
                    "path": { "type": "string", "description": "Path of the file to append to" },
                    "content": { "type": "string", "description": "Text content to append" }
                },
                "required": ["path", "content"]
            }),
        },
        ToolInfo {
            name: "list_dir",
            description: "List the entries of a directory (names only).",
            category: "filesystem",
            schema: json!({
                "type": "object",
                "properties": { "path": { "type": "string", "description": "Directory to list" } },
                "required": ["path"]
            }),
        },
        ToolInfo {
            name: "delete_file",
            description: "Delete a single file. Directories are never removed.",
            category: "filesystem",
            schema: json!({
                "type": "object",
                "properties": { "path": { "type": "string", "description": "Path of the file to delete" } },
                "required": ["path"]
            }),
        },
        ToolInfo {
            name: "web_search",
            description: "Search the web. Currently a placeholder: no provider is configured.",
            category: "web",
            schema: json!({
                "type": "object",
                "properties": { "query": { "type": "string", "description": "Search query" } },
                "required": ["query"]
            }),
        },
        ToolInfo {
            name: "shell_run",
            description:
                "Run a whitelisted shell command (ls, pwd, echo, cat, date) without a shell.",
            category: "system",
            schema: json!({
                "type": "object",
                "properties": {
                    "command": { "type": "string", "description": "Command name, must be whitelisted" },
                    "args": { "type": "array", "items": { "type": "string" }, "description": "Command arguments" }
                },
                "required": ["command"]
            }),
        },
        ToolInfo {
            name: "get_time",
            description: "Return the current UTC time and Unix timestamp.",
            category: "system",
            schema: json!({ "type": "object", "properties": {} }),
        },
        ToolInfo {
            name: "get_cwd",
            description: "Return the backend process's current working directory.",
            category: "system",
            schema: json!({ "type": "object", "properties": {} }),
        },
        ToolInfo {
            name: "calculator",
            description: "Evaluate a simple arithmetic expression (+, -, *, /, %, parentheses).",
            category: "utility",
            schema: json!({
                "type": "object",
                "properties": { "expression": { "type": "string", "description": "Arithmetic expression, e.g. \"(2+3)*4\"" } },
                "required": ["expression"]
            }),
        },
    ]
}

/// Returns `true` when `name` is a registered built-in tool.
pub fn is_allowed_tool(name: &str) -> bool {
    tool_registry().iter().any(|t| t.name == name)
}

// ---------------------------------------------------------------------------
// Endpoint: GET /api/tools
// ---------------------------------------------------------------------------

/// `GET /api/tools` -> `{"tools":[{name, description, category, schema}, ...]}`
pub fn list_tools() -> Value {
    let tools: Vec<ToolInfo> = tool_registry();
    json!({ "tools": tools })
}

// ---------------------------------------------------------------------------
// Endpoint: GET /api/marketplace
// ---------------------------------------------------------------------------

/// Static sample catalog. Kept deliberately simple — no plugin loader, no
/// downloads; a fuller plugin system can be layered on later.
fn marketplace_catalog() -> Vec<MarketplaceItem> {
    vec![
        MarketplaceItem {
            id: "web-scraper",
            name: "Web Scraper",
            description: "Fetch a page and extract its main text content.",
            kind: "plugin",
            category: "web",
        },
        MarketplaceItem {
            id: "code-review",
            name: "Code Reviewer",
            description: "Review a diff and suggest improvements.",
            kind: "plugin",
            category: "developer",
        },
        MarketplaceItem {
            id: "git-helper",
            name: "Git Helper",
            description: "Common git workflows: status, diff summary, commit drafting.",
            kind: "tool",
            category: "developer",
        },
        MarketplaceItem {
            id: "note-taker",
            name: "Note Taker",
            description: "Capture quick notes into a dated markdown journal.",
            kind: "plugin",
            category: "productivity",
        },
        MarketplaceItem {
            id: "rss-reader",
            name: "RSS Reader",
            description: "Follow RSS/Atom feeds and summarize new entries.",
            kind: "plugin",
            category: "news",
        },
        MarketplaceItem {
            id: "translator",
            name: "Translator",
            description: "Translate text between Vietnamese and English.",
            kind: "tool",
            category: "utility",
        },
        MarketplaceItem {
            id: "todo-board",
            name: "Todo Board",
            description: "A simple persistent todo list with priorities.",
            kind: "plugin",
            category: "productivity",
        },
        MarketplaceItem {
            id: "weather",
            name: "Weather",
            description: "Current weather and short forecast for a city.",
            kind: "tool",
            category: "utility",
        },
        MarketplaceItem {
            id: "pdf-reader",
            name: "PDF Reader",
            description: "Extract text from PDF documents.",
            kind: "plugin",
            category: "documents",
        },
        MarketplaceItem {
            id: "timer",
            name: "Timer",
            description: "Countdown timer with desktop notification on completion.",
            kind: "tool",
            category: "productivity",
        },
    ]
}

/// `GET /api/marketplace` -> `{"items":[{id, name, description, kind, category}, ...]}`
pub fn list_marketplace() -> Value {
    let items: Vec<MarketplaceItem> = marketplace_catalog();
    json!({ "items": items })
}

// ---------------------------------------------------------------------------
// Endpoint: POST /api/tools/run
// ---------------------------------------------------------------------------

/// Errors that can occur while running a tool.
#[derive(Debug)]
enum ToolError {
    UnknownTool(String),
    MissingArg(&'static str),
    InvalidArg(String),
    PathRejected(String),
    Io(io::Error),
    CommandFailed(String),
    CalcError(String),
}

impl fmt::Display for ToolError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ToolError::UnknownTool(name) => write!(f, "unknown tool: {name}"),
            ToolError::MissingArg(arg) => write!(f, "missing required argument: {arg}"),
            ToolError::InvalidArg(msg) => write!(f, "invalid argument: {msg}"),
            ToolError::PathRejected(msg) => write!(f, "path rejected: {msg}"),
            ToolError::Io(e) => write!(f, "io error: {e}"),
            ToolError::CommandFailed(msg) => write!(f, "command failed: {msg}"),
            ToolError::CalcError(msg) => write!(f, "calculator error: {msg}"),
        }
    }
}

impl From<io::Error> for ToolError {
    fn from(e: io::Error) -> Self {
        ToolError::Io(e)
    }
}

fn ok(result: String) -> Value {
    json!({ "ok": true, "result": result })
}

fn err(e: ToolError) -> Value {
    json!({ "ok": false, "error": e.to_string() })
}

/// `POST /api/tools/run` with body `{"tool": "...", "args": {...}}`.
///
/// Returns `{"ok": true, "result": "..."}` on success or
/// `{"ok": false, "error": "..."}` on failure. Never panics on
/// attacker-controlled input: all errors are returned as JSON.
pub fn run_tool(tool: &str, args: &Value) -> Value {
    if !is_allowed_tool(tool) {
        return err(ToolError::UnknownTool(tool.to_owned()));
    }

    let outcome: Result<String, ToolError> = match tool {
        "read_file" => tool_read_file(args),
        "write_file" => tool_write_file(args),
        "append_file" => tool_append_file(args),
        "list_dir" => tool_list_dir(args),
        "delete_file" => tool_delete_file(args),
        "web_search" => tool_web_search(args),
        "shell_run" => tool_shell_run(args),
        "get_time" => tool_get_time(args),
        "get_cwd" => tool_get_cwd(args),
        "calculator" => tool_calculator(args),
        // is_allowed_tool() guarantees one of the arms above; this branch is
        // unreachable in practice but keeps the match exhaustive if the
        // registry ever changes.
        _ => Err(ToolError::UnknownTool(tool.to_owned())),
    };

    match outcome {
        Ok(result) => ok(result),
        Err(e) => err(e),
    }
}

// ---------------------------------------------------------------------------
// Argument helpers
// ---------------------------------------------------------------------------

fn arg_str<'a>(args: &'a Value, name: &'static str) -> Result<&'a str, ToolError> {
    args.get(name)
        .and_then(Value::as_str)
        .ok_or(ToolError::MissingArg(name))
}

// ---------------------------------------------------------------------------
// Path security
// ---------------------------------------------------------------------------

/// Validate a user-supplied path for file tools.
///
/// Rejects:
/// * empty paths and NUL bytes,
/// * any `..` (parent directory) component — the classic path traversal,
/// * any path component merely *containing* `..` (e.g. `....//`), so the
///   check does not depend on platform-specific separator rules: a backslash
///   is a separator on Windows but an ordinary filename character on Unix,
///   and traversal must be blocked on both.
///
///   Note: this deliberately does *not* resolve symlinks or canonicalize the
///   path, so a symlink inside the working tree pointing outside can still
///   escape. Full sandboxing (canonicalize + prefix check against an allowed
///   root) should be added when these tools are exposed beyond the local
///   desktop user.
fn sanitize_path(raw: &str) -> Result<PathBuf, ToolError> {
    if raw.is_empty() {
        return Err(ToolError::PathRejected("empty path".to_owned()));
    }
    if raw.contains('\0') {
        return Err(ToolError::PathRejected("path contains NUL byte".to_owned()));
    }
    let path = Path::new(raw);
    for component in path.components() {
        match component {
            Component::ParentDir => {
                return Err(ToolError::PathRejected(format!(
                    "parent directory component not allowed: {raw}"
                )))
            }
            Component::Prefix(_) => {
                return Err(ToolError::PathRejected(format!(
                    "path prefix not allowed: {raw}"
                )))
            }
            Component::Normal(name) => {
                // Platform-independent hardening: even where `..` is not a
                // separator-adjacent component (e.g. `..\x` on Unix), a
                // component containing ".." is never a legitimate target for
                // these tools and is rejected outright.
                if name.to_string_lossy().contains("..") {
                    return Err(ToolError::PathRejected(format!(
                        "suspicious path component not allowed: {raw}"
                    )));
                }
            }
            Component::CurDir | Component::RootDir => {}
        }
    }
    Ok(path.to_path_buf())
}

// ---------------------------------------------------------------------------
// Tool implementations
// ---------------------------------------------------------------------------

fn tool_read_file(args: &Value) -> Result<String, ToolError> {
    let raw = arg_str(args, "path")?;
    let path = sanitize_path(raw)?;
    let metadata = std::fs::metadata(&path)?;
    if !metadata.is_file() {
        return Err(ToolError::InvalidArg(format!("not a file: {raw}")));
    }
    if metadata.len() > MAX_READ_BYTES {
        return Err(ToolError::InvalidArg(format!(
            "file too large ({} bytes, max {} bytes)",
            metadata.len(),
            MAX_READ_BYTES
        )));
    }
    // Read at most MAX_READ_BYTES + 1 so a file that grew between the
    // metadata check and the read is still capped.
    let file = std::fs::File::open(&path)?;
    let mut limited = file.take(MAX_READ_BYTES + 1);
    let mut content = String::new();
    use std::io::Read as _;
    limited.read_to_string(&mut content)?;
    if content.len() as u64 > MAX_READ_BYTES {
        return Err(ToolError::InvalidArg(format!(
            "file too large (over {} bytes)",
            MAX_READ_BYTES
        )));
    }
    Ok(content)
}

fn tool_write_file(args: &Value) -> Result<String, ToolError> {
    let raw = arg_str(args, "path")?;
    let content = arg_str(args, "content")?;
    if content.len() > MAX_WRITE_BYTES {
        return Err(ToolError::InvalidArg(format!(
            "content too large ({} bytes, max {} bytes)",
            content.len(),
            MAX_WRITE_BYTES
        )));
    }
    let path = sanitize_path(raw)?;
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }
    std::fs::write(&path, content)?;
    Ok(format!("wrote {} bytes to {raw}", content.len()))
}

fn tool_append_file(args: &Value) -> Result<String, ToolError> {
    let raw = arg_str(args, "path")?;
    let content = arg_str(args, "content")?;
    let path = sanitize_path(raw)?;
    if path.exists() {
        let existing = std::fs::metadata(&path)?.len() as usize;
        if existing.saturating_add(content.len()) > MAX_WRITE_BYTES {
            return Err(ToolError::InvalidArg(format!(
                "append would exceed {} bytes",
                MAX_WRITE_BYTES
            )));
        }
    } else if content.len() > MAX_WRITE_BYTES {
        return Err(ToolError::InvalidArg(format!(
            "content too large ({} bytes, max {} bytes)",
            content.len(),
            MAX_WRITE_BYTES
        )));
    }
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }
    use std::io::Write as _;
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)?;
    file.write_all(content.as_bytes())?;
    Ok(format!("appended {} bytes to {raw}", content.len()))
}

fn tool_list_dir(args: &Value) -> Result<String, ToolError> {
    let raw = arg_str(args, "path")?;
    let path = sanitize_path(raw)?;
    let metadata = std::fs::metadata(&path)?;
    if !metadata.is_dir() {
        return Err(ToolError::InvalidArg(format!("not a directory: {raw}")));
    }
    let mut names: Vec<String> = Vec::new();
    for entry in std::fs::read_dir(&path)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        let suffix = if entry.file_type()?.is_dir() { "/" } else { "" };
        names.push(format!("{name}{suffix}"));
    }
    names.sort();
    Ok(names.join("\n"))
}

fn tool_delete_file(args: &Value) -> Result<String, ToolError> {
    let raw = arg_str(args, "path")?;
    let path = sanitize_path(raw)?;
    let metadata = std::fs::metadata(&path)?;
    if !metadata.is_file() {
        return Err(ToolError::InvalidArg(format!(
            "refusing to delete non-file: {raw}"
        )));
    }
    std::fs::remove_file(&path)?;
    Ok(format!("deleted {raw}"))
}

fn tool_web_search(args: &Value) -> Result<String, ToolError> {
    let query = arg_str(args, "query")?;
    // Placeholder: no search provider is configured in the Rust backend yet.
    Ok(format!(
        "web_search is not configured (query was: {query}). \
         Set up a search provider to enable this tool."
    ))
}

/// Returns `true` if `s` contains any shell metacharacter.
fn contains_shell_meta(s: &str) -> bool {
    s.chars().any(|c| SHELL_META.contains(&c))
}

fn tool_shell_run(args: &Value) -> Result<String, ToolError> {
    let command = arg_str(args, "command")?;
    if !SHELL_WHITELIST.contains(&command) {
        return Err(ToolError::InvalidArg(format!(
            "command not whitelisted: {command} (allowed: {})",
            SHELL_WHITELIST.join(", ")
        )));
    }
    // Reject anything that looks like a path or flag-smuggling attempt on the
    // command itself; only a bare whitelisted name is accepted.
    if command.contains('/') || command.contains('\\') {
        return Err(ToolError::InvalidArg(format!(
            "command must be a bare name: {command}"
        )));
    }

    let mut cmd_args: Vec<String> = Vec::new();
    if let Some(Value::Array(items)) = args.get("args") {
        for item in items {
            let s = item
                .as_str()
                .ok_or_else(|| ToolError::InvalidArg("args must be strings".to_owned()))?;
            if contains_shell_meta(s) {
                return Err(ToolError::InvalidArg(format!(
                    "argument contains shell metacharacter: {s}"
                )));
            }
            cmd_args.push(s.to_owned());
        }
    }

    // No shell is spawned: the binary is executed directly, so even a missed
    // metacharacter could not be interpreted by sh.
    let output = Command::new(command)
        .args(&cmd_args)
        .output()
        .map_err(|e| ToolError::CommandFailed(format!("failed to spawn {command}: {e}")))?;

    let mut result = String::from_utf8_lossy(&output.stdout).into_owned();
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(ToolError::CommandFailed(format!(
            "exit {}: {}{}",
            output.status.code().unwrap_or(-1),
            result,
            stderr
        )));
    }
    // Trim one trailing newline for cleaner JSON output; keep it simple.
    if result.ends_with('\n') {
        result.pop();
    }
    Ok(result)
}

/// Convert Unix seconds to an ISO-8601-ish UTC string without any date
/// library (Howard Hinnant's civil-from-days algorithm).
fn unix_to_utc_string(secs: u64) -> String {
    let days = (secs / 86_400) as i64;
    let time_of_day = secs % 86_400;

    // days -> civil date
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = doy - (153 * mp + 2) / 5 + 1; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
    let year = if m <= 2 { y + 1 } else { y };

    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        year,
        m,
        d,
        time_of_day / 3600,
        (time_of_day % 3600) / 60,
        time_of_day % 60
    )
}

fn tool_get_time(_args: &Value) -> Result<String, ToolError> {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|e| ToolError::CommandFailed(format!("system clock error: {e}")))?;
    let secs = now.as_secs();
    Ok(format!("{} (unix: {})", unix_to_utc_string(secs), secs))
}

fn tool_get_cwd(_args: &Value) -> Result<String, ToolError> {
    let cwd = std::env::current_dir()?;
    Ok(cwd.to_string_lossy().into_owned())
}

// ---------------------------------------------------------------------------
// Calculator: tiny recursive-descent parser (no eval)
// ---------------------------------------------------------------------------

struct CalcParser<'a> {
    chars: Vec<char>,
    pos: usize,
    _marker: std::marker::PhantomData<&'a ()>,
}

impl<'a> CalcParser<'a> {
    fn new(input: &'a str) -> Self {
        Self {
            chars: input.chars().collect(),
            pos: 0,
            _marker: std::marker::PhantomData,
        }
    }

    fn peek(&self) -> Option<char> {
        self.chars.get(self.pos).copied()
    }

    fn skip_ws(&mut self) {
        while matches!(self.peek(), Some(c) if c.is_whitespace()) {
            self.pos += 1;
        }
    }

    fn parse(&mut self) -> Result<f64, String> {
        let value = self.parse_add()?;
        self.skip_ws();
        if self.pos != self.chars.len() {
            return Err(format!(
                "unexpected character: '{}'",
                self.peek().unwrap_or('?')
            ));
        }
        Ok(value)
    }

    fn parse_add(&mut self) -> Result<f64, String> {
        let mut value = self.parse_mul()?;
        loop {
            self.skip_ws();
            match self.peek() {
                Some('+') => {
                    self.pos += 1;
                    value += self.parse_mul()?;
                }
                Some('-') => {
                    self.pos += 1;
                    value -= self.parse_mul()?;
                }
                _ => break,
            }
        }
        Ok(value)
    }

    fn parse_mul(&mut self) -> Result<f64, String> {
        let mut value = self.parse_unary()?;
        loop {
            self.skip_ws();
            match self.peek() {
                Some('*') => {
                    self.pos += 1;
                    value *= self.parse_unary()?;
                }
                Some('/') => {
                    self.pos += 1;
                    let rhs = self.parse_unary()?;
                    if rhs == 0.0 {
                        return Err("division by zero".to_owned());
                    }
                    value /= rhs;
                }
                Some('%') => {
                    self.pos += 1;
                    let rhs = self.parse_unary()?;
                    if rhs == 0.0 {
                        return Err("modulo by zero".to_owned());
                    }
                    value %= rhs;
                }
                _ => break,
            }
        }
        Ok(value)
    }

    fn parse_unary(&mut self) -> Result<f64, String> {
        self.skip_ws();
        match self.peek() {
            Some('-') => {
                self.pos += 1;
                Ok(-self.parse_unary()?)
            }
            Some('+') => {
                self.pos += 1;
                self.parse_unary()
            }
            _ => self.parse_primary(),
        }
    }

    fn parse_primary(&mut self) -> Result<f64, String> {
        self.skip_ws();
        match self.peek() {
            Some('(') => {
                self.pos += 1;
                let value = self.parse_add()?;
                self.skip_ws();
                if self.peek() == Some(')') {
                    self.pos += 1;
                    Ok(value)
                } else {
                    Err("missing closing parenthesis".to_owned())
                }
            }
            Some(c) if c.is_ascii_digit() || c == '.' => self.parse_number(),
            Some(c) => Err(format!("unexpected character: '{c}'")),
            None => Err("unexpected end of expression".to_owned()),
        }
    }

    fn parse_number(&mut self) -> Result<f64, String> {
        let start = self.pos;
        let mut seen_dot = false;
        while let Some(c) = self.peek() {
            if c.is_ascii_digit() {
                self.pos += 1;
            } else if c == '.' && !seen_dot {
                seen_dot = true;
                self.pos += 1;
            } else {
                break;
            }
        }
        let text: String = self.chars[start..self.pos].iter().collect();
        text.parse::<f64>()
            .map_err(|_| format!("invalid number: '{text}'"))
    }
}

fn tool_calculator(args: &Value) -> Result<String, ToolError> {
    let expr = arg_str(args, "expression")?;
    if expr.len() > 256 {
        return Err(ToolError::CalcError("expression too long".to_owned()));
    }
    let mut parser = CalcParser::new(expr);
    match parser.parse() {
        Ok(value) => {
            // Print integers without a trailing ".0" for cleaner output.
            if value.fract() == 0.0 && value.abs() < 1e15 {
                Ok(format!("{}", value as i64))
            } else {
                Ok(format!("{value}"))
            }
        }
        Err(msg) => Err(ToolError::CalcError(msg)),
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEST_COUNTER: AtomicU64 = AtomicU64::new(0);

    /// Unique temp dir per test so tests can run in parallel safely.
    fn unique_temp_dir(prefix: &str) -> PathBuf {
        let id = TEST_COUNTER.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!(
            "boom_tools_test_{}_{}_{}",
            prefix,
            std::process::id(),
            id
        ));
        std::fs::create_dir_all(&dir).expect("create temp dir");
        dir
    }

    fn ok_result(v: &Value) -> Option<&str> {
        if v.get("ok").and_then(Value::as_bool) == Some(true) {
            v.get("result").and_then(Value::as_str)
        } else {
            None
        }
    }

    fn err_message(v: &Value) -> Option<&str> {
        if v.get("ok").and_then(Value::as_bool) == Some(false) {
            v.get("error").and_then(Value::as_str)
        } else {
            None
        }
    }

    // -- GET /api/tools ----------------------------------------------------

    #[test]
    fn list_tools_returns_expected_shape() {
        let v = list_tools();
        let tools = v
            .get("tools")
            .and_then(Value::as_array)
            .expect("tools array");
        assert!(tools.len() >= 7, "expected at least the 7 core tools");
        for t in tools {
            for field in ["name", "description", "category", "schema"] {
                assert!(t.get(field).is_some(), "tool missing field {field}");
            }
        }
        let names: Vec<&str> = tools
            .iter()
            .filter_map(|t| t.get("name").and_then(Value::as_str))
            .collect();
        for required in [
            "read_file",
            "write_file",
            "list_dir",
            "web_search",
            "shell_run",
            "get_time",
            "calculator",
        ] {
            assert!(names.contains(&required), "missing tool {required}");
        }
    }

    // -- POST /api/tools/run: unknown tool ---------------------------------

    #[test]
    fn unknown_tool_is_rejected() {
        let v = run_tool("rm_rf_everything", &json!({}));
        let msg = err_message(&v).expect("expected error");
        assert!(msg.contains("unknown tool"), "unexpected: {msg}");
    }

    #[test]
    fn unknown_tool_similar_to_whitelisted_is_rejected() {
        // Case sensitivity / lookalikes must not slip through the whitelist.
        for name in ["READ_FILE", "read-file", "read_file ", " shell_run"] {
            let v = run_tool(name, &json!({}));
            assert!(
                err_message(&v).is_some(),
                "name {name:?} should be rejected"
            );
        }
    }

    // -- read_file ----------------------------------------------------------

    #[test]
    fn read_file_roundtrip() {
        let dir = unique_temp_dir("read");
        let path = dir.join("hello.txt");
        std::fs::write(&path, "hello boom").expect("write fixture");
        let v = run_tool("read_file", &json!({ "path": path.to_str().unwrap() }));
        assert_eq!(ok_result(&v), Some("hello boom"));
    }

    #[test]
    fn read_file_rejects_path_traversal() {
        for evil in [
            "../secret.txt",
            "a/../../etc/passwd",
            "..\\windows\\secret.txt",
            "/tmp/../etc/passwd",
            "....//etc/passwd",
        ] {
            let v = run_tool("read_file", &json!({ "path": evil }));
            let msg = err_message(&v).expect("expected traversal to be blocked");
            assert!(
                msg.contains("path rejected") || msg.contains("parent directory"),
                "path {evil:?} was not blocked as traversal: {msg}"
            );
        }
    }

    #[test]
    fn read_file_rejects_oversize_file() {
        let dir = unique_temp_dir("big");
        let path = dir.join("big.bin");
        let big = vec![b'x'; (MAX_READ_BYTES + 1024) as usize];
        std::fs::write(&path, big).expect("write big fixture");
        let v = run_tool("read_file", &json!({ "path": path.to_str().unwrap() }));
        let msg = err_message(&v).expect("expected size error");
        assert!(msg.contains("too large"), "unexpected: {msg}");
    }

    #[test]
    fn read_file_rejects_missing_arg() {
        let v = run_tool("read_file", &json!({}));
        let msg = err_message(&v).expect("expected error");
        assert!(
            msg.contains("missing required argument"),
            "unexpected: {msg}"
        );
    }

    #[test]
    fn read_file_rejects_directory() {
        let dir = unique_temp_dir("isdir");
        let v = run_tool("read_file", &json!({ "path": dir.to_str().unwrap() }));
        assert!(err_message(&v).is_some());
    }

    // -- write_file / append_file / delete_file ----------------------------

    #[test]
    fn write_then_append_then_read() {
        let dir = unique_temp_dir("write");
        let path = dir.join("sub").join("note.txt");
        let p = path.to_str().unwrap().to_owned();

        let v = run_tool("write_file", &json!({ "path": p, "content": "line1\n" }));
        assert!(ok_result(&v).is_some(), "write failed: {v}");

        let v = run_tool("append_file", &json!({ "path": p, "content": "line2\n" }));
        assert!(ok_result(&v).is_some(), "append failed: {v}");

        let v = run_tool("read_file", &json!({ "path": p }));
        assert_eq!(ok_result(&v), Some("line1\nline2\n"));
    }

    #[test]
    fn write_file_rejects_path_traversal() {
        let v = run_tool(
            "write_file",
            &json!({ "path": "../evil.txt", "content": "pwned" }),
        );
        assert!(err_message(&v).is_some(), "traversal write must be blocked");
    }

    #[test]
    fn delete_file_removes_only_files() {
        let dir = unique_temp_dir("delete");
        let path = dir.join("gone.txt");
        std::fs::write(&path, "bye").expect("write fixture");
        let p = path.to_str().unwrap().to_owned();

        let v = run_tool("delete_file", &json!({ "path": p }));
        assert!(ok_result(&v).is_some(), "delete failed: {v}");
        assert!(!path.exists());

        // Refusing to delete directories.
        let v = run_tool("delete_file", &json!({ "path": dir.to_str().unwrap() }));
        let msg = err_message(&v).expect("expected refusal");
        assert!(msg.contains("non-file"), "unexpected: {msg}");
    }

    // -- list_dir ------------------------------------------------------------

    #[test]
    fn list_dir_lists_entries() {
        let dir = unique_temp_dir("ls");
        std::fs::write(dir.join("b.txt"), "").expect("fixture");
        std::fs::write(dir.join("a.txt"), "").expect("fixture");
        std::fs::create_dir(dir.join("sub")).expect("fixture");
        let v = run_tool("list_dir", &json!({ "path": dir.to_str().unwrap() }));
        let out = ok_result(&v).expect("list_dir failed");
        assert!(out.contains("a.txt"));
        assert!(out.contains("b.txt"));
        assert!(
            out.contains("sub/"),
            "directories should be suffixed: {out}"
        );
    }

    #[test]
    fn list_dir_rejects_traversal() {
        let v = run_tool("list_dir", &json!({ "path": ".." }));
        assert!(err_message(&v).is_some());
    }

    // -- shell_run -----------------------------------------------------------

    #[test]
    fn shell_run_allows_whitelisted_commands() {
        let v = run_tool(
            "shell_run",
            &json!({ "command": "echo", "args": ["hello"] }),
        );
        assert_eq!(ok_result(&v), Some("hello"));
    }

    #[test]
    fn shell_run_rejects_non_whitelisted_command() {
        for cmd in ["rm", "sh", "bash", "curl", "python3", "/bin/echo", "./echo"] {
            let v = run_tool("shell_run", &json!({ "command": cmd, "args": [] }));
            let msg = err_message(&v).expect("expected rejection");
            assert!(
                msg.contains("not whitelisted") || msg.contains("bare name"),
                "command {cmd:?} not blocked properly: {msg}"
            );
        }
    }

    #[test]
    fn shell_run_rejects_shell_metacharacters_in_args() {
        for evil in [
            "hi; rm -rf /",
            "a|b",
            "$(whoami)",
            "`id`",
            "a&&b",
            "x > /tmp/out",
            "a'b",
            "a\"b",
        ] {
            let v = run_tool("shell_run", &json!({ "command": "echo", "args": [evil] }));
            let msg = err_message(&v).expect("expected metachar rejection");
            assert!(
                msg.contains("metacharacter"),
                "arg {evil:?} not blocked: {msg}"
            );
        }
    }

    #[test]
    fn shell_run_command_injection_via_fake_command_is_rejected() {
        // Even if the whitelist check were bypassed, no shell is spawned, but
        // the whitelist itself must stop this first.
        let v = run_tool(
            "shell_run",
            &json!({ "command": "echo hello; id", "args": [] }),
        );
        assert!(err_message(&v).is_some());
    }

    // -- web_search (placeholder) --------------------------------------------

    #[test]
    fn web_search_returns_not_configured() {
        let v = run_tool("web_search", &json!({ "query": "rust" }));
        let out = ok_result(&v).expect("web_search should succeed as placeholder");
        assert!(out.contains("not configured"), "unexpected: {out}");
    }

    // -- get_time / get_cwd ---------------------------------------------------

    #[test]
    fn get_time_returns_parseable_output() {
        let v = run_tool("get_time", &json!({}));
        let out = ok_result(&v).expect("get_time failed");
        assert!(
            out.contains('T') && out.ends_with('Z') || out.contains("unix:"),
            "unexpected time format: {out}"
        );
    }

    #[test]
    fn get_cwd_returns_existing_dir() {
        let v = run_tool("get_cwd", &json!({}));
        let out = ok_result(&v).expect("get_cwd failed");
        assert!(Path::new(out).is_dir(), "cwd is not a dir: {out}");
    }

    // -- calculator ------------------------------------------------------------

    #[test]
    fn calculator_evaluates_expressions() {
        let cases = [
            ("2+3*4", "14"),
            ("(2+3)*4", "20"),
            ("10/4", "2.5"),
            ("10%3", "1"),
            ("-5+2", "-3"),
            (" 2 * (3 + 4) ", "14"),
            ("2.5*2", "5"),
        ];
        for (expr, expected) in cases {
            let v = run_tool("calculator", &json!({ "expression": expr }));
            assert_eq!(
                ok_result(&v),
                Some(expected),
                "wrong result for {expr:?}: {v}"
            );
        }
    }

    #[test]
    fn calculator_rejects_bad_input() {
        for expr in [
            "",
            "2+",
            "2/0",
            "10%0",
            "(2+3",
            "2+foo",
            "__import__",
            "1;2",
        ] {
            let v = run_tool("calculator", &json!({ "expression": expr }));
            assert!(
                err_message(&v).is_some(),
                "expression {expr:?} should be rejected"
            );
        }
    }

    // -- GET /api/marketplace ---------------------------------------------------

    #[test]
    fn marketplace_returns_expected_shape() {
        let v = list_marketplace();
        let items = v
            .get("items")
            .and_then(Value::as_array)
            .expect("items array");
        assert!(items.len() >= 10, "expected ~10 sample items");
        for item in items {
            for field in ["id", "name", "description", "kind", "category"] {
                assert!(item.get(field).is_some(), "item missing field {field}");
            }
            let kind = item.get("kind").and_then(Value::as_str).unwrap();
            assert!(kind == "plugin" || kind == "tool", "invalid kind: {kind}");
        }
        let ids: Vec<&str> = items
            .iter()
            .filter_map(|i| i.get("id").and_then(Value::as_str))
            .collect();
        let unique: std::collections::HashSet<&str> = ids.iter().copied().collect();
        assert_eq!(ids.len(), unique.len(), "duplicate marketplace ids");
    }

    // -- request deserialization --------------------------------------------------

    #[test]
    fn run_tool_request_deserializes() {
        let req: RunToolRequest =
            serde_json::from_str(r#"{"tool":"read_file","args":{"path":"a.txt"}}"#)
                .expect("deserialize");
        assert_eq!(req.tool, "read_file");
        assert_eq!(req.args.get("path").and_then(Value::as_str), Some("a.txt"));
    }

    // -- sanitize_path unit tests --------------------------------------------------

    #[test]
    fn sanitize_path_allows_normal_relative_paths() {
        assert!(sanitize_path("notes/todo.txt").is_ok());
        assert!(sanitize_path("/tmp/abs.txt").is_ok());
        assert!(sanitize_path("./rel.txt").is_ok());
    }

    #[test]
    fn sanitize_path_rejects_nul_and_empty() {
        assert!(sanitize_path("").is_err());
        assert!(sanitize_path("a\0b").is_err());
    }
}
