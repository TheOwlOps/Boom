//! Real channel connections: Telegram, Discord and Messenger against
//! their live platform APIs.
//!
//! Port of Python's `gateway/channels/telegram.py` reply path with the
//! registry mechanics of `gateway/channel.py`, extended to Discord
//! (`GET /users/@me` verify, channel message send, message polling) and
//! Messenger (Graph API Page-token verify + send).
//!
//! Credentials live in `$BOOM_HOME/channels/<id>.json` (default
//! `~/.boom/channels/<id>.json`, mode 0600), written atomically, one file
//! per channel:
//!
//! ```json
//! {"token": "...", "bot_username": "mybot", "connected_at": "2026-10-17T10:00:00"}
//! ```
//!
//! The pre-0.6 single-file layout (`$BOOM_HOME/channels.json`) is still
//! read as a fallback and migrated on the next connect/disconnect.
//!
//! The token is never echoed into responses, logs, or error messages.
//! Tests never touch the real platform APIs: every API base is injectable
//! and unit tests drive a local mock HTTP server.

pub mod discord;
pub mod line;
pub mod messenger;
pub mod slack;
pub mod telegram;
pub mod whatsapp;
pub mod zalo;

#[cfg(test)]
mod mock;

use std::{collections::HashMap, fs, path::PathBuf, sync::Mutex};

use serde::{Deserialize, Serialize};

#[cfg(test)]
use serde_json::Value;

use super::state::*;

/// Display name per channel id.
fn display_name(id: &str) -> &'static str {
    match id {
        "telegram" => "Telegram",
        "discord" => "Discord",
        "slack" => "Slack",
        "whatsapp" => "WhatsApp",
        "line" => "Line",
        "feishu" => "Feishu",
        "zalo" => "Zalo",
        "messenger" => "Messenger",
        _ => "Unknown",
    }
}

/// Channels with a real platform implementation (verify + send;
/// Telegram and Discord also poll for ingress; Messenger/WhatsApp/LINE/
/// Zalo receive via webhooks).
const REAL_CHANNELS: &[&str] = &[
    "telegram",
    "discord",
    "messenger",
    "slack",
    "whatsapp",
    "line",
    "zalo",
];

/// One channel's stored credential.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct StoredChannel {
    #[serde(default)]
    token: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    bot_username: Option<String>,
    #[serde(default)]
    connected_at: String,
}

/// Legacy `$BOOM_HOME/channels.json` document (pre-0.6 single file).
#[derive(Debug, Default, Serialize, Deserialize)]
struct LegacyChannelsFile {
    #[serde(default)]
    channels: HashMap<String, StoredChannel>,
}

/// Keep filenames safe: only alphanumerics, dash, underscore.
fn safe_filename(id: &str) -> Option<String> {
    if id.is_empty()
        || !id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
    {
        return None;
    }
    Some(format!("{id}.json"))
}

// ---------------------------------------------------------------------------
// File-backed registry
// ---------------------------------------------------------------------------

/// Channel registry persisted to `$BOOM_HOME/channels/<id>.json` (0600).
///
/// Construct with [`FileChannelRegistry::new`] (honours `BOOM_HOME`, like
/// Python) or [`FileChannelRegistry::with_base_dir`] for tests.
pub struct FileChannelRegistry {
    base_dir: PathBuf,
    channels_dir: PathBuf,
    legacy_path: PathBuf,
    telegram_api: String,
    discord_api: String,
    messenger_api: String,
    slack_api: String,
    whatsapp_api: String,
    line_api: String,
    zalo_api: String,
    /// In-memory lock so concurrent connect/disconnect calls serialize
    /// their read-modify-write cycles.
    write_lock: Mutex<()>,
    /// Single-use OAuth CSRF states → creation time (Messenger login).
    /// Entries expire after [`OAUTH_STATE_TTL_SECS`].
    oauth_states: Mutex<HashMap<String, std::time::Instant>>,
    /// Webhook ingress queue: channel id → pending inbound messages.
    /// Webhook routes push here; `poll_updates` drains per channel.
    webhook_queue: Mutex<HashMap<String, Vec<InboundMessage>>>,
    /// Verify secrets for inbound webhooks (Messenger/WhatsApp `hub.verify_token`).
    webhook_verify_tokens: Mutex<HashMap<String, String>>,
}

/// How long a minted OAuth `state` stays valid (seconds).
const OAUTH_STATE_TTL_SECS: u64 = 600;

/// Filename (inside `$BOOM_HOME/channels/`) holding the Facebook App
/// credentials for the Messenger OAuth login flow.
const OAUTH_CONFIG_FILENAME: &str = "messenger_oauth.json";

/// Mint an unpredictable CSRF state token without extra dependencies:
/// hash of (wall-clock nanos, pid, atomic counter) rendered as hex.
fn mint_oauth_state() -> String {
    use std::collections::hash_map::DefaultHasher;
    use std::hash::{Hash, Hasher};
    use std::sync::atomic::{AtomicU64, Ordering};
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let mut h = DefaultHasher::new();
    nanos.hash(&mut h);
    std::process::id().hash(&mut h);
    COUNTER.fetch_add(1, Ordering::Relaxed).hash(&mut h);
    format!("{:016x}{:016x}", h.finish(), {
        let mut h2 = DefaultHasher::new();
        nanos.wrapping_add(0x9e3779b97f4a7c15).hash(&mut h2);
        h2.finish()
    })
}

impl FileChannelRegistry {
    /// Production constructor: `$BOOM_HOME/channels/`, real platform APIs.
    pub fn new() -> Self {
        Self::with_base_dir(boom_home_dir())
    }

    /// Constructor with an explicit base directory (tests).
    pub fn with_base_dir(base_dir: PathBuf) -> Self {
        Self::with_base_dir_and_apis(
            base_dir,
            telegram::API_BASE.to_string(),
            discord::API_BASE.to_string(),
            messenger::API_BASE.to_string(),
            slack::API_BASE.to_string(),
            whatsapp::API_BASE.to_string(),
            line::API_BASE.to_string(),
            zalo::API_BASE.to_string(),
        )
    }

    /// Constructor with an explicit base directory and Telegram API base
    /// (kept for compatibility; other platforms use their real bases).
    /// Tests driving all platforms through one mock server can use
    /// [`FileChannelRegistry::with_base_dir_and_apis`].
    pub fn with_base_dir_and_api(base_dir: PathBuf, telegram_api: String) -> Self {
        Self::with_base_dir_and_apis(
            base_dir,
            telegram_api,
            discord::API_BASE.to_string(),
            messenger::API_BASE.to_string(),
            slack::API_BASE.to_string(),
            whatsapp::API_BASE.to_string(),
            line::API_BASE.to_string(),
            zalo::API_BASE.to_string(),
        )
    }

    /// Constructor with an explicit base directory and per-platform API
    /// bases (tests drive local mock servers through this).
    #[allow(clippy::too_many_arguments)]
    pub fn with_base_dir_and_apis(
        base_dir: PathBuf,
        telegram_api: String,
        discord_api: String,
        messenger_api: String,
        slack_api: String,
        whatsapp_api: String,
        line_api: String,
        zalo_api: String,
    ) -> Self {
        let channels_dir = base_dir.join("channels");
        let legacy_path = base_dir.join("channels.json");
        Self {
            base_dir,
            channels_dir,
            legacy_path,
            telegram_api,
            discord_api,
            messenger_api,
            slack_api,
            whatsapp_api,
            line_api,
            zalo_api,
            write_lock: Mutex::new(()),
            oauth_states: Mutex::new(HashMap::new()),
            webhook_queue: Mutex::new(HashMap::new()),
            webhook_verify_tokens: Mutex::new(HashMap::new()),
        }
    }

    /// The legacy config file path (useful for tests / diagnostics).
    pub fn path(&self) -> &PathBuf {
        &self.legacy_path
    }

    /// Per-channel config file path.
    fn channel_path(&self, id: &str) -> Option<PathBuf> {
        safe_filename(id).map(|f| self.channels_dir.join(f))
    }

    /// Load one channel's credential: per-channel file first, then the
    /// legacy single-file layout.
    fn load_channel(&self, id: &str) -> Option<StoredChannel> {
        if let Some(path) = self.channel_path(id) {
            if let Ok(raw) = fs::read_to_string(&path) {
                if let Ok(stored) = serde_json::from_str::<StoredChannel>(&raw) {
                    if !stored.token.trim().is_empty() {
                        return Some(stored);
                    }
                }
            }
        }
        // Legacy fallback.
        if let Ok(raw) = fs::read_to_string(&self.legacy_path) {
            if let Ok(doc) = serde_json::from_str::<LegacyChannelsFile>(&raw) {
                if let Some(stored) = doc.channels.get(id) {
                    if !stored.token.trim().is_empty() {
                        return Some(stored.clone());
                    }
                }
            }
        }
        None
    }

    /// Atomic 0600 write of one channel's file; also scrubs the id from
    /// the legacy file so the migration is one-way.
    fn save_channel(&self, id: &str, stored: &StoredChannel) -> Result<(), String> {
        let filename = safe_filename(id)
            .ok_or_else(|| format!("invalid channel id: {id}"))?;
        let text = serde_json::to_string_pretty(stored)
            .map_err(|e| format!("failed to serialize channel config: {e}"))?;
        atomic_write_secret(&self.channels_dir, &filename, &text)
            .map_err(|e| format!("failed to save channel config: {e}"))?;
        self.scrub_legacy(id);
        Ok(())
    }

    /// Remove one channel's file (missing file is fine); also scrubs the
    /// legacy file. Returns true if anything was removed.
    fn remove_channel(&self, id: &str) -> bool {
        let mut removed = false;
        if let Some(path) = self.channel_path(id) {
            if fs::remove_file(&path).is_ok() {
                removed = true;
            }
        }
        if self.scrub_legacy(id) {
            removed = true;
        }
        removed
    }

    /// Delete `id` from the legacy single-file layout. Returns true when
    /// an entry was actually removed (and the file rewritten).
    fn scrub_legacy(&self, id: &str) -> bool {
        let raw = match fs::read_to_string(&self.legacy_path) {
            Ok(r) => r,
            Err(_) => return false,
        };
        let mut doc: LegacyChannelsFile = match serde_json::from_str(&raw) {
            Ok(d) => d,
            Err(_) => return false,
        };
        if doc.channels.remove(id).is_none() {
            return false;
        }
        let text = match serde_json::to_string_pretty(&doc) {
            Ok(t) => t,
            Err(_) => return false,
        };
        atomic_write_secret(&self.base_dir, "channels.json", &text).is_ok()
    }

    fn is_supported(id: &str) -> bool {
        REAL_CHANNELS.contains(&id)
    }

    fn unsupported_info(id: &str) -> ChannelInfo {
        let known = KNOWN_CHANNELS.contains(&id);
        ChannelInfo {
            id: id.to_string(),
            name: display_name(id).to_string(),
            status: "unsupported".to_string(),
            supported: false,
            detail: Some(if known {
                format!("{} chưa hỗ trợ trong bản này", display_name(id))
            } else {
                "unknown channel".to_string()
            }),
        }
    }

    fn stored_token(&self, id: &str) -> Result<StoredChannel, ChannelError> {
        match self.load_channel(id) {
            Some(s) => Ok(s),
            None => Err(ChannelError::bad_request(format!(
                "{} chưa được kết nối — hãy connect trước",
                display_name(id)
            ))),
        }
    }

    // ------------------------------------------------------------------
    // Messenger Facebook OAuth login flow
    // ------------------------------------------------------------------

    /// Path of the OAuth app-credentials file.
    fn oauth_config_path(&self) -> PathBuf {
        self.channels_dir.join(OAUTH_CONFIG_FILENAME)
    }

    /// Load the saved Facebook App credentials, if any.
    fn load_oauth_config(&self) -> Option<MessengerOAuthConfig> {
        let raw = fs::read_to_string(self.oauth_config_path()).ok()?;
        let cfg: MessengerOAuthConfig = serde_json::from_str(&raw).ok()?;
        if cfg.app_id.trim().is_empty() || cfg.app_secret.is_empty() {
            return None;
        }
        Some(cfg)
    }

    /// Persist Facebook App credentials (0600, atomic). The secret is
    /// never echoed back to callers.
    fn save_oauth_config(&self, app_id: &str, app_secret: &str) -> Result<(), String> {
        let app_id = app_id.trim();
        if app_id.is_empty() {
            return Err("Facebook App ID is required".to_string());
        }
        if app_secret.is_empty() {
            return Err("Facebook App Secret is required".to_string());
        }
        let cfg = MessengerOAuthConfig {
            app_id: app_id.to_string(),
            app_secret: app_secret.to_string(),
        };
        let text = serde_json::to_string_pretty(&cfg)
            .map_err(|e| format!("failed to serialize oauth config: {e}"))?;
        atomic_write_secret(&self.channels_dir, OAUTH_CONFIG_FILENAME, &text)
            .map_err(|e| format!("failed to save oauth config: {e}"))?;
        Ok(())
    }

    /// Mint a single-use CSRF state and build the Facebook Login URL.
    /// `redirect_uri` is the backend's own callback URL (derived from the
    /// request Host so it always matches this server's address).
    fn oauth_start(&self, redirect_uri: &str) -> Result<String, ChannelError> {
        let cfg = self.load_oauth_config().ok_or_else(|| {
            ChannelError::bad_request(
                "Facebook App chưa được cấu hình — nhập App ID và App Secret trước",
            )
        })?;
        let state = mint_oauth_state();
        // Prune expired states, then store the fresh one.
        if let Ok(mut states) = self.oauth_states.lock() {
            let now = std::time::Instant::now();
            states.retain(|_, t| now.duration_since(*t).as_secs() < OAUTH_STATE_TTL_SECS);
            states.insert(state.clone(), now);
        }
        Ok(messenger::authorize_url(&cfg.app_id, redirect_uri, &state))
    }

    /// Validate the single-use `state`, exchange `code` for a user token,
    /// take the first managed Page and persist its Page token as the
    /// messenger credential (verified first, like a manual connect).
    /// `Ok` → the connected Page's display name.
    async fn oauth_callback(
        &self,
        code: &str,
        state: &str,
        redirect_uri: &str,
    ) -> Result<String, ChannelError> {
        // Single-use state check first: unknown/expired/replayed → 400.
        let fresh = self
            .oauth_states
            .lock()
            .map(|mut states| {
                let valid = states
                    .remove(state)
                    .map(|t| std::time::Instant::now().duration_since(t).as_secs() < OAUTH_STATE_TTL_SECS)
                    .unwrap_or(false);
                // Opportunistic prune on every callback.
                let now = std::time::Instant::now();
                states.retain(|_, t| now.duration_since(*t).as_secs() < OAUTH_STATE_TTL_SECS);
                valid
            })
            .unwrap_or(false);
        if !fresh {
            return Err(ChannelError::bad_request(
                "OAuth state không hợp lệ hoặc đã hết hạn — hãy bấm Connect lại",
            ));
        }
        let cfg = self.load_oauth_config().ok_or_else(|| {
            ChannelError::new(500, "Facebook App config biến mất giữa chừng — hãy cấu hình lại")
        })?;
        let messenger_api = self.messenger_api.clone();
        let msg_cfg = messenger::OAuthConfig {
            app_id: cfg.app_id.clone(),
            app_secret: cfg.app_secret.clone(),
        };
        let user_token = messenger::exchange_code(&messenger_api, &msg_cfg, redirect_uri, code)
            .await
            .map_err(ChannelError::upstream)?;
        let pages = messenger::list_pages(&messenger_api, &user_token)
            .await
            .map_err(ChannelError::upstream)?;
        // Never keep the short-lived user token: only the Page token is stored.
        drop(user_token);
        let page = pages.into_iter().next().ok_or_else(|| {
            ChannelError::upstream(
                "Tài khoản Facebook này không quản lý Page nào — hãy tạo Page rồi thử lại",
            )
        })?;
        let page_name = if page.name.is_empty() {
            page.id.clone()
        } else {
            page.name.clone()
        };
        // `connect` re-verifies the Page token live before persisting it.
        self.connect("messenger", &page.access_token).await?;
        Ok(page_name)
    }

    /// Drain (take + clear) the webhook queue for one channel.
    /// Inherent method so both the trait impl and tests can use it.
    pub(crate) fn drain_webhook_queue(&self, id: &str) -> Vec<InboundMessage> {
        self.webhook_queue
            .lock()
            .map(|mut q| q.remove(id).unwrap_or_default())
            .unwrap_or_default()
    }
}

impl Default for FileChannelRegistry {
    fn default() -> Self {
        Self::new()
    }
}

impl ChannelRegistry for FileChannelRegistry {
    fn channel_ids(&self) -> Vec<String> {
        KNOWN_CHANNELS.iter().map(|s| s.to_string()).collect()
    }

    fn info(&self, id: &str) -> ChannelInfo {
        if !Self::is_supported(id) {
            return Self::unsupported_info(id);
        }
        match self.load_channel(id) {
            Some(stored) => ChannelInfo {
                id: id.to_string(),
                name: display_name(id).to_string(),
                status: "connected".to_string(),
                supported: true,
                detail: stored
                    .bot_username
                    .as_deref()
                    .map(|u| format!("@{u}")),
            },
            None => ChannelInfo {
                id: id.to_string(),
                name: display_name(id).to_string(),
                status: "not-configured".to_string(),
                supported: true,
                detail: Some(match id {
                    "telegram" => "Chưa cấu hình — dán bot token để kết nối".to_string(),
                    "discord" => "Chưa cấu hình — dán Discord bot token để kết nối".to_string(),
                    "messenger" => {
                        "Chưa cấu hình — dán Facebook Page access token để kết nối".to_string()
                    }
                    "slack" => "Chưa cấu hình — dán Slack Bot token (xoxb-...) để kết nối".to_string(),
                    "whatsapp" => {
                        "Chưa cấu hình — nhập <access_token>:<phone_number_id> để kết nối".to_string()
                    }
                    "line" => "Chưa cấu hình — dán LINE Channel access token để kết nối".to_string(),
                    "zalo" => "Chưa cấu hình — dán Zalo OA access token để kết nối".to_string(),
                    _ => "Chưa cấu hình".to_string(),
                }),
            },
        }
    }

    fn connect(
        &self,
        id: &str,
        token: &str,
    ) -> BoxFuture<'_, Result<ChannelConnectInfo, ChannelError>> {
        let id = id.to_string();
        let token = token.trim().to_string();
        let (telegram_api, discord_api, messenger_api, slack_api, whatsapp_api, line_api, zalo_api) = (
            self.telegram_api.clone(),
            self.discord_api.clone(),
            self.messenger_api.clone(),
            self.slack_api.clone(),
            self.whatsapp_api.clone(),
            self.line_api.clone(),
            self.zalo_api.clone(),
        );
        Box::pin(async move {
            if !KNOWN_CHANNELS.contains(&id.as_str()) {
                return Err(ChannelError::not_found(format!("unknown channel: {id}")));
            }
            if !Self::is_supported(&id) {
                return Err(ChannelError::bad_request(format!(
                    "{} chưa hỗ trợ trong bản này",
                    display_name(&id)
                )));
            }
            if token.is_empty() {
                return Err(ChannelError::bad_request("token is required"));
            }
            // Live verification BEFORE persisting: a bad credential is
            // never saved. Error messages never contain the token.
            let (username, message) = match id.as_str() {
                "telegram" => {
                    if !telegram::valid_bot_token_format(&token) {
                        // Cheap pre-check so an obviously-wrong paste fails
                        // fast; the live getMe call is the real validation.
                        return Err(ChannelError::bad_request(
                            "invalid bot token format (expected <digits>:<secret>)",
                        ));
                    }
                    let identity = telegram::verify_token(&telegram_api, &token)
                        .await
                        .map_err(ChannelError::upstream)?;
                    let username = identity.username.clone();
                    let message = match &username {
                        Some(u) => format!("Đã kết nối Telegram bot @{u}"),
                        None => "Đã kết nối Telegram bot".to_string(),
                    };
                    (username, message)
                }
                "discord" => {
                    // Discord bot tokens have no stable public format —
                    // the live /users/@me call is the validation.
                    let identity = discord::verify_token(&discord_api, &token)
                        .await
                        .map_err(ChannelError::upstream)?;
                    let username = Some(identity.username.clone());
                    let message = match &identity.display_name {
                        Some(d) => format!("Đã kết nối Discord bot {d} (@{})", identity.username),
                        None => format!("Đã kết nối Discord bot @{}", identity.username),
                    };
                    (username, message)
                }
                "messenger" => {
                    let page = messenger::verify_token(&messenger_api, &token)
                        .await
                        .map_err(ChannelError::upstream)?;
                    let message = if page.name.is_empty() {
                        "Đã kết nối Messenger Page".to_string()
                    } else {
                        format!("Đã kết nối Messenger Page \"{}\"", page.name)
                    };
                    (Some(page.name.clone()), message)
                }
                "slack" => {
                    let identity = slack::verify_token(&slack_api, &token)
                        .await
                        .map_err(ChannelError::upstream)?;
                    let message = format!(
                        "Đã kết nối Slack bot {} (team {})",
                        identity.user_id, identity.team
                    );
                    (Some(identity.user_id.clone()), message)
                }
                "whatsapp" => {
                    let identity = whatsapp::verify_token(&whatsapp_api, &token)
                        .await
                        .map_err(ChannelError::upstream)?;
                    let message = match &identity.verified_name {
                        Some(n) => format!("Đã kết nối WhatsApp Business \"{n}\" ({})", identity.display_phone_number),
                        None => format!("Đã kết nối WhatsApp {}", identity.display_phone_number),
                    };
                    (identity.verified_name.clone(), message)
                }
                "line" => {
                    let identity = line::verify_token(&line_api, &token)
                        .await
                        .map_err(ChannelError::upstream)?;
                    let message = if identity.display_name.is_empty() {
                        "Đã kết nối LINE Bot".to_string()
                    } else {
                        format!("Đã kết nối LINE Bot \"{}\"", identity.display_name)
                    };
                    (Some(identity.display_name.clone()), message)
                }
                "zalo" => {
                    let identity = zalo::verify_token(&zalo_api, &token)
                        .await
                        .map_err(ChannelError::upstream)?;
                    let message = if identity.name.is_empty() {
                        "Đã kết nối Zalo OA".to_string()
                    } else {
                        format!("Đã kết nối Zalo OA \"{}\"", identity.name)
                    };
                    (Some(identity.name.clone()), message)
                }
                _ => unreachable!("is_supported guard above"),
            };
            let _guard = self.write_lock.lock().map_err(|_| {
                ChannelError::new(500, "failed to save channel config")
            })?;
            self.save_channel(
                &id,
                &StoredChannel {
                    token,
                    bot_username: username.clone(),
                    connected_at: now_iso(),
                },
            )
            .map_err(|e| ChannelError::new(500, e))?;
            Ok(ChannelConnectInfo {
                bot_username: username,
                message,
            })
        })
    }

    fn disconnect(&self, id: &str) -> Result<(), ChannelError> {
        if !KNOWN_CHANNELS.contains(&id) {
            return Err(ChannelError::not_found(format!("unknown channel: {id}")));
        }
        if !Self::is_supported(id) {
            return Err(ChannelError::bad_request(format!(
                "{} chưa hỗ trợ trong bản này",
                display_name(id)
            )));
        }
        let _guard = self
            .write_lock
            .lock()
            .map_err(|_| ChannelError::new(500, "failed to save channel config"))?;
        // Idempotent: removing a missing entry still succeeds.
        self.remove_channel(id);
        Ok(())
    }

    fn test(
        &self,
        id: &str,
        chat_id: Option<&str>,
        message: Option<&str>,
    ) -> BoxFuture<'_, Result<String, ChannelError>> {
        let id = id.to_string();
        let chat_id = chat_id.map(|s| s.trim().to_string()).filter(|s| !s.is_empty());
        let message = message.map(|s| s.to_string());
        let (telegram_api, discord_api, messenger_api, slack_api, whatsapp_api, line_api, zalo_api) = (
            self.telegram_api.clone(),
            self.discord_api.clone(),
            self.messenger_api.clone(),
            self.slack_api.clone(),
            self.whatsapp_api.clone(),
            self.line_api.clone(),
            self.zalo_api.clone(),
        );
        Box::pin(async move {
            if !KNOWN_CHANNELS.contains(&id.as_str()) {
                return Err(ChannelError::not_found(format!("unknown channel: {id}")));
            }
            if !Self::is_supported(&id) {
                return Err(ChannelError::bad_request(format!(
                    "{} chưa hỗ trợ trong bản này",
                    display_name(&id)
                )));
            }
            let stored = self.stored_token(&id)?;
            let name = display_name(&id);
            match chat_id {
                Some(chat) => {
                    let text = message.unwrap_or_else(|| "Boom test message".to_string());
                    match id.as_str() {
                        "telegram" => {
                            let msg_id =
                                telegram::send_text(&telegram_api, &stored.token, &chat, &text)
                                    .await
                                    .map_err(ChannelError::upstream)?;
                            Ok(format!("Đã gửi tin nhắn test tới {chat} (message_id {msg_id})"))
                        }
                        "discord" => {
                            let msg_id =
                                discord::send_text(&discord_api, &stored.token, &chat, &text)
                                    .await
                                    .map_err(ChannelError::upstream)?;
                            Ok(format!(
                                "Đã gửi tin nhắn test tới Discord channel {chat} (message {msg_id})"
                            ))
                        }
                        "messenger" => {
                            let msg_id =
                                messenger::send_text(&messenger_api, &stored.token, &chat, &text)
                                    .await
                                    .map_err(ChannelError::upstream)?;
                            Ok(format!(
                                "Đã gửi tin nhắn test tới Messenger user {chat} (message {msg_id})"
                            ))
                        }
                        "slack" => {
                            let ts =
                                slack::send_text(&slack_api, &stored.token, &chat, &text)
                                    .await
                                    .map_err(ChannelError::upstream)?;
                            Ok(format!(
                                "Đã gửi tin nhắn test tới Slack channel {chat} (ts {ts})"
                            ))
                        }
                        "whatsapp" => {
                            let msg_id =
                                whatsapp::send_text(&whatsapp_api, &stored.token, &chat, &text)
                                    .await
                                    .map_err(ChannelError::upstream)?;
                            Ok(format!(
                                "Đã gửi tin nhắn test tới WhatsApp {chat} (message {msg_id})"
                            ))
                        }
                        "line" => {
                            line::send_text(&line_api, &stored.token, &chat, &text)
                                .await
                                .map_err(ChannelError::upstream)?;
                            Ok(format!("Đã gửi tin nhắn test tới LINE user {chat}"))
                        }
                        "zalo" => {
                            let msg_id =
                                zalo::send_text(&zalo_api, &stored.token, &chat, &text)
                                    .await
                                    .map_err(ChannelError::upstream)?;
                            Ok(format!(
                                "Đã gửi tin nhắn test tới Zalo user {chat} (message {msg_id})"
                            ))
                        }
                        _ => unreachable!("is_supported guard above"),
                    }
                }
                None => {
                    // Re-verify the stored credential.
                    let detail = match id.as_str() {
                        "telegram" => {
                            let identity = telegram::verify_token(&telegram_api, &stored.token)
                                .await
                                .map_err(ChannelError::upstream)?;
                            match identity.username {
                                Some(u) => format!("@{u}"),
                                None => "bot".to_string(),
                            }
                        }
                        "discord" => {
                            let identity = discord::verify_token(&discord_api, &stored.token)
                                .await
                                .map_err(ChannelError::upstream)?;
                            format!("@{}", identity.username)
                        }
                        "messenger" => {
                            let page = messenger::verify_token(&messenger_api, &stored.token)
                                .await
                                .map_err(ChannelError::upstream)?;
                            if page.name.is_empty() {
                                "Page".to_string()
                            } else {
                                format!("\"{}\"", page.name)
                            }
                        }
                        "slack" => {
                            let identity = slack::verify_token(&slack_api, &stored.token)
                                .await
                                .map_err(ChannelError::upstream)?;
                            format!("bot {} (team {})", identity.user_id, identity.team)
                        }
                        "whatsapp" => {
                            let identity = whatsapp::verify_token(&whatsapp_api, &stored.token)
                                .await
                                .map_err(ChannelError::upstream)?;
                            identity.display_phone_number.clone()
                        }
                        "line" => {
                            let identity = line::verify_token(&line_api, &stored.token)
                                .await
                                .map_err(ChannelError::upstream)?;
                            if identity.display_name.is_empty() {
                                "Bot".to_string()
                            } else {
                                format!("\"{}\"", identity.display_name)
                            }
                        }
                        "zalo" => {
                            let identity = zalo::verify_token(&zalo_api, &stored.token)
                                .await
                                .map_err(ChannelError::upstream)?;
                            if identity.name.is_empty() {
                                "OA".to_string()
                            } else {
                                format!("\"{}\"", identity.name)
                            }
                        }
                        _ => unreachable!("is_supported guard above"),
                    };
                    Ok(format!("Token hợp lệ — {name} {detail} đang hoạt động"))
                }
            }
        })
    }

    fn poll_updates(
        &self,
        id: &str,
        chat_id: Option<&str>,
        after: Option<&str>,
    ) -> BoxFuture<'_, Result<Vec<InboundMessage>, ChannelError>> {
        let id = id.to_string();
        let chat_id = chat_id.map(|s| s.trim().to_string()).filter(|s| !s.is_empty());
        let after = after.map(|s| s.trim().to_string()).filter(|s| !s.is_empty());
        let (telegram_api, discord_api) = (self.telegram_api.clone(), self.discord_api.clone());
        Box::pin(async move {
            if !KNOWN_CHANNELS.contains(&id.as_str()) {
                return Err(ChannelError::not_found(format!("unknown channel: {id}")));
            }
            if !Self::is_supported(&id) {
                return Err(ChannelError::bad_request(format!(
                    "{} chưa hỗ trợ trong bản này",
                    display_name(&id)
                )));
            }
            // Webhook-based channels drain the in-memory ingress queue
            // (populated by POST /api/channels/{id}/webhook).
            if matches!(id.as_str(), "messenger" | "whatsapp" | "line" | "zalo") {
                let _stored = self.stored_token(&id)?;
                let mut msgs = self.drain_webhook_queue(&id);
                if let Some(filter) = chat_id.as_deref() {
                    msgs.retain(|m| m.chat_id == filter);
                }
                if let Some(cursor) = after.as_deref() {
                    if let Some(pos) = msgs.iter().position(|m| m.id == cursor) {
                        msgs = msgs.split_off(pos + 1);
                    }
                }
                return Ok(msgs);
            }
            if id.as_str() == "slack" {
                // Honest 501: Slack ingress needs Socket Mode or Events API.
                let _stored = self.stored_token(&id)?;
                return Err(ChannelError::new(501, slack::poll_ingress_hint()));
            }
            let stored = self.stored_token(&id)?;
            match id.as_str() {
                "telegram" => {
                    let msgs = telegram::get_updates(
                        &telegram_api,
                        &stored.token,
                        after.as_deref(),
                        chat_id.as_deref(),
                    )
                    .await
                    .map_err(ChannelError::upstream)?;
                    Ok(msgs)
                }
                "discord" => {
                    let channel = chat_id.ok_or_else(|| {
                        ChannelError::bad_request(
                            "Discord poll cần chat_id là channel id (ví dụ ?chat_id=123456789)",
                        )
                    })?;
                    let msgs = discord::poll_messages(
                        &discord_api,
                        &stored.token,
                        &channel,
                        after.as_deref(),
                    )
                    .await
                    .map_err(ChannelError::upstream)?;
                    Ok(msgs)
                }
                _ => unreachable!("is_supported guard above"),
            }
        })
    }

    /// Push inbound webhook messages into the per-channel queue.
    /// Called by the webhook routes after signature verification.
    fn push_webhook_messages(&self, id: &str, msgs: Vec<InboundMessage>) {
        if msgs.is_empty() {
            return;
        }
        if let Ok(mut q) = self.webhook_queue.lock() {
            let entry = q.entry(id.to_string()).or_default();
            entry.extend(msgs);
            // Cap the queue so a forgotten webhook can't grow unbounded.
            const MAX_QUEUE: usize = 500;
            if entry.len() > MAX_QUEUE {
                let excess = entry.len() - MAX_QUEUE;
                entry.drain(..excess);
            }
        }
    }

    /// Set the verify token used to validate inbound webhook requests
    /// (`hub.verify_token` for Messenger/WhatsApp). Stored in memory only.
    fn set_webhook_verify_token(&self, id: &str, token: &str) {
        if let Ok(mut m) = self.webhook_verify_tokens.lock() {
            if token.trim().is_empty() {
                m.remove(id);
            } else {
                m.insert(id.to_string(), token.trim().to_string());
            }
        }
    }

    /// Get the configured webhook verify token for a channel, if any.
    fn webhook_verify_token(&self, id: &str) -> Option<String> {
        self.webhook_verify_tokens
            .lock()
            .map(|m| m.get(id).cloned())
            .unwrap_or(None)
    }

    /// Get the stored raw token for a channel (for operation dispatch).
    /// Returns a ChannelError when not connected.
    fn channel_token(&self, id: &str) -> Result<String, ChannelError> {
        Ok(self.stored_token(id)?.token)
    }

    /// API base override for a channel (used by bot tools).
    fn channel_api_base(&self, id: &str) -> String {
        match id {
            "telegram" => self.telegram_api.clone(),
            "discord" => self.discord_api.clone(),
            "messenger" => self.messenger_api.clone(),
            "slack" => self.slack_api.clone(),
            "whatsapp" => self.whatsapp_api.clone(),
            "line" => self.line_api.clone(),
            "zalo" => self.zalo_api.clone(),
            _ => String::new(),
        }
    }

    fn messenger_oauth_config(&self) -> Option<MessengerOAuthConfig> {
        self.load_oauth_config()
    }

    fn save_messenger_oauth_config(
        &self,
        app_id: &str,
        app_secret: &str,
    ) -> Result<(), ChannelError> {
        let _guard = self
            .write_lock
            .lock()
            .map_err(|_| ChannelError::new(500, "failed to save oauth config"))?;
        self.save_oauth_config(app_id, app_secret)
            .map_err(ChannelError::bad_request)
    }

    fn messenger_oauth_start(&self, redirect_uri: &str) -> Result<String, ChannelError> {
        self.oauth_start(redirect_uri)
    }

    fn messenger_oauth_callback(
        &self,
        code: &str,
        state: &str,
        redirect_uri: &str,
    ) -> BoxFuture<'_, Result<String, ChannelError>> {
        let code = code.to_string();
        let state = state.to_string();
        let redirect_uri = redirect_uri.to_string();
        Box::pin(async move {
            self.oauth_callback(&code, &state, &redirect_uri).await
        })
    }
}

// ---------------------------------------------------------------------------
// Tests — never touch the real platform APIs; local mock servers stand in.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use mock::{mock_server, mock_server_n, GOOD_TOKEN};

    fn test_registry(api_base: &str) -> (tempfile::TempDir, FileChannelRegistry) {
        let dir = tempfile::tempdir().expect("tempdir");
        let reg = FileChannelRegistry::with_base_dir_and_api(
            dir.path().to_path_buf(),
            api_base.to_string(),
        );
        (dir, reg)
    }

    fn test_registry_all(base: &str) -> (tempfile::TempDir, FileChannelRegistry) {
        let dir = tempfile::tempdir().expect("tempdir");
        let reg = FileChannelRegistry::with_base_dir_and_apis(
            dir.path().to_path_buf(),
            base.to_string(),
            base.to_string(),
            base.to_string(),
            base.to_string(),
            base.to_string(),
            base.to_string(),
            base.to_string(),
        );
        (dir, reg)
    }

    #[test]
    fn supported_channels_are_real() {
        let (_dir, reg) = test_registry("http://127.0.0.1:1");
        for id in [
            "telegram",
            "discord",
            "messenger",
            "slack",
            "whatsapp",
            "line",
            "zalo",
        ] {
            let info = reg.info(id);
            assert!(info.supported, "id: {id}");
            assert_eq!(info.status, "not-configured", "id: {id}");
            assert!(info.detail.is_some(), "id: {id}");
        }
    }

    #[test]
    fn remaining_channels_are_honest() {
        let (_dir, reg) = test_registry("http://127.0.0.1:1");
        for id in ["feishu"] {
            let info = reg.info(id);
            assert_eq!(info.status, "unsupported", "id: {id}");
            assert!(!info.supported, "id: {id}");
            let detail = info.detail.expect("detail");
            assert!(detail.contains("chưa hỗ trợ trong bản này"), "detail: {detail}");
        }
        let unknown = reg.info("tiktok");
        assert_eq!(unknown.status, "unsupported");
        assert_eq!(unknown.detail.as_deref(), Some("unknown channel"));
    }

    #[tokio::test]
    async fn connect_rejects_bad_format_without_network() {
        let (_dir, reg) = test_registry("http://127.0.0.1:1");
        let err = reg.connect("telegram", "not-a-token").await.unwrap_err();
        assert_eq!(err.status, 400);
        assert!(err.message.contains("invalid bot token format"), "msg: {}", err.message);
        assert_eq!(reg.info("telegram").status, "not-configured");
    }

    #[tokio::test]
    async fn connect_rejects_empty_token() {
        let (_dir, reg) = test_registry("http://127.0.0.1:1");
        let err = reg.connect("telegram", "   ").await.unwrap_err();
        assert_eq!(err.status, 400);
    }

    #[tokio::test]
    async fn connect_rejects_unsupported_channel() {
        let (_dir, reg) = test_registry("http://127.0.0.1:1");
        // feishu remains unimplemented.
        let err = reg.connect("feishu", GOOD_TOKEN).await.unwrap_err();
        assert_eq!(err.status, 400);
        assert!(err.message.contains("chưa hỗ trợ"), "msg: {}", err.message);
    }

    #[tokio::test]
    async fn connect_rejects_unknown_channel() {
        let (_dir, reg) = test_registry("http://127.0.0.1:1");
        let err = reg.connect("tiktok", GOOD_TOKEN).await.unwrap_err();
        assert_eq!(err.status, 404);
    }

    #[tokio::test]
    async fn connect_verifies_then_persists_per_channel_file() {
        let (base, _handle) = mock_server(
            200,
            r#"{"ok":true,"result":{"id":1,"first_name":"T","username":"testbot"}}"#,
        )
        .await;
        let (dir, reg) = test_registry(&base);
        let info = reg.connect("telegram", GOOD_TOKEN).await.expect("connect");
        assert_eq!(info.bot_username.as_deref(), Some("testbot"));

        let status = reg.info("telegram");
        assert_eq!(status.status, "connected");
        assert_eq!(status.detail.as_deref(), Some("@testbot"));

        // Per-channel file layout: $BOOM_HOME/channels/telegram.json.
        let path = dir.path().join("channels").join("telegram.json");
        assert!(path.exists(), "per-channel file must exist");
        let raw = fs::read_to_string(&path).expect("read telegram.json");
        let v: Value = serde_json::from_str(&raw).expect("valid JSON");
        assert_eq!(v["token"], Value::String(GOOD_TOKEN.to_string()));

        // Persisted: a fresh registry on the same dir sees it.
        let reg2 = FileChannelRegistry::with_base_dir_and_api(
            dir.path().to_path_buf(),
            base.clone(),
        );
        assert_eq!(reg2.info("telegram").status, "connected");
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn channel_file_is_owner_only() {
        use std::os::unix::fs::PermissionsExt;
        let (base, _handle) = mock_server(
            200,
            r#"{"ok":true,"result":{"id":1,"first_name":"T"}}"#,
        )
        .await;
        let (dir, reg) = test_registry(&base);
        reg.connect("telegram", GOOD_TOKEN).await.expect("connect");
        let mode = fs::metadata(dir.path().join("channels").join("telegram.json"))
            .expect("metadata")
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode, 0o600, "channel file must be 0600");
    }

    #[tokio::test]
    async fn legacy_single_file_is_migrated_on_read() {
        // Pre-0.6 layout: $BOOM_HOME/channels.json.
        let dir = tempfile::tempdir().expect("tempdir");
        let legacy = serde_json::json!({
            "channels": {
                "telegram": {"token": GOOD_TOKEN, "bot_username": "oldbot", "connected_at": "2026-01-01T00:00:00"}
            }
        });
        fs::write(
            dir.path().join("channels.json"),
            serde_json::to_string_pretty(&legacy).unwrap(),
        )
        .unwrap();
        let reg = FileChannelRegistry::with_base_dir_and_api(
            dir.path().to_path_buf(),
            "http://127.0.0.1:1".to_string(),
        );
        // Legacy entry is visible without any new file.
        assert_eq!(reg.info("telegram").status, "connected");
        assert_eq!(reg.info("telegram").detail.as_deref(), Some("@oldbot"));
        // Disconnect scrubs the legacy entry too.
        reg.disconnect("telegram").expect("disconnect");
        assert_eq!(reg.info("telegram").status, "not-configured");
        let raw = fs::read_to_string(dir.path().join("channels.json")).unwrap();
        let v: Value = serde_json::from_str(&raw).unwrap();
        assert!(v["channels"].get("telegram").is_none());
    }

    #[tokio::test]
    async fn connect_does_not_persist_rejected_token() {
        let (base, _handle) = mock_server(
            401,
            r#"{"ok":false,"error_code":401,"description":"Unauthorized"}"#,
        )
        .await;
        let (dir, reg) = test_registry(&base);
        let err = reg.connect("telegram", GOOD_TOKEN).await.unwrap_err();
        assert_eq!(err.status, 502, "msg: {}", err.message);
        assert!(!err.message.contains("TESTTOKEN"), "msg: {}", err.message);
        assert_eq!(reg.info("telegram").status, "not-configured");
        assert!(
            !dir.path().join("channels").join("telegram.json").exists(),
            "rejected token must not be saved"
        );
    }

    #[tokio::test]
    async fn disconnect_removes_token_and_is_idempotent() {
        let (base, _handle) = mock_server(
            200,
            r#"{"ok":true,"result":{"id":1,"first_name":"T","username":"testbot"}}"#,
        )
        .await;
        let (dir, reg) = test_registry(&base);
        reg.connect("telegram", GOOD_TOKEN).await.expect("connect");
        reg.disconnect("telegram").expect("disconnect");
        assert_eq!(reg.info("telegram").status, "not-configured");
        assert!(!dir.path().join("channels").join("telegram.json").exists());
        // Second disconnect still succeeds.
        reg.disconnect("telegram").expect("idempotent disconnect");
        // Disconnecting an unconnected but supported channel still succeeds.
        reg.disconnect("slack").expect("idempotent disconnect");
        assert!(reg.disconnect("feishu").is_err());
    }

    #[tokio::test]
    async fn test_without_token_fails_clearly() {
        let (_dir, reg) = test_registry("http://127.0.0.1:1");
        let err = reg
            .test("telegram", Some("123"), None)
            .await
            .unwrap_err();
        assert_eq!(err.status, 400);
        assert!(err.message.contains("chưa được kết nối"), "msg: {}", err.message);
    }

    #[tokio::test]
    async fn test_verifies_token_when_no_chat_id() {
        // connect() and test() each make one request → two connections.
        let (base, _handle) = mock_server_n(
            200,
            r#"{"ok":true,"result":{"id":1,"first_name":"T","username":"testbot"}}"#,
            2,
        )
        .await;
        let (_dir, reg) = test_registry(&base);
        reg.connect("telegram", GOOD_TOKEN).await.expect("connect");
        let msg = reg.test("telegram", None, None).await.expect("test");
        assert!(msg.contains("@testbot"), "msg: {msg}");
    }

    #[tokio::test]
    async fn test_sends_message_when_chat_id_given() {
        // One mock server handles getMe (connect) then sendMessage (test).
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let addr = listener.local_addr().expect("addr");
        let server = tokio::spawn(async move {
            use tokio::io::{AsyncReadExt, AsyncWriteExt};
            let mut bodies = Vec::new();
            for _ in 0..2 {
                let (mut sock, _) = listener.accept().await.expect("accept");
                let mut req: Vec<u8> = Vec::new();
                let mut buf = vec![0u8; 8192];
                let header_end = loop {
                    let n = sock.read(&mut buf).await.expect("read");
                    if n == 0 {
                        break None;
                    }
                    req.extend_from_slice(&buf[..n]);
                    if let Some(pos) = mock::find_header_end(&req) {
                        break Some(pos);
                    }
                };
                let body = if let Some(end) = header_end {
                    let headers = String::from_utf8_lossy(&req[..end]).into_owned();
                    let len = headers
                        .lines()
                        .find_map(|l| {
                            let (k, v) = l.split_once(':')?;
                            (k.trim().eq_ignore_ascii_case("content-length"))
                                .then(|| v.trim().parse::<usize>().ok())
                                .flatten()
                        })
                        .unwrap_or(0);
                    while req.len() < end + len {
                        let n = sock.read(&mut buf).await.expect("read body");
                        if n == 0 {
                            break;
                        }
                        req.extend_from_slice(&buf[..n]);
                    }
                    String::from_utf8_lossy(&req[end..]).into_owned()
                } else {
                    String::new()
                };
                bodies.push(body);
                let payload = if bodies.len() == 1 {
                    r#"{"ok":true,"result":{"id":1,"first_name":"T","username":"testbot"}}"#
                } else {
                    r#"{"ok":true,"result":{"message_id":7}}"#
                };
                let resp = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{payload}",
                    payload.len()
                );
                sock.write_all(resp.as_bytes()).await.expect("write");
            }
            bodies
        });
        let base = format!("http://{addr}");
        let (_dir, reg) = test_registry(&base);
        reg.connect("telegram", GOOD_TOKEN).await.expect("connect");
        let msg = reg
            .test("telegram", Some("12345"), Some("ping"))
            .await
            .expect("test send");
        assert!(msg.contains("12345"), "msg: {msg}");
        let bodies = server.await.expect("server task");
        assert_eq!(bodies.len(), 2);
        let send_body: Value = serde_json::from_str(&bodies[1]).expect("send body JSON");
        assert_eq!(send_body["chat_id"], Value::String("12345".to_string()));
        assert_eq!(send_body["text"], Value::String("ping".to_string()));
    }

    // -- discord registry flow -------------------------------------------

    #[tokio::test]
    async fn discord_connect_verify_send_poll() {
        // verify(connect) → verify(test) → send(test) → poll.
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let addr = listener.local_addr().expect("addr");
        let server = tokio::spawn(async move {
            use tokio::io::{AsyncReadExt, AsyncWriteExt};
            let mut paths = Vec::new();
            for _ in 0..4 {
                let (mut sock, _) = listener.accept().await.expect("accept");
                let mut req: Vec<u8> = Vec::new();
                let mut buf = vec![0u8; 8192];
                loop {
                    let n = sock.read(&mut buf).await.expect("read");
                    if n == 0 {
                        break;
                    }
                    req.extend_from_slice(&buf[..n]);
                    if mock::find_header_end(&req).is_some() {
                        break;
                    }
                }
                let text = String::from_utf8_lossy(&req).into_owned();
                paths.push(text.lines().next().unwrap_or("").to_string());
                let payload = match paths.len() {
                    1 | 2 => r#"{"id":"111","username":"boomi","global_name":"Boomi"}"#,
                    3 => r#"{"id":"222","content":"hi"}"#,
                    _ => r#"[{"id":"222","channel_id":"9","author":{"username":"boomi"},"content":"hi","timestamp":"2026-10-17T07:00:00+00:00"}]"#,
                };
                let resp = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{payload}",
                    payload.len()
                );
                sock.write_all(resp.as_bytes()).await.expect("write");
            }
            paths
        });
        let base = format!("http://{addr}");
        let (_dir, reg) = test_registry_all(&base);
        let info = reg.connect("discord", "DISCORDTOKEN").await.expect("connect");
        assert_eq!(info.bot_username.as_deref(), Some("boomi"));
        assert!(reg.info("discord").detail.as_deref() == Some("@boomi"));
        let msg = reg.test("discord", None, None).await.expect("verify");
        assert!(msg.contains("@boomi"), "msg: {msg}");
        let sent = reg
            .test("discord", Some("9"), Some("hi"))
            .await
            .expect("send");
        assert!(sent.contains("222"), "sent: {sent}");
        let updates = reg
            .poll_updates("discord", Some("9"), None)
            .await
            .expect("poll");
        assert_eq!(updates.len(), 1);
        assert_eq!(updates[0].id, "222");
        assert_eq!(updates[0].chat_id, "9");
        let paths = server.await.expect("server task");
        assert!(paths[0].starts_with("GET /users/@me "), "p: {}", paths[0]);
        assert!(paths[2].starts_with("POST /channels/9/messages "), "p: {}", paths[2]);
        assert!(
            paths[3].starts_with("GET /channels/9/messages?limit=50 "),
            "p: {}",
            paths[3]
        );
    }

    #[tokio::test]
    async fn discord_poll_requires_channel_id() {
        let (base, _handle) = mock_server(
            200,
            r#"{"id":"111","username":"boomi"}"#,
        )
        .await;
        let (_dir, reg) = test_registry_all(&base);
        reg.connect("discord", "DISCORDTOKEN").await.expect("connect");
        let err = reg.poll_updates("discord", None, None).await.unwrap_err();
        assert_eq!(err.status, 400);
        assert!(err.message.contains("channel id"), "msg: {}", err.message);
    }

    #[tokio::test]
    async fn discord_connect_rejected_token_not_saved() {
        let (base, _handle) = mock_server(
            401,
            r#"{"message":"401: Unauthorized","code":0}"#,
        )
        .await;
        let (dir, reg) = test_registry_all(&base);
        let err = reg.connect("discord", "BADTOKEN").await.unwrap_err();
        assert_eq!(err.status, 502);
        assert!(!err.message.contains("BADTOKEN"), "msg: {}", err.message);
        assert!(!dir.path().join("channels").join("discord.json").exists());
    }

    // -- messenger registry flow ------------------------------------------

    #[tokio::test]
    async fn messenger_connect_verify_send() {
        // verify(connect) → verify(test) → send(test).
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let addr = listener.local_addr().expect("addr");
        let server = tokio::spawn(async move {
            use tokio::io::{AsyncReadExt, AsyncWriteExt};
            let mut paths = Vec::new();
            for _ in 0..3 {
                let (mut sock, _) = listener.accept().await.expect("accept");
                let mut req: Vec<u8> = Vec::new();
                let mut buf = vec![0u8; 8192];
                loop {
                    let n = sock.read(&mut buf).await.expect("read");
                    if n == 0 {
                        break;
                    }
                    req.extend_from_slice(&buf[..n]);
                    if mock::find_header_end(&req).is_some() {
                        break;
                    }
                }
                let text = String::from_utf8_lossy(&req).into_owned();
                paths.push(text.lines().next().unwrap_or("").to_string());
                let payload = match paths.len() {
                    1 | 2 => r#"{"id":"987","name":"Boom Page"}"#,
                    _ => r#"{"recipient_id":"111","message_id":"mid.1"}"#,
                };
                let resp = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{payload}",
                    payload.len()
                );
                sock.write_all(resp.as_bytes()).await.expect("write");
            }
            paths
        });
        let base = format!("http://{addr}");
        let (_dir, reg) = test_registry_all(&base);
        let info = reg.connect("messenger", "PAGETOKEN").await.expect("connect");
        assert!(info.message.contains("Boom Page"), "msg: {}", info.message);
        let msg = reg.test("messenger", None, None).await.expect("verify");
        assert!(msg.contains("Boom Page"), "msg: {msg}");
        let sent = reg
            .test("messenger", Some("111"), Some("hi"))
            .await
            .expect("send");
        assert!(sent.contains("mid.1"), "sent: {sent}");
        let paths = server.await.expect("server task");
        assert!(paths[0].starts_with("GET /me?fields=id,name&access_token="), "p: {}", paths[0]);
        assert!(paths[2].starts_with("POST /me/messages?access_token="), "p: {}", paths[2]);
    }

    #[tokio::test]
    async fn messenger_poll_drains_webhook_queue() {
        let (base, _handle) = mock_server(
            200,
            r#"{"id":"987","name":"P"}"#,
        )
        .await;
        let (_dir, reg) = test_registry_all(&base);
        reg.connect("messenger", "PAGETOKEN").await.expect("connect");
        // Empty queue → empty result (no more fake 501).
        let msgs = reg.poll_updates("messenger", None, None).await.expect("poll");
        assert!(msgs.is_empty());
        // Push via webhook → poll drains.
        reg.push_webhook_messages(
            "messenger",
            vec![InboundMessage {
                id: "mid.1".to_string(),
                chat_id: "PAGE1:USER1".to_string(),
                sender: Some("USER1".to_string()),
                text: "hello".to_string(),
                ts: None,
            }],
        );
        let msgs = reg.poll_updates("messenger", None, None).await.expect("poll");
        assert_eq!(msgs.len(), 1);
        assert_eq!(msgs[0].text, "hello");
        // Queue drained — second poll is empty.
        let msgs = reg.poll_updates("messenger", None, None).await.expect("poll");
        assert!(msgs.is_empty());
    }

    #[tokio::test]
    async fn slack_poll_is_honest_501_not_unsupported() {
        let (base, _handle) = mock_server(
            200,
            r#"{"ok":true,"user_id":"U1","team":"T"}"#,
        )
        .await;
        let (_dir, reg) = test_registry_all(&base);
        reg.connect("slack", "xoxb-test").await.expect("connect");
        let err = reg.poll_updates("slack", None, None).await.unwrap_err();
        // 501 with webhook guidance — NOT a fake "unsupported".
        assert_eq!(err.status, 501);
        assert!(err.message.contains("Socket Mode"), "msg: {}", err.message);
        assert!(!err.message.contains("chưa hỗ trợ"), "msg: {}", err.message);
    }

    #[tokio::test]
    async fn telegram_poll_returns_updates() {
        // Pre-seed the credential file so poll() never calls getMe; the
        // single canned response is getUpdates-shaped.
        let dir = tempfile::tempdir().expect("tempdir");
        let stored = serde_json::json!({"token": GOOD_TOKEN, "connected_at": "t"});
        let chdir = dir.path().join("channels");
        fs::create_dir_all(&chdir).unwrap();
        fs::write(
            chdir.join("telegram.json"),
            serde_json::to_string_pretty(&stored).unwrap(),
        )
        .unwrap();
        let (base, handle) = mock_server(
            200,
            r#"{"ok":true,"result":[{"update_id":5,"message":{"message_id":1,"from":{"id":2,"first_name":"A"},"chat":{"id":9},"date":1,"text":"yo"}}]}"#,
        )
        .await;
        let reg = FileChannelRegistry::with_base_dir_and_api(
            dir.path().to_path_buf(),
            base.clone(),
        );
        let msgs = reg
            .poll_updates("telegram", None, Some("4"))
            .await
            .expect("poll");
        assert_eq!(msgs.len(), 1);
        assert_eq!(msgs[0].id, "5");
        assert_eq!(msgs[0].chat_id, "9");
        assert_eq!(msgs[0].text, "yo");
        let reqs = handle.await.expect("server task");
        assert!(
            reqs[0].contains("getUpdates?timeout=0&offset=4"),
            "req: {}",
            reqs[0]
        );
    }

    #[test]
    fn channel_ids_mirror_known_channels() {
        let (_dir, reg) = test_registry("http://127.0.0.1:1");
        let ids = reg.channel_ids();
        assert_eq!(
            ids,
            vec![
                "telegram", "discord", "slack", "whatsapp", "line", "feishu", "zalo", "messenger"
            ]
        );
    }

    #[test]
    fn safe_filename_rejects_traversal() {
        assert_eq!(safe_filename("telegram"), Some("telegram.json".to_string()));
        assert!(safe_filename("../evil").is_none());
        assert!(safe_filename("a/b").is_none());
        assert!(safe_filename("").is_none());
    }

    // ---------------- Messenger OAuth login flow ----------------

    /// Path-routing mock for the OAuth flow:
    /// - `GET /oauth/access_token` → user token
    /// - `GET /me/accounts` → one page with a page token
    /// - `GET /me?` → page identity (the connect-time verify)
    async fn oauth_mock_server() -> (String, tokio::task::JoinHandle<Vec<String>>) {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind mock server");
        let addr = listener.local_addr().expect("local addr");
        let handle = tokio::spawn(async move {
            let mut requests = Vec::new();
            for _ in 0..3 {
                let (mut sock, _) = listener.accept().await.expect("accept");
                let mut req: Vec<u8> = Vec::new();
                let mut buf = vec![0u8; 8192];
                loop {
                    let n = sock.read(&mut buf).await.expect("read");
                    if n == 0 {
                        break;
                    }
                    req.extend_from_slice(&buf[..n]);
                    if mock::find_header_end(&req).is_some() {
                        break;
                    }
                }
                let text = String::from_utf8_lossy(&req).into_owned();
                requests.push(text.clone());
                let body = if text.contains("GET /oauth/access_token") {
                    r#"{"access_token":"USERTOKEN","token_type":"bearer"}"#
                } else if text.contains("GET /me/accounts") {
                    r#"{"data":[{"id":"987","name":"Boom Page","access_token":"PAGETOKEN"}]}"#
                } else {
                    r#"{"id":"987","name":"Boom Page"}"#
                };
                let resp = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                sock.write_all(resp.as_bytes()).await.expect("write");
            }
            requests
        });
        (format!("http://{addr}"), handle)
    }

    fn oauth_test_registry(base: &str) -> (tempfile::TempDir, FileChannelRegistry) {
        let dir = tempfile::tempdir().expect("tempdir");
        let reg = FileChannelRegistry::with_base_dir_and_apis(
            dir.path().to_path_buf(),
            "http://127.0.0.1:1".to_string(),
            "http://127.0.0.1:1".to_string(),
            base.to_string(),
            "http://127.0.0.1:1".to_string(),
            "http://127.0.0.1:1".to_string(),
            "http://127.0.0.1:1".to_string(),
            "http://127.0.0.1:1".to_string(),
        );
        (dir, reg)
    }

    #[test]
    fn oauth_config_roundtrip() {
        let (dir, reg) = test_registry("http://127.0.0.1:1");
        assert!(reg.messenger_oauth_config().is_none());
        reg.save_messenger_oauth_config("123456", "s3cr3t")
            .expect("save");
        let cfg = reg.messenger_oauth_config().expect("load");
        assert_eq!(cfg.app_id, "123456");
        assert_eq!(cfg.app_secret, "s3cr3t");
        assert!(dir.path().join("channels").join("messenger_oauth.json").exists());
        // Reload from disk on a fresh registry.
        let reg2 = FileChannelRegistry::with_base_dir(dir.path().to_path_buf());
        assert_eq!(
            reg2.messenger_oauth_config().expect("reload").app_id,
            "123456"
        );
    }

    #[test]
    fn oauth_config_rejects_empty() {
        let (_dir, reg) = test_registry("http://127.0.0.1:1");
        assert!(reg.save_messenger_oauth_config("", "x").is_err());
        assert!(reg.save_messenger_oauth_config("123", "").is_err());
        assert!(reg.messenger_oauth_config().is_none());
    }

    #[cfg(unix)]
    #[test]
    fn oauth_config_file_is_owner_only() {
        use std::os::unix::fs::PermissionsExt;
        let (dir, reg) = test_registry("http://127.0.0.1:1");
        reg.save_messenger_oauth_config("123", "s3cr3t").expect("save");
        let mode = fs::metadata(dir.path().join("channels").join("messenger_oauth.json"))
            .expect("metadata")
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode, 0o600, "oauth config must be 0600");
    }

    #[test]
    fn oauth_start_requires_config() {
        let (_dir, reg) = test_registry("http://127.0.0.1:1");
        let err = reg
            .messenger_oauth_start("http://localhost:18080/api/channels/messenger/oauth/callback")
            .unwrap_err();
        assert_eq!(err.status, 400);
    }

    #[test]
    fn oauth_start_returns_login_url_with_single_use_state() {
        let (_dir, reg) = test_registry("http://127.0.0.1:1");
        reg.save_messenger_oauth_config("123456", "s3cr3t").expect("save");
        let redirect = "http://localhost:18080/api/channels/messenger/oauth/callback";
        let url = reg.messenger_oauth_start(redirect).expect("start");
        assert!(url.contains("https://www.facebook.com/"), "url: {url}");
        assert!(url.contains("client_id=123456"), "url: {url}");
        assert!(url.contains("redirect_uri=http%3A%2F%2Flocalhost%3A18080"), "url: {url}");
        // A state was minted and stored.
        let states = reg.oauth_states.lock().expect("lock");
        assert_eq!(states.len(), 1);
    }

    #[tokio::test]
    async fn oauth_callback_rejects_unknown_state_without_network() {
        let (_dir, reg) = test_registry("http://127.0.0.1:1");
        reg.save_messenger_oauth_config("123", "s3cr3t").expect("save");
        let err = reg
            .messenger_oauth_callback("code", "nope", "http://localhost:1/cb")
            .await
            .unwrap_err();
        assert_eq!(err.status, 400);
    }

    #[tokio::test]
    async fn oauth_callback_full_flow_connects_page() {
        let (base, handle) = oauth_mock_server().await;
        let (_dir, reg) = oauth_test_registry(&base);
        reg.save_messenger_oauth_config("123456", "s3cr3t").expect("save");
        let redirect = "http://localhost:18080/api/channels/messenger/oauth/callback";
        let url = reg.messenger_oauth_start(redirect).expect("start");
        // Pull the minted state out of the URL.
        let state = url
            .split("state=")
            .nth(1)
            .expect("state in url")
            .split('&')
            .next()
            .expect("state value")
            .to_string();
        let page_name = reg
            .messenger_oauth_callback("thecode", &state, redirect)
            .await
            .expect("callback");
        assert_eq!(page_name, "Boom Page");
        assert_eq!(reg.info("messenger").status, "connected");
        // State is single-use: replaying fails.
        let replay = reg
            .messenger_oauth_callback("thecode", &state, redirect)
            .await
            .unwrap_err();
        assert_eq!(replay.status, 400);
        let reqs = handle.await.expect("server task");
        assert_eq!(reqs.len(), 3, "exchange + accounts + verify");
        assert!(reqs[0].contains("GET /oauth/access_token"), "req: {}", reqs[0]);
        assert!(reqs[1].contains("GET /me/accounts"), "req: {}", reqs[1]);
    }
}
