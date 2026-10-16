//! Anthropic provider client.
//!
//! `POST {base_url}/v1/messages` with the `x-api-key` header and
//! `anthropic-version: 2023-06-01`. There is no Anthropic client in the Python
//! codebase (only OpenAI-compatible), so the message conversion here follows
//! the Messages API contract:
//!
//! - [`Role::System`] messages become the top-level `system` parameter.
//! - [`Role::Tool`] messages become `user` turns with `tool_result` blocks,
//!   correlated via [`Message::tool_call_id`].
//! - Assistant [`Message::tool_calls`] become `tool_use` content blocks, so a
//!   returned [`ChatResponse`] can be fed back as history for multi-turn tool
//!   use.
//!
//! Streaming uses Anthropic's named SSE events (`message_start`,
//! `content_block_delta`, `message_delta`, ...); `input_json_delta` chunks are
//! accumulated per content-block index and emitted once as `tool_calls`.

use super::{
    drive_stream, effective_model, ensure_success, get_u64, none_if_blank, trunc,
    ChatRequest, ChatResponse, EventQueue, EventStream, LlmClient, Message, ProviderError,
    Role, SseEvent, StreamAccumulator, StreamEvent, ToolCall, Usage,
};
use async_trait::async_trait;
use serde_json::Value;
use std::{collections::BTreeMap, time::Duration};

/// Version header required by the Anthropic API.
const ANTHROPIC_VERSION: &str = "2023-06-01";

/// Fallback when the request does not set `max_tokens` (the API requires it).
const DEFAULT_MAX_TOKENS: u32 = 4096;

pub struct AnthropicClient {
    base_url: String,
    api_key: Option<String>,
    model: String,
    http: reqwest::Client,
}

impl AnthropicClient {
    pub fn new(
        base_url: impl Into<String>,
        api_key: Option<String>,
        model: impl Into<String>,
        timeout: Duration,
    ) -> Result<Self, ProviderError> {
        let http = reqwest::Client::builder()
            .timeout(timeout)
            .build()
            .map_err(ProviderError::from_reqwest)?;
        Ok(Self {
            base_url: base_url.into().trim_end_matches('/').to_string(),
            api_key: api_key.filter(|k| !k.trim().is_empty()),
            model: model.into(),
            http,
        })
    }

    fn key(&self) -> Result<&str, ProviderError> {
        self.api_key.as_deref().ok_or_else(|| {
            ProviderError::MissingKey(
                "No API key configured for Anthropic. Provide api_key in ProviderConfig."
                    .to_string(),
            )
        })
    }

    fn text_block(text: &str) -> Value {
        serde_json::json!({"type": "text", "text": text})
    }

    /// Append a message, merging with the previous one when the role matches
    /// (the API requires strict user/assistant alternation).
    fn push_message(out: &mut Vec<Value>, role: &str, blocks: Vec<Value>) {
        let blocks: Vec<Value> = blocks
            .into_iter()
            .filter(|b| {
                // The API rejects empty text blocks.
                !(b.get("type").and_then(|t| t.as_str()) == Some("text")
                    && b
                        .get("text")
                        .and_then(|t| t.as_str())
                        .map(str::is_empty)
                        .unwrap_or(true))
            })
            .collect();
        if blocks.is_empty() {
            return;
        }
        if let Some(last) = out.last_mut() {
            if last.get("role").and_then(|r| r.as_str()) == Some(role) {
                if let Some(content) = last.get_mut("content").and_then(|c| c.as_array_mut()) {
                    content.extend(blocks);
                    return;
                }
            }
        }
        out.push(serde_json::json!({"role": role, "content": blocks}));
    }

    /// Convert [`Message`]s to `(system, messages)` for the Messages API.
    /// `pub(crate)` so tests can assert on the exact wire format.
    pub(crate) fn convert_messages(messages: &[Message]) -> (Option<String>, Vec<Value>) {
        let mut system_parts: Vec<String> = Vec::new();
        let mut out: Vec<Value> = Vec::new();
        for m in messages {
            match m.role {
                Role::System => system_parts.push(m.content.clone()),
                Role::User => Self::push_message(&mut out, "user", vec![Self::text_block(&m.content)]),
                Role::Assistant => {
                    let mut blocks = Vec::new();
                    if !m.content.is_empty() {
                        blocks.push(Self::text_block(&m.content));
                    }
                    for tc in &m.tool_calls {
                        blocks.push(serde_json::json!({
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }));
                    }
                    Self::push_message(&mut out, "assistant", blocks);
                }
                Role::Tool => {
                    // tool_call_id carries the tool_use id from the assistant turn.
                    let block = serde_json::json!({
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id.clone().unwrap_or_default(),
                        "content": m.content,
                    });
                    Self::push_message(&mut out, "user", vec![block]);
                }
            }
        }
        let system = if system_parts.is_empty() {
            None
        } else {
            Some(system_parts.join("\n\n"))
        };
        (system, out)
    }

    /// Build the `/v1/messages` JSON body. `pub(crate)` for unit tests.
    pub(crate) fn build_body(&self, req: &ChatRequest, model: &str, stream: bool) -> Value {
        let (system, messages) = Self::convert_messages(&req.messages);
        let mut body = serde_json::json!({
            "model": model,
            "max_tokens": req.max_tokens.unwrap_or(DEFAULT_MAX_TOKENS),
            "messages": messages,
            "stream": stream,
        });
        if let Some(system) = system {
            body["system"] = Value::String(system);
        }
        if let Some(temperature) = req.temperature {
            body["temperature"] = serde_json::json!(temperature);
        }
        if let Some(tools) = req.tools.as_deref().filter(|t| !t.is_empty()) {
            let mapped: Vec<Value> = tools
                .iter()
                .map(|t| {
                    serde_json::json!({
                        "name": t.name,
                        "description": t.description.clone().unwrap_or_default(),
                        "input_schema": t.parameters,
                    })
                })
                .collect();
            body["tools"] = Value::Array(mapped);
        }
        body
    }

    /// Parse a non-streaming message body. `pub(crate)` for unit tests.
    pub(crate) fn parse_response(data: &Value) -> Result<ChatResponse, ProviderError> {
        if data.get("type").and_then(|v| v.as_str()) == Some("error") {
            let msg = data
                .get("error")
                .and_then(|e| e.get("message"))
                .and_then(|v| v.as_str())
                .unwrap_or("unknown error");
            return Err(ProviderError::Api {
                status: 200,
                message: format!("Anthropic error: {}", trunc(msg, 400)),
            });
        }
        let mut content = String::new();
        let mut thought = String::new();
        let mut tool_calls = Vec::new();
        if let Some(blocks) = data.get("content").and_then(|v| v.as_array()) {
            for block in blocks {
                match block.get("type").and_then(|v| v.as_str()) {
                    Some("text") => {
                        if let Some(t) = block.get("text").and_then(|v| v.as_str()) {
                            content.push_str(t);
                        }
                    }
                    Some("thinking") => {
                        // Extended-thinking blocks map onto `thought`, like
                        // `<thought>` tags in the OpenAI client.
                        if let Some(t) = block.get("thinking").and_then(|v| v.as_str()) {
                            thought.push_str(t);
                        }
                    }
                    Some("tool_use") => tool_calls.push(ToolCall {
                        id: block
                            .get("id")
                            .and_then(|v| v.as_str())
                            .map(str::to_string)
                            .unwrap_or_else(|| format!("call_{}", tool_calls.len())),
                        name: block
                            .get("name")
                            .and_then(|v| v.as_str())
                            .unwrap_or("")
                            .to_string(),
                        arguments: block
                            .get("input")
                            .cloned()
                            .unwrap_or_else(|| Value::Object(Default::default())),
                        args_error: None,
                    }),
                    _ => {}
                }
            }
        }
        let usage = data.get("usage").map(|u| Usage {
            input_tokens: get_u64(u, "input_tokens"),
            output_tokens: get_u64(u, "output_tokens"),
            cache_read_tokens: get_u64(u, "cache_read_input_tokens"),
            cache_write_tokens: get_u64(u, "cache_creation_input_tokens"),
            reasoning_tokens: 0,
        });
        Ok(ChatResponse {
            content,
            thought: none_if_blank(thought),
            tool_calls,
            usage,
            finish_reason: data
                .get("stop_reason")
                .and_then(|v| v.as_str())
                .map(str::to_string),
        })
    }
}

// The API key must never leak through Debug / logging.
impl std::fmt::Debug for AnthropicClient {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AnthropicClient")
            .field("base_url", &self.base_url)
            .field("api_key", &self.api_key.as_ref().map(|_| "[redacted]"))
            .field("model", &self.model)
            .finish_non_exhaustive()
    }
}

#[async_trait]
impl LlmClient for AnthropicClient {
    async fn complete(&self, req: &ChatRequest) -> Result<ChatResponse, ProviderError> {
        let model = effective_model(&self.model, req);
        let url = format!("{}/v1/messages", self.base_url);
        let resp = self
            .http
            .post(&url)
            .header("x-api-key", self.key()?)
            .header("anthropic-version", ANTHROPIC_VERSION)
            .json(&self.build_body(req, model, false))
            .send()
            .await
            .map_err(ProviderError::from_reqwest)?;
        let resp = ensure_success(resp, "anthropic", model).await?;
        let data: Value = resp.json().await.map_err(ProviderError::json_error)?;
        Self::parse_response(&data)
    }

    async fn stream(&self, req: &ChatRequest) -> Result<EventStream, ProviderError> {
        let model = effective_model(&self.model, req);
        let url = format!("{}/v1/messages", self.base_url);
        let resp = self
            .http
            .post(&url)
            .header("x-api-key", self.key()?)
            .header("anthropic-version", ANTHROPIC_VERSION)
            .header("Accept", "text/event-stream")
            .json(&self.build_body(req, model, true))
            .send()
            .await
            .map_err(ProviderError::from_reqwest)?;
        let resp = ensure_success(resp, "anthropic", model).await?;
        Ok(drive_stream(resp, AnthropicStreamAccum::new()))
    }

    fn provider_id(&self) -> &'static str {
        "anthropic"
    }

    fn default_model(&self) -> &str {
        &self.model
    }
}

/// A `tool_use` block being assembled from `input_json_delta` chunks.
#[derive(Debug, Default)]
struct PendingToolUse {
    id: Option<String>,
    name: String,
    json: String,
}

/// Streaming accumulator for Anthropic's named SSE events.
#[derive(Debug, Default)]
pub(crate) struct AnthropicStreamAccum {
    queue: EventQueue,
    tools: BTreeMap<u32, PendingToolUse>,
    usage: Usage,
}

impl AnthropicStreamAccum {
    pub fn new() -> Self {
        Self::default()
    }
}

impl StreamAccumulator for AnthropicStreamAccum {
    fn feed(&mut self, event: &SseEvent) {
        if self.queue.is_done() || self.queue.has_failed() {
            return;
        }
        let data = event.data.trim();
        if data.is_empty() {
            return;
        }
        let v: Value = match serde_json::from_str(data) {
            Ok(v) => v,
            Err(_) => return, // malformed frames are not fatal
        };
        match event.event.as_str() {
            "message_start" => {
                if let Some(u) = v.get("message").and_then(|m| m.get("usage")) {
                    self.usage.input_tokens = get_u64(u, "input_tokens");
                }
            }
            "content_block_start" => {
                let index = v.get("index").and_then(|i| i.as_u64()).unwrap_or(0) as u32;
                if let Some(block) = v.get("content_block") {
                    if block.get("type").and_then(|t| t.as_str()) == Some("tool_use") {
                        self.tools.insert(
                            index,
                            PendingToolUse {
                                id: block
                                    .get("id")
                                    .and_then(|i| i.as_str())
                                    .map(str::to_string),
                                name: block
                                    .get("name")
                                    .and_then(|n| n.as_str())
                                    .unwrap_or("")
                                    .to_string(),
                                json: String::new(),
                            },
                        );
                    }
                }
            }
            "content_block_delta" => {
                let index = v.get("index").and_then(|i| i.as_u64()).unwrap_or(0) as u32;
                if let Some(delta) = v.get("delta") {
                    match delta.get("type").and_then(|t| t.as_str()) {
                        Some("text_delta") => {
                            if let Some(t) = delta.get("text").and_then(|t| t.as_str()) {
                                if !t.is_empty() {
                                    self.queue.push_event(StreamEvent::Token {
                                        text: t.to_string(),
                                    });
                                }
                            }
                        }
                        Some("input_json_delta") => {
                            if let Some(p) = delta.get("partial_json").and_then(|p| p.as_str()) {
                                self.tools.entry(index).or_default().json.push_str(p);
                            }
                        }
                        _ => {}
                    }
                }
            }
            "message_delta" => {
                if let Some(u) = v.get("usage") {
                    let out = get_u64(u, "output_tokens");
                    if out > 0 {
                        self.usage.output_tokens = out;
                    }
                }
            }
            "error" => {
                let msg = v
                    .get("error")
                    .and_then(|e| e.get("message"))
                    .and_then(|m| m.as_str())
                    .unwrap_or("unknown stream error");
                self.queue.push_error(ProviderError::StreamError(format!(
                    "Anthropic stream error: {}",
                    trunc(msg, 400)
                )));
            }
            _ => {} // ping, message_stop, content_block_stop carry nothing
        }
    }

    fn finish(&mut self) {
        if self.queue.is_done() || self.queue.has_failed() {
            return;
        }
        // BTreeMap iteration is sorted by content-block index.
        let mut calls = Vec::new();
        for (index, slot) in std::mem::take(&mut self.tools) {
            let raw = if slot.json.is_empty() {
                None
            } else {
                Some(Value::String(slot.json))
            };
            let (arguments, args_error) =
                super::openai::parse_tool_arguments(raw.as_ref());
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
        let usage = std::mem::take(&mut self.usage);
        let has_usage = usage.input_tokens > 0 || usage.output_tokens > 0;
        self.queue.push_event(StreamEvent::Done {
            usage: has_usage.then_some(usage),
            thought: None,
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

    fn client() -> AnthropicClient {
        AnthropicClient::new(
            "https://api.anthropic.com",
            Some("sk-ant-test".to_string()),
            "claude-sonnet-4-5",
            Duration::from_secs(120),
        )
        .unwrap()
    }

    #[test]
    fn build_body_converts_messages() {
        let c = client();
        let req = ChatRequest::new(
            "claude-sonnet-4-5",
            vec![
                Message::system("Be concise."),
                Message::system("Second line."),
                Message::user("Weather?"),
                Message::tool_result("toolu_1", "{\"temp\": 22}"),
            ],
        );
        let body = c.build_body(&req, "claude-sonnet-4-5", false);
        assert_eq!(body["model"], serde_json::json!("claude-sonnet-4-5"));
        assert_eq!(body["max_tokens"], serde_json::json!(4096));
        assert_eq!(body["stream"], serde_json::json!(false));
        assert_eq!(
            body["system"],
            serde_json::json!("Be concise.\n\nSecond line.")
        );
        // user + tool_result merge into one user turn (alternation).
        assert_eq!(body["messages"].as_array().unwrap().len(), 1);
        let msg = &body["messages"][0];
        assert_eq!(msg["role"], serde_json::json!("user"));
        assert_eq!(msg["content"][0]["type"], serde_json::json!("text"));
        assert_eq!(
            msg["content"][1]["type"],
            serde_json::json!("tool_result")
        );
        assert_eq!(
            msg["content"][1]["tool_use_id"],
            serde_json::json!("toolu_1")
        );
    }

    #[test]
    fn build_body_maps_tools_to_input_schema() {
        let c = client();
        let mut req = ChatRequest::new("m", vec![Message::user("hi")]);
        req.tools = Some(vec![crate::providers::ToolSpec {
            name: "get_weather".to_string(),
            description: Some("Get weather".to_string()),
            parameters: serde_json::json!({"type": "object"}),
        }]);
        let body = c.build_body(&req, "m", false);
        assert_eq!(
            body["tools"][0]["input_schema"],
            serde_json::json!({"type": "object"})
        );
        assert_eq!(body["tools"][0]["name"], serde_json::json!("get_weather"));
    }

    #[test]
    fn parse_response_reads_blocks() {
        let data = serde_json::json!({
            "id": "msg_01",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "user wants weather"},
                {"type": "text", "text": "I'll check."},
                {"type": "tool_use", "id": "toolu_01", "name": "get_weather",
                 "input": {"city": "Hanoi"}}
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 25, "output_tokens": 40,
                      "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}
        });
        let resp = AnthropicClient::parse_response(&data).unwrap();
        assert_eq!(resp.content, "I'll check.");
        assert_eq!(resp.thought.as_deref(), Some("user wants weather"));
        assert_eq!(resp.tool_calls.len(), 1);
        assert_eq!(resp.tool_calls[0].id, "toolu_01");
        assert_eq!(
            resp.tool_calls[0].arguments,
            serde_json::json!({"city": "Hanoi"})
        );
        let u = resp.usage.unwrap();
        assert_eq!(u.input_tokens, 25);
        assert_eq!(u.output_tokens, 40);
        assert_eq!(u.cache_read_tokens, 10);
        assert_eq!(u.cache_write_tokens, 5);
        assert_eq!(resp.finish_reason.as_deref(), Some("tool_use"));
    }

    #[test]
    fn parse_response_error_type_is_api_error() {
        let data = serde_json::json!({
            "type": "error",
            "error": {"type": "authentication_error", "message": "invalid key"}
        });
        assert!(matches!(
            AnthropicClient::parse_response(&data),
            Err(ProviderError::Api { .. })
        ));
    }

    fn feed_all(accum: &mut AnthropicStreamAccum, events: &[(&str, &str)]) -> Vec<String> {
        for (name, data) in events {
            accum.feed(&SseEvent {
                event: name.to_string(),
                data: data.to_string(),
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
    fn sse_accum_reassembles_tool_use() {
        let mut accum = AnthropicStreamAccum::new();
        let out = feed_all(
            &mut accum,
            &[
                ("message_start", r#"{"type":"message_start","message":{"usage":{"input_tokens":25,"output_tokens":0}}}"#),
                ("content_block_delta", r#"{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"I'll check "}}"#),
                ("content_block_delta", r#"{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"the weather."}}"#),
                ("content_block_start", r#"{"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_01","name":"get_weather","input":{}}}"#),
                ("content_block_delta", r#"{"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\"city\":"}}"#),
                ("content_block_delta", r#"{"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":" \"Hanoi\"}"}}"#),
                ("message_delta", r#"{"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":40}}"#),
                ("message_stop", r#"{"type":"message_stop"}"#),
                ("ping", r#"{"type":"ping"}"#),
            ],
        );
        // 2 tokens + tool_calls + done
        assert_eq!(out.len(), 4);
        assert_eq!(
            StreamEvent::from_json(&out[0]).unwrap(),
            StreamEvent::Token { text: "I'll check ".to_string() }
        );
        assert_eq!(
            StreamEvent::from_json(&out[1]).unwrap(),
            StreamEvent::Token { text: "the weather.".to_string() }
        );
        match StreamEvent::from_json(&out[2]).unwrap() {
            StreamEvent::ToolCalls { calls } => {
                assert_eq!(calls.len(), 1);
                assert_eq!(calls[0].id, "toolu_01");
                assert_eq!(calls[0].name, "get_weather");
                assert_eq!(calls[0].arguments, serde_json::json!({"city": "Hanoi"}));
                assert_eq!(calls[0].args_error, None);
            }
            other => panic!("expected ToolCalls, got {other:?}"),
        }
        match StreamEvent::from_json(&out[3]).unwrap() {
            StreamEvent::Done { usage, .. } => {
                let u = usage.unwrap();
                assert_eq!(u.input_tokens, 25);
                assert_eq!(u.output_tokens, 40);
            }
            other => panic!("expected Done, got {other:?}"),
        }
    }

    #[test]
    fn client_debug_redacts_key() {
        let dbg = format!("{:?}", client());
        assert!(dbg.contains("[redacted]"));
        assert!(!dbg.contains("sk-ant-test"));
    }

    #[tokio::test]
    async fn missing_key_fails_before_network() {
        let c = AnthropicClient::new(
            "https://api.anthropic.com",
            None,
            "claude-sonnet-4-5",
            Duration::from_secs(5),
        )
        .unwrap();
        let req = ChatRequest::new("m", vec![Message::user("hi")]);
        assert!(matches!(
            c.complete(&req).await,
            Err(ProviderError::MissingKey(_))
        ));
        assert!(matches!(
            c.stream(&req).await,
            Err(ProviderError::MissingKey(_))
        ));
    }
}
