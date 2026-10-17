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
pub mod messenger;
pub mod telegram;

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

/// Channels with a real platform implementation (verify + send; Telegram
/// and Discord also poll for ingress).
const REAL_CHANNELS: &[&str] = &["telegram", "discord", "messenger"];

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
    /// In-memory lock so concurrent connect/disconnect calls serialize
    /// their read-modify-write cycles.
    write_lock: Mutex<()>,
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
        )
    }

    /// Constructor with an explicit base directory and Telegram API base
    /// (kept for compatibility; Discord/Messenger use their real bases).
    /// Tests driving all three platforms through one mock server can use
    /// [`FileChannelRegistry::with_base_dir_and_apis`].
    pub fn with_base_dir_and_api(base_dir: PathBuf, telegram_api: String) -> Self {
        Self::with_base_dir_and_apis(
            base_dir,
            telegram_api,
            discord::API_BASE.to_string(),
            messenger::API_BASE.to_string(),
        )
    }

    /// Constructor with an explicit base directory and per-platform API
    /// bases (tests drive local mock servers through this).
    pub fn with_base_dir_and_apis(
        base_dir: PathBuf,
        telegram_api: String,
        discord_api: String,
        messenger_api: String,
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
            write_lock: Mutex::new(()),
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
        let (telegram_api, discord_api, messenger_api) = (
            self.telegram_api.clone(),
            self.discord_api.clone(),
            self.messenger_api.clone(),
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
        let (telegram_api, discord_api, messenger_api) = (
            self.telegram_api.clone(),
            self.discord_api.clone(),
            self.messenger_api.clone(),
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
                "messenger" => {
                    // Honest 501, not "unsupported": the credential path is
                    // real, but Messenger offers no polling API — ingress
                    // requires a webhook subscription.
                    Err(ChannelError::new(
                        501,
                        "Messenger không có API polling — nhận tin nhắn cần webhook: xem docs/channels.md",
                    ))
                }
                _ => unreachable!("is_supported guard above"),
            }
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
        );
        (dir, reg)
    }

    #[test]
    fn supported_channels_are_real() {
        let (_dir, reg) = test_registry("http://127.0.0.1:1");
        for id in ["telegram", "discord", "messenger"] {
            let info = reg.info(id);
            assert!(info.supported, "id: {id}");
            assert_eq!(info.status, "not-configured", "id: {id}");
            assert!(info.detail.is_some(), "id: {id}");
        }
    }

    #[test]
    fn remaining_channels_are_honest() {
        let (_dir, reg) = test_registry("http://127.0.0.1:1");
        for id in ["slack", "whatsapp", "line", "feishu", "zalo"] {
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
        let err = reg.connect("slack", GOOD_TOKEN).await.unwrap_err();
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
        assert!(reg.disconnect("slack").is_err());
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
    async fn messenger_poll_is_honest_501_not_unsupported() {
        let (base, _handle) = mock_server(
            200,
            r#"{"id":"987","name":"P"}"#,
        )
        .await;
        let (_dir, reg) = test_registry_all(&base);
        reg.connect("messenger", "PAGETOKEN").await.expect("connect");
        let err = reg.poll_updates("messenger", Some("111"), None).await.unwrap_err();
        // 501 with webhook guidance — NOT a fake "unsupported".
        assert_eq!(err.status, 501);
        assert!(err.message.contains("webhook"), "msg: {}", err.message);
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
}
