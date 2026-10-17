//! Shared server state and the injection traits other workers implement.
//!
//! `AppState` is `Clone` (everything behind it is `Arc`) so Axum can share it
//! across handlers. The traits are object-safe (`dyn`-compatible); the
//! default implementations live in [`super::defaults`].

use std::{
    collections::{BTreeSet, HashMap, VecDeque},
    future::Future,
    path::PathBuf,
    pin::Pin,
    sync::{Arc, Mutex},
    time::Instant,
};

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// Boxed future shorthand used by the object-safe async trait methods.
pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Mirrors Python `MAX_BODY_BYTES` (1 MiB request-body cap).
pub const MAX_BODY_BYTES: usize = 1024 * 1024;
/// Mirrors Python `_LOG_MAX`: the /api/logs ring buffer keeps 200 entries.
pub const LOG_MAX: usize = 200;
/// Mirrors Python `_ACTIVITY_FRESH_SEC`: newest event within this window
/// counts as the "current" activity.
pub const ACTIVITY_FRESH_SEC: i64 = 30;
/// Mirrors Python `CHAT_STRATEGIES`.
pub const CHAT_STRATEGIES: &[&str] = &["auto", "react", "dag", "mass"];
/// Mirrors Python `KNOWN_CHANNELS`: the fixed channel list for
/// `GET /api/channels/status`.
pub const KNOWN_CHANNELS: &[&str] = &[
    "telegram", "discord", "slack", "whatsapp", "line", "feishu", "zalo", "messenger",
];

/// Mirrors Python `CHANNEL_CREDENTIAL_HINT`: the honest "why not connected"
/// detail shown per channel when the daemon cannot deliver.
pub fn channel_credential_hint(name: &str) -> &'static str {
    match name {
        "telegram" => "TELEGRAM_BOT_TOKEN not set",
        "discord" => "discord token not given to the gateway daemon",
        "slack" => "BOOM_SLACK_BOT_TOKEN not set",
        "whatsapp" => "BOOM_WHATSAPP_TOKEN not set",
        "line" => "BOOM_LINE_CHANNEL_TOKEN not set",
        "feishu" => "BOOM_FEISHU_APP_ID / BOOM_FEISHU_APP_SECRET not set",
        "zalo" => "BOOM_ZALO_OA_ACCESS_TOKEN not set",
        "messenger" => "BOOM_MESSENGER_PAGE_TOKEN not set",
        _ => "not configured",
    }
}

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------

/// Which tools a chat turn may use, resolved from a custom bot's config.
/// Mirrors `_resolve_bot_context`: allowlist, the deny-only sentinel, or the
/// full toolset.
#[derive(Clone, Debug)]
pub enum BotTools {
    /// Full toolset.
    All,
    /// Explicit allowlist (allowed minus denied).
    Allow(Vec<String>),
    /// All tools except these (deny-only bot).
    DenyOnly(Vec<String>),
}

/// Input for one agent turn.
#[derive(Clone, Debug)]
pub struct TurnRequest {
    pub prompt: String,
    pub strategy: String,
    pub session_id: String,
    pub system_prompt: Option<String>,
    pub tools: BotTools,
}

/// Output of one agent turn. Serialized as
/// `{reply, session_id, strategy, errors}` — the exact shape the frontend's
/// `parseChatReply` requires.
#[derive(Clone, Debug, Default)]
pub struct TurnResult {
    pub reply: String,
    pub strategy: String,
    pub errors: Map<String, Value>,
}

/// Why a chat turn could not run.
#[derive(Clone, Debug)]
pub enum TurnError {
    /// No brain wired — the server answers 503 like Python's
    /// `"agent brain not configured"`.
    NotConfigured,
    /// The turn itself failed.
    Failed(String),
}

/// The agent backend (Python: `BoomAPI` + injected `brain`/`loop`/`daemon`).
pub trait Harness: Send + Sync {
    /// LLM model name reported by `GET /api/health`; `None` → "unconfigured".
    fn model_name(&self) -> Option<String> {
        None
    }

    /// Whether the gateway daemon can deliver to `channel`
    /// (drives `GET /api/channels/status`).
    fn can_deliver(&self, _channel: &str) -> bool {
        false
    }

    /// Restore a ReAct session before the turn. `Err` → 500
    /// `"failed to load session: ..."`.
    fn load_session(&self, _session_id: &str) -> Result<(), String> {
        Ok(())
    }

    /// `false` when no brain is wired — `/api/chat` answers 503 without
    /// taking the turn lock, mirroring Python's `brain is None` check.
    fn is_configured(&self) -> bool {
        true
    }

    /// Run one chat turn.
    fn run_turn(&self, req: TurnRequest) -> BoxFuture<'_, Result<TurnResult, TurnError>>;

    /// Last DAG run nodes for `GET /api/dag/status`. Empty when the brain
    /// records no last-run state (the honest Python behaviour).
    fn dag_nodes(&self) -> Vec<DagNode> {
        Vec::new()
    }
}

/// One DAG node in the `GET /api/dag/status` contract shape.
#[derive(Clone, Debug, Serialize)]
pub struct DagNode {
    pub id: String,
    pub name: String,
    pub status: String,
    pub dependencies: Vec<String>,
}

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

/// Tool metadata for `GET /api/tools`: `{name, description, group}`.
#[derive(Clone, Debug)]
pub struct ToolInfo {
    pub name: String,
    pub description: String,
    pub group: String,
}

/// Tool registry (Python: `AgentEnvironment.get_tools()`).
pub trait ToolRegistry: Send + Sync {
    fn list_tools(&self) -> Vec<ToolInfo>;
    /// Run one tool. `Ok` carries the stringified result, `Err` the error
    /// message — both map to HTTP 200 with `{ok: true/false, ...}` exactly
    /// like Python's `_handle_tool_run`.
    fn run_tool(&self, name: &str, args: &Map<String, Value>) -> BoxFuture<'_, Result<String, String>>;
}

// ---------------------------------------------------------------------------
// Providers
// ---------------------------------------------------------------------------

/// Provider metadata (Python: `ProviderManager.list_providers()` item).
/// Never carries secrets; the route adds `has_key` itself.
#[derive(Clone, Debug)]
pub struct ProviderMeta {
    pub id: String,
    pub name: String,
    pub models: Vec<String>,
    pub needs_key: bool,
    pub default_base_url: String,
    pub show_base_url: bool,
    pub allow_custom_model: bool,
}

/// `POST /api/providers/config` body.
#[derive(Clone, Debug)]
pub struct ProviderConfig {
    pub provider_id: String,
    pub api_key: String,
    pub model: String,
    pub base_url: Option<String>,
}

/// Config-save failure, mapped to the same status codes as Python.
#[derive(Clone, Debug)]
pub enum ProviderConfigError {
    /// 400 `"unknown provider: {id}"` (Python: `UnknownProviderError`).
    UnknownProvider(String),
    /// 400 with the message (Python: `ValueError`).
    Invalid(String),
    /// 500 `"failed to save provider config"`.
    Internal(String),
}

/// `GET /api/providers/ollama/status` shape.
#[derive(Clone, Debug, Serialize)]
pub struct OllamaStatus {
    pub running: bool,
    pub models: Vec<String>,
}

/// Provider registry (Python: `providers.manager.ProviderManager`).
pub trait ProviderRegistry: Send + Sync {
    fn list_providers(&self) -> Vec<ProviderMeta>;
    fn has_key(&self, id: &str) -> bool;
    fn active_provider(&self) -> Option<String>;
    fn save_config(&self, cfg: ProviderConfig) -> Result<(), ProviderConfigError>;
    /// Returns `(ok, message)`; `Err` → 500 `"failed to test provider connection"`.
    fn test_connection(&self, id: &str) -> BoxFuture<'_, Result<(bool, String), String>>;
    /// Probes the local Ollama daemon; never fails — answers
    /// `{running: false, models: []}` when Ollama is down.
    fn ollama_status(&self) -> BoxFuture<'_, OllamaStatus>;
}

// ---------------------------------------------------------------------------
// Storage: bots, groups, marketplace state
// ---------------------------------------------------------------------------

/// Custom bot row — the exact `GET /api/bots` item shape the frontend's
/// `parseBot` requires (`created_at`/`updated_at` included).
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Bot {
    pub id: String,
    pub name: String,
    pub description: String,
    pub system_prompt: String,
    pub avatar_emoji: String,
    pub allowed_tools: Vec<String>,
    pub denied_tools: Vec<String>,
    pub channel_bindings: Vec<String>,
    pub created_at: String,
    pub updated_at: String,
    pub is_active: bool,
}

/// Input for `POST /api/bots`.
#[derive(Clone, Debug)]
pub struct NewBot {
    pub name: String,
    pub description: String,
    pub system_prompt: String,
    pub avatar_emoji: String,
    pub allowed_tools: Option<Vec<String>>,
    pub denied_tools: Option<Vec<String>>,
    pub channel_bindings: Option<Vec<String>>,
}

/// Group row — the exact `GET /api/groups` item shape.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Group {
    pub channel: String,
    pub group_id: String,
    pub name: String,
    pub settings: Value,
    pub message_count: i64,
    pub member_count: i64,
    pub policy: Value,
    pub created_at: String,
    pub last_active: String,
}

/// Bot group: a multi-bot chat room. Bots in a group can exchange messages
/// with each other through `POST /api/bot-groups/{id}/message`, each reply
/// generated by a real harness turn — never faked.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct BotGroup {
    pub id: String,
    pub name: String,
    pub bot_ids: Vec<String>,
    /// Unix epoch seconds.
    pub created_at: i64,
}

/// One persisted message in a bot group's log: either an incoming message
/// (from a member bot, or from the user when `bot_id` is empty) or a bot's
/// harness-generated reply.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct BotGroupMessage {
    pub id: String,
    pub group_id: String,
    pub bot_id: String,
    pub bot_name: String,
    pub text: String,
    /// Unix epoch seconds.
    pub created_at: i64,
}

/// Chat session summary — the exact `GET /api/sessions` item shape.
/// Timestamps use the same `YYYY-MM-DDTHH:MM:SS` shape as the rest of the
/// API surface (bots, groups). `message_count` is the real row count from
/// the `messages` table (0 until the turn loop persists messages).
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SessionSummary {
    pub id: String,
    pub title: String,
    pub created_at: String,
    pub updated_at: String,
    pub message_count: u64,
}

/// Installed plugin metadata for marketplace catalog items.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PluginMeta {
    pub name: String,
    pub description: String,
    pub icon: Option<String>,
    pub version: String,
    pub author: String,
    pub enabled: bool,
}

/// Storage failure. `NotFound` → 404, `Invalid` → 400, `Internal` → 500 —
/// mirroring how the Python handlers map `{"error": ...}` dicts.
#[derive(Clone, Debug)]
pub enum StorageError {
    NotFound(String),
    Invalid(String),
    Internal(String),
}

/// Marketplace failure carrying the HTTP status the route should return
/// (Python: `MarketplaceError.status`).
#[derive(Clone, Debug)]
pub struct MarketplaceError {
    pub message: String,
    pub status: u16,
}

impl MarketplaceError {
    pub fn new(message: impl Into<String>, status: u16) -> Self {
        Self { message: message.into(), status }
    }
}

/// Persistent state for bots, groups and marketplace enablement
/// (Python: `GroupManager` + `BotManager` + marketplace state file).
pub trait Storage: Send + Sync {
    // -- bots --
    fn list_bots(&self) -> Vec<Bot>;
    fn create_bot(&self, input: NewBot) -> Result<Bot, StorageError>;
    fn get_bot(&self, id: &str) -> Option<Bot>;
    fn update_bot(&self, id: &str, patch: &Map<String, Value>) -> Result<Bot, StorageError>;
    fn delete_bot(&self, id: &str) -> bool;
    fn set_active_bot(&self, id: &str) -> bool;
    fn active_bot(&self) -> Option<Bot>;

    // -- groups --
    fn list_groups(&self) -> Vec<Group>;
    fn get_group(&self, channel: &str, group_id: &str) -> Option<Group>;
    fn set_group_muted(&self, channel: &str, group_id: &str, muted: bool) -> Result<(), StorageError>;
    fn set_group_policy(&self, channel: &str, group_id: &str, policy: &Value) -> Result<Value, StorageError>;

    // -- bot groups (multi-bot chat rooms) --
    /// Create a bot group. `Invalid` when the name is blank, `NotFound`
    /// when any bot id is unknown.
    fn create_bot_group(&self, name: &str, bot_ids: Vec<String>) -> Result<BotGroup, StorageError>;
    /// All bot groups, oldest first.
    fn list_bot_groups(&self) -> Vec<BotGroup>;
    fn get_bot_group(&self, id: &str) -> Option<BotGroup>;
    /// Delete a group and all its messages. `false` when the id is unknown.
    fn delete_bot_group(&self, id: &str) -> bool;
    /// Add a bot to a group (idempotent). `NotFound` for an unknown group
    /// or an unknown bot id.
    fn add_bot_to_group(&self, group_id: &str, bot_id: &str) -> Result<BotGroup, StorageError>;
    /// Remove a bot from a group. `NotFound` for an unknown group, or when
    /// the bot is not a member of it.
    fn remove_bot_from_group(&self, group_id: &str, bot_id: &str) -> Result<BotGroup, StorageError>;
    /// Persist one message in a group's log. `NotFound` for an unknown group.
    fn add_bot_group_message(
        &self,
        group_id: &str,
        bot_id: &str,
        bot_name: &str,
        text: &str,
    ) -> Result<BotGroupMessage, StorageError>;
    /// All messages in a group, oldest first. Empty for unknown groups.
    fn list_bot_group_messages(&self, group_id: &str) -> Vec<BotGroupMessage>;

    // -- sessions --
    /// List chat sessions, most recently active first. Rows are created
    /// when a chat turn runs (see [`touch_session`](Storage::touch_session)),
    /// so this is the real conversation list, never invented.
    fn list_sessions(&self) -> Vec<SessionSummary>;
    /// Create the session row if it does not exist yet, otherwise bump its
    /// `updated_at`. Idempotent; best-effort — a storage failure must never
    /// break the chat request that triggered it.
    fn touch_session(&self, session_id: &str);

    // -- marketplace --
    fn plugins(&self) -> Vec<PluginMeta>;
    fn enable_plugin(&self, name: &str) -> Result<(), StorageError>;
    /// Record (upsert) a plugin's metadata, e.g. after discovering it on
    /// disk. Overwrites any existing entry for the same name.
    fn install_plugin(&self, meta: &PluginMeta) -> Result<(), StorageError>;
    fn uninstall_plugin(&self, name: &str) -> bool;
    fn disabled_tools(&self) -> BTreeSet<String>;
    fn set_tool_disabled(&self, name: &str, disabled: bool);
}

// ---------------------------------------------------------------------------
// Updater, Zalo-personal
// ---------------------------------------------------------------------------

/// `GET /api/updates/check` shape (Python: `version.check_github_update()`).
#[derive(Clone, Debug, Serialize)]
pub struct UpdateInfo {
    pub has_update: bool,
    pub current_version: String,
    pub latest_version: String,
    pub download_url: String,
    pub release_notes: String,
    pub published_at: String,
}

/// Update checker (Python: `agent.src.version.check_github_update`).
/// `Err` → 502 `{"error": ...}`.
pub trait Updater: Send + Sync {
    fn check(&self) -> BoxFuture<'_, Result<UpdateInfo, String>>;
}

/// Zalo-personal login failure kinds.
#[derive(Clone, Debug)]
pub enum ZaloLoginError {
    /// 500 `"zalo-personal adapter is unavailable"`.
    Unavailable,
    /// 400 with a scrubbed message (Python: `ValueError`).
    InvalidCredentials(String),
    /// 500 with a scrubbed message (Python: `RuntimeError` / other).
    Failed(String),
}

/// Zalo-personal channel adapter (Python:
/// `gateway.channels.zalo_personal.ZaloPersonalAdapter`). The raw phone
/// number is never exposed — only `phone_masked`.
pub trait ZaloPersonal: Send + Sync {
    fn status(&self) -> (bool, Option<String>);
    fn logout(&self) -> Result<(), String>;
    fn login(&self, phone: &str, password: &str) -> BoxFuture<'_, Result<Option<String>, ZaloLoginError>>;
}

// ---------------------------------------------------------------------------
// Channels
// ---------------------------------------------------------------------------

/// Public status of one channel for `GET /api/channels`.
///
/// `status` is one of `connected`, `disconnected`, `not-configured` or
/// `unsupported`. Channels the Rust core does not implement report
/// `unsupported` with an honest `detail` — never "not wired".
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ChannelInfo {
    pub id: String,
    pub name: String,
    pub status: String,
    pub supported: bool,
    pub detail: Option<String>,
}

/// Info returned by a successful `POST /api/channels/:id/connect`.
#[derive(Clone, Debug, Serialize)]
pub struct ChannelConnectInfo {
    pub bot_username: Option<String>,
    pub message: String,
}

/// Channel failure carrying the HTTP status the route should return.
#[derive(Clone, Debug)]
pub struct ChannelError {
    pub status: u16,
    pub message: String,
}

impl ChannelError {
    pub fn new(status: u16, message: impl Into<String>) -> Self {
        Self { status, message: message.into() }
    }

    pub fn bad_request(message: impl Into<String>) -> Self {
        Self::new(400, message)
    }

    pub fn not_found(message: impl Into<String>) -> Self {
        Self::new(404, message)
    }

    /// Upstream (platform API) failure.
    pub fn upstream(message: impl Into<String>) -> Self {
        Self::new(502, message)
    }
}

/// Channel registry (Rust-native port of Python's `gateway/channel.py`
/// adapters + `KNOWN_CHANNELS`). Telegram is implemented for real against
/// the Bot API; every other platform reports `unsupported` honestly.
///
/// Secrets live only in the 0600 config file; they are never returned by
/// `info` and never appear in error messages or logs.
pub trait ChannelRegistry: Send + Sync {
    /// Fixed channel list (mirrors [`KNOWN_CHANNELS`] order).
    fn channel_ids(&self) -> Vec<String>;
    /// Current public status of one channel. Unknown ids report
    /// `unsupported` with `detail` "unknown channel".
    fn info(&self, id: &str) -> ChannelInfo;
    /// Save the credential after a live verification against the platform
    /// API. `Ok` only when the platform accepted the credential.
    fn connect(&self, id: &str, token: &str) -> BoxFuture<'_, Result<ChannelConnectInfo, ChannelError>>;
    /// Drop the stored credential. Idempotent: disconnecting an
    /// unconfigured channel succeeds.
    fn disconnect(&self, id: &str) -> Result<(), ChannelError>;
    /// Live self-test: with `chat_id` sends a test message, without it
    /// just re-verifies the stored credential. `Ok` carries a short
    /// human-readable confirmation.
    fn test(
        &self,
        id: &str,
        chat_id: Option<&str>,
        message: Option<&str>,
    ) -> BoxFuture<'_, Result<String, ChannelError>>;
}

/// `$BOOM_HOME`, else `~/.boom` (Python: `os.environ.get("BOOM_HOME", ...)`
/// in `agent/src/config.py`). Touches no files.
pub(crate) fn boom_home_dir() -> PathBuf {
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

/// Owner-read/write-only (Python: `CONFIG_MODE = 0o600`).
#[cfg(unix)]
const SECRET_FILE_MODE: u32 = 0o600;

/// Owner-only permissions on a secret file (Unix; a no-op on Windows).
#[cfg(unix)]
pub(crate) fn restrict_permissions(path: &std::path::Path) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(SECRET_FILE_MODE))
}

#[cfg(not(unix))]
pub(crate) fn restrict_permissions(_path: &std::path::Path) -> std::io::Result<()> {
    Ok(())
}

/// Atomic, owner-only write of a secret-bearing config file: write temp,
/// chmod 0600, rename, then re-apply the mode (rename can reset it on some
/// filesystems). Returns the final path.
pub(crate) fn atomic_write_secret(
    dir: &std::path::Path,
    filename: &str,
    text: &str,
) -> std::io::Result<PathBuf> {
    std::fs::create_dir_all(dir)?;
    let tmp = dir.join(format!("{filename}.tmp"));
    std::fs::write(&tmp, text)?;
    restrict_permissions(&tmp)?;
    let final_path = dir.join(filename);
    std::fs::rename(&tmp, &final_path)?;
    restrict_permissions(&final_path)?;
    Ok(final_path)
}

// ---------------------------------------------------------------------------
// AppState
// ---------------------------------------------------------------------------

/// One log entry in the /api/logs ring buffer: `{ts, msg}`.
#[derive(Clone, Debug, Serialize)]
pub struct LogEntry {
    pub ts: String,
    pub msg: String,
}

/// Shared server state. Everything is `Arc`-wrapped so handlers stay cheap
/// to clone; sibling workers inject their real implementations.
#[derive(Clone)]
pub struct AppState {
    pub harness: Arc<dyn Harness>,
    pub tools: Arc<dyn ToolRegistry>,
    pub providers: Arc<dyn ProviderRegistry>,
    pub storage: Arc<dyn Storage>,
    pub updater: Arc<dyn Updater>,
    pub zalo: Arc<dyn ZaloPersonal>,
    /// Real channel connections (Telegram Bot API) + honest `unsupported`
    /// states for the platforms this build does not implement.
    pub channels: Arc<dyn ChannelRegistry>,
    /// Reported by /api/health and /api/version.
    pub version: String,
    /// Server start time for `uptime_sec`.
    pub start: Instant,
    /// Directory scanned by /api/skills (each subdir with SKILL.md).
    /// `None` → 0 skills, like a missing skills/ dir in Python.
    pub skills_dir: Option<PathBuf>,
    /// In-memory log ring buffer (newest last), cap [`LOG_MAX`].
    pub log: Arc<Mutex<VecDeque<LogEntry>>>,
    /// Serializes chat turns; a second concurrent turn gets 429 "agent busy"
    /// (Python: `turn_lock` + `acquire(blocking=False)`).
    pub turn: Arc<tokio::sync::Mutex<()>>,
    /// Channel-name → display data for /api/channels/status details. Kept
    /// for API compatibility with Python's daemon-backed checks.
    pub daemon_wired: bool,
}

impl AppState {
    /// Append to the log ring buffer (Python: `api_log`), keeping at most
    /// [`LOG_MAX`] entries, message truncated to 500 chars.
    pub fn api_log(&self, msg: impl Into<String>) {
        let mut msg: String = msg.into();
        if msg.len() > 500 {
            msg.truncate(500);
        }
        let entry = LogEntry { ts: now_iso(), msg };
        if let Ok(mut log) = self.log.lock() {
            log.push_back(entry);
            while log.len() > LOG_MAX {
                log.pop_front();
            }
        }
    }
}

/// Current UTC time as `YYYY-MM-DDTHH:MM:SS` (Python: `isoformat(timespec="seconds")`).
pub fn now_iso() -> String {
    chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S").to_string()
}

/// Current Unix epoch seconds (Python: `int(time.time())`).
pub fn now_epoch() -> i64 {
    chrono::Utc::now().timestamp()
}

/// Parse an ISO-8601 timestamp to epoch seconds; `None` on failure
/// (Python: `_parse_ts_iso`).
pub fn parse_ts_iso(ts: &str) -> Option<i64> {
    chrono::NaiveDateTime::parse_from_str(ts, "%Y-%m-%dT%H:%M:%S")
        .ok()
        .map(|dt| dt.and_utc().timestamp())
        .or_else(|| {
            chrono::DateTime::parse_from_rfc3339(ts)
                .ok()
                .map(|dt| dt.timestamp())
        })
}

/// Format Unix epoch seconds as `YYYY-MM-DDTHH:MM:SS` (UTC), the same shape
/// [`now_iso`] produces — used for session rows whose schema stores epoch
/// seconds while the API surface speaks ISO strings.
pub fn epoch_to_iso(epoch: i64) -> String {
    chrono::DateTime::from_timestamp(epoch, 0)
        .map(|dt| dt.format("%Y-%m-%dT%H:%M:%S").to_string())
        .unwrap_or_else(now_iso)
}

/// Split an item id into `(kind, name)` (Python: `MarketplaceBackend._split_id`).
/// Bare names resolve to an installed plugin first, then a builtin tool.
pub fn split_marketplace_id(
    item_id: &str,
    plugins: &HashMap<String, PluginMeta>,
    tool_names: &std::collections::HashSet<String>,
) -> Result<(String, String), MarketplaceError> {
    let id = item_id.trim();
    if id.is_empty() {
        return Err(MarketplaceError::new("item id is required", 400));
    }
    if let Some((prefix, name)) = id.split_once(':') {
        let (prefix, name) = (prefix.trim().to_lowercase(), name.trim());
        if prefix == "plugin" && !name.is_empty() {
            return Ok(("plugin".to_string(), name.to_string()));
        }
        if prefix == "tool" && !name.is_empty() {
            return Ok(("tool".to_string(), name.to_string()));
        }
        return Err(MarketplaceError::new(format!("invalid item id '{id}'"), 400));
    }
    if plugins.contains_key(id) {
        return Ok(("plugin".to_string(), id.to_string()));
    }
    if tool_names.contains(id) {
        return Ok(("tool".to_string(), id.to_string()));
    }
    Err(MarketplaceError::new(format!("item '{id}' not found"), 404))
}

/// Normalize a JSON value into a `Vec<String>` (Python: `_normalize_str_list`).
pub fn normalize_str_list(value: &Value, field: &str) -> Result<Vec<String>, String> {
    match value {
        Value::Null => Ok(Vec::new()),
        Value::Array(items) => {
            let mut out = Vec::new();
            for v in items {
                let s = match v {
                    Value::String(s) => s.trim().to_string(),
                    Value::Null => String::new(),
                    other => other.to_string().trim().to_string(),
                };
                if !s.is_empty() {
                    out.push(s);
                }
            }
            // De-duplicate while preserving order.
            let mut seen = std::collections::HashSet::new();
            out.retain(|s| seen.insert(s.clone()));
            Ok(out)
        }
        _ => Err(format!("{field} must be a list of strings or None")),
    }
}
