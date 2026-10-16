//! Default (no-op / in-memory) implementations of the [`super::state`] traits.
//!
//! These let the server boot and serve honest responses without any backend
//! wired: nothing is invented — unavailable pieces degrade exactly the way
//! `gateway/api.py` degrades (503 for chat, `running: false` for Ollama,
//! adapter-unavailable for Zalo, empty lists where Python answers empty).
//!
//! Sibling workers replace these with real implementations via [`AppState`].

use std::{
    collections::{BTreeSet, HashMap, HashSet, VecDeque},
    path::PathBuf,
    sync::{Arc, Mutex},
    time::Instant,
};

use serde_json::{Map, Value};

use super::state::*;

// ---------------------------------------------------------------------------
// Harness: not configured
// ---------------------------------------------------------------------------

/// Stand-in harness used until a real brain is injected.
/// `run_turn` always fails with [`TurnError::NotConfigured`], so `/api/chat`
/// answers 503 `"agent brain not configured"` — mirroring Python's
/// `brain is None` branch.
pub struct NoHarness;

impl Harness for NoHarness {
    fn is_configured(&self) -> bool {
        false
    }

    fn run_turn(&self, _req: TurnRequest) -> BoxFuture<'_, Result<TurnResult, TurnError>> {
        Box::pin(async { Err(TurnError::NotConfigured) })
    }
}

// ---------------------------------------------------------------------------
// ToolRegistry: empty
// ---------------------------------------------------------------------------

/// Empty tool registry. `/api/tools` answers `{"tools": [], "count": 0}`,
/// mirroring Python's try/except fallback when the environment is missing.
pub struct EmptyToolRegistry;

impl ToolRegistry for EmptyToolRegistry {
    fn list_tools(&self) -> Vec<ToolInfo> {
        Vec::new()
    }

    fn run_tool(&self, name: &str, _args: &Map<String, Value>) -> BoxFuture<'_, Result<String, String>> {
        let name = name.to_string();
        Box::pin(async move { Err(format!("tool not found: {name}")) })
    }
}

// ---------------------------------------------------------------------------
// ProviderRegistry: static metadata, in-memory config
// ---------------------------------------------------------------------------

/// Static copy of Python's `PROVIDER_DEFS` (metadata only, no secrets).
fn provider_defs() -> Vec<ProviderMeta> {
    let def = |id: &str,
               name: &str,
               models: &[&str],
               needs_key: bool,
               default_base_url: &str,
               show_base_url: bool| {
        ProviderMeta {
            id: id.to_string(),
            name: name.to_string(),
            models: models.iter().map(|s| s.to_string()).collect(),
            needs_key,
            default_base_url: default_base_url.to_string(),
            show_base_url,
            allow_custom_model: true,
        }
    };
    vec![
        def("openai", "OpenAI",
            &["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o4-mini"],
            true, "https://api.openai.com/v1", false),
        def("anthropic", "Anthropic",
            &["claude-sonnet-4-5", "claude-opus-4-5", "claude-haiku-4-5"],
            true, "https://api.anthropic.com", false),
        def("google", "Google (Gemini)",
            &["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
            true, "https://generativelanguage.googleapis.com", false),
        def("xai", "xAI (Grok)",
            &["grok-4", "grok-3", "grok-3-mini"],
            true, "https://api.x.ai/v1", false),
        def("deepseek", "DeepSeek",
            &["deepseek-chat", "deepseek-reasoner"],
            true, "https://api.deepseek.com/v1", false),
        def("ollama", "Ollama (local)",
            &["llama3.3", "qwen3", "mistral-small3.1", "gemma3"],
            false, "http://localhost:11434/v1", true),
        def("custom", "Custom (OpenAI-compatible)",
            &[], true, "", true),
    ]
}

/// Provider registry with static metadata and an in-memory config store.
/// API keys are kept in memory only and never serialized into responses.
pub struct StaticProviderRegistry {
    /// provider_id -> (api_key, model, base_url)
    configs: Mutex<HashMap<String, (String, String, String)>>,
    active: Mutex<Option<String>>,
}

impl StaticProviderRegistry {
    pub fn new() -> Self {
        Self { configs: Mutex::new(HashMap::new()), active: Mutex::new(None) }
    }
}

impl Default for StaticProviderRegistry {
    fn default() -> Self {
        Self::new()
    }
}

impl ProviderRegistry for StaticProviderRegistry {
    fn list_providers(&self) -> Vec<ProviderMeta> {
        provider_defs()
    }

    fn has_key(&self, id: &str) -> bool {
        self.configs
            .lock()
            .ok()
            .and_then(|c| c.get(id).map(|(k, _, _)| !k.trim().is_empty()))
            .unwrap_or(false)
    }

    fn active_provider(&self) -> Option<String> {
        self.active.lock().ok().and_then(|a| a.clone())
    }

    fn save_config(&self, cfg: ProviderConfig) -> Result<(), ProviderConfigError> {
        let defn = provider_defs()
            .into_iter()
            .find(|d| d.id == cfg.provider_id)
            .ok_or_else(|| ProviderConfigError::UnknownProvider(cfg.provider_id.clone()))?;
        let api_key = cfg.api_key.trim().to_string();
        let mut model = cfg.model.trim().to_string();
        let base_url = cfg.base_url.as_deref().unwrap_or("").trim().to_string();
        if defn.needs_key && api_key.is_empty() {
            return Err(ProviderConfigError::Invalid(format!(
                "{} requires an API key",
                defn.name
            )));
        }
        if model.is_empty() {
            model = defn.models.first().cloned().unwrap_or_default();
        }
        let base_url = if base_url.is_empty() { defn.default_base_url } else { base_url };
        if let Ok(mut configs) = self.configs.lock() {
            configs.insert(cfg.provider_id, (api_key, model, base_url));
        }
        Ok(())
    }

    fn test_connection(&self, id: &str) -> BoxFuture<'_, Result<(bool, String), String>> {
        let id = id.to_string();
        Box::pin(async move {
            // Honest default: without provider wiring we cannot probe.
            // (The real worker replaces this with per-provider HTTP probes.)
            if provider_defs().iter().any(|d| d.id == id) {
                Ok((false, "provider test not configured in this build".to_string()))
            } else {
                Err(format!("unknown provider: {id}"))
            }
        })
    }

    fn ollama_status(&self) -> BoxFuture<'_, OllamaStatus> {
        Box::pin(async move { probe_ollama().await })
    }
}

/// Probe the local Ollama daemon (`GET http://localhost:11434/api/tags`,
/// 3s timeout). Never fails: any error degrades to
/// `{running: false, models: []}` — mirroring `_check_ollama_status`.
async fn probe_ollama() -> OllamaStatus {
    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(3))
        .build()
    {
        Ok(c) => c,
        Err(_) => return OllamaStatus { running: false, models: vec![] },
    };
    let resp = match client.get("http://localhost:11434/api/tags").send().await {
        Ok(r) => r,
        Err(_) => return OllamaStatus { running: false, models: vec![] },
    };
    if !resp.status().is_success() {
        return OllamaStatus { running: false, models: vec![] };
    }
    let data: Value = match resp.json().await {
        Ok(v) => v,
        Err(_) => return OllamaStatus { running: false, models: vec![] },
    };
    let mut models = Vec::new();
    if let Some(tags) = data.get("models").and_then(|m| m.as_array()) {
        for entry in tags {
            if let Some(name) = entry.get("name").and_then(|n| n.as_str()) {
                // "llama3.1:latest" -> "llama3.1"; keep non-latest tags.
                let (base, tag) = match name.split_once(':') {
                    Some((b, t)) => (b, t),
                    None => (name, ""),
                };
                if tag.is_empty() || tag == "latest" {
                    models.push(base.to_string());
                } else {
                    models.push(format!("{base}:{tag}"));
                }
            }
        }
    }
    OllamaStatus { running: true, models }
}

// ---------------------------------------------------------------------------
// Storage: in-memory bots / groups / marketplace state
// ---------------------------------------------------------------------------

#[derive(Default)]
struct MemoryInner {
    bots: HashMap<String, Bot>,
    groups: HashMap<(String, String), Group>,
    plugins: HashMap<String, PluginMeta>,
    disabled_tools: BTreeSet<String>,
    next_bot: u64,
}

/// In-memory storage. Groups start empty (Python only registers groups via
/// the message pipeline, never via the API — empty is the honest state).
pub struct MemoryStorage {
    inner: Mutex<MemoryInner>,
}

impl MemoryStorage {
    pub fn new() -> Self {
        Self { inner: Mutex::new(MemoryInner::default()) }
    }

    /// Test helper: register a group directly.
    pub fn insert_group(&self, group: Group) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.groups.insert((group.channel.clone(), group.group_id.clone()), group);
        }
    }

    /// Test helper: register an installed plugin directly.
    pub fn insert_plugin(&self, plugin: PluginMeta) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.plugins.insert(plugin.name.clone(), plugin);
        }
    }
}

impl Default for MemoryStorage {
    fn default() -> Self {
        Self::new()
    }
}

impl Storage for MemoryStorage {
    fn list_bots(&self) -> Vec<Bot> {
        let inner = match self.inner.lock() {
            Ok(g) => g,
            Err(_) => return Vec::new(),
        };
        let mut bots: Vec<Bot> = inner.bots.values().cloned().collect();
        // Active bot first, then by name — mirrors Python's ORDER BY.
        bots.sort_by(|a, b| {
            b.is_active
                .cmp(&a.is_active)
                .then_with(|| a.name.cmp(&b.name))
                .then_with(|| a.id.cmp(&b.id))
        });
        bots
    }

    fn create_bot(&self, input: NewBot) -> Result<Bot, StorageError> {
        if input.name.trim().is_empty() {
            return Err(StorageError::Invalid("name is required".to_string()));
        }
        let mut inner = self.inner.lock().map_err(|_| {
            StorageError::Internal("failed to create bot".to_string())
        })?;
        inner.next_bot += 1;
        let id = format!("bot_{}_{}", now_epoch(), inner.next_bot);
        let now = now_iso();
        let bot = Bot {
            id: id.clone(),
            name: input.name.trim().to_string(),
            description: input.description,
            system_prompt: input.system_prompt,
            avatar_emoji: if input.avatar_emoji.is_empty() { "🤖".to_string() } else { input.avatar_emoji },
            allowed_tools: input.allowed_tools.unwrap_or_default(),
            denied_tools: input.denied_tools.unwrap_or_default(),
            channel_bindings: input.channel_bindings.unwrap_or_default(),
            created_at: now.clone(),
            updated_at: now,
            is_active: false,
        };
        inner.bots.insert(id, bot.clone());
        Ok(bot)
    }

    fn get_bot(&self, id: &str) -> Option<Bot> {
        self.inner.lock().ok()?.bots.get(id).cloned()
    }

    fn update_bot(&self, id: &str, patch: &Map<String, Value>) -> Result<Bot, StorageError> {
        if id.trim().is_empty() {
            return Err(StorageError::Invalid("bot_id must be a non-empty string".to_string()));
        }
        let mut inner = self.inner.lock().map_err(|_| {
            StorageError::Internal("failed to update bot".to_string())
        })?;
        let bot = inner
            .bots
            .get_mut(id)
            .ok_or_else(|| StorageError::NotFound(format!("bot not found: '{id}'")))?;

        let mut touched = false;
        for (key, value) in patch {
            match key.as_str() {
                "name" => {
                    let name = value.as_str().ok_or_else(|| {
                        StorageError::Invalid("name must be a non-empty string".to_string())
                    })?;
                    if name.trim().is_empty() {
                        return Err(StorageError::Invalid(
                            "name must be a non-empty string".to_string(),
                        ));
                    }
                    bot.name = name.trim().to_string();
                    touched = true;
                }
                "description" | "system_prompt" => {
                    let s = value.as_str().ok_or_else(|| {
                        StorageError::Invalid(format!("{key} must be a string"))
                    })?;
                    if key == "description" {
                        bot.description = s.to_string();
                    } else {
                        bot.system_prompt = s.to_string();
                    }
                    touched = true;
                }
                "avatar_emoji" => {
                    let s = value.as_str().ok_or_else(|| {
                        StorageError::Invalid("avatar_emoji must be a string".to_string())
                    })?;
                    bot.avatar_emoji = if s.is_empty() { "🤖".to_string() } else { s.to_string() };
                    touched = true;
                }
                "allowed_tools" | "denied_tools" | "channel_bindings" => {
                    let list = normalize_str_list(value, key)
                        .map_err(StorageError::Invalid)?;
                    match key.as_str() {
                        "allowed_tools" => bot.allowed_tools = list,
                        "denied_tools" => bot.denied_tools = list,
                        _ => bot.channel_bindings = list,
                    }
                    touched = true;
                }
                // Unknown fields are silently ignored, like Python.
                _ => {}
            }
        }
        if !touched {
            return Err(StorageError::Invalid("no updatable fields given".to_string()));
        }
        bot.updated_at = now_iso();
        Ok(bot.clone())
    }

    fn delete_bot(&self, id: &str) -> bool {
        if id.trim().is_empty() {
            return false;
        }
        self.inner
            .lock()
            .map(|mut inner| inner.bots.remove(id).is_some())
            .unwrap_or(false)
    }

    fn set_active_bot(&self, id: &str) -> bool {
        let id = id.trim();
        if id.is_empty() {
            return false;
        }
        let mut inner = match self.inner.lock() {
            Ok(g) => g,
            Err(_) => return false,
        };
        if !inner.bots.contains_key(id) {
            return false;
        }
        for bot in inner.bots.values_mut() {
            bot.is_active = bot.id == id;
        }
        true
    }

    fn active_bot(&self) -> Option<Bot> {
        self.inner
            .lock()
            .ok()?
            .bots
            .values()
            .find(|b| b.is_active)
            .cloned()
    }

    fn list_groups(&self) -> Vec<Group> {
        let inner = match self.inner.lock() {
            Ok(g) => g,
            Err(_) => return Vec::new(),
        };
        let mut groups: Vec<Group> = inner.groups.values().cloned().collect();
        groups.sort_by(|a, b| {
            a.channel
                .cmp(&b.channel)
                .then_with(|| a.group_id.cmp(&b.group_id))
        });
        groups
    }

    fn get_group(&self, channel: &str, group_id: &str) -> Option<Group> {
        self.inner
            .lock()
            .ok()?
            .groups
            .get(&(channel.to_string(), group_id.to_string()))
            .cloned()
    }

    fn set_group_muted(&self, channel: &str, group_id: &str, muted: bool) -> Result<(), StorageError> {
        let mut inner = self.inner.lock().map_err(|_| {
            StorageError::Internal("failed to update mute state".to_string())
        })?;
        let group = inner
            .groups
            .get_mut(&(channel.to_string(), group_id.to_string()))
            .ok_or_else(|| {
                StorageError::NotFound(format!("group not registered: {channel}/{group_id}"))
            })?;
        let mut settings = group.settings.as_object().cloned().unwrap_or_default();
        settings.insert("muted".to_string(), Value::Bool(muted));
        group.settings = Value::Object(settings);
        Ok(())
    }

    fn set_group_policy(
        &self,
        channel: &str,
        group_id: &str,
        policy: &Value,
    ) -> Result<Value, StorageError> {
        // Must be JSON-serializable (it always is as serde_json::Value).
        serde_json::to_string(policy)
            .map_err(|e| StorageError::Invalid(format!("policy is not JSON-serializable: {e}")))?;
        let mut inner = self.inner.lock().map_err(|_| {
            StorageError::Internal("failed to save policy".to_string())
        })?;
        let group = inner
            .groups
            .get_mut(&(channel.to_string(), group_id.to_string()))
            .ok_or_else(|| {
                StorageError::NotFound(format!(
                    "group not registered: channel='{channel}' group_id='{group_id}'"
                ))
            })?;
        group.policy = policy.clone();
        Ok(policy.clone())
    }

    fn plugins(&self) -> Vec<PluginMeta> {
        self.inner
            .lock()
            .map(|inner| {
                let mut v: Vec<PluginMeta> = inner.plugins.values().cloned().collect();
                v.sort_by(|a, b| a.name.cmp(&b.name));
                v
            })
            .unwrap_or_default()
    }

    fn enable_plugin(&self, name: &str) -> Result<(), StorageError> {
        let mut inner = self.inner.lock().map_err(|_| {
            StorageError::Internal(format!("could not enable plugin '{name}'"))
        })?;
        let plugin = inner
            .plugins
            .get_mut(name)
            .ok_or_else(|| StorageError::NotFound(format!("plugin '{name}' not found")))?;
        plugin.enabled = true;
        Ok(())
    }

    fn uninstall_plugin(&self, name: &str) -> bool {
        self.inner
            .lock()
            .map(|mut inner| inner.plugins.remove(name).is_some())
            .unwrap_or(false)
    }

    fn disabled_tools(&self) -> BTreeSet<String> {
        self.inner
            .lock()
            .map(|inner| inner.disabled_tools.clone())
            .unwrap_or_default()
    }

    fn set_tool_disabled(&self, name: &str, disabled: bool) {
        if let Ok(mut inner) = self.inner.lock() {
            if disabled {
                inner.disabled_tools.insert(name.to_string());
            } else {
                inner.disabled_tools.remove(name);
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Updater: real GitHub check
// ---------------------------------------------------------------------------

/// Release-notes truncation, mirroring Python's `RELEASE_NOTES_LIMIT`.
const RELEASE_NOTES_LIMIT: usize = 2000;

/// Real update checker: `GET https://api.github.com/repos/TheOwlOps/Boom/releases/latest`
/// with a 10s timeout (Python: `version.check_github_update`). Any failure is
/// returned as `Err` so the route can answer 502 honestly — never fabricated.
pub struct GithubUpdater {
    pub repo: String,
    pub current_version: String,
}

impl GithubUpdater {
    pub fn new(current_version: impl Into<String>) -> Self {
        Self {
            repo: "TheOwlOps/Boom".to_string(),
            current_version: current_version.into(),
        }
    }
}

impl Updater for GithubUpdater {
    fn check(&self) -> BoxFuture<'_, Result<UpdateInfo, String>> {
        let repo = self.repo.clone();
        let current = self.current_version.clone();
        Box::pin(async move {
            let client = reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(10))
                .build()
                .map_err(|e| format!("update check failed: {e}"))?;
            let url = format!("https://api.github.com/repos/{repo}/releases/latest");
            let resp = client
                .get(&url)
                .header("Accept", "application/vnd.github+json")
                .header("User-Agent", "boom-updater")
                .send()
                .await
                .map_err(|e| format!("Network error contacting GitHub: {e}"))?;
            if !resp.status().is_success() {
                return Err(format!("GitHub API returned HTTP {}", resp.status()));
            }
            let payload: Value = resp
                .json()
                .await
                .map_err(|e| format!("Invalid JSON from GitHub API: {e}"))?;
            let tag = payload
                .get("tag_name")
                .and_then(|t| t.as_str())
                .ok_or_else(|| "GitHub API response missing tag_name".to_string())?;
            let latest = tag.strip_prefix('v').or_else(|| tag.strip_prefix('V')).unwrap_or(tag);
            let has_update = compare_versions(latest, &current) > 0;
            let notes = payload
                .get("body")
                .and_then(|b| b.as_str())
                .unwrap_or("");
            let mut notes = notes.to_string();
            if notes.len() > RELEASE_NOTES_LIMIT {
                notes.truncate(RELEASE_NOTES_LIMIT);
            }
            Ok(UpdateInfo {
                has_update,
                current_version: current,
                latest_version: latest.to_string(),
                download_url: payload
                    .get("html_url")
                    .and_then(|u| u.as_str())
                    .unwrap_or("")
                    .to_string(),
                release_notes: notes,
                published_at: payload
                    .get("published_at")
                    .and_then(|p| p.as_str())
                    .unwrap_or("")
                    .to_string(),
            })
        })
    }
}

/// Numeric dot-separated version comparison (Python: `version.compare_versions`).
fn compare_versions(a: &str, b: &str) -> i32 {
    let parse = |v: &str| {
        v.split('.')
            .map(|p| p.chars().take_while(|c| c.is_ascii_digit()).collect::<String>())
            .map(|p| p.parse::<u64>().unwrap_or(0))
            .collect::<Vec<_>>()
    };
    let (mut av, mut bv) = (parse(a), parse(b));
    let n = av.len().max(bv.len());
    av.resize(n, 0);
    bv.resize(n, 0);
    for (x, y) in av.iter().zip(bv.iter()) {
        if x != y {
            return if x > y { 1 } else { -1 };
        }
    }
    0
}

// ---------------------------------------------------------------------------
// Zalo-personal: unavailable stub
// ---------------------------------------------------------------------------

/// Zalo-personal is an optional channel: without the adapter the routes stay
/// honest — status reports disconnected, login answers 500
/// `"zalo-personal adapter is unavailable"`, logout is always safe (200).
/// Mirrors the Python handlers' adapter-missing branches.
pub struct NoZalo;

impl ZaloPersonal for NoZalo {
    fn status(&self) -> (bool, Option<String>) {
        (false, None)
    }

    fn logout(&self) -> Result<(), String> {
        Ok(())
    }

    fn login(&self, _phone: &str, _password: &str) -> BoxFuture<'_, Result<Option<String>, ZaloLoginError>> {
        Box::pin(async { Err(ZaloLoginError::Unavailable) })
    }
}

// ---------------------------------------------------------------------------
// AppState constructor
// ---------------------------------------------------------------------------

impl AppState {
    /// Build state with all default implementations. Sibling workers replace
    /// individual fields with their real backends.
    pub fn with_defaults() -> Self {
        Self::with_defaults_at_skills_dir(default_skills_dir())
    }

    pub fn with_defaults_at_skills_dir(skills_dir: Option<PathBuf>) -> Self {
        let version = env!("CARGO_PKG_VERSION").to_string();
        Self {
            harness: Arc::new(NoHarness),
            tools: Arc::new(EmptyToolRegistry),
            providers: Arc::new(StaticProviderRegistry::new()),
            storage: Arc::new(MemoryStorage::new()),
            updater: Arc::new(GithubUpdater::new(version.clone())),
            zalo: Arc::new(NoZalo),
            version,
            start: Instant::now(),
            skills_dir,
            log: Arc::new(Mutex::new(VecDeque::new())),
            turn: Arc::new(tokio::sync::Mutex::new(())),
            daemon_wired: false,
        }
    }
}

/// Best-effort skills dir: `<cwd>/skills`, mirroring Python's repo-root
/// `skills/` scan. Missing dir → 0 skills (honest empty).
fn default_skills_dir() -> Option<PathBuf> {
    let dir = std::env::current_dir().ok()?.join("skills");
    dir.is_dir().then_some(dir)
}

/// Marketplace tool-name set helper for [`split_marketplace_id`].
pub fn tool_name_set(tools: &[ToolInfo]) -> HashSet<String> {
    tools.iter().map(|t| t.name.clone()).collect()
}

/// Marketplace plugin map helper for [`split_marketplace_id`].
pub fn plugin_map(plugins: &[PluginMeta]) -> HashMap<String, PluginMeta> {
    plugins.iter().map(|p| (p.name.clone(), p.clone())).collect()
}
