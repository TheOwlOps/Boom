//! OpenAI and OpenAI-compatible provider client.
//!
//! Covers the `openai`, `xai`, `deepseek`, `ollama`, and `custom` provider
//! ids: `POST {base_url}/chat/completions` with a Bearer token, exactly like
//! `LLMProvider` in `agent/src/providers/llm.py` (thought scrubbing,
//! reasoning-content fallback, tool-call argument error capture, SSE tool-call
//! delta accumulation, `stream_options.include_usage`).

use super::{
    drive_stream, effective_model, ensure_success, none_if_blank, trunc, ChatRequest,
    ChatResponse, EventQueue, EventStream, LlmClient, ProviderError, ProviderKind, SseEvent,
    StreamAccumulator, StreamEvent, ToolCall, Usage,
};
use async_trait::async_trait;
use serde_json::Value;
use std::{collections::BTreeMap, time::Duration};

/// OpenAI / OpenAI-compatible chat client.
///
/// `kind` selects the registry entry (`openai`, `xai`, `deepseek`, `ollama`,
/// `custom`); `needs_key` comes from it — keyless providers like Ollama send
/// no `Authorization` header.
pub struct OpenAiClient {
    base_url: String,
    api_key: Option<String>,
    model: String,
    kind: ProviderKind,
    http: reqwest::Client,
}

impl OpenAiClient {
    pub fn new(
        base_url: impl Into<String>,
        api_key: Option<String>,
        model: impl Into<String>,
        timeout: Duration,
        kind: ProviderKind,
    ) -> Result<Self, ProviderError> {
        debug_assert!(
            kind.is_openai_compatible(),
            "OpenAiClient only serves OpenAI-compatible providers"
        );
        let http = reqwest::Client::builder()
            .timeout(timeout)
            .build()
            .map_err(ProviderError::from_reqwest)?;
        Ok(Self {
            base_url: base_url.into().trim_end_matches('/').to_string(),
            api_key: api_key.filter(|k| !k.trim().is_empty()),
            model: model.into(),
            kind,
            http,
        })
    }

    /// The configured key, or [`ProviderError::MissingKey`] when the provider
    /// requires one. Returns `""` for keyless providers (Ollama).
    fn key(&self) -> Result<&str, ProviderError> {
        match self.api_key.as_deref() {
            Some(k) => Ok(k),
            None if self.kind.needs_key() => Err(ProviderError::MissingKey(format!(
                "No API key configured for OpenAI-compatible provider at {}. \
                 Provide api_key in ProviderConfig.",
                self.base_url
            ))),
            None => Ok(""),
        }
    }

    /// Build the `/chat/completions` JSON body. `pub(crate)` so tests can
    /// assert on the exact wire format without network I/O.
    pub(crate) fn build_body(&self, req: &ChatRequest, model: &str, stream: bool) -> Value {
        let mut body = serde_json::json!({
            "model": model,
            "messages": req.messages,
            "stream": stream,
        });
        if stream {
            // Ask for the final usage block; without it streamed turns report 0 tokens.
            body["stream_options"] = serde_json::json!({"include_usage": true});
        }
        if let Some(max_tokens) = req.max_tokens {
            body["max_tokens"] = Value::from(max_tokens);
        }
        if let Some(temperature) = req.temperature {
            body["temperature"] = serde_json::json!(temperature);
        }
        if let Some(tools) = req.tools.as_deref().filter(|t| !t.is_empty()) {
            let mapped: Vec<Value> = tools
                .iter()
                .map(|t| {
                    let mut func = serde_json::Map::new();
                    func.insert("name".to_string(), Value::String(t.name.clone()));
                    if let Some(desc) = &t.description {
                        func.insert("description".to_string(), Value::String(desc.clone()));
                    }
                    func.insert("parameters".to_string(), t.parameters.clone());
                    serde_json::json!({"type": "function", "function": Value::Object(func)})
                })
                .collect();
            body["tools"] = Value::Array(mapped);
        }
        body
    }

    /// Parse a non-streaming completion body. `pub(crate)` for unit tests.
    pub(crate) fn parse_response(data: &Value, model: &str) -> Result<ChatResponse, ProviderError> {
        if let Some(err) = data.get("error") {
            return Err(ProviderError::Api {
                status: 200,
                message: format!(
                    "LLM error from provider for model '{model}': {}",
                    trunc(&err.to_string(), 400)
                ),
            });
        }
        let choice = data
            .get("choices")
            .and_then(|c| c.as_array())
            .and_then(|c| c.first())
            .ok_or_else(|| {
                ProviderError::Parse(format!("LLM returned no choices for model '{model}'"))
            })?;
        let msg = choice.get("message").cloned().unwrap_or(Value::Null);

        let raw_content = msg.get("content").and_then(|c| c.as_str()).unwrap_or("");
        let (inline_thought, content) = super::types::scrub_thought(raw_content);
        // Reasoning models expose chain-of-thought on a dedicated field.
        let mut thought = inline_thought;
        if thought.is_none() {
            if let Some(rc) = msg.get("reasoning_content").and_then(|v| v.as_str()) {
                thought = none_if_blank(rc.trim().to_string());
            }
        }

        let mut tool_calls = Vec::new();
        if let Some(tcs) = msg.get("tool_calls").and_then(|v| v.as_array()) {
            for (i, tc) in tcs.iter().enumerate() {
                let func = tc.get("function").cloned().unwrap_or(Value::Null);
                let (arguments, args_error) = parse_tool_arguments(func.get("arguments"));
                tool_calls.push(ToolCall {
                    id: tc
                        .get("id")
                        .and_then(|v| v.as_str())
                        .map(str::to_string)
                        .unwrap_or_else(|| format!("call_{i}")),
                    name: func
                        .get("name")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string(),
                    arguments,
                    args_error,
                });
            }
        }

        Ok(ChatResponse {
            content,
            thought,
            tool_calls,
            usage: data.get("usage").map(Self::parse_usage),
            finish_reason: choice
                .get("finish_reason")
                .and_then(|v| v.as_str())
                .map(str::to_string),
        })
    }

    /// Map an OpenAI-style `usage` object onto [`Usage`]. Absent fields stay 0.
    /// Mirrors `LLMProvider.parse_usage` in `llm.py`.
    pub(crate) fn parse_usage(u: &Value) -> Usage {
        let prompt_details = u.get("prompt_tokens_details");
        let completion_details = u.get("completion_tokens_details");
        Usage {
            input_tokens: super::get_u64(u, "prompt_tokens"),
            output_tokens: super::get_u64(u, "completion_tokens"),
            cache_read_tokens: prompt_details.map(|d| super::get_u64(d, "cached_tokens")).unwrap_or(0),
            cache_write_tokens: prompt_details
                .map(|d| super::get_u64(d, "cache_creation_tokens"))
                .unwrap_or(0),
            reasoning_tokens: completion_details
                .map(|d| super::get_u64(d, "reasoning_tokens"))
                .unwrap_or(0),
        }
    }
}

/// Parse accumulated tool-call arguments, mirroring the Python helper:
/// returns `({}, None)` for missing/empty input and captures JSON errors
/// instead of raising.
pub(crate) fn parse_tool_arguments(raw: Option<&Value>) -> (Value, Option<String>) {
    match raw {
        None => (Value::Object(Default::default()), None),
        Some(Value::String(s)) if s.trim().is_empty() => (Value::Object(Default::default()), None),
        Some(Value::String(s)) => match serde_json::from_str(s) {
            Ok(v) => (v, None),
            Err(e) => (
                Value::Object(Default::default()),
                Some(format!("{e}: {}", trunc(s, 200))),
            ),
        },
        Some(v) => (v.clone(), None),
    }
}

// The API key must never leak through Debug / logging.
impl std::fmt::Debug for OpenAiClient {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("OpenAiClient")
            .field("base_url", &self.base_url)
            .field("api_key", &self.api_key.as_ref().map(|_| "[redacted]"))
            .field("model", &self.model)
            .field("kind", &self.kind)
            .finish_non_exhaustive()
    }
}

#[async_trait]
impl LlmClient for OpenAiClient {
    async fn complete(&self, req: &ChatRequest) -> Result<ChatResponse, ProviderError> {
        let model = effective_model(&self.model, req);
        let key = self.key()?;
        let url = format!("{}/chat/completions", self.base_url);

        let mut rb = self.http.post(&url).json(&self.build_body(req, model, false));
        if !key.is_empty() {
            rb = rb.bearer_auth(key);
        }
        let resp = rb.send().await.map_err(ProviderError::from_reqwest)?;
        let resp = ensure_success(resp, "openai-compatible", model).await?;
        let data: Value = resp.json().await.map_err(ProviderError::json_error)?;
        Self::parse_response(&data, model)
    }

    async fn stream(&self, req: &ChatRequest) -> Result<EventStream, ProviderError> {
        let model = effective_model(&self.model, req);
        let key = self.key()?;
        let url = format!("{}/chat/completions", self.base_url);

        let mut rb = self
            .http
            .post(&url)
            .header("Accept", "text/event-stream")
            .json(&self.build_body(req, model, true));
        if !key.is_empty() {
            rb = rb.bearer_auth(key);
        }
        let resp = rb.send().await.map_err(ProviderError::from_reqwest)?;
        let resp = ensure_success(resp, "openai-compatible", model).await?;
        Ok(drive_stream(resp, OpenAiStreamAccum::new()))
    }

    fn provider_id(&self) -> &'static str {
        self.kind.id()
    }

    fn default_model(&self) -> &str {
        &self.model
    }
}

/// One tool call being assembled from `tool_calls` deltas keyed by `index`.
#[derive(Debug, Default)]
struct PendingCall {
    id: Option<String>,
    name: String,
    arguments: String,
}

/// Streaming accumulator for the OpenAI chat-completions SSE dialect.
///
/// Tool calls arrive as deltas keyed by `index` (name/id on the first chunk,
/// JSON `arguments` text split across chunks); they are accumulated here and
/// emitted once as a single `tool_calls` event so consumers never see partial
/// JSON. Mirrors `_iter_sse_events` in `llm.py`.
#[derive(Debug, Default)]
pub(crate) struct OpenAiStreamAccum {
    queue: EventQueue,
    calls: BTreeMap<u32, PendingCall>,
    thought: String,
    usage: Option<Usage>,
}

impl OpenAiStreamAccum {
    pub fn new() -> Self {
        Self::default()
    }
}

impl StreamAccumulator for OpenAiStreamAccum {
    fn feed(&mut self, event: &SseEvent) {
        if self.queue.is_done() || self.queue.has_failed() {
            return;
        }
        let data = event.data.trim();
        if data.is_empty() || data == "[DONE]" {
            return;
        }
        let chunk: Value = match serde_json::from_str(data) {
            Ok(v) => v,
            Err(_) => return, // keep-alive / malformed frames are not fatal
        };
        if let Some(err) = chunk.get("error") {
            self.queue.push_error(ProviderError::StreamError(format!(
                "LLM stream error: {}",
                trunc(&err.to_string(), 400)
            )));
            return;
        }

        let delta = chunk
            .get("choices")
            .and_then(|c| c.as_array())
            .and_then(|c| c.first())
            .and_then(|c| c.get("delta"))
            .cloned()
            .unwrap_or(Value::Null);

        if let Some(text) = delta.get("content").and_then(|v| v.as_str()) {
            if !text.is_empty() {
                self.queue
                    .push_event(StreamEvent::Token { text: text.to_string() });
            }
        }
        if let Some(rc) = delta.get("reasoning_content").and_then(|v| v.as_str()) {
            if !rc.is_empty() {
                self.thought.push_str(rc);
            }
        }
        if let Some(tcs) = delta.get("tool_calls").and_then(|v| v.as_array()) {
            for tc in tcs {
                let index = tc.get("index").and_then(|v| v.as_u64()).unwrap_or(0) as u32;
                let slot = self.calls.entry(index).or_default();
                if let Some(id) = tc.get("id").and_then(|v| v.as_str()) {
                    slot.id = Some(id.to_string());
                }
                if let Some(func) = tc.get("function") {
                    if let Some(name) = func.get("name").and_then(|v| v.as_str()) {
                        if !name.is_empty() {
                            slot.name = name.to_string();
                        }
                    }
                    if let Some(args) = func.get("arguments").and_then(|v| v.as_str()) {
                        slot.arguments.push_str(args);
                    }
                }
            }
        }
        if let Some(u) = chunk.get("usage") {
            self.usage = Some(OpenAiClient::parse_usage(u));
        }
    }

    fn finish(&mut self) {
        if self.queue.is_done() || self.queue.has_failed() {
            return;
        }
        // BTreeMap iteration is sorted by index, like Python's sorted().
        let mut calls = Vec::new();
        for (index, slot) in std::mem::take(&mut self.calls) {
            let raw = if slot.arguments.is_empty() {
                None
            } else {
                Some(Value::String(slot.arguments))
            };
            let (arguments, args_error) = parse_tool_arguments(raw.as_ref());
            calls.push(ToolCall {
                id: slot.id.unwrap_or_else(|| format!("call_{index}")),
                name: slot.name,
                arguments,
                args_error,
            });
        }
        if !calls.is_empty() {
            self.queue.push_event(StreamEvent::ToolCalls { calls });
        }
        self.queue.push_event(StreamEvent::Done {
            usage: self.usage.take(),
            thought: none_if_blank(std::mem::take(&mut self.thought)),
        });
        self.queue.set_finished();
    }

    fn pop(&mut self) -> Option<Result<String, ProviderError>> {
        self.queue.pop()
    }

    fn is_done(&self) -> bool {
        self.queue.is_done()
    }

    fn push_transport_error(&mut self, e: ProviderError) {
        self.queue.push_error(e);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::providers::types::scrub_thought;
    use crate::providers::Message;

    fn client() -> OpenAiClient {
        OpenAiClient::new(
            "https://api.openai.com/v1",
            Some("sk-test".to_string()),
            "gpt-4o",
            Duration::from_secs(120),
            ProviderKind::OpenAI,
        )
        .unwrap()
    }

    fn sample_request() -> ChatRequest {
        let mut req = ChatRequest::new(
            "gpt-4o",
            vec![
                Message::system("You are helpful."),
                Message::user("What is the weather?"),
            ],
        );
        req.tools = Some(vec![crate::providers::ToolSpec {
            name: "get_weather".to_string(),
            description: Some("Get the weather".to_string()),
            parameters: serde_json::json!({"type": "object", "properties": {}}),
        }]);
        req
    }

    #[test]
    fn build_body_matches_python_payload() {
        let c = client();
        let body = c.build_body(&sample_request(), "gpt-4o", false);
        assert_eq!(body["model"], serde_json::json!("gpt-4o"));
        assert_eq!(body["stream"], serde_json::json!(false));
        assert!(body.get("stream_options").is_none());
        assert_eq!(body["messages"][0]["role"], serde_json::json!("system"));
        let tool = &body["tools"][0];
        assert_eq!(tool["type"], serde_json::json!("function"));
        assert_eq!(tool["function"]["name"], serde_json::json!("get_weather"));
    }

    #[test]
    fn build_body_stream_requests_usage() {
        let c = client();
        let body = c.build_body(&sample_request(), "gpt-4o", true);
        assert_eq!(body["stream"], serde_json::json!(true));
        assert_eq!(
            body["stream_options"]["include_usage"],
            serde_json::json!(true)
        );
    }

    #[test]
    fn build_body_omits_empty_optionals() {
        let c = client();
        let req = ChatRequest::new("m", vec![Message::user("hi")]);
        let body = c.build_body(&req, "m", false);
        assert!(body.get("tools").is_none());
        assert!(body.get("max_tokens").is_none());
        assert!(body.get("temperature").is_none());
    }

    #[test]
    fn parse_response_extracts_everything() {
        let data = serde_json::json!({
            "id": "chatcmpl-1",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "<thought>Need weather data</thought>The weather is sunny.",
                    "tool_calls": [{
                        "id": "call_abc", "type": "function",
                        "function": {"name": "get_weather", "arguments": "{\"city\": \"Hanoi\"}"}
                    }]
                },
                "finish_reason": "tool_calls"
            }],
            "usage": {
                "prompt_tokens": 20, "completion_tokens": 35, "total_tokens": 55,
                "prompt_tokens_details": {"cached_tokens": 4},
                "completion_tokens_details": {"reasoning_tokens": 12}
            }
        });
        let resp = OpenAiClient::parse_response(&data, "gpt-4o").unwrap();
        assert_eq!(resp.content, "The weather is sunny.");
        assert_eq!(resp.thought.as_deref(), Some("Need weather data"));
        assert_eq!(resp.tool_calls.len(), 1);
        assert_eq!(resp.tool_calls[0].id, "call_abc");
        assert_eq!(resp.tool_calls[0].name, "get_weather");
        assert_eq!(
            resp.tool_calls[0].arguments,
            serde_json::json!({"city": "Hanoi"})
        );
        assert_eq!(resp.tool_calls[0].args_error, None);
        let usage = resp.usage.unwrap();
        assert_eq!(usage.input_tokens, 20);
        assert_eq!(usage.output_tokens, 35);
        assert_eq!(usage.cache_read_tokens, 4);
        assert_eq!(usage.reasoning_tokens, 12);
        assert_eq!(resp.finish_reason.as_deref(), Some("tool_calls"));
    }

    #[test]
    fn parse_response_reasoning_content_fallback() {
        let data = serde_json::json!({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "42",
                    "reasoning_content": "  because reasons  "
                },
                "finish_reason": "stop"
            }]
        });
        let resp = OpenAiClient::parse_response(&data, "deepseek-reasoner").unwrap();
        assert_eq!(resp.content, "42");
        assert_eq!(resp.thought.as_deref(), Some("because reasons"));
    }

    #[test]
    fn parse_response_bad_tool_args_captured() {
        let data = serde_json::json!({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_1", "type": "function",
                        "function": {"name": "f", "arguments": "{bad json"}
                    }]
                },
                "finish_reason": "tool_calls"
            }]
        });
        let resp = OpenAiClient::parse_response(&data, "m").unwrap();
        assert_eq!(resp.tool_calls[0].arguments, serde_json::json!({}));
        assert!(resp.tool_calls[0].args_error.is_some());
    }

    #[test]
    fn parse_response_no_choices_is_parse_error() {
        let data = serde_json::json!({"choices": []});
        assert!(matches!(
            OpenAiClient::parse_response(&data, "m"),
            Err(ProviderError::Parse(_))
        ));
    }

    #[test]
    fn parse_response_error_object_is_api_error() {
        let data = serde_json::json!({"error": {"message": "bad key", "type": "auth"}});
        match OpenAiClient::parse_response(&data, "m") {
            Err(ProviderError::Api { status, message }) => {
                assert_eq!(status, 200);
                assert!(message.contains("bad key"));
            }
            other => panic!("expected Api error, got {other:?}"),
        }
    }

    #[test]
    fn scrub_thought_unit() {
        let (t, c) = scrub_thought("a <thought>x</thought> b");
        assert_eq!(t.as_deref(), Some("x"));
        assert_eq!(c, "a  b");
    }

    fn feed_all(accum: &mut OpenAiStreamAccum, payloads: &[&str]) -> Vec<String> {
        for p in payloads {
            accum.feed(&SseEvent {
                event: "message".to_string(),
                data: p.to_string(),
            });
        }
        accum.finish();
        let mut out = Vec::new();
        while let Some(item) = accum.pop() {
            out.push(item.unwrap());
        }
        out
    }

    #[test]
    fn sse_accum_reassembles_tool_call_deltas() {
        let mut accum = OpenAiStreamAccum::new();
        let out = feed_all(
            &mut accum,
            &[
                r#"{"choices":[{"index":0,"delta":{"role":"assistant","content":"The weather "},"finish_reason":null}]}"#,
                r#"{"choices":[{"index":0,"delta":{"content":"in Hanoi."},"finish_reason":null}]}"#,
                r#"{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_abc","type":"function","function":{"name":"get_weather","arguments":""}}]},"finish_reason":null}]}"#,
                r#"{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"city\":"}}]},"finish_reason":null}]}"#,
                r#"{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":" \"Hanoi\"}"}}]},"finish_reason":null}]}"#,
                r#"{"choices":[],"usage":{"prompt_tokens":20,"completion_tokens":35,"prompt_tokens_details":{"cached_tokens":2}}}"#,
                "[DONE]",
                "not json at all",
                "",
            ],
        );
        assert_eq!(out.len(), 4);
        let t0 = StreamEvent::from_json(&out[0]).unwrap();
        assert_eq!(
            t0,
            StreamEvent::Token {
                text: "The weather ".to_string()
            }
        );
        let t1 = StreamEvent::from_json(&out[1]).unwrap();
        assert_eq!(
            t1,
            StreamEvent::Token {
                text: "in Hanoi.".to_string()
            }
        );
        match StreamEvent::from_json(&out[2]).unwrap() {
            StreamEvent::ToolCalls { calls } => {
                assert_eq!(calls.len(), 1);
                assert_eq!(calls[0].id, "call_abc");
                assert_eq!(calls[0].name, "get_weather");
                assert_eq!(calls[0].arguments, serde_json::json!({"city": "Hanoi"}));
            }
            other => panic!("expected ToolCalls, got {other:?}"),
        }
        match StreamEvent::from_json(&out[3]).unwrap() {
            StreamEvent::Done { usage, thought } => {
                let u = usage.unwrap();
                assert_eq!(u.input_tokens, 20);
                assert_eq!(u.output_tokens, 35);
                assert_eq!(u.cache_read_tokens, 2);
                assert_eq!(thought, None);
            }
            other => panic!("expected Done, got {other:?}"),
        }
    }

    #[test]
    fn sse_accum_collects_reasoning_and_surfaces_error_payload() {
        let mut accum = OpenAiStreamAccum::new();
        accum.feed(&SseEvent {
            event: "message".to_string(),
            data: r#"{"choices":[{"delta":{"reasoning_content":"let me think"}}]}"#.to_string(),
        });
        accum.finish();
        let mut out = Vec::new();
        while let Some(item) = accum.pop() {
            out.push(item.unwrap());
        }
        assert_eq!(out.len(), 1); // only Done (no tokens, no tool calls)
        match StreamEvent::from_json(&out[0]).unwrap() {
            StreamEvent::Done { thought, .. } => {
                assert_eq!(thought.as_deref(), Some("let me think"))
            }
            other => panic!("expected Done, got {other:?}"),
        }

        let mut bad = OpenAiStreamAccum::new();
        bad.feed(&SseEvent {
            event: "message".to_string(),
            data: r#"{"error":{"message":"overloaded"}}"#.to_string(),
        });
        bad.finish();
        match bad.pop() {
            Some(Err(ProviderError::StreamError(msg))) => assert!(msg.contains("overloaded")),
            other => panic!("expected StreamError, got {other:?}"),
        }
        assert!(bad.is_done());
    }

    #[test]
    fn client_debug_redacts_key() {
        let dbg = format!("{:?}", client());
        assert!(dbg.contains("[redacted]"));
        assert!(!dbg.contains("sk-test"));
    }

    #[tokio::test]
    async fn missing_key_fails_before_network() {
        let c = OpenAiClient::new(
            "https://api.openai.com/v1",
            None,
            "gpt-4o",
            Duration::from_secs(5),
            ProviderKind::OpenAI,
        )
        .unwrap();
        let req = ChatRequest::new("gpt-4o", vec![Message::user("hi")]);
        assert!(matches!(
            c.complete(&req).await,
            Err(ProviderError::MissingKey(_))
        ));
        assert!(matches!(
            c.stream(&req).await,
            Err(ProviderError::MissingKey(_))
        ));
    }

    #[tokio::test]
    async fn keyless_client_builds_requests_without_auth_header() {
        // Ollama-style: no key required; the key check must not fire.
        // We don't assert the header itself (no network); we assert the
        // request path is reached by checking it fails on connection, not key.
        let c = OpenAiClient::new(
            "http://127.0.0.1:9",
            None,
            "llama3.3",
            Duration::from_secs(2),
            ProviderKind::Ollama,
        )
        .unwrap();
        let req = ChatRequest::new("", vec![Message::user("hi")]);
        // Empty req.model falls back to the client default.
        assert_eq!(super::effective_model("llama3.3", &req), "llama3.3");
        // Connection refused -> transport error, NOT MissingKey.
        assert!(matches!(
            c.complete(&req).await,
            Err(ProviderError::Http(_))
        ));
    }
}
