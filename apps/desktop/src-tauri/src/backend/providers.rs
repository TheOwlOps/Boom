//! Provider configuration logic for the Boom Rust backend.
//!
//! Provider credentials and model preferences are persisted in
//! `~/.boom/providers.json`, using the same file layout as the legacy Python
//! backend:
//!
//! ```json
//! {
//!   "openai": { "api_key": "...", "model": "gpt-4o", "base_url": "https://api.openai.com/v1" },
//!   "active": "openai"
//! }
//! ```
//!
//! The API key is stored on disk but is never returned by any read path:
//! [`ProviderInfo`] carries `has_key` (a boolean) instead of the key itself,
//! and [`ConfigResponse`] echoes only a success flag.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};

/// Metadata for one built-in provider: its config-file id, display name and
/// default base URL.
#[derive(Debug, Clone, Copy)]
pub struct ProviderMeta {
    /// Config-file / API identifier, e.g. `"openai"`.
    pub id: &'static str,
    /// Human-readable display name, e.g. `"OpenAI"`.
    pub name: &'static str,
    /// Default base URL used when the user has not configured one.
    /// Empty for `custom`, which has no sensible default.
    pub default_base_url: &'static str,
}

/// The built-in providers reported when no config file exists yet.
pub const PROVIDER_METADATA: [ProviderMeta; 7] = [
    ProviderMeta {
        id: "openai",
        name: "OpenAI",
        default_base_url: "https://api.openai.com/v1",
    },
    ProviderMeta {
        id: "anthropic",
        name: "Anthropic",
        default_base_url: "https://api.anthropic.com",
    },
    ProviderMeta {
        id: "google",
        name: "Google",
        default_base_url: "https://generativelanguage.googleapis.com/v1beta/openai",
    },
    ProviderMeta {
        id: "xai",
        name: "xAI",
        default_base_url: "https://api.x.ai/v1",
    },
    ProviderMeta {
        id: "deepseek",
        name: "DeepSeek",
        default_base_url: "https://api.deepseek.com/v1",
    },
    ProviderMeta {
        id: "ollama",
        name: "Ollama",
        default_base_url: "http://localhost:11434/v1",
    },
    ProviderMeta {
        id: "custom",
        name: "Custom",
        default_base_url: "",
    },
];

/// Per-provider credentials persisted in `providers.json`.
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct ProviderEntry {
    /// API key; stored on disk, never returned by any read endpoint.
    #[serde(default)]
    pub api_key: String,
    #[serde(default)]
    pub model: String,
    #[serde(default)]
    pub base_url: String,
}

/// The whole `providers.json` document: per-provider entries plus the active id.
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct ProvidersFile {
    #[serde(default)]
    pub active: Option<String>,
    #[serde(flatten)]
    pub entries: HashMap<String, ProviderEntry>,
}

/// One provider as returned by `GET /api/providers`.
#[derive(Debug, Clone, Serialize)]
pub struct ProviderInfo {
    pub id: String,
    pub name: String,
    /// True when a non-blank API key is stored; the key itself is never exposed.
    pub has_key: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub base_url: Option<String>,
}

impl ProviderInfo {
    /// Builds the public view of a built-in provider, overlaying any stored
    /// entry on top of the built-in metadata defaults.
    fn from_meta(meta: &ProviderMeta, entry: Option<&ProviderEntry>) -> Self {
        let (model, base_url, has_key) = match entry {
            Some(e) => (
                none_if_empty(&e.model),
                none_if_empty(&e.base_url).or_else(|| none_if_empty(meta.default_base_url)),
                !e.api_key.trim().is_empty(),
            ),
            None => (None, none_if_empty(meta.default_base_url), false),
        };
        ProviderInfo {
            id: meta.id.to_string(),
            name: meta.name.to_string(),
            has_key,
            model,
            base_url,
        }
    }
}

/// Body accepted by `POST /api/providers/config`.
#[derive(Debug, Deserialize)]
pub struct ProviderConfigRequest {
    /// Provider id, e.g. `"openai"`. Required.
    pub provider: String,
    /// New API key. When omitted, the stored key is left untouched.
    #[serde(default)]
    pub api_key: Option<String>,
    /// Model name, e.g. `"gpt-4o"`. When omitted, the stored value is kept.
    #[serde(default)]
    pub model: Option<String>,
    /// Base URL override. When omitted, the stored value is kept.
    #[serde(default)]
    pub base_url: Option<String>,
}

/// Body returned by `POST /api/providers/config`.
///
/// Deliberately contains no `api_key` field: the key must never be echoed
/// back in a response.
#[derive(Debug, Clone, Serialize)]
pub struct ConfigResponse {
    pub ok: bool,
    /// True when a non-blank API key is now stored for the provider.
    pub has_key: bool,
}

/// Body returned by `GET /api/providers`.
#[derive(Debug, Clone, Serialize)]
pub struct ProvidersResponse {
    pub providers: Vec<ProviderInfo>,
    pub active: String,
}

/// Errors that can occur while reading or writing provider configuration.
#[derive(Debug)]
pub enum ConfigError {
    /// The home directory could not be determined (`HOME` is unset).
    HomeNotSet,
    /// The request is invalid, e.g. an empty provider id.
    InvalidProvider(String),
    /// Filesystem failure while reading or writing the config file.
    Io(std::io::Error),
    /// The config file exists but is not valid JSON.
    Json(serde_json::Error),
}

impl ConfigError {
    /// Maps the error to the HTTP status the caller should return.
    pub fn status_code(&self) -> axum::http::StatusCode {
        match self {
            ConfigError::InvalidProvider(_) => axum::http::StatusCode::BAD_REQUEST,
            ConfigError::HomeNotSet | ConfigError::Io(_) | ConfigError::Json(_) => {
                axum::http::StatusCode::INTERNAL_SERVER_ERROR
            }
        }
    }
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ConfigError::HomeNotSet => write!(f, "could not determine home directory"),
            ConfigError::InvalidProvider(id) => {
                write!(f, "invalid provider id: {:?}", id)
            }
            ConfigError::Io(e) => write!(f, "config file I/O error: {e}"),
            ConfigError::Json(e) => write!(f, "config file is not valid JSON: {e}"),
        }
    }
}

impl std::error::Error for ConfigError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            ConfigError::Io(e) => Some(e),
            ConfigError::Json(e) => Some(e),
            _ => None,
        }
    }
}

/// Returns `None` for blank strings so unset values are omitted from API
/// responses instead of serialising as `""`.
fn none_if_empty(s: &str) -> Option<String> {
    let trimmed = s.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

/// Restricts the config file to owner-only access on Unix (`chmod 0600`),
/// matching the Python backend's behaviour for a file holding API keys.
#[cfg(unix)]
fn restrict_permissions(path: &Path) -> Result<(), ConfigError> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).map_err(ConfigError::Io)
}

/// No permission hardening available outside Unix; writing still succeeds.
#[cfg(not(unix))]
fn restrict_permissions(_path: &Path) -> Result<(), ConfigError> {
    Ok(())
}

/// Reads and writes provider configuration at one `providers.json` path.
///
/// The store borrows no global state: callers inject the path, which keeps
/// unit tests hermetic (pointed at a temp dir) while production passes the
/// real `~/.boom/providers.json` via [`ProviderStore::default_path`].
#[derive(Debug, Clone)]
pub struct ProviderStore {
    config_path: PathBuf,
}

impl ProviderStore {
    /// Creates a store for an explicit config file path.
    pub fn new(config_path: PathBuf) -> Self {
        ProviderStore { config_path }
    }

    /// Resolves the production config path, `~/.boom/providers.json`.
    pub fn default_path() -> Result<PathBuf, ConfigError> {
        let home = std::env::var("HOME").map_err(|_| ConfigError::HomeNotSet)?;
        Ok(PathBuf::from(home).join(".boom").join("providers.json"))
    }

    /// Returns the path this store reads from and writes to.
    pub fn config_path(&self) -> &Path {
        &self.config_path
    }

    /// Reads the config file, tolerating its absence (treated as "no
    /// providers configured yet"). A present-but-corrupt file is an error.
    fn read_file(&self) -> Result<ProvidersFile, ConfigError> {
        match fs::read_to_string(&self.config_path) {
            Ok(text) => serde_json::from_str(&text).map_err(ConfigError::Json),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(ProvidersFile::default()),
            Err(e) => Err(ConfigError::Io(e)),
        }
    }

    /// Lists all known providers with `has_key` flags for `GET /api/providers`.
    ///
    /// Built-in providers come first in a fixed order; any extra providers
    /// found in the config file follow in sorted order.
    pub fn list(&self) -> Result<ProvidersResponse, ConfigError> {
        let file = self.read_file()?;
        let mut providers = Vec::with_capacity(PROVIDER_METADATA.len() + file.entries.len());

        for meta in PROVIDER_METADATA.iter() {
            providers.push(ProviderInfo::from_meta(meta, file.entries.get(meta.id)));
        }

        // Extra providers the user added directly to the config file.
        let mut extras: Vec<&String> = file
            .entries
            .keys()
            .filter(|id| !PROVIDER_METADATA.iter().any(|m| m.id == id.as_str()))
            .collect();
        extras.sort();
        for id in extras {
            if let Some(entry) = file.entries.get(id) {
                providers.push(ProviderInfo {
                    id: id.clone(),
                    name: id.clone(),
                    has_key: !entry.api_key.trim().is_empty(),
                    model: none_if_empty(&entry.model),
                    base_url: none_if_empty(&entry.base_url),
                });
            }
        }

        let active = file
            .active
            .as_deref()
            .and_then(none_if_empty)
            .unwrap_or_else(|| "openai".to_string());

        Ok(ProvidersResponse { providers, active })
    }

    /// Stores a provider's credentials for `POST /api/providers/config`.
    ///
    /// Fields are merged, not replaced: omitted fields keep their stored
    /// value, so a request carrying only `api_key` does not wipe the model.
    /// The config directory is created on demand and the file is restricted
    /// to owner-only access on Unix.
    pub fn set_config(&self, req: ProviderConfigRequest) -> Result<ConfigResponse, ConfigError> {
        let provider = req.provider.trim();
        if provider.is_empty() {
            return Err(ConfigError::InvalidProvider(req.provider));
        }

        let mut file = self.read_file()?;
        let entry = file.entries.entry(provider.to_string()).or_default();
        if let Some(api_key) = req.api_key {
            entry.api_key = api_key.trim().to_string();
        }
        if let Some(model) = req.model {
            entry.model = model.trim().to_string();
        }
        if let Some(base_url) = req.base_url {
            entry.base_url = base_url.trim().to_string();
        }
        let has_key = !entry.api_key.trim().is_empty();

        if let Some(parent) = self.config_path.parent() {
            fs::create_dir_all(parent).map_err(ConfigError::Io)?;
        }
        let serialized = serde_json::to_string_pretty(&file).map_err(ConfigError::Json)?;
        fs::write(&self.config_path, serialized).map_err(ConfigError::Io)?;
        restrict_permissions(&self.config_path)?;

        Ok(ConfigResponse { ok: true, has_key })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static COUNTER: AtomicU64 = AtomicU64::new(0);

    /// A store pointed at a fresh, unique temp directory per test.
    fn temp_store(test_name: &str) -> (ProviderStore, PathBuf) {
        let n = COUNTER.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!(
            "boom-backend-providers-test-{}-{}-{}",
            test_name,
            std::process::id(),
            n
        ));
        let path = dir.join("providers.json");
        (ProviderStore::new(path), dir)
    }

    #[test]
    fn missing_file_returns_seven_defaults() {
        let (store, _dir) = temp_store("defaults");
        let res = store.list().expect("listing defaults must succeed");

        assert_eq!(res.providers.len(), 7);
        let ids: Vec<&str> = res.providers.iter().map(|p| p.id.as_str()).collect();
        assert_eq!(
            ids,
            vec!["openai", "anthropic", "google", "xai", "deepseek", "ollama", "custom"]
        );
        assert!(res.providers.iter().all(|p| !p.has_key));
        assert_eq!(res.active, "openai");
        assert_eq!(res.providers[0].name, "OpenAI");
        // Built-in defaults surface even without a config file.
        assert_eq!(res.providers[0].base_url.as_deref(), Some("https://api.openai.com/v1"));
        // `custom` has no default base URL.
        assert!(res.providers[6].base_url.is_none());
    }

    #[test]
    fn set_config_round_trip_and_has_key() {
        let (store, _dir) = temp_store("roundtrip");

        let req = ProviderConfigRequest {
            provider: "openai".to_string(),
            api_key: Some("sk-test-123".to_string()),
            model: Some("gpt-4o".to_string()),
            base_url: Some("https://api.openai.com/v1".to_string()),
        };
        let res = store.set_config(req).expect("set_config must succeed");
        assert!(res.ok);
        assert!(res.has_key);

        let listed = store.list().expect("list must succeed");
        let openai = listed.providers.iter().find(|p| p.id == "openai").expect("openai listed");
        assert!(openai.has_key);
        assert_eq!(openai.model.as_deref(), Some("gpt-4o"));
    }

    #[test]
    fn set_config_merges_fields() {
        let (store, _dir) = temp_store("merge");

        store
            .set_config(ProviderConfigRequest {
                provider: "deepseek".to_string(),
                api_key: Some("sk-ds".to_string()),
                model: Some("deepseek-chat".to_string()),
                base_url: None,
            })
            .unwrap();
        // Second call only rotates the key; the model must survive.
        let res = store
            .set_config(ProviderConfigRequest {
                provider: "deepseek".to_string(),
                api_key: Some("sk-ds-2".to_string()),
                model: None,
                base_url: None,
            })
            .unwrap();
        assert!(res.has_key);

        let listed = store.list().unwrap();
        let ds = listed.providers.iter().find(|p| p.id == "deepseek").unwrap();
        assert_eq!(ds.model.as_deref(), Some("deepseek-chat"));
    }

    #[test]
    fn response_never_contains_the_key() {
        let res = ConfigResponse { ok: true, has_key: true };
        let json = serde_json::to_string(&res).unwrap();
        assert!(!json.contains("api_key"), "key field must not be serialised: {json}");

        // Even a full ProvidersFile serialisation path (list()) must not leak.
        let (store, _dir) = temp_store("noleak");
        store
            .set_config(ProviderConfigRequest {
                provider: "openai".to_string(),
                api_key: Some("sk-super-secret".to_string()),
                model: None,
                base_url: None,
            })
            .unwrap();
        let listed_json = serde_json::to_string(&store.list().unwrap()).unwrap();
        assert!(!listed_json.contains("sk-super-secret"), "key leaked in list(): {listed_json}");
    }

    #[test]
    fn empty_provider_id_is_rejected() {
        let (store, _dir) = temp_store("invalid");
        let err = store
            .set_config(ProviderConfigRequest {
                provider: "   ".to_string(),
                api_key: Some("x".to_string()),
                model: None,
                base_url: None,
            })
            .expect_err("empty provider id must fail");
        assert_eq!(err.status_code(), axum::http::StatusCode::BAD_REQUEST);
    }

    #[test]
    fn unknown_provider_ids_appear_as_extras() {
        let (store, _dir) = temp_store("extras");
        store
            .set_config(ProviderConfigRequest {
                provider: "myproxy".to_string(),
                api_key: Some("k".to_string()),
                model: None,
                base_url: Some("https://proxy.example/v1".to_string()),
            })
            .unwrap();

        let listed = store.list().unwrap();
        assert_eq!(listed.providers.len(), 8);
        let extra = listed.providers.last().unwrap();
        assert_eq!(extra.id, "myproxy");
        assert!(extra.has_key);
        assert_eq!(extra.base_url.as_deref(), Some("https://proxy.example/v1"));
    }

    #[test]
    fn corrupt_file_is_an_error() {
        let (store, dir) = temp_store("corrupt");
        fs::create_dir_all(&dir).unwrap();
        fs::write(store.config_path(), "{ not valid json").unwrap();
        let err = store.list().expect_err("corrupt file must fail");
        assert!(matches!(err, ConfigError::Json(_)));
    }

    #[test]
    fn active_is_read_from_file() {
        let (store, dir) = temp_store("active");
        fs::create_dir_all(&dir).unwrap();
        fs::write(
            store.config_path(),
            r#"{"active": "deepseek", "openai": {"api_key": "k", "model": "gpt-4o", "base_url": ""}}"#,
        )
        .unwrap();

        let listed = store.list().unwrap();
        assert_eq!(listed.active, "deepseek");
        let openai = listed.providers.iter().find(|p| p.id == "openai").unwrap();
        assert!(openai.has_key);
    }

}
