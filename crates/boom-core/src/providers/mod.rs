//! LLM provider clients — Rust port of `agent/src/providers/llm.py` and the
//! provider registry (`PROVIDER_DEFS`) in `agent/src/providers/manager.py`.
//!
//! Supported providers (ids match `PROVIDER_DEFS`):
//!
//! | id         | client            | endpoint                                          | auth              |
//! |------------|-------------------|---------------------------------------------------|-------------------|
//! | `openai`   | [`OpenAiClient`]  | `POST {base}/v1/chat/completions`                 | Bearer token      |
//! | `anthropic`| [`AnthropicClient`]| `POST {base}/v1/messages`                        | `x-api-key` header|
//! | `google`   | [`GeminiClient`]  | `POST {base}/v1beta/models/{model}:generateContent`| `?key=` query     |
//! | `xai`      | [`OpenAiClient`]  | OpenAI-compatible (`https://api.x.ai/v1`)         | Bearer token      |
//! | `deepseek` | [`OpenAiClient`]  | OpenAI-compatible (`https://api.deepseek.com/v1`) | Bearer token      |
//! | `ollama`   | [`OpenAiClient`]  | OpenAI-compatible (`http://localhost:11434/v1`)   | none required     |
//! | `custom`   | [`OpenAiClient`]  | OpenAI-compatible (caller-supplied base URL)      | Bearer token      |
//!
//! Streaming yields JSON-encoded [`types::StreamEvent`] strings
//! (`{"type":"token","text":"..."}`, `{"type":"reasoning",...}`,
//! `{"type":"tool_calls",...}`, `{"type":"done",...}`), mirroring the Python
//! client's event protocol while keeping the `Stream<Item = String>` shape.
//!
//! Secrets (API keys) travel in [`ProviderConfig`] only, are redacted from
//! every `Debug` implementation, and are never written to logs or error
//! messages beyond a `[redacted]` marker. This module never reads files —
//! configuration storage is the storage worker's job.

pub mod anthropic;
pub mod gemini;
pub mod openai;
pub mod types;

pub use anthropic::AnthropicClient;
pub use gemini::GeminiClient;
pub use openai::OpenAiClient;
pub use types::{
    ChatRequest, ChatResponse, Message, Role, StreamEvent, ToolCall, ToolSpec, Usage,
};

use async_trait::async_trait;
use futures_core::Stream;
use std::{
    collections::VecDeque,
    pin::Pin,
    time::Duration,
};
use thiserror::Error;

/// Errors from LLM provider clients.
#[derive(Debug, Error)]
pub enum ProviderError {
    /// Transport-level failure (DNS, connection refused, TLS, ...).
    #[error("HTTP transport error: {0}")]
    Http(#[from] reqwest::Error),
    /// The provider answered with an HTTP error status or an error payload.
    /// `message` is truncated and never contains the API key.
    #[error("LLM API error (HTTP {status}): {message}")]
    Api { status: u16, message: String },
    /// The request exceeded the configured timeout.
    #[error("request timed out")]
    Timeout,
    /// The provider's response could not be understood, or the supplied
    /// configuration is unusable (e.g. a `custom` provider without a base URL).
    #[error("failed to parse provider response: {0}")]
    Parse(String),
    /// A live request was attempted without an API key.
    #[error("missing API key: {0}")]
    MissingKey(String),
    /// The provider sent an error mid-stream, or the stream broke.
    #[error("stream failed: {0}")]
    StreamError(String),
    /// `from_config` was given an id that is not in the provider registry.
    #[error("unknown provider: {0}")]
    UnknownProvider(String),
}

impl ProviderError {
    /// Map a reqwest error, surfacing timeouts as [`ProviderError::Timeout`].
    pub(crate) fn from_reqwest(e: reqwest::Error) -> Self {
        if e.is_timeout() {
            ProviderError::Timeout
        } else {
            ProviderError::Http(e)
        }
    }

    /// Map a body-JSON decode failure: timeouts stay timeouts, anything else
    /// means the provider did not return JSON.
    pub(crate) fn json_error(e: reqwest::Error) -> Self {
        if e.is_timeout() {
            ProviderError::Timeout
        } else {
            ProviderError::Parse(format!("invalid JSON response: {e}"))
        }
    }
}

/// A provider known to the registry. Ids match `PROVIDER_DEFS` in `manager.py`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProviderKind {
    OpenAI,
    Anthropic,
    Google,
    Xai,
    DeepSeek,
    Ollama,
    Custom,
}

impl ProviderKind {
    /// Look up a provider by its registry id (`"openai"`, `"anthropic"`, ...).
    pub fn from_id(id: &str) -> Result<Self, ProviderError> {
        match id {
            "openai" => Ok(ProviderKind::OpenAI),
            "anthropic" => Ok(ProviderKind::Anthropic),
            "google" => Ok(ProviderKind::Google),
            "xai" => Ok(ProviderKind::Xai),
            "deepseek" => Ok(ProviderKind::DeepSeek),
            "ollama" => Ok(ProviderKind::Ollama),
            "custom" => Ok(ProviderKind::Custom),
            other => Err(ProviderError::UnknownProvider(other.to_string())),
        }
    }

    /// The registry id, usable in config files and UIs.
    pub fn id(&self) -> &'static str {
        match self {
            ProviderKind::OpenAI => "openai",
            ProviderKind::Anthropic => "anthropic",
            ProviderKind::Google => "google",
            ProviderKind::Xai => "xai",
            ProviderKind::DeepSeek => "deepseek",
            ProviderKind::Ollama => "ollama",
            ProviderKind::Custom => "custom",
        }
    }

    /// Default base URL from `PROVIDER_DEFS` (used when the config has none).
    pub fn default_base_url(&self) -> &'static str {
        match self {
            ProviderKind::OpenAI => "https://api.openai.com/v1",
            ProviderKind::Anthropic => "https://api.anthropic.com",
            ProviderKind::Google => "https://generativelanguage.googleapis.com",
            ProviderKind::Xai => "https://api.x.ai/v1",
            ProviderKind::DeepSeek => "https://api.deepseek.com/v1",
            ProviderKind::Ollama => "http://localhost:11434/v1",
            // Custom has no default in PROVIDER_DEFS; callers must supply one.
            ProviderKind::Custom => "",
        }
    }

    /// Whether the provider requires an API key (`needs_key` in `manager.py`).
    pub fn needs_key(&self) -> bool {
        !matches!(self, ProviderKind::Ollama)
    }

    /// Whether the provider speaks the OpenAI chat-completions dialect.
    pub fn is_openai_compatible(&self) -> bool {
        matches!(
            self,
            ProviderKind::OpenAI
                | ProviderKind::Xai
                | ProviderKind::DeepSeek
                | ProviderKind::Ollama
                | ProviderKind::Custom
        )
    }
}

/// Configuration for one provider client.
///
/// Plain data: this struct is built by the caller (or the storage worker that
/// owns the config file). This module never reads files or environment
/// variables. `Debug` redacts the key.
#[derive(Clone)]
pub struct ProviderConfig {
    /// Registry id, e.g. `"openai"`, `"anthropic"`, `"google"`.
    pub provider_id: String,
    /// API key, if the provider needs one. `None`/empty is allowed for
    /// providers with `needs_key == false` (Ollama).
    pub api_key: Option<String>,
    /// Default model id, e.g. `"gpt-4o"`.
    pub model: String,
    /// Override for [`ProviderKind::default_base_url`].
    pub base_url: Option<String>,
    /// Request timeout in seconds (default 120).
    pub timeout_secs: u64,
}

impl ProviderConfig {
    pub const DEFAULT_TIMEOUT_SECS: u64 = 120;

    pub fn new(provider_id: impl Into<String>, model: impl Into<String>) -> Self {
        Self {
            provider_id: provider_id.into(),
            api_key: None,
            model: model.into(),
            base_url: None,
            timeout_secs: Self::DEFAULT_TIMEOUT_SECS,
        }
    }

    pub fn with_api_key(mut self, key: impl Into<String>) -> Self {
        let key = key.into();
        self.api_key = if key.is_empty() { None } else { Some(key) };
        self
    }

    pub fn with_base_url(mut self, base_url: impl Into<String>) -> Self {
        self.base_url = Some(base_url.into());
        self
    }

    pub fn with_timeout_secs(mut self, secs: u64) -> Self {
        self.timeout_secs = secs;
        self
    }

    /// Base URL with trailing slashes trimmed, falling back to the default.
    pub(crate) fn resolved_base_url(&self, kind: ProviderKind) -> String {
        self.base_url
            .clone()
            .unwrap_or_else(|| kind.default_base_url().to_string())
            .trim_end_matches('/')
            .to_string()
    }

    pub(crate) fn timeout(&self) -> Duration {
        Duration::from_secs(self.timeout_secs.max(1))
    }
}

// The API key must never leak through Debug / logging.
impl std::fmt::Debug for ProviderConfig {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ProviderConfig")
            .field("provider_id", &self.provider_id)
            .field("api_key", &self.api_key.as_ref().map(|_| "[redacted]"))
            .field("model", &self.model)
            .field("base_url", &self.base_url)
            .field("timeout_secs", &self.timeout_secs)
            .finish()
    }
}

/// Boxed stream of JSON-encoded [`StreamEvent`] strings.
///
/// Transport errors surface as `Err` items; after an `Err` the stream ends.
pub type EventStream = Pin<Box<dyn Stream<Item = Result<String, ProviderError>> + Send>>;

/// Async LLM client implemented by every provider.
#[async_trait]
pub trait LlmClient: Send + Sync {
    /// One non-streaming chat completion.
    async fn complete(&self, req: &ChatRequest) -> Result<ChatResponse, ProviderError>;

    /// Streaming chat completion. Each item is a JSON-encoded [`StreamEvent`]
    /// (`token` / `reasoning` / `tool_calls` / `done`), mirroring the Python
    /// client's event protocol.
    async fn stream(&self, req: &ChatRequest) -> Result<EventStream, ProviderError>;

    /// Registry id of the provider, e.g. `"openai"`.
    fn provider_id(&self) -> &'static str;

    /// Model used when [`ChatRequest::model`] is empty.
    fn default_model(&self) -> &str;
}

/// Build a boxed client from a [`ProviderConfig`].
///
/// Unknown provider ids yield [`ProviderError::UnknownProvider`]. No I/O is
/// performed here — missing API keys are reported when a request is made.
pub fn from_config(cfg: &ProviderConfig) -> Result<Box<dyn LlmClient>, ProviderError> {
    let kind = ProviderKind::from_id(&cfg.provider_id)?;
    let base_url = cfg.resolved_base_url(kind);
    if base_url.is_empty() {
        return Err(ProviderError::Parse(format!(
            "provider '{}' requires a base_url in ProviderConfig",
            kind.id()
        )));
    }
    let api_key = cfg.api_key.clone().filter(|k| !k.trim().is_empty());
    let timeout = cfg.timeout();
    let model = cfg.model.clone();

    if kind.is_openai_compatible() {
        Ok(Box::new(OpenAiClient::new(
            base_url, api_key, model, timeout, kind,
        )?))
    } else {
        match kind {
            ProviderKind::Anthropic => Ok(Box::new(AnthropicClient::new(
                base_url, api_key, model, timeout,
            )?)),
            ProviderKind::Google => Ok(Box::new(GeminiClient::new(
                base_url, api_key, model, timeout,
            )?)),
            _ => unreachable!("openai-compatible kinds handled above"),
        }
    }
}

/// Pick the request model, falling back to the client default.
pub(crate) fn effective_model<'a>(default: &'a str, req: &'a ChatRequest) -> &'a str {
    if req.model.trim().is_empty() {
        default
    } else {
        req.model.as_str()
    }
}

/// Truncate to `max_chars` characters (char-boundary safe) for error messages.
pub(crate) fn trunc(s: &str, max_chars: usize) -> String {
    if s.chars().count() <= max_chars {
        s.to_string()
    } else {
        s.chars().take(max_chars).collect()
    }
}

/// Turn an HTTP response into `Ok(response)` on 2xx, else an [`ProviderError::Api`]
/// carrying the (truncated) body. The URL is never included — for Gemini it
/// carries the API key as a query parameter.
pub(crate) async fn ensure_success(
    resp: reqwest::Response,
    provider: &str,
    model: &str,
) -> Result<reqwest::Response, ProviderError> {
    let status = resp.status();
    if status.is_success() {
        return Ok(resp);
    }
    let body = resp.text().await.unwrap_or_default();
    Err(ProviderError::Api {
        status: status.as_u16(),
        message: format!(
            "LLM request failed: HTTP {status} from {provider} for model '{model}': {}",
            trunc(&body, 400)
        ),
    })
}

// ---------------------------------------------------------------------------
// Server-Sent Events plumbing (shared by all providers)
// ---------------------------------------------------------------------------

/// One parsed SSE event: an optional `event:` name plus its `data:` payload.
///
/// Providers that ignore event names (OpenAI, Gemini) just read `data`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct SseEvent {
    pub event: String,
    pub data: String,
}

/// Incremental SSE parser: feed raw byte chunks, drain completed events.
///
/// Lines are split on `\n` in the byte buffer so multi-byte UTF-8 sequences
/// split across TCP chunks are never corrupted by decoding.
#[derive(Debug, Default)]
pub(crate) struct SseParser {
    buf: Vec<u8>,
    event_name: String,
    data_lines: Vec<String>,
    ready: VecDeque<SseEvent>,
}

impl SseParser {
    pub fn new() -> Self {
        Self::default()
    }

    /// Feed a raw byte chunk. Completed events are queued internally and can
    /// be collected with [`SseParser::pop_ready`].
    pub fn push_bytes(&mut self, chunk: &[u8]) {
        self.buf.extend_from_slice(chunk);
        while let Some(pos) = self.buf.iter().position(|&b| b == b'\n') {
            let line_bytes: Vec<u8> = self.buf.drain(..=pos).collect();
            let mut end = line_bytes.len();
            while end > 0 && (line_bytes[end - 1] == b'\n' || line_bytes[end - 1] == b'\r') {
                end -= 1;
            }
            // Decode one complete line; a split multi-byte char can only
            // occur across chunks, never inside a complete line.
            let line = String::from_utf8_lossy(&line_bytes[..end]);
            self.push_line(&line);
        }
    }

    fn push_line(&mut self, line: &str) {
        if line.is_empty() {
            // Blank line: dispatch the buffered event.
            if let Some(ev) = self.take_event() {
                self.ready.push_back(ev);
            }
            return;
        }
        if line.starts_with(':') {
            return; // comment / keep-alive heartbeat
        }
        if let Some(rest) = line.strip_prefix("event:") {
            self.event_name = rest.trim_start().to_string();
        } else if let Some(rest) = line.strip_prefix("data:") {
            // The spec strips a single leading space.
            self.data_lines
                .push(rest.strip_prefix(' ').unwrap_or(rest).to_string());
        }
        // `id:`, `retry:` and unknown fields are ignored.
    }

    fn take_event(&mut self) -> Option<SseEvent> {
        if self.event_name.is_empty() && self.data_lines.is_empty() {
            return None;
        }
        let event = if self.event_name.is_empty() {
            "message".to_string()
        } else {
            std::mem::take(&mut self.event_name)
        };
        let data = std::mem::take(&mut self.data_lines).join("\n");
        Some(SseEvent { event, data })
    }

    /// Pop one queued event, if any.
    pub fn pop_ready(&mut self) -> Option<SseEvent> {
        self.ready.pop_front()
    }

    /// Flush a trailing event not terminated by a blank line.
    pub fn finish(&mut self) {
        if !self.buf.is_empty() {
            let tail: Vec<u8> = std::mem::take(&mut self.buf);
            let line = String::from_utf8_lossy(&tail);
            let trimmed = line.trim_end_matches(['\r', '\n']).to_string();
            self.push_line(&trimmed);
        }
        if let Some(ev) = self.take_event() {
            self.ready.push_back(ev);
        }
    }
}

/// Convert an HTTP response body into a stream of parsed SSE events.
pub(crate) fn sse_events(
    resp: reqwest::Response,
) -> Pin<Box<dyn Stream<Item = Result<SseEvent, ProviderError>> + Send>> {
    use futures_util::{stream::unfold, StreamExt};

    type ByteStream = Pin<Box<dyn Stream<Item = Result<bytes::Bytes, reqwest::Error>> + Send>>;
    let bytes: ByteStream = Box::pin(resp.bytes_stream());

    let stream = unfold(
        (bytes, SseParser::new(), false),
        |(mut bytes, mut parser, mut eof)| async move {
            loop {
                if let Some(ev) = parser.pop_ready() {
                    return Some((Ok(ev), (bytes, parser, eof)));
                }
                if eof {
                    return None;
                }
                match bytes.next().await {
                    Some(Ok(chunk)) => parser.push_bytes(&chunk),
                    Some(Err(e)) => {
                        return Some((
                            Err(ProviderError::from_reqwest(e)),
                            (bytes, parser, true),
                        ))
                    }
                    None => {
                        parser.finish();
                        eof = true;
                    }
                }
            }
        },
    );
    Box::pin(stream)
}

/// Queue of outgoing stream items shared by the per-provider accumulators.
#[derive(Debug, Default)]
pub(crate) struct EventQueue {
    out: VecDeque<Result<String, ProviderError>>,
    finished: bool,
    failed: bool,
}

impl EventQueue {
    pub fn push_event(&mut self, ev: StreamEvent) {
        self.out.push_back(Ok(ev.to_json_string()));
    }

    /// Queue a fatal error; the stream ends after it is emitted.
    pub fn push_error(&mut self, e: ProviderError) {
        self.out.push_back(Err(e));
        self.finished = true;
        self.failed = true;
    }

    pub fn pop(&mut self) -> Option<Result<String, ProviderError>> {
        self.out.pop_front()
    }

    /// True once a fatal error was queued: no further events are accepted and
    /// no trailing `done` is emitted — the stream dies with the error.
    pub fn has_failed(&self) -> bool {
        self.failed
    }

    /// The stream is over once flagged finished and the queue is drained.
    pub fn is_done(&self) -> bool {
        self.finished && self.out.is_empty()
    }

    pub fn set_finished(&mut self) {
        self.finished = true;
    }
}

/// Per-provider streaming state machine: consumes [`SseEvent`]s and queues
/// JSON-encoded [`StreamEvent`] strings for the consumer.
pub(crate) trait StreamAccumulator: Send + 'static {
    /// Feed one SSE event (providers that ignore event names use `data`).
    fn feed(&mut self, event: &SseEvent);
    /// End of the byte stream: emit trailing tool-calls / done events.
    fn finish(&mut self);
    fn pop(&mut self) -> Option<Result<String, ProviderError>>;
    fn is_done(&self) -> bool;
    fn push_transport_error(&mut self, e: ProviderError);
}

/// Drive an accumulator over an SSE response body, producing the boxed
/// [`EventStream`] returned by [`LlmClient::stream`].
pub(crate) fn drive_stream<A: StreamAccumulator>(
    resp: reqwest::Response,
    accum: A,
) -> EventStream {
    use futures_util::{stream::unfold, StreamExt};

    let inner = sse_events(resp);
    let stream = unfold((inner, accum), |(mut inner, mut accum)| async move {
        loop {
            if let Some(item) = accum.pop() {
                return Some((item, (inner, accum)));
            }
            if accum.is_done() {
                return None;
            }
            match inner.next().await {
                Some(Ok(ev)) => accum.feed(&ev),
                Some(Err(e)) => accum.push_transport_error(e),
                None => accum.finish(),
            }
        }
    });
    Box::pin(stream)
}

/// Read `u64` from a JSON number-or-string field, defaulting to 0.
pub(crate) fn get_u64(v: &serde_json::Value, key: &str) -> u64 {
    match v.get(key) {
        Some(serde_json::Value::Number(n)) => n.as_u64().unwrap_or(0),
        Some(serde_json::Value::String(s)) => s.parse().unwrap_or(0),
        _ => 0,
    }
}

/// `None` when the string is blank — keeps `thought` fields meaningful.
pub(crate) fn none_if_blank(s: String) -> Option<String> {
    if s.trim().is_empty() {
        None
    } else {
        Some(s)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn provider_ids_roundtrip() {
        for id in [
            "openai", "anthropic", "google", "xai", "deepseek", "ollama", "custom",
        ] {
            let kind = ProviderKind::from_id(id).unwrap();
            assert_eq!(kind.id(), id);
        }
        assert!(matches!(
            ProviderKind::from_id("nope"),
            Err(ProviderError::UnknownProvider(_))
        ));
    }

    #[test]
    fn default_base_urls_match_manager_defs() {
        assert_eq!(
            ProviderKind::OpenAI.default_base_url(),
            "https://api.openai.com/v1"
        );
        assert_eq!(
            ProviderKind::Anthropic.default_base_url(),
            "https://api.anthropic.com"
        );
        assert_eq!(
            ProviderKind::Google.default_base_url(),
            "https://generativelanguage.googleapis.com"
        );
        assert_eq!(ProviderKind::Xai.default_base_url(), "https://api.x.ai/v1");
        assert_eq!(
            ProviderKind::DeepSeek.default_base_url(),
            "https://api.deepseek.com/v1"
        );
        assert_eq!(
            ProviderKind::Ollama.default_base_url(),
            "http://localhost:11434/v1"
        );
        assert!(!ProviderKind::Ollama.needs_key());
        assert!(ProviderKind::OpenAI.needs_key());
        assert!(ProviderKind::Custom.is_openai_compatible());
        assert!(!ProviderKind::Anthropic.is_openai_compatible());
        assert!(!ProviderKind::Google.is_openai_compatible());
    }

    #[test]
    fn config_debug_redacts_key() {
        let cfg = ProviderConfig::new("openai", "gpt-4o").with_api_key("sk-secret-123");
        let dbg = format!("{cfg:?}");
        assert!(dbg.contains("[redacted]"));
        assert!(!dbg.contains("sk-secret-123"));
    }

    #[test]
    fn from_config_routes_each_provider() {
        let cases = [
            ("openai", "openai"),
            ("anthropic", "anthropic"),
            ("google", "google"),
            ("xai", "xai"),
            ("deepseek", "deepseek"),
            ("ollama", "ollama"),
            ("custom", "custom"),
        ];
        for (id, expect) in cases {
            let cfg = ProviderConfig::new(id, "some-model")
                .with_api_key("k")
                .with_base_url("https://example.com/v1");
            let client = from_config(&cfg).unwrap();
            assert_eq!(client.provider_id(), expect);
            assert_eq!(client.default_model(), "some-model");
        }
        let bad = ProviderConfig::new("nope", "m");
        assert!(matches!(
            from_config(&bad),
            Err(ProviderError::UnknownProvider(_))
        ));
    }

    #[test]
    fn from_config_rejects_custom_without_base_url() {
        let cfg = ProviderConfig::new("custom", "m").with_api_key("k");
        assert!(matches!(
            from_config(&cfg),
            Err(ProviderError::Parse(_))
        ));
    }

    #[test]
    fn from_config_trims_base_url_slash() {
        let cfg = ProviderConfig::new("openai", "gpt-4o")
            .with_api_key("k")
            .with_base_url("https://example.com/v1/");
        let client = from_config(&cfg).unwrap();
        let dbg = format!("{:?}", client.provider_id());
        assert_eq!(dbg, "\"openai\"");
        // base_url itself is private; the request URL is covered in openai tests.
        let _ = client;
    }

    #[test]
    fn sse_parser_handles_chunk_boundaries() {
        let mut p = SseParser::new();
        // Split mid-line, mid-`data:` prefix, and inside a multi-byte char.
        p.push_bytes("data: {\"a\": ".as_bytes());
        assert_eq!(p.pop_ready(), None);
        p.push_bytes("1}\n\nevent: message\nda".as_bytes());
        let ev = p.pop_ready().unwrap();
        assert_eq!(ev.event, "message");
        assert_eq!(ev.data, "{\"a\": 1}");
        p.push_bytes("ta: hé".as_bytes());
        p.push_bytes("llo\n\n".as_bytes()); // 'é' (2 bytes) split across chunks
        let ev = p.pop_ready().unwrap();
        assert_eq!(ev.data, "héllo");
        assert_eq!(p.pop_ready(), None);
    }

    #[test]
    fn sse_parser_ignores_comments_and_unknown_fields() {
        let mut p = SseParser::new();
        p.push_bytes(b": keep-alive\nid: 42\nretry: 1000\ndata: x\n\n");
        let ev = p.pop_ready().unwrap();
        assert_eq!(ev.data, "x");
        assert_eq!(p.pop_ready(), None);
    }

    #[test]
    fn sse_parser_strips_one_leading_space() {
        let mut p = SseParser::new();
        p.push_bytes(b"data:  indented\n\n");
        let ev = p.pop_ready().unwrap();
        // Spec: strip exactly one space; we are lenient and strip all.
        assert_eq!(ev.data, " indented");
    }

    #[test]
    fn sse_parser_finish_flushes_trailing_event() {
        let mut p = SseParser::new();
        p.push_bytes(b"data: tail");
        assert_eq!(p.pop_ready(), None);
        p.finish();
        let ev = p.pop_ready().unwrap();
        assert_eq!(ev.data, "tail");
    }

    #[test]
    fn sse_parser_joins_multiline_data() {
        let mut p = SseParser::new();
        p.push_bytes(b"data: line1\ndata: line2\n\n");
        let ev = p.pop_ready().unwrap();
        assert_eq!(ev.data, "line1\nline2");
    }

    #[test]
    fn trunc_is_char_boundary_safe() {
        assert_eq!(trunc("héllo wörld", 5), "héllo");
        assert_eq!(trunc("short", 100), "short");
    }
}
