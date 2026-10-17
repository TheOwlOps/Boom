//! `boom_proxy` — a transparent middleware layer between the app and the
//! LLM providers.
//!
//! Every LLM call flows `App → boom_proxy → Provider
//! (OpenAI/Anthropic/Gemini/...)`. The proxy is invisible to callers: it
//! implements [`crate::providers::LlmClient`] by delegating to an inner
//! client, adding:
//!
//! - **Logging** — every request's metadata (provider, model, message
//!   count, latency, outcome) in a bounded ring buffer. API keys and
//!   message bodies are never logged unless `log_bodies` is explicitly
//!   enabled in the config.
//! - **Retry** — failed calls are retried with exponential backoff
//!   (`max_retries`, default 3). Only retryable failures are retried
//!   (transport errors, timeouts, HTTP 429 and 5xx); client errors like
//!   400/401/403 are returned immediately.
//! - **Rate limiting** — a per-provider minimum interval between calls
//!   (`min_interval_ms`), configured in `$BOOM_HOME/proxy.json`.
//! - **Normalization** — [`transform::normalize_request`] fills in an empty
//!   model with the client's default and clamps out-of-range parameters.
//! - **Stats** — per-provider call counts, error counts and latencies,
//!   exposed via `GET /api/proxy/stats`; recent log entries via
//!   `GET /api/proxy/logs`.
//!
//! ## Wiring
//!
//! [`wrap`] takes any boxed [`crate::providers::LlmClient`] and returns a
//! boxed client with the proxy applied. The server calls it in
//! `client_from_active_config` (`server/defaults.rs`), so every harness
//! turn transparently goes through the proxy. The most recently wrapped
//! client registers its shared state in a process-global slot, which the
//! `/api/proxy/*` routes read — no `AppState` changes required.
//!
//! ## Config
//!
//! Optional `$BOOM_HOME/proxy.json` (falls back to `~/.boom/proxy.json`).
//! Missing file or invalid JSON → [`ProxyConfig::default`]. Example:
//!
//! ```json
//! {
//!   "enabled": true,
//!   "max_retries": 3,
//!   "base_backoff_ms": 500,
//!   "min_interval_ms": { "openai": 0, "anthropic": 1000 },
//!   "log_requests": true,
//!   "log_bodies": false
//! }
//! ```

pub mod middleware;
pub mod transform;

use std::{
    collections::{HashMap, VecDeque},
    sync::{Arc, LazyLock, RwLock},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use async_trait::async_trait;
use serde::{Deserialize, Serialize};

use crate::providers::{ChatRequest, ChatResponse, EventStream, LlmClient, ProviderError};

use middleware::{is_retryable, rate_wait, ProxyRateState};

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

/// Proxy behaviour, optionally loaded from `$BOOM_HOME/proxy.json`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProxyConfig {
    /// Master switch. When false, [`wrap`] returns the inner client
    /// unwrapped (no logging, retry, rate limiting or stats).
    #[serde(default = "default_true")]
    pub enabled: bool,
    /// Retries per call on retryable failures (default 3).
    #[serde(default = "default_retries")]
    pub max_retries: u32,
    /// Base backoff in ms; attempt `n` waits `base * 2^(n-1)` (default 500).
    #[serde(default = "default_backoff_ms")]
    pub base_backoff_ms: u64,
    /// Per-provider minimum interval between calls, in ms. Providers not
    /// listed (or mapped to 0) are unthrottled.
    #[serde(default)]
    pub min_interval_ms: HashMap<String, u64>,
    /// Record per-call metadata in the ring buffer (default true).
    #[serde(default = "default_true")]
    pub log_requests: bool,
    /// Also store the request message contents in log entries. Default
    /// false — message bodies may contain private data.
    #[serde(default)]
    pub log_bodies: bool,
}

fn default_true() -> bool {
    true
}
fn default_retries() -> u32 {
    3
}
fn default_backoff_ms() -> u64 {
    500
}

impl Default for ProxyConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            max_retries: default_retries(),
            base_backoff_ms: default_backoff_ms(),
            min_interval_ms: HashMap::new(),
            log_requests: true,
            log_bodies: false,
        }
    }
}

impl ProxyConfig {
    /// Load from `$BOOM_HOME/proxy.json` (`~/.boom/proxy.json` fallback).
    /// Any failure (missing file, invalid JSON) yields the default.
    pub fn load() -> Self {
        let path = proxy_config_path();
        std::fs::read_to_string(&path)
            .ok()
            .and_then(|raw| serde_json::from_str(&raw).ok())
            .unwrap_or_default()
    }

    /// Minimum interval for a provider id, or zero when unthrottled.
    pub fn interval_for(&self, provider_id: &str) -> Duration {
        Duration::from_millis(self.min_interval_ms.get(provider_id).copied().unwrap_or(0))
    }
}

/// Resolve `$BOOM_HOME/proxy.json`, falling back to `~/.boom/proxy.json`.
pub fn proxy_config_path() -> std::path::PathBuf {
    let base = std::env::var("BOOM_HOME")
        .ok()
        .map(std::path::PathBuf::from)
        .or_else(dirs::home_dir)
        .unwrap_or_else(|| std::path::PathBuf::from("."));
    // When BOOM_HOME is set we use it directly; otherwise it is the home
    // dir and we need the `.boom` subfolder.
    let dir = if std::env::var("BOOM_HOME").is_ok() {
        base
    } else {
        base.join(".boom")
    };
    dir.join("proxy.json")
}

// ---------------------------------------------------------------------------
// Stats + logs (shared state)
// ---------------------------------------------------------------------------

/// Aggregate counters for one provider.
#[derive(Debug, Clone, Default, Serialize)]
pub struct ProviderStats {
    pub calls: u64,
    pub errors: u64,
    #[serde(default)]
    pub total_latency_ms: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_error: Option<String>,
    #[serde(default)]
    pub last_call_ts: u64,
}

/// One recorded proxy call. Never contains API keys; message bodies only
/// when the config enables `log_bodies`.
#[derive(Debug, Clone, Serialize)]
pub struct LogEntry {
    pub ts: u64,
    pub provider: String,
    pub model: String,
    pub messages: usize,
    pub streaming: bool,
    pub latency_ms: u64,
    pub ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    pub attempts: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bodies: Option<Vec<String>>,
}

/// Bounded ring buffer + per-provider stats + rate-limiter clocks.
///
/// The aggregate parts (`inner`) are shared across all `wrap()` calls in
/// the process, so stats accumulate across turns even though the server
/// builds a fresh client per turn. The `config` is reloaded on every
/// `wrap()`, so `$BOOM_HOME/proxy.json` edits take effect without restart.
pub struct SharedState {
    pub config: ProxyConfig,
    inner: Arc<SharedInner>,
}

#[derive(Default)]
struct SharedInner {
    stats: std::sync::Mutex<HashMap<String, ProviderStats>>,
    logs: std::sync::Mutex<VecDeque<LogEntry>>,
    rate: std::sync::Mutex<ProxyRateState>,
}

const LOG_CAP: usize = 200;

/// Recover a poisoned [`std::sync::Mutex`] instead of cascading the panic.
///
/// A thread that panics while holding one of these locks must not take down
/// every later request. Stats/logs/rate state is observability data, so we
/// reclaim the (possibly partially updated) guard via
/// [`std::sync::PoisonError::into_inner`]. Behaviour is unchanged when the
/// lock is not poisoned.
fn mutex_recover<'a, T>(m: &'a std::sync::Mutex<T>) -> std::sync::MutexGuard<'a, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// Same recovery for the process-global [`RwLock`] registry (read side).
fn rw_read_recover<T>(m: &RwLock<T>) -> std::sync::RwLockReadGuard<'_, T> {
    m.read().unwrap_or_else(|e| e.into_inner())
}

/// Same recovery for the process-global [`RwLock`] registry (write side).
fn rw_write_recover<T>(m: &RwLock<T>) -> std::sync::RwLockWriteGuard<'_, T> {
    m.write().unwrap_or_else(|e| e.into_inner())
}

impl SharedState {
    fn new(config: ProxyConfig, inner: Arc<SharedInner>) -> Self {
        Self { config, inner }
    }

    fn record(&self, entry: LogEntry) {
        {
            let mut stats = mutex_recover(&self.inner.stats);
            let s = stats.entry(entry.provider.clone()).or_default();
            s.calls += 1;
            s.total_latency_ms = s.total_latency_ms.saturating_add(entry.latency_ms);
            s.last_call_ts = entry.ts;
            if !entry.ok {
                s.errors += 1;
                s.last_error = entry.error.clone();
            }
        }
        if self.config.log_requests {
            let mut logs = mutex_recover(&self.inner.logs);
            if logs.len() >= LOG_CAP {
                logs.pop_front();
            }
            logs.push_back(entry);
        }
    }

    /// Snapshot of per-provider stats for `GET /api/proxy/stats`.
    pub fn stats_snapshot(&self) -> HashMap<String, ProviderStats> {
        mutex_recover(&self.inner.stats).clone()
    }

    /// Newest-first log entries for `GET /api/proxy/logs`.
    pub fn logs_snapshot(&self, limit: usize) -> Vec<LogEntry> {
        let logs = mutex_recover(&self.inner.logs);
        logs.iter().rev().take(limit).cloned().collect()
    }

    fn rate_limit(&self, provider_id: &str) -> Option<Duration> {
        let interval = self.config.interval_for(provider_id);
        if interval.is_zero() {
            return None;
        }
        let mut rate = mutex_recover(&self.inner.rate);
        rate_wait(&mut rate, provider_id, interval)
    }
}

fn now_epoch_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Scrub anything key-like out of a string before it reaches logs/stats.
/// Provider errors already redact keys, but this is a second line of
/// defence for anything unexpected.
fn scrub_secrets(s: &str) -> String {
    // Redact `sk-...` style tokens and `key=...` query fragments.
    let mut out = s.to_string();
    for prefix in ["sk-", "sk-ant-", "xai-"] {
        let mut search_from = 0;
        while let Some(pos) = out[search_from..].find(prefix) {
            let start = search_from + pos;
            let end = out[start..]
                .find(|c: char| c.is_whitespace() || c == '"' || c == '\'' || c == '&')
                .map(|e| start + e)
                .unwrap_or(out.len());
            out.replace_range(start..end, &format!("{prefix}[redacted]"));
            search_from = start + prefix.len() + "[redacted]".len();
        }
    }
    out
}

// ---------------------------------------------------------------------------
// Global slot: the most recently wrapped client publishes its state here so
// the /api/proxy/* routes can read it without AppState changes.
// ---------------------------------------------------------------------------

static CURRENT: LazyLock<RwLock<Option<Arc<SharedState>>>> =
    LazyLock::new(|| RwLock::new(None));

/// Shared state of the most recently wrapped client, if any.
pub fn current_state() -> Option<Arc<SharedState>> {
    rw_read_recover(&CURRENT).clone()
}

// ---------------------------------------------------------------------------
// ProxyClient
// ---------------------------------------------------------------------------

/// A [`LlmClient`] that routes every call through the boom_proxy
/// middleware (logging, retry, rate limiting) before delegating to the
/// wrapped provider client.
pub struct ProxyClient {
    inner: Box<dyn LlmClient>,
    provider_id: &'static str,
    state: Arc<SharedState>,
}

impl ProxyClient {
    fn log_call(
        &self,
        req: &ChatRequest,
        streaming: bool,
        latency: Duration,
        attempts: u32,
        result: &Result<ChatResponse, ProviderError>,
    ) {
        let (ok, error) = match result {
            Ok(_) => (true, None),
            Err(e) => (false, Some(scrub_secrets(&e.to_string()))),
        };
        let bodies = if self.state.config.log_bodies {
            Some(
                req.messages
                    .iter()
                    .map(|m| format!("{:?}: {}", m.role, middleware::trunc_chars(&m.content, 500)))
                    .collect(),
            )
        } else {
            None
        };
        self.state.record(LogEntry {
            ts: now_epoch_secs(),
            provider: self.provider_id.to_string(),
            model: req.model.clone(),
            messages: req.messages.len(),
            streaming,
            latency_ms: latency.as_millis().min(u128::from(u64::MAX)) as u64,
            ok,
            error,
            attempts,
            bodies,
        });
    }

    async fn call_with_retry(
        &self,
        req: &ChatRequest,
    ) -> (Result<ChatResponse, ProviderError>, u32) {
        let cfg = &self.state.config;
        let mut attempts: u32 = 0;
        loop {
            attempts += 1;
            match self.inner.complete(req).await {
                Ok(resp) => return (Ok(resp), attempts),
                Err(e) if attempts <= cfg.max_retries && is_retryable(&e) => {
                    let backoff = cfg.base_backoff_ms.saturating_mul(1u64 << (attempts - 1).min(10));
                    tokio::time::sleep(Duration::from_millis(backoff)).await;
                }
                Err(e) => return (Err(e), attempts),
            }
        }
    }
}

#[async_trait]
impl LlmClient for ProxyClient {
    async fn complete(&self, req: &ChatRequest) -> Result<ChatResponse, ProviderError> {
        let req = transform::normalize_request(req, self.inner.default_model());
        if let Some(wait) = self.state.rate_limit(self.provider_id) {
            tokio::time::sleep(wait).await;
        }
        let started = Instant::now();
        let (result, attempts) = self.call_with_retry(&req).await;
        let latency = started.elapsed();
        self.log_call(&req, false, latency, attempts, &result);
        result
    }

    async fn stream(&self, req: &ChatRequest) -> Result<EventStream, ProviderError> {
        let req = transform::normalize_request(req, self.inner.default_model());
        if let Some(wait) = self.state.rate_limit(self.provider_id) {
            tokio::time::sleep(wait).await;
        }
        let started = Instant::now();
        // Retry only the handshake: once the stream is open, mid-stream
        // failures belong to the consumer, not the proxy.
        let cfg = &self.state.config;
        let mut attempts: u32 = 0;
        let stream = loop {
            attempts += 1;
            match self.inner.stream(&req).await {
                Ok(s) => break Ok(s),
                Err(e) if attempts <= cfg.max_retries && is_retryable(&e) => {
                    let backoff = cfg.base_backoff_ms.saturating_mul(1u64 << (attempts - 1).min(10));
                    tokio::time::sleep(Duration::from_millis(backoff)).await;
                }
                Err(e) => break Err(e),
            }
        };
        let latency = started.elapsed();
        // Adapt the stream result into a ChatResponse-shaped outcome for logging.
        let logged: Result<ChatResponse, ProviderError> = match &stream {
            Ok(_) => Ok(ChatResponse::default()),
            Err(e) => Err(ProviderError::StreamError(scrub_secrets(&e.to_string()))),
        };
        self.log_call(&req, true, latency, attempts, &logged);
        stream
    }

    fn provider_id(&self) -> &'static str {
        self.provider_id
    }

    fn default_model(&self) -> &str {
        self.inner.default_model()
    }
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

/// Wrap a provider client with the boom_proxy middleware.
///
/// When the proxy config is disabled, the inner client is returned
/// unwrapped. Otherwise the wrapped client registers its shared state in
/// the process-global slot read by `GET /api/proxy/stats` and
/// `GET /api/proxy/logs`. Stats, logs and rate-limiter clocks are shared
/// across all `wrap()` calls in the process (so they accumulate over
/// turns); the config is reloaded on every call.
pub fn wrap(inner: Box<dyn LlmClient>) -> Box<dyn LlmClient> {
    let config = ProxyConfig::load();
    if !config.enabled {
        return inner;
    }
    let provider_id = inner.provider_id();
    let state = {
        let mut guard = rw_write_recover(&CURRENT);
        let shared = guard
            .as_ref()
            .map(|s| s.inner.clone())
            .unwrap_or_default();
        let state = Arc::new(SharedState::new(config, shared));
        *guard = Some(state.clone());
        state
    };
    Box::new(ProxyClient {
        inner,
        provider_id,
        state,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::providers::{Message, Role};

    struct ScriptClient {
        failures: std::sync::Mutex<u32>,
        content: String,
    }

    impl ScriptClient {
        fn new(failures: u32, content: &str) -> Self {
            Self {
                failures: std::sync::Mutex::new(failures),
                content: content.to_string(),
            }
        }
    }

    #[async_trait]
    impl LlmClient for ScriptClient {
        async fn complete(
            &self,
            _req: &ChatRequest,
        ) -> Result<ChatResponse, ProviderError> {
            let mut f = self.failures.lock().unwrap();
            if *f > 0 {
                *f -= 1;
                return Err(ProviderError::Timeout);
            }
            Ok(ChatResponse {
                content: self.content.clone(),
                ..ChatResponse::default()
            })
        }

        async fn stream(
            &self,
            _req: &ChatRequest,
        ) -> Result<EventStream, ProviderError> {
            Ok(Box::pin(futures_util::stream::empty()))
        }

        fn provider_id(&self) -> &'static str {
            "test-proxy"
        }

        fn default_model(&self) -> &str {
            "test-model"
        }
    }

    fn req() -> ChatRequest {
        ChatRequest::new("", vec![Message::new(Role::User, "hi")])
    }

    fn wrap_with(config: ProxyConfig, inner: Box<dyn LlmClient>) -> ProxyClient {
        let provider_id = inner.provider_id();
        let state = Arc::new(SharedState::new(config, Arc::new(SharedInner::default())));
        ProxyClient {
            inner,
            provider_id,
            state,
        }
    }

    #[tokio::test]
    async fn retry_succeeds_after_transient_failures() {
        let client = wrap_with(
            ProxyConfig {
                max_retries: 3,
                base_backoff_ms: 1,
                ..ProxyConfig::default()
            },
            Box::new(ScriptClient::new(2, "recovered")),
        );
        let resp = client.complete(&req()).await.unwrap();
        assert_eq!(resp.content, "recovered");
        let stats = client.state.stats_snapshot();
        assert_eq!(stats["test-proxy"].calls, 1);
        assert_eq!(stats["test-proxy"].errors, 0);
    }

    #[tokio::test]
    async fn retry_gives_up_after_max_retries() {
        let client = wrap_with(
            ProxyConfig {
                max_retries: 2,
                base_backoff_ms: 1,
                ..ProxyConfig::default()
            },
            Box::new(ScriptClient::new(99, "never")),
        );
        let err = client.complete(&req()).await.unwrap_err();
        assert!(matches!(err, ProviderError::Timeout));
        let stats = client.state.stats_snapshot();
        assert_eq!(stats["test-proxy"].calls, 1);
        assert_eq!(stats["test-proxy"].errors, 1);
        assert!(stats["test-proxy"].last_error.is_some());
    }

    #[tokio::test]
    async fn non_retryable_errors_return_immediately() {
        struct BadKey;
        #[async_trait]
        impl LlmClient for BadKey {
            async fn complete(
                &self,
                _req: &ChatRequest,
            ) -> Result<ChatResponse, ProviderError> {
                Err(ProviderError::Api {
                    status: 401,
                    message: "bad key".into(),
                })
            }
            async fn stream(
                &self,
                _req: &ChatRequest,
            ) -> Result<EventStream, ProviderError> {
                unreachable!()
            }
            fn provider_id(&self) -> &'static str {
                "test-proxy"
            }
            fn default_model(&self) -> &str {
                "m"
            }
        }
        let client = wrap_with(
            ProxyConfig {
                max_retries: 5,
                base_backoff_ms: 1,
                ..ProxyConfig::default()
            },
            Box::new(BadKey),
        );
        let started = Instant::now();
        let err = client.complete(&req()).await.unwrap_err();
        assert!(matches!(err, ProviderError::Api { status: 401, .. }));
        // No retries → returns fast (well under the backoff schedule).
        assert!(started.elapsed() < Duration::from_millis(500));
    }

    #[tokio::test]
    async fn normalize_fills_empty_model() {
        let client = wrap_with(
            ProxyConfig::default(),
            Box::new(ScriptClient::new(0, "ok")),
        );
        let resp = client.complete(&req()).await.unwrap();
        assert_eq!(resp.content, "ok");
        let logs = client.state.logs_snapshot(1);
        assert_eq!(logs[0].model, "test-model");
        assert_eq!(logs[0].messages, 1);
        assert!(logs[0].ok);
    }

    #[test]
    fn scrub_secrets_redacts_tokens() {
        let s = scrub_secrets("failed with key sk-abc123XYZ here");
        assert!(!s.contains("sk-abc123XYZ"));
        assert!(s.contains("sk-[redacted]"));
        assert_eq!(scrub_secrets("clean message"), "clean message");
    }

    #[test]
    fn config_defaults_are_sane() {
        let cfg = ProxyConfig::default();
        assert!(cfg.enabled);
        assert_eq!(cfg.max_retries, 3);
        assert!(cfg.log_requests);
        assert!(!cfg.log_bodies);
        assert_eq!(cfg.interval_for("openai"), Duration::ZERO);
    }

    #[test]
    fn proxy_config_path_respects_boom_home() {
        let dir = tempfile::tempdir().unwrap();
        let prev = std::env::var("BOOM_HOME").ok();
        std::env::set_var("BOOM_HOME", dir.path());
        let p = proxy_config_path();
        assert_eq!(p, dir.path().join("proxy.json"));
        match prev {
            Some(v) => std::env::set_var("BOOM_HOME", v),
            None => std::env::remove_var("BOOM_HOME"),
        }
    }

    #[test]
    fn invalid_config_json_falls_back_to_default() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("proxy.json"), "{not json").unwrap();
        let prev = std::env::var("BOOM_HOME").ok();
        std::env::set_var("BOOM_HOME", dir.path());
        let cfg = ProxyConfig::load();
        assert_eq!(cfg.max_retries, 3);
        match prev {
            Some(v) => std::env::set_var("BOOM_HOME", v),
            None => std::env::remove_var("BOOM_HOME"),
        }
    }

    #[tokio::test]
    async fn stats_accumulate_across_wraps_sharing_inner() {
        // Two ProxyClients sharing one SharedInner (as `wrap()` does via
        // the global slot) must accumulate stats, not reset them.
        let shared = Arc::new(SharedInner::default());
        let mk = || {
            let inner: Box<dyn LlmClient> = Box::new(ScriptClient::new(0, "ok"));
            ProxyClient {
                inner,
                provider_id: "test-proxy",
                state: Arc::new(SharedState::new(ProxyConfig::default(), shared.clone())),
            }
        };
        mk().complete(&req()).await.unwrap();
        mk().complete(&req()).await.unwrap();
        let stats = shared
            .stats
            .lock()
            .unwrap()
            .get("test-proxy")
            .cloned()
            .unwrap();
        assert_eq!(stats.calls, 2);
        assert_eq!(stats.errors, 0);
        // ... and the log ring buffer holds both entries.
        assert_eq!(shared.logs.lock().unwrap().len(), 2);
    }

    #[test]
    fn poisoned_locks_recover_without_panic() {
        // A thread that panics while holding a proxy lock must not take
        // down later requests: the recovery helpers reclaim the guard.
        let m = std::sync::Mutex::new(1u32);
        let _ = std::thread::scope(|s| {
            s.spawn(|| {
                let _g = m.lock().unwrap();
                panic!("boom");
            })
            .join()
        });
        assert!(m.is_poisoned());
        let g = mutex_recover(&m);
        assert_eq!(*g, 1);

        let rw = RwLock::new(2u32);
        let _ = std::thread::scope(|s| {
            s.spawn(|| {
                let _g = rw.write().unwrap();
                panic!("boom");
            })
            .join()
        });
        assert!(rw.is_poisoned());
        assert_eq!(*rw_read_recover(&rw), 2);
        assert_eq!(*rw_write_recover(&rw), 2);
    }
}
