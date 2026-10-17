//! Real channel connections: Telegram via the Bot API.
//!
//! Port of Python's `gateway/channels/telegram.py` reply path
//! (`getMe` verify + `sendMessage`) with the registry mechanics of
//! `gateway/channel.py`. Every other platform in [`KNOWN_CHANNELS`]
//! reports `unsupported` honestly — the Rust core never claims a
//! connection it cannot make.
//!
//! Credentials live in `$BOOM_HOME/channels.json` (default
//! `~/.boom/channels.json`, mode 0600), written atomically:
//!
//! ```json
//! {"channels": {"telegram": {"token": "...", "bot_username": "mybot", "connected_at": "2026-10-17T10:00:00"}}}
//! ```
//!
//! The token is never echoed into responses, logs, or error messages.
//! Tests never touch the real Telegram API: the API base is injectable
//! and unit tests drive a local mock HTTP server.

use std::{collections::HashMap, fs, path::PathBuf, sync::Mutex, time::Duration};

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::state::*;

/// File name inside the Boom home directory.
const CONFIG_FILENAME: &str = "channels.json";
/// The real Telegram Bot API base (overridable in tests).
const TELEGRAM_API_BASE: &str = "https://api.telegram.org";
/// Verify timeout (matches Python's probe style).
const VERIFY_TIMEOUT: Duration = Duration::from_secs(10);
/// Send timeout (Python's `send_text` uses 35s).
const SEND_TIMEOUT: Duration = Duration::from_secs(35);

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

/// Whole `channels.json` document.
#[derive(Debug, Default, Serialize, Deserialize)]
struct ChannelsFile {
    #[serde(default)]
    channels: HashMap<String, StoredChannel>,
}

/// Bot identity returned by `getMe`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BotIdentity {
    pub id: i64,
    pub username: Option<String>,
    pub first_name: String,
}

/// Telegram bot tokens look like `123456789:AAE...` — digits, a colon,
/// then the secret part. This is a cheap format pre-check; the real
/// validation is the live `getMe` call.
pub fn valid_bot_token_format(token: &str) -> bool {
    match token.split_once(':') {
        Some((head, tail)) => {
            !head.is_empty()
                && head.bytes().all(|b| b.is_ascii_digit())
                && !tail.trim().is_empty()
        }
        None => false,
    }
}

/// Parse a `getMe` response body. `Ok` → bot identity; `Err` → a
/// human-readable message that never contains the token.
pub fn parse_get_me(body: &Value) -> Result<BotIdentity, String> {
    if body.get("ok").and_then(Value::as_bool).unwrap_or(false) {
        let result = body.get("result").ok_or_else(|| {
            "Telegram returned ok:true without a result".to_string()
        })?;
        let id = result.get("id").and_then(Value::as_i64).ok_or_else(|| {
            "Telegram getMe response missing bot id".to_string()
        })?;
        let first_name = result
            .get("first_name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let username = result
            .get("username")
            .and_then(Value::as_str)
            .map(|s| s.to_string());
        Ok(BotIdentity { id, username, first_name })
    } else {
        let description = body
            .get("description")
            .and_then(Value::as_str)
            .unwrap_or("unknown error")
            .trim();
        let code = body.get("error_code").and_then(Value::as_i64);
        Err(match code {
            Some(c) => format!("Telegram rejected the token (HTTP {c}): {description}"),
            None => format!("Telegram rejected the token: {description}"),
        })
    }
}

/// Parse a `sendMessage` response body. `Ok` → the sent message id.
pub fn parse_send_message(body: &Value) -> Result<i64, String> {
    if body.get("ok").and_then(Value::as_bool).unwrap_or(false) {
        body.get("result")
            .and_then(|r| r.get("message_id"))
            .and_then(Value::as_i64)
            .ok_or_else(|| "Telegram sendMessage response missing message_id".to_string())
    } else {
        let description = body
            .get("description")
            .and_then(Value::as_str)
            .unwrap_or("unknown error")
            .trim();
        let code = body.get("error_code").and_then(Value::as_i64);
        Err(match code {
            Some(c) => format!("Telegram refused to send (HTTP {c}): {description}"),
            None => format!("Telegram refused to send: {description}"),
        })
    }
}

/// Read a JSON response body; non-JSON or empty bodies degrade to a
/// generic message (Telegram error descriptions live in JSON, but a
/// proxy/CDN may answer HTML).
async fn read_json_body(resp: reqwest::Response) -> Result<Value, String> {
    let status = resp.status();
    let text = resp.text().await.map_err(|_| "request failed".to_string())?;
    let text = text.trim();
    if text.is_empty() {
        return Err(format!("Telegram returned HTTP {} with an empty body", status.as_u16()));
    }
    serde_json::from_str::<Value>(text)
        .map_err(|_| format!("Telegram returned HTTP {} with a non-JSON body", status.as_u16()))
}

/// Generic transport failure message. Deliberately generic: reqwest errors
/// can echo the request URL, which carries the bot token in the path.
fn transport_error(err: reqwest::Error) -> String {
    if err.is_timeout() {
        "connection to Telegram timed out".to_string()
    } else if err.is_connect() {
        "could not reach Telegram (connection failed)".to_string()
    } else {
        "request to Telegram failed".to_string()
    }
}

/// Live `getMe` verify against the Bot API. `Err` messages never contain
/// the token.
pub async fn verify_token(api_base: &str, token: &str) -> Result<BotIdentity, String> {
    let client = reqwest::Client::builder()
        .timeout(VERIFY_TIMEOUT)
        .build()
        .map_err(|_| "failed to build HTTP client".to_string())?;
    // The token goes in the URL path, exactly like every Bot API client
    // (including Python's `gateway/channels/telegram.py`).
    let url = format!("{}/bot{}/getMe", api_base.trim_end_matches('/'), token);
    let resp = client.get(&url).send().await.map_err(transport_error)?;
    let body = read_json_body(resp).await?;
    parse_get_me(&body)
}

/// Live `sendMessage` against the Bot API. `Ok` → sent message id.
pub async fn send_text(
    api_base: &str,
    token: &str,
    chat_id: &str,
    text: &str,
) -> Result<i64, String> {
    let client = reqwest::Client::builder()
        .timeout(SEND_TIMEOUT)
        .build()
        .map_err(|_| "failed to build HTTP client".to_string())?;
    let url = format!(
        "{}/bot{}/sendMessage",
        api_base.trim_end_matches('/'),
        token
    );
    let resp = client
        .post(&url)
        .json(&serde_json::json!({ "chat_id": chat_id, "text": text }))
        .send()
        .await
        .map_err(transport_error)?;
    let body = read_json_body(resp).await?;
    parse_send_message(&body)
}

// ---------------------------------------------------------------------------
// File-backed registry
// ---------------------------------------------------------------------------

/// Channel registry persisted to `$BOOM_HOME/channels.json` (0600).
///
/// Construct with [`FileChannelRegistry::new`] (honours `BOOM_HOME`, like
/// Python) or [`FileChannelRegistry::with_base_dir`] for tests.
pub struct FileChannelRegistry {
    base_dir: PathBuf,
    path: PathBuf,
    api_base: String,
    /// In-memory lock so concurrent connect/disconnect calls serialize
    /// their read-modify-write cycles.
    write_lock: Mutex<()>,
}

impl FileChannelRegistry {
    /// Production constructor: `$BOOM_HOME/channels.json`, real Telegram API.
    pub fn new() -> Self {
        Self::with_base_dir(boom_home_dir())
    }

    /// Constructor with an explicit base directory (tests).
    pub fn with_base_dir(base_dir: PathBuf) -> Self {
        Self::with_base_dir_and_api(base_dir, TELEGRAM_API_BASE.to_string())
    }

    /// Constructor with an explicit base directory and Telegram API base
    /// (tests drive a local mock server through this).
    pub fn with_base_dir_and_api(base_dir: PathBuf, api_base: String) -> Self {
        let path = base_dir.join(CONFIG_FILENAME);
        Self {
            base_dir,
            path,
            api_base,
            write_lock: Mutex::new(()),
        }
    }

    /// The config file path (useful for tests / diagnostics).
    pub fn path(&self) -> &PathBuf {
        &self.path
    }

    /// Load the file, degrading to empty on any problem.
    fn load(&self) -> ChannelsFile {
        match fs::read_to_string(&self.path) {
            Ok(raw) => serde_json::from_str::<ChannelsFile>(&raw).unwrap_or_default(),
            Err(_) => ChannelsFile::default(),
        }
    }

    /// Atomic 0600 write.
    fn save(&self, data: &ChannelsFile) -> Result<(), String> {
        let text = serde_json::to_string_pretty(data)
            .map_err(|e| format!("failed to serialize channel config: {e}"))?;
        atomic_write_secret(&self.base_dir, CONFIG_FILENAME, &text)
            .map_err(|e| format!("failed to save channel config: {e}"))?;
        Ok(())
    }

    fn stored(&self, id: &str) -> Option<StoredChannel> {
        self.load().channels.get(id).cloned()
    }

    fn is_supported(id: &str) -> bool {
        id == "telegram"
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
        match self.stored(id) {
            Some(stored) if !stored.token.trim().is_empty() => ChannelInfo {
                id: id.to_string(),
                name: display_name(id).to_string(),
                status: "connected".to_string(),
                supported: true,
                detail: stored
                    .bot_username
                    .as_deref()
                    .map(|u| format!("@{u}")),
            },
            _ => ChannelInfo {
                id: id.to_string(),
                name: display_name(id).to_string(),
                status: "not-configured".to_string(),
                supported: true,
                detail: Some(
                    "Chưa cấu hình — dán bot token để kết nối".to_string(),
                ),
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
        let api_base = self.api_base.clone();
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
            if !valid_bot_token_format(&token) {
                // Cheap pre-check so an obviously-wrong paste fails fast;
                // the live getMe call is the real validation.
                return Err(ChannelError::bad_request(
                    "invalid bot token format (expected <digits>:<secret>)",
                ));
            }
            // Live verification BEFORE persisting: a bad token is never
            // saved. The error message never contains the token.
            let identity = verify_token(&api_base, &token)
                .await
                .map_err(ChannelError::upstream)?;
            let username = identity.username.clone();
            let message = match &username {
                Some(u) => format!("Đã kết nối Telegram bot @{u}"),
                None => "Đã kết nối Telegram bot".to_string(),
            };
            let _guard = self.write_lock.lock().map_err(|_| {
                ChannelError::new(500, "failed to save channel config")
            })?;
            let mut data = self.load();
            data.channels.insert(
                id.clone(),
                StoredChannel {
                    token,
                    bot_username: username.clone(),
                    connected_at: now_iso(),
                },
            );
            self.save(&data)
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
        let mut data = self.load();
        // Idempotent: removing a missing entry still succeeds.
        data.channels.remove(id);
        self.save(&data).map_err(|e| ChannelError::new(500, e))?;
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
        let api_base = self.api_base.clone();
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
            let stored = match self.stored(&id) {
                Some(s) if !s.token.trim().is_empty() => s,
                _ => {
                    return Err(ChannelError::bad_request(
                        "Telegram chưa được kết nối — hãy connect trước",
                    ))
                }
            };
            match chat_id {
                Some(chat) => {
                    let text = message.unwrap_or_else(|| "Boom test message".to_string());
                    let msg_id = send_text(&api_base, &stored.token, &chat, &text)
                        .await
                        .map_err(ChannelError::upstream)?;
                    Ok(format!("Đã gửi tin nhắn test tới {chat} (message_id {msg_id})"))
                }
                None => {
                    let identity = verify_token(&api_base, &stored.token)
                        .await
                        .map_err(ChannelError::upstream)?;
                    Ok(match identity.username {
                        Some(u) => format!("Token hợp lệ — bot @{u} đang hoạt động"),
                        None => "Token hợp lệ — bot đang hoạt động".to_string(),
                    })
                }
            }
        })
    }
}

// ---------------------------------------------------------------------------
// Tests — never touch the real Telegram API; a local mock server stands in.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // -- pure logic ------------------------------------------------------

    #[test]
    fn token_format_validation() {
        assert!(valid_bot_token_format("123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"));
        assert!(valid_bot_token_format("1:x"));
        assert!(!valid_bot_token_format(""));
        assert!(!valid_bot_token_format("no-colon-here"));
        assert!(!valid_bot_token_format(":onlysecret"));
        assert!(!valid_bot_token_format("abc:secret"));
        assert!(!valid_bot_token_format("123:"));
        assert!(!valid_bot_token_format("123:   "));
    }

    #[test]
    fn parse_get_me_ok() {
        let body: Value = serde_json::from_str(
            r#"{"ok":true,"result":{"id":123456789,"is_bot":true,"first_name":"Boomi","username":"boomi_bot"}}"#,
        )
        .unwrap();
        let id = parse_get_me(&body).expect("must parse");
        assert_eq!(
            id,
            BotIdentity {
                id: 123456789,
                username: Some("boomi_bot".to_string()),
                first_name: "Boomi".to_string(),
            }
        );
    }

    #[test]
    fn parse_get_me_ok_without_username() {
        let body: Value = serde_json::from_str(
            r#"{"ok":true,"result":{"id":7,"first_name":"X"}}"#,
        )
        .unwrap();
        let id = parse_get_me(&body).expect("must parse");
        assert_eq!(id.username, None);
    }

    #[test]
    fn parse_get_me_rejected_never_echoes_token() {
        let body: Value = serde_json::from_str(
            r#"{"ok":false,"error_code":401,"description":"Unauthorized"}"#,
        )
        .unwrap();
        let err = parse_get_me(&body).unwrap_err();
        assert!(err.contains("401"), "err: {err}");
        assert!(err.contains("Unauthorized"), "err: {err}");
        // A malicious description echoing the token would be a leak —
        // descriptions come from Telegram, but assert the shape anyway.
        assert!(!err.contains("bot"), "err: {err}");
    }

    #[test]
    fn parse_get_me_malformed() {
        let body: Value = serde_json::from_str(r#"{"ok":true}"#).unwrap();
        assert!(parse_get_me(&body).is_err());
        let body: Value = serde_json::from_str(r#"{"unexpected":1}"#).unwrap();
        let err = parse_get_me(&body).unwrap_err();
        assert!(err.contains("unknown error"), "err: {err}");
    }

    #[test]
    fn parse_send_message_ok() {
        let body: Value = serde_json::from_str(
            r#"{"ok":true,"result":{"message_id":42,"chat":{"id":1},"text":"hi"}}"#,
        )
        .unwrap();
        assert_eq!(parse_send_message(&body).expect("must parse"), 42);
    }

    #[test]
    fn parse_send_message_refused() {
        let body: Value = serde_json::from_str(
            r#"{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}"#,
        )
        .unwrap();
        let err = parse_send_message(&body).unwrap_err();
        assert!(err.contains("chat not found"), "err: {err}");
    }

    // -- mock HTTP server (stands in for api.telegram.org) ---------------

    /// Serve one canned JSON response; returns the base URL and the full
    /// raw request (headers + body) the client sent.
    async fn mock_server(
        status: u16,
        body: &'static str,
    ) -> (String, tokio::task::JoinHandle<String>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind mock server");
        let addr = listener.local_addr().expect("local addr");
        let handle = tokio::spawn(async move {
            use tokio::io::{AsyncReadExt, AsyncWriteExt};
            let (mut sock, _) = listener.accept().await.expect("accept");
            let mut req: Vec<u8> = Vec::new();
            let mut buf = vec![0u8; 8192];
            // Read headers first.
            let header_end = loop {
                let n = sock.read(&mut buf).await.expect("read");
                if n == 0 {
                    break None;
                }
                req.extend_from_slice(&buf[..n]);
                if let Some(pos) = find_header_end(&req) {
                    break Some(pos);
                }
            };
            // Then the body, per Content-Length.
            if let Some(end) = header_end {
                let headers = String::from_utf8_lossy(&req[..end]).into_owned();
                let content_len = headers
                    .lines()
                    .find_map(|l| {
                        let (k, v) = l.split_once(':')?;
                        (k.trim().eq_ignore_ascii_case("content-length"))
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
            let request_text = String::from_utf8_lossy(&req).into_owned();
            let reason = match status {
                200 => "OK",
                400 => "Bad Request",
                401 => "Unauthorized",
                _ => "Error",
            };
            let resp = format!(
                "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            sock.write_all(resp.as_bytes()).await.expect("write");
            request_text
        });
        (format!("http://{addr}"), handle)
    }

    fn find_header_end(buf: &[u8]) -> Option<usize> {
        buf.windows(4)
            .position(|w| w == b"\r\n\r\n")
            .map(|p| p + 4)
    }

    /// Bind a port and immediately drop it, so connecting is refused.
    async fn closed_port_url() -> String {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let addr = listener.local_addr().expect("local addr");
        drop(listener);
        format!("http://{addr}")
    }

    fn test_registry(api_base: &str) -> (tempfile::TempDir, FileChannelRegistry) {
        let dir = tempfile::tempdir().expect("tempdir");
        let reg = FileChannelRegistry::with_base_dir_and_api(
            dir.path().to_path_buf(),
            api_base.to_string(),
        );
        (dir, reg)
    }

    const GOOD_TOKEN: &str = "123456:TESTTOKEN";

    #[tokio::test]
    async fn verify_token_ok_against_mock() {
        let (base, handle) = mock_server(
            200,
            r#"{"ok":true,"result":{"id":1,"first_name":"T","username":"testbot"}}"#,
        )
        .await;
        let id = verify_token(&base, GOOD_TOKEN).await.expect("verify");
        assert_eq!(id.username.as_deref(), Some("testbot"));
        let req = handle.await.expect("server task");
        // Token travels in the URL path, exactly like the Bot API requires.
        assert!(req.starts_with("GET /bot123456:TESTTOKEN/getMe "), "req: {req}");
    }

    #[tokio::test]
    async fn verify_token_rejected_against_mock() {
        let (base, _handle) = mock_server(
            401,
            r#"{"ok":false,"error_code":401,"description":"Unauthorized"}"#,
        )
        .await;
        let err = verify_token(&base, GOOD_TOKEN).await.unwrap_err();
        assert!(err.contains("Unauthorized"), "err: {err}");
        // The token must never leak into the error.
        assert!(!err.contains(GOOD_TOKEN), "err: {err}");
        assert!(!err.contains("TESTTOKEN"), "err: {err}");
    }

    #[tokio::test]
    async fn verify_token_connection_refused_is_generic() {
        let base = closed_port_url().await;
        let err = verify_token(&base, GOOD_TOKEN).await.unwrap_err();
        assert!(!err.is_empty());
        assert!(!err.contains(GOOD_TOKEN), "err: {err}");
        assert!(!err.contains("TESTTOKEN"), "err: {err}");
    }

    #[tokio::test]
    async fn send_text_posts_json_body() {
        let (base, handle) = mock_server(
            200,
            r#"{"ok":true,"result":{"message_id":99}}"#,
        )
        .await;
        let msg_id = send_text(&base, GOOD_TOKEN, "12345", "hello")
            .await
            .expect("send");
        assert_eq!(msg_id, 99);
        let req = handle.await.expect("server task");
        assert!(req.starts_with("POST /bot123456:TESTTOKEN/sendMessage "), "req: {req}");
        assert!(req.contains(r#""chat_id":"12345""#), "req: {req}");
        assert!(req.contains(r#""text":"hello""#), "req: {req}");
    }

    #[tokio::test]
    async fn send_text_refused() {
        let (base, _handle) = mock_server(
            400,
            r#"{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}"#,
        )
        .await;
        let err = send_text(&base, GOOD_TOKEN, "nope", "hi")
            .await
            .unwrap_err();
        assert!(err.contains("chat not found"), "err: {err}");
        assert!(!err.contains("TESTTOKEN"), "err: {err}");
    }

    // -- registry behaviour ----------------------------------------------

    #[test]
    fn unsupported_channels_are_honest() {
        let (_dir, reg) = test_registry("http://127.0.0.1:1");
        for id in ["discord", "slack", "whatsapp", "line", "feishu", "zalo", "messenger"] {
            let info = reg.info(id);
            assert_eq!(info.status, "unsupported", "id: {id}");
            assert!(!info.supported, "id: {id}");
            let detail = info.detail.expect("detail");
            assert!(detail.contains("chưa hỗ trợ trong bản này"), "detail: {detail}");
            assert!(!detail.contains("not wired"), "detail: {detail}");
        }
        let unknown = reg.info("tiktok");
        assert_eq!(unknown.status, "unsupported");
        assert_eq!(unknown.detail.as_deref(), Some("unknown channel"));
    }

    #[test]
    fn telegram_starts_not_configured() {
        let (_dir, reg) = test_registry("http://127.0.0.1:1");
        let info = reg.info("telegram");
        assert_eq!(info.status, "not-configured");
        assert!(info.supported);
        assert!(info.detail.is_some());
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
        let err = reg.connect("discord", GOOD_TOKEN).await.unwrap_err();
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
    async fn connect_verifies_then_persists() {
        let (base, _handle) = mock_server(
            200,
            r#"{"ok":true,"result":{"id":1,"first_name":"T","username":"testbot"}}"#,
        )
        .await;
        let (_dir, reg) = test_registry(&base);
        let info = reg.connect("telegram", GOOD_TOKEN).await.expect("connect");
        assert_eq!(info.bot_username.as_deref(), Some("testbot"));

        let status = reg.info("telegram");
        assert_eq!(status.status, "connected");
        assert_eq!(status.detail.as_deref(), Some("@testbot"));

        // Persisted: a fresh registry on the same dir sees it, and the
        // token is on disk but never in info().
        let reg2 = FileChannelRegistry::with_base_dir_and_api(
            _dir.path().to_path_buf(),
            base.clone(),
        );
        assert_eq!(reg2.info("telegram").status, "connected");
        let raw = fs::read_to_string(reg2.path()).expect("read channels.json");
        let v: Value = serde_json::from_str(&raw).expect("valid JSON");
        assert_eq!(v["channels"]["telegram"]["token"], Value::String(GOOD_TOKEN.to_string()));
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn channels_file_is_owner_only() {
        use std::os::unix::fs::PermissionsExt;
        let (base, _handle) = mock_server(
            200,
            r#"{"ok":true,"result":{"id":1,"first_name":"T"}}"#,
        )
        .await;
        let (_dir, reg) = test_registry(&base);
        reg.connect("telegram", GOOD_TOKEN).await.expect("connect");
        let mode = fs::metadata(reg.path())
            .expect("metadata")
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode, 0o600, "channels.json must be 0600");
    }

    #[tokio::test]
    async fn connect_does_not_persist_rejected_token() {
        let (base, _handle) = mock_server(
            401,
            r#"{"ok":false,"error_code":401,"description":"Unauthorized"}"#,
        )
        .await;
        let (_dir, reg) = test_registry(&base);
        let err = reg.connect("telegram", GOOD_TOKEN).await.unwrap_err();
        assert_eq!(err.status, 502, "msg: {}", err.message);
        assert!(!err.message.contains("TESTTOKEN"), "msg: {}", err.message);
        assert_eq!(reg.info("telegram").status, "not-configured");
        assert!(!reg.path().exists(), "rejected token must not be saved");
    }

    #[tokio::test]
    async fn disconnect_removes_token_and_is_idempotent() {
        let (base, _handle) = mock_server(
            200,
            r#"{"ok":true,"result":{"id":1,"first_name":"T","username":"testbot"}}"#,
        )
        .await;
        let (_dir, reg) = test_registry(&base);
        reg.connect("telegram", GOOD_TOKEN).await.expect("connect");
        reg.disconnect("telegram").expect("disconnect");
        assert_eq!(reg.info("telegram").status, "not-configured");
        // Second disconnect still succeeds.
        reg.disconnect("telegram").expect("idempotent disconnect");
        assert!(reg.disconnect("discord").is_err());
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
        let (base, _handle) = mock_server(
            200,
            r#"{"ok":true,"result":{"id":1,"first_name":"T","username":"testbot"}}"#,
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
                    if let Some(pos) = find_header_end(&req) {
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

    #[tokio::test]
    async fn channel_ids_mirror_known_channels() {
        let (_dir, reg) = test_registry("http://127.0.0.1:1");
        let ids = reg.channel_ids();
        assert_eq!(
            ids,
            vec![
                "telegram", "discord", "slack", "whatsapp", "line", "feishu", "zalo", "messenger"
            ]
        );
    }
}
