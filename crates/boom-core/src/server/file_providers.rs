//! File-backed [`ProviderRegistry`](super::state::ProviderRegistry).
//!
//! Drop-in replacement for [`super::defaults::StaticProviderRegistry`] that
//! persists credentials to `$BOOM_HOME/providers.json` (default
//! `~/.boom/providers.json`, mode 0600) in exactly the shape Python's
//! `agent/src/providers/manager.py` writes, so existing users keep their
//! configs across restarts:
//!
//! ```json
//! {"active": "openai", "providers": {"openai": {"api_key": "...", "model": "gpt-4o", "base_url": "https://api.openai.com/v1"}}}
//! ```
//!
//! Read path mirrors Python's `_load`: a missing file, invalid JSON, or a
//! non-object document all degrade to `{active: None, providers: {}}`.
//! Writes are atomic (temp file + rename) and re-apply 0600 after the
//! rename, exactly like Python's `_save`. Secrets are never logged — the
//! key only ever lives inside the config file.

use std::{collections::HashMap, fs, path::PathBuf, time::Duration};

use serde::{Deserialize, Serialize};

use super::defaults::{probe_ollama, provider_defs};
use super::state::*;

/// File name inside the Boom home directory (Python: `CONFIG_FILENAME`).
const CONFIG_FILENAME: &str = "providers.json";
/// Owner-read/write-only (Python: `CONFIG_MODE = 0o600`).
#[cfg(unix)]
const CONFIG_MODE: u32 = 0o600;
/// Probe timeout (Python: `TEST_TIMEOUT_SEC = 10.0`).
const TEST_TIMEOUT: Duration = Duration::from_secs(10);

/// One provider's stored credentials — the exact JSON object Python writes
/// per provider id (`{"api_key", "model", "base_url"}`).
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct StoredConfig {
    #[serde(default)]
    api_key: String,
    #[serde(default)]
    model: String,
    #[serde(default)]
    base_url: String,
}

/// Whole `providers.json` document: `{"active", "providers"}`.
/// Unknown extra keys are ignored on read (Python only ever writes these
/// two), and `active: null` maps to `None`.
#[derive(Debug, Default, Serialize, Deserialize)]
struct ProvidersFile {
    #[serde(default)]
    active: Option<String>,
    #[serde(default)]
    providers: HashMap<String, StoredConfig>,
}

/// Provider registry that persists to `$BOOM_HOME/providers.json`.
///
/// Construct with [`FileProviderRegistry::new`] (honours `BOOM_HOME`, like
/// Python) or [`FileProviderRegistry::with_base_dir`] for tests.
pub struct FileProviderRegistry {
    base_dir: PathBuf,
    path: PathBuf,
}

impl FileProviderRegistry {
    /// Resolve the config dir from `BOOM_HOME`, falling back to `~/.boom`
    /// — the same convention as Python's `ProviderManager.__init__` and
    /// `agent/src/config.py`. Touches no files.
    pub fn new() -> Self {
        Self::with_base_dir(default_base_dir())
    }

    /// Constructor with an explicit base directory (Python's injectable
    /// `base_dir`; used by tests to avoid the real `~/.boom`).
    pub fn with_base_dir(base_dir: PathBuf) -> Self {
        let path = base_dir.join(CONFIG_FILENAME);
        Self { base_dir, path }
    }

    /// The config file path (useful for tests / diagnostics).
    pub fn path(&self) -> &PathBuf {
        &self.path
    }

    /// Load the file, degrading to empty on any problem (Python `_load`).
    fn load(&self) -> ProvidersFile {
        match fs::read_to_string(&self.path) {
            Ok(raw) => serde_json::from_str::<ProvidersFile>(&raw).unwrap_or_default(),
            Err(_) => ProvidersFile::default(),
        }
    }

    /// Atomic write with 0600 permissions (Python `_save`).
    fn save(&self, data: &ProvidersFile) -> Result<(), ProviderConfigError> {
        fs::create_dir_all(&self.base_dir).map_err(|e| {
            ProviderConfigError::Internal(format!("failed to create config dir: {e}"))
        })?;
        let text = serde_json::to_string_pretty(data).map_err(|e| {
            ProviderConfigError::Internal(format!("failed to serialize provider config: {e}"))
        })?;
        let tmp = self.base_dir.join("providers.json.tmp");
        fs::write(&tmp, text).map_err(|e| {
            ProviderConfigError::Internal(format!("failed to save provider config: {e}"))
        })?;
        restrict_permissions(&tmp).map_err(|e| {
            ProviderConfigError::Internal(format!("failed to save provider config: {e}"))
        })?;
        fs::rename(&tmp, &self.path).map_err(|e| {
            ProviderConfigError::Internal(format!("failed to save provider config: {e}"))
        })?;
        // rename() can reset the mode on some filesystems — enforce again.
        restrict_permissions(&self.path).map_err(|e| {
            ProviderConfigError::Internal(format!("failed to save provider config: {e}"))
        })?;
        Ok(())
    }

    fn get_stored(&self, id: &str) -> Option<StoredConfig> {
        self.load().providers.get(id).cloned()
    }
}

impl Default for FileProviderRegistry {
    fn default() -> Self {
        Self::new()
    }
}

/// `$BOOM_HOME`, else `~/.boom` (Python: `os.environ.get("BOOM_HOME", ...)`).
fn default_base_dir() -> PathBuf {
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

/// Owner-only permissions on Unix; a no-op on Windows (per task spec).
#[cfg(unix)]
fn restrict_permissions(path: &std::path::Path) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(CONFIG_MODE))
}

#[cfg(not(unix))]
fn restrict_permissions(_path: &std::path::Path) -> std::io::Result<()> {
    Ok(())
}

// ---------------------------------------------------------------------------
// Connection probes (Python: ProviderManager._build_probe / test_connection)
// ---------------------------------------------------------------------------

/// Map an HTTP status onto Python's `(ok, message)` contract. Never
/// includes the API key in the message.
fn status_outcome(status: reqwest::StatusCode) -> (bool, String) {
    let code = status.as_u16();
    if status.is_success() {
        (true, "connected".to_string())
    } else if code == 401 || code == 403 {
        (
            false,
            format!("authentication failed (HTTP {code}): check the API key"),
        )
    } else {
        (false, format!("request failed (HTTP {code})"))
    }
}

/// Transport failure → `(false, message)`. The message is deliberately
/// generic: reqwest errors can echo the request URL, and the Gemini probe
/// carries the key as a query parameter.
fn transport_outcome(err: reqwest::Error) -> (bool, String) {
    if err.is_timeout() {
        (false, "connection timed out".to_string())
    } else if err.is_connect() {
        (false, "connection failed".to_string())
    } else {
        (false, "request failed".to_string())
    }
}

/// OpenAI-compatible probe: `GET {base}/models` with a Bearer token
/// (OpenAI, xAI, DeepSeek, Ollama, custom).
async fn probe_openai_models(
    client: &reqwest::Client,
    base: &str,
    api_key: &str,
) -> (bool, String) {
    let mut req = client.get(format!("{base}/models"));
    if !api_key.is_empty() {
        req = req.bearer_auth(api_key);
    }
    match req.send().await {
        Ok(resp) => status_outcome(resp.status()),
        Err(e) => transport_outcome(e),
    }
}

/// Anthropic probe: `POST {base}/v1/messages` with `max_tokens: 1`.
async fn probe_anthropic(
    client: &reqwest::Client,
    base: &str,
    api_key: &str,
    model: &str,
) -> (bool, String) {
    let body = serde_json::json!({
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    });
    let resp = client
        .post(format!("{base}/v1/messages"))
        .header("x-api-key", api_key)
        .header("anthropic-version", "2023-06-01")
        .json(&body)
        .send()
        .await;
    match resp {
        Ok(resp) => status_outcome(resp.status()),
        Err(e) => transport_outcome(e),
    }
}

/// Gemini probe: `GET {base}/v1beta/models?key=...`.
async fn probe_gemini(client: &reqwest::Client, base: &str, api_key: &str) -> (bool, String) {
    let url = format!("{base}/v1beta/models?key={}", percent_encode(api_key));
    match client.get(&url).send().await {
        Ok(resp) => status_outcome(resp.status()),
        Err(e) => transport_outcome(e),
    }
}

/// Minimal percent-encoding for one query-string value (the API key).
fn percent_encode(s: &str) -> String {
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
// ProviderRegistry impl
// ---------------------------------------------------------------------------

impl ProviderRegistry for FileProviderRegistry {
    fn list_providers(&self) -> Vec<ProviderMeta> {
        provider_defs()
    }

    fn has_key(&self, id: &str) -> bool {
        self.get_stored(id)
            .map(|c| !c.api_key.trim().is_empty())
            .unwrap_or(false)
    }

    fn active_provider(&self) -> Option<String> {
        let active = self.load().active?;
        // Mirror Python's get_active_provider: only known ids count.
        if provider_defs().iter().any(|d| d.id == active) {
            Some(active)
        } else {
            None
        }
    }

    fn save_config(&self, cfg: ProviderConfig) -> Result<(), ProviderConfigError> {
        // Same validation as StaticProviderRegistry.
        let defn = provider_defs()
            .into_iter()
            .find(|d| d.id == cfg.provider_id)
            .ok_or_else(|| ProviderConfigError::UnknownProvider(cfg.provider_id.clone()))?;
        let api_key = cfg.api_key.trim().to_string();
        let mut model = cfg.model.trim().to_string();
        let base_url = cfg.base_url.as_deref().unwrap_or("").trim().to_string();
        // Sentinel "keep existing": the UI sends an empty key when the key
        // input is untouched (it renders as bullets for a server-side key).
        // Keep the previously stored key in that case; only reject when no
        // key was ever stored for this provider.
        let api_key = if api_key.is_empty() {
            self.get_stored(&cfg.provider_id)
                .map(|c| c.api_key)
                .unwrap_or_default()
        } else {
            api_key
        };
        if defn.needs_key && api_key.trim().is_empty() {
            return Err(ProviderConfigError::Invalid(format!(
                "{} requires an API key",
                defn.name
            )));
        }
        if model.is_empty() {
            model = defn.models.first().cloned().unwrap_or_default();
        }
        let base_url = if base_url.is_empty() {
            defn.default_base_url.clone()
        } else {
            base_url
        };
        let mut data = self.load();
        data.providers.insert(
            cfg.provider_id.clone(),
            StoredConfig {
                api_key,
                model,
                base_url,
            },
        );
        // Saving a config activates that provider.
        data.active = Some(cfg.provider_id);
        self.save(&data)
    }

    fn test_connection(&self, id: &str) -> BoxFuture<'_, Result<(bool, String), String>> {
        let id = id.to_string();
        Box::pin(async move {
            let defn = provider_defs()
                .into_iter()
                .find(|d| d.id == id)
                .ok_or_else(|| format!("unknown provider: {id}"))?;
            let stored = match self.get_stored(&id) {
                Some(c) => c,
                None => {
                    return Ok((
                        false,
                        "no configuration saved for this provider yet".to_string(),
                    ))
                }
            };
            let api_key = stored.api_key.trim().to_string();
            if defn.needs_key && api_key.is_empty() {
                return Ok((false, "API key is required but not set".to_string()));
            }
            let base = stored.base_url.trim();
            let base = if base.is_empty() {
                defn.default_base_url.clone()
            } else {
                base.to_string()
            };
            let base = base.trim_end_matches('/').to_string();
            let model = if stored.model.trim().is_empty() {
                defn.models.first().cloned().unwrap_or_default()
            } else {
                stored.model.trim().to_string()
            };
            let client = reqwest::Client::builder()
                .timeout(TEST_TIMEOUT)
                .build()
                .map_err(|e| format!("failed to build HTTP client: {e}"))?;
            // Probe kind per provider, mirroring Python's PROVIDER_DEFS
            // "probe" field: anthropic → messages ping, google → Gemini
            // models list, everything else → OpenAI-compatible /models.
            Ok(match id.as_str() {
                "anthropic" => probe_anthropic(&client, &base, &api_key, &model).await,
                "google" => probe_gemini(&client, &base, &api_key).await,
                _ => probe_openai_models(&client, &base, &api_key).await,
            })
        })
    }

    fn ollama_status(&self) -> BoxFuture<'_, OllamaStatus> {
        Box::pin(async move { probe_ollama().await })
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn test_registry() -> (tempfile::TempDir, FileProviderRegistry) {
        let dir = tempfile::tempdir().expect("tempdir");
        let reg = FileProviderRegistry::with_base_dir(dir.path().to_path_buf());
        (dir, reg)
    }

    fn save_openai(reg: &FileProviderRegistry, base_url: Option<&str>) {
        reg.save_config(ProviderConfig {
            provider_id: "openai".to_string(),
            api_key: "sk-test-key".to_string(),
            model: "gpt-4o".to_string(),
            base_url: base_url.map(|s| s.to_string()),
        })
        .expect("save_config");
    }

    #[test]
    fn list_providers_uses_shared_defs() {
        let (_dir, reg) = test_registry();
        let ids: Vec<_> = reg.list_providers().iter().map(|m| m.id.clone()).collect();
        assert_eq!(
            ids,
            vec!["openai", "anthropic", "google", "xai", "deepseek", "ollama", "custom"]
        );
    }

    #[test]
    fn save_then_reload_from_disk() {
        let (dir, reg) = test_registry();
        save_openai(&reg, None);

        // A fresh instance on the same dir sees the persisted config.
        let reg2 = FileProviderRegistry::with_base_dir(dir.path().to_path_buf());
        assert!(reg2.has_key("openai"));
        assert!(!reg2.has_key("anthropic"));
        assert_eq!(reg2.active_provider().as_deref(), Some("openai"));

        // The file itself has the exact Python shape.
        let raw = fs::read_to_string(reg2.path()).expect("read providers.json");
        let v: Value = serde_json::from_str(&raw).expect("valid JSON");
        assert_eq!(v["active"], Value::String("openai".to_string()));
        let openai = &v["providers"]["openai"];
        assert_eq!(openai["api_key"], Value::String("sk-test-key".to_string()));
        assert_eq!(openai["model"], Value::String("gpt-4o".to_string()));
        assert_eq!(
            openai["base_url"],
            Value::String("https://api.openai.com/v1".to_string())
        );
    }

    #[test]
    fn save_sets_active_to_saved_provider() {
        let (_dir, reg) = test_registry();
        save_openai(&reg, None);
        assert_eq!(reg.active_provider().as_deref(), Some("openai"));
        reg.save_config(ProviderConfig {
            provider_id: "deepseek".to_string(),
            api_key: "sk-ds".to_string(),
            model: String::new(), // empty → first defn model
            base_url: None,       // empty → default base URL
        })
        .expect("save deepseek");
        assert_eq!(reg.active_provider().as_deref(), Some("deepseek"));

        let raw = fs::read_to_string(reg.path()).unwrap();
        let v: Value = serde_json::from_str(&raw).unwrap();
        assert_eq!(v["providers"]["deepseek"]["model"], Value::String("deepseek-chat".to_string()));
        assert_eq!(
            v["providers"]["deepseek"]["base_url"],
            Value::String("https://api.deepseek.com/v1".to_string())
        );
    }

    #[test]
    fn reads_python_written_file() {
        let (dir, reg) = test_registry();
        // Byte-shape Python's _save writes (incl. "active": null variant).
        fs::write(
            dir.path().join("providers.json"),
            r#"{"active": "xai", "providers": {"xai": {"api_key": "xai-key", "model": "grok-4", "base_url": "https://api.x.ai/v1"}}}"#,
        )
        .unwrap();
        assert!(reg.has_key("xai"));
        assert_eq!(reg.active_provider().as_deref(), Some("xai"));

        fs::write(
            dir.path().join("providers.json"),
            r#"{"active": null, "providers": {}}"#,
        )
        .unwrap();
        assert_eq!(reg.active_provider(), None);
        assert!(!reg.has_key("openai"));

        // Unknown active id is ignored, like Python's get_active_provider.
        fs::write(
            dir.path().join("providers.json"),
            r#"{"active": "nope", "providers": {}}"#,
        )
        .unwrap();
        assert_eq!(reg.active_provider(), None);

        // Corrupt / non-object files degrade to empty, like Python's _load.
        for bad in ["not json at all", "[1,2,3]", ""] {
            fs::write(dir.path().join("providers.json"), bad).unwrap();
            assert_eq!(reg.active_provider(), None);
            assert!(!reg.has_key("openai"));
        }
        // Missing file → empty.
        fs::remove_file(dir.path().join("providers.json")).unwrap();
        assert_eq!(reg.active_provider(), None);
    }

    #[test]
    fn save_validates_like_static_registry() {
        let (_dir, reg) = test_registry();
        // Unknown provider.
        let err = reg
            .save_config(ProviderConfig {
                provider_id: "nope".to_string(),
                api_key: "k".to_string(),
                model: "m".to_string(),
                base_url: None,
            })
            .unwrap_err();
        assert!(matches!(err, ProviderConfigError::UnknownProvider(_)));
        // Missing key where required.
        let err = reg
            .save_config(ProviderConfig {
                provider_id: "openai".to_string(),
                api_key: "   ".to_string(),
                model: "gpt-4o".to_string(),
                base_url: None,
            })
            .unwrap_err();
        assert!(matches!(err, ProviderConfigError::Invalid(_)));
        // No key needed for ollama → saves fine.
        reg.save_config(ProviderConfig {
            provider_id: "ollama".to_string(),
            api_key: String::new(),
            model: String::new(),
            base_url: None,
        })
        .expect("ollama save");
        assert!(!reg.has_key("ollama")); // empty key → has_key false
        assert_eq!(reg.active_provider().as_deref(), Some("ollama"));
    }

    #[test]
    fn save_empty_key_keeps_stored_key() {
        let (_dir, reg) = test_registry();
        // "custom" needs a key; save one first.
        reg.save_config(ProviderConfig {
            provider_id: "custom".to_string(),
            api_key: "sk-original".to_string(),
            model: "my-model".to_string(),
            base_url: Some("http://localhost:9999/v1".to_string()),
        })
        .expect("initial save");
        // Re-save with an empty key — the UI's "keep existing" sentinel —
        // plus a changed model and base_url.
        reg.save_config(ProviderConfig {
            provider_id: "custom".to_string(),
            api_key: String::new(),
            model: "other-model".to_string(),
            base_url: Some("http://localhost:9998/v1".to_string()),
        })
        .expect("sentinel save keeps the old key");
        let raw = fs::read_to_string(reg.path()).unwrap();
        let v: Value = serde_json::from_str(&raw).unwrap();
        let custom = &v["providers"]["custom"];
        assert_eq!(custom["api_key"], Value::String("sk-original".to_string()));
        assert_eq!(
            custom["model"],
            Value::String("other-model".to_string())
        );
        assert_eq!(
            custom["base_url"],
            Value::String("http://localhost:9998/v1".to_string())
        );
        assert!(reg.has_key("custom"));
    }

    #[test]
    fn save_empty_key_with_no_stored_key_still_rejects() {
        let (_dir, reg) = test_registry();
        // Nothing stored for "custom" → an empty key is genuinely missing.
        let err = reg
            .save_config(ProviderConfig {
                provider_id: "custom".to_string(),
                api_key: String::new(),
                model: "my-model".to_string(),
                base_url: None,
            })
            .unwrap_err();
        assert!(matches!(err, ProviderConfigError::Invalid(_)));
        assert!(!reg.has_key("custom"));
    }

    #[cfg(unix)]
    #[test]
    fn config_file_is_owner_only() {
        use std::os::unix::fs::PermissionsExt;
        let (_dir, reg) = test_registry();
        save_openai(&reg, None);
        let mode = fs::metadata(reg.path())
            .expect("metadata")
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode, 0o600, "providers.json must be 0600");
    }

    #[tokio::test]
    async fn test_connection_rejects_unknown_provider() {
        let (_dir, reg) = test_registry();
        let err = reg.test_connection("nope").await.unwrap_err();
        assert!(err.contains("unknown provider"));
    }

    #[tokio::test]
    async fn test_connection_needs_saved_config() {
        let (_dir, reg) = test_registry();
        let (ok, msg) = reg.test_connection("openai").await.unwrap();
        assert!(!ok);
        assert_eq!(msg, "no configuration saved for this provider yet");
    }

    #[tokio::test]
    async fn test_connection_requires_key() {
        let (dir, reg) = test_registry();
        // Hand-write a keyless config (save_config would reject it).
        fs::write(
            dir.path().join("providers.json"),
            r#"{"active": "openai", "providers": {"openai": {"api_key": "", "model": "gpt-4o", "base_url": "https://api.openai.com/v1"}}}"#,
        )
        .unwrap();
        let (ok, msg) = reg.test_connection("openai").await.unwrap();
        assert!(!ok);
        assert_eq!(msg, "API key is required but not set");
    }

    // -- mock HTTP server for probe tests --------------------------------

    /// Serve one canned JSON response; returns the base URL and the raw
    /// request text the probe sent.
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
            let mut buf = vec![0u8; 8192];
            let mut req = Vec::new();
            loop {
                let n = sock.read(&mut buf).await.expect("read");
                if n == 0 {
                    break;
                }
                req.extend_from_slice(&buf[..n]);
                if req.windows(4).any(|w| w == b"\r\n\r\n") {
                    break;
                }
            }
            let request_text = String::from_utf8_lossy(&req).into_owned();
            let reason = match status {
                200 => "OK",
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

    /// Bind a port and immediately drop it, so connecting is refused.
    async fn closed_port_url() -> String {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let addr = listener.local_addr().expect("local addr");
        drop(listener);
        format!("http://{addr}")
    }

    #[tokio::test]
    async fn probe_openai_models_ok() {
        let (base, handle) = mock_server(200, r#"{"data":[]}"#).await;
        let (_dir, reg) = test_registry();
        save_openai(&reg, Some(&base));
        let (ok, msg) = reg.test_connection("openai").await.unwrap();
        assert!(ok, "msg: {msg}");
        assert_eq!(msg, "connected");
        let req = handle.await.expect("server task");
        assert!(req.starts_with("GET /models "), "request: {req}");
        // reqwest emits the AUTHORIZATION constant lowercase; check
        // case-insensitively.
        assert!(
            req.to_lowercase().contains("authorization: bearer sk-test-key"),
            "request: {req}"
        );
    }

    #[tokio::test]
    async fn probe_openai_models_unauthorized() {
        let (base, _handle) = mock_server(401, r#"{"error":"bad key"}"#).await;
        let (_dir, reg) = test_registry();
        save_openai(&reg, Some(&base));
        let (ok, msg) = reg.test_connection("openai").await.unwrap();
        assert!(!ok);
        assert!(msg.contains("authentication failed (HTTP 401)"), "msg: {msg}");
        assert!(msg.contains("check the API key"), "msg: {msg}");
    }

    #[tokio::test]
    async fn probe_anthropic_sends_messages_ping() {
        let (base, handle) = mock_server(200, r#"{"content":[]}"#).await;
        let (_dir, reg) = test_registry();
        reg.save_config(ProviderConfig {
            provider_id: "anthropic".to_string(),
            api_key: "sk-ant-test".to_string(),
            model: "claude-haiku-4-5".to_string(),
            base_url: Some(base),
        })
        .expect("save");
        let (ok, msg) = reg.test_connection("anthropic").await.unwrap();
        assert!(ok, "msg: {msg}");
        let req = handle.await.expect("server task");
        assert!(req.starts_with("POST /v1/messages "), "request: {req}");
        assert!(req.contains("x-api-key: sk-ant-test"), "request: {req}");
        assert!(req.contains("anthropic-version: 2023-06-01"), "request: {req}");
        assert!(req.contains(r#""max_tokens":1"#), "request: {req}");
    }

    #[tokio::test]
    async fn probe_gemini_lists_models_with_key_param() {
        let (base, handle) = mock_server(200, r#"{"models":[]}"#).await;
        let (_dir, reg) = test_registry();
        reg.save_config(ProviderConfig {
            provider_id: "google".to_string(),
            api_key: "gemini-key".to_string(),
            model: "gemini-2.5-flash".to_string(),
            base_url: Some(base),
        })
        .expect("save");
        let (ok, msg) = reg.test_connection("google").await.unwrap();
        assert!(ok, "msg: {msg}");
        let req = handle.await.expect("server task");
        assert!(
            req.starts_with("GET /v1beta/models?key=gemini-key "),
            "request: {req}"
        );
    }

    #[tokio::test]
    async fn probe_connection_refused_is_not_ok() {
        let base = closed_port_url().await;
        let (_dir, reg) = test_registry();
        save_openai(&reg, Some(&base));
        let (ok, msg) = reg.test_connection("openai").await.unwrap();
        assert!(!ok);
        assert!(!msg.is_empty(), "message must be present");
        // The key must never leak into the message.
        assert!(!msg.contains("sk-test-key"), "msg: {msg}");
    }

    #[test]
    fn percent_encode_leaves_safe_chars() {
        assert_eq!(percent_encode("abcXYZ019-_ .~"), "abcXYZ019-_%20.~");
        assert_eq!(percent_encode("a+b=c"), "a%2Bb%3Dc");
    }

    #[test]
    fn boom_home_env_selects_base_dir() {
        // default_base_dir honours BOOM_HOME (unit-level check only; the
        // registry constructor itself never touches the filesystem).
        let dir = tempfile::tempdir().unwrap();
        std::env::set_var("BOOM_HOME", dir.path());
        let resolved = default_base_dir();
        std::env::remove_var("BOOM_HOME");
        assert_eq!(resolved, dir.path());
    }
}
