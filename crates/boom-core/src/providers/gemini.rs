//! Google Gemini provider client.
//!
//! `POST {base_url}/v1beta/models/{model}:generateContent` (streaming:
//! `:streamGenerateContent` with `alt=sse`). The API key travels as a `key`
//! query parameter — it is added with reqwest's `.query()` (properly encoded)
//! and never appears in logs or error messages.
//!
//! Message conversion:
//!
//! - [`Role::System`] messages become `systemInstruction`.
//! - [`Role::Assistant`] becomes `role: "model"`; assistant
//!   [`Message::tool_calls`] become `functionCall` parts so a returned
//!   [`ChatResponse`] can be fed back as history.
//! - [`Role::Tool`] becomes a `user` turn with a `functionResponse` part; the
//!   function name is taken from [`Message::tool_call_id`].
//!
//! Streaming chunks are plain `data:` lines, each a full
//! `GenerateContentResponse` JSON object; text parts yield `token` events and
//! `functionCall` parts are collected and emitted once as `tool_calls`.

use super::{
    drive_stream, effective_model, ensure_success, get_u64, trunc, ChatRequest, ChatResponse,
    EventQueue, EventStream, LlmClient, Message, ProviderError, Role, SseEvent,
    StreamAccumulator, StreamEvent, ToolCall, Usage,
};
use async_trait::async_trait;
use serde_json::Value;
use std::time::Duration;

pub struct GeminiClient {
    base_url: String,
    api_key: Option<String>,
    model: String,
    http: reqwest::Client,
}

impl GeminiClient {
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
                "No API key configured for Google Gemini. Provide api_key in ProviderConfig."
                    .to_string(),
            )
        })
    }

    /// Convert [`Message`]s to `(system_instruction, contents)`.
    /// `pub(crate)` so tests can assert on the exact wire format.
    pub(crate) fn convert_messages(messages: &[Message]) -> (Option<String>, Vec<Value>) {
        let mut system_parts: Vec<String> = Vec::new();
        let mut out: Vec<Value> = Vec::new();

        // Merge consecutive same-role turns into one `contents` entry.
        let mut push = |role: &str, part: Value| {
            if let Some(last) = out.last_mut() {
                if last.get("role").and_then(|r| r.as_str()) == Some(role) {
                    if let Some(parts) = last.get_mut("parts").and_then(|p| p.as_array_mut()) {
                        parts.push(part);
                        return;
                    }
                }
            }
            out.push(serde_json::json!({"role": role, "parts": [part]}));
        };

        for m in messages {
            match m.role {
                Role::System => system_parts.push(m.content.clone()),
                Role::User => push("user", serde_json::json!({"text": m.content})),
                Role::Assistant => {
                    if !m.content.is_empty() {
                        push("model", serde_json::json!({"text": m.content}));
                    }
                    for tc in &m.tool_calls {
                        push(
                            "model",
                            serde_json::json!({"functionCall": {"name": tc.name, "args": tc.arguments}}),
                        );
                    }
                }
                Role::Tool => {
                    // tool_call_id carries the function name here.
                    let name = m.tool_call_id.clone().unwrap_or_default();
                    push(
                        "user",
                        serde_json::json!({
                            "functionResponse": {
                                "name": name,
                                "response": {"result": m.content},
                            }
                        }),
                    );
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

    /// Build the `generateContent` JSON body. `pub(crate)` for unit tests.
    pub(crate) fn build_body(&self, req: &ChatRequest) -> Value {
        let (system, contents) = Self::convert_messages(&req.messages);
        let mut body = serde_json::json!({ "contents": contents });
        if let Some(system) = system {
            body["systemInstruction"] = serde_json::json!({"parts": [{"text": system}]});
        }
        let mut gen = serde_json::Map::new();
        if let Some(max_tokens) = req.max_tokens {
            gen.insert("maxOutputTokens".to_string(), Value::from(max_tokens));
        }
        if let Some(temperature) = req.temperature {
            gen.insert("temperature".to_string(), serde_json::json!(temperature));
        }
        if !gen.is_empty() {
            body["generationConfig"] = Value::Object(gen);
        }
        if let Some(tools) = req.tools.as_deref().filter(|t| !t.is_empty()) {
            let decls: Vec<Value> = tools
                .iter()
                .map(|t| {
                    serde_json::json!({
                        "name": t.name,
                        "description": t.description.clone().unwrap_or_default(),
                        "parameters": t.parameters,
                    })
                })
                .collect();
            body["tools"] = serde_json::json!([{"functionDeclarations": decls}]);
        }
        body
    }

    /// Parse a non-streaming `generateContent` body. `pub(crate)` for tests.
    pub(crate) fn parse_response(data: &Value) -> Result<ChatResponse, ProviderError> {
        if let Some(err) = data.get("error") {
            let fallback = err.to_string();
            let msg = err
                .get("message")
                .and_then(|m| m.as_str())
                .unwrap_or(fallback.as_str());
            return Err(ProviderError::Api {
                status: err.get("code").and_then(|c| c.as_u64()).unwrap_or(200) as u16,
                message: format!("Gemini error: {}", trunc(msg, 400)),
            });
        }
        let candidate = data
            .get("candidates")
            .and_then(|c| c.as_array())
            .and_then(|c| c.first());
        let candidate = match candidate {
            Some(c) => c,
            None => {
                if let Some(reason) = data
                    .get("promptFeedback")
                    .and_then(|p| p.get("blockReason"))
                    .and_then(|r| r.as_str())
                {
                    return Err(ProviderError::Api {
                        status: 200,
                        message: format!("Gemini blocked the prompt: {reason}"),
                    });
                }
                return Err(ProviderError::Parse(
                    "Gemini returned no candidates".to_string(),
                ));
            }
        };

        let mut content = String::new();
        let mut tool_calls = Vec::new();
        if let Some(parts) = candidate
            .get("content")
            .and_then(|c| c.get("parts"))
            .and_then(|p| p.as_array())
        {
            for part in parts {
                if let Some(t) = part.get("text").and_then(|t| t.as_str()) {
                    content.push_str(t);
                }
                if let Some(fc) = part.get("functionCall") {
                    tool_calls.push(ToolCall {
                        id: format!("call_{}", tool_calls.len()),
                        name: fc
                            .get("name")
                            .and_then(|n| n.as_str())
                            .unwrap_or("")
                            .to_string(),
                        arguments: fc
                            .get("args")
                            .cloned()
                            .unwrap_or_else(|| Value::Object(Default::default())),
                        args_error: None,
                    });
                }
            }
        }

        let usage = data.get("usageMetadata").map(|u| Usage {
            input_tokens: get_u64(u, "promptTokenCount"),
            output_tokens: get_u64(u, "candidatesTokenCount"),
            cache_read_tokens: get_u64(u, "cachedContentTokenCount"),
            cache_write_tokens: 0,
            reasoning_tokens: 0,
        });

        Ok(ChatResponse {
            content,
            thought: None, // generateContent has no reasoning channel
            tool_calls,
            usage,
            finish_reason: candidate
                .get("finishReason")
                .and_then(|r| r.as_str())
                .map(str::to_string),
        })
    }

    fn url(&self, model: &str, action: &str) -> String {
        format!(
            "{}/v1beta/models/{model}:{action}",
            self.base_url,
            model = model,
            action = action
        )
    }
}

// The API key must never leak through Debug / logging.
impl std::fmt::Debug for GeminiClient {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("GeminiClient")
            .field("base_url", &self.base_url)
            .field("api_key", &self.api_key.as_ref().map(|_| "[redacted]"))
            .field("model", &self.model)
            .finish_non_exhaustive()
    }
}

#[async_trait]
impl LlmClient for GeminiClient {
    async fn complete(&self, req: &ChatRequest) -> Result<ChatResponse, ProviderError> {
        let model = effective_model(&self.model, req);
        let resp = self
            .http
            .post(self.url(model, "generateContent"))
            .query(&[("key", self.key()?)])
            .json(&self.build_body(req))
            .send()
            .await
            .map_err(ProviderError::from_reqwest)?;
        let resp = ensure_success(resp, "gemini", model).await?;
        let data: Value = resp.json().await.map_err(ProviderError::json_error)?;
        Self::parse_response(&data)
    }

    async fn stream(&self, req: &ChatRequest) -> Result<EventStream, ProviderError> {
        let model = effective_model(&self.model, req);
        let resp = self
            .http
            .post(self.url(model, "streamGenerateContent"))
            .query(&[("key", self.key()?), ("alt", "sse")])
            .header("Accept", "text/event-stream")
            .json(&self.build_body(req))
            .send()
            .await
            .map_err(ProviderError::from_reqwest)?;
        let resp = ensure_success(resp, "gemini", model).await?;
        Ok(drive_stream(resp, GeminiStreamAccum::new()))
    }

    fn provider_id(&self) -> &'static str {
        "google"
    }

    fn default_model(&self) -> &str {
        &self.model
    }
}

/// Streaming accumulator for Gemini's `alt=sse` dialect: each `data:` line is
/// a complete `GenerateContentResponse` JSON object.
#[derive(Debug, Default)]
pub(crate) struct GeminiStreamAccum {
    queue: EventQueue,
    calls: Vec<ToolCall>,
    usage: Option<Usage>,
}

impl GeminiStreamAccum {
    pub fn new() -> Self {
        Self::default()
    }
}

impl StreamAccumulator for GeminiStreamAccum {
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
            Err(_) => return, // malformed frames are not fatal
        };
        if let Some(err) = chunk.get("error") {
            let fallback = err.to_string();
            let msg = err
                .get("message")
                .and_then(|m| m.as_str())
                .unwrap_or(fallback.as_str());
            self.queue.push_error(ProviderError::StreamError(format!(
                "Gemini stream error: {}",
                trunc(msg, 400)
            )));
            return;
        }
        let candidates = chunk
            .get("candidates")
            .and_then(|c| c.as_array())
            .cloned()
            .unwrap_or_default();
        for cand in &candidates {
            if let Some(parts) = cand
                .get("content")
                .and_then(|c| c.get("parts"))
                .and_then(|p| p.as_array())
            {
                for part in parts {
                    if let Some(t) = part.get("text").and_then(|t| t.as_str()) {
                        if !t.is_empty() {
                            self.queue.push_event(StreamEvent::Token {
                                text: t.to_string(),
                            });
                        }
                    }
                    if let Some(fc) = part.get("functionCall") {
                        self.calls.push(ToolCall {
                            id: format!("call_{}", self.calls.len()),
                            name: fc
                                .get("name")
                                .and_then(|n| n.as_str())
                                .unwrap_or("")
                                .to_string(),
                            arguments: fc
                                .get("args")
                                .cloned()
                                .unwrap_or_else(|| Value::Object(Default::default())),
                            args_error: None,
                        });
                    }
                }
            }
        }
        if let Some(u) = chunk.get("usageMetadata") {
            self.usage = Some(Usage {
                input_tokens: get_u64(u, "promptTokenCount"),
                output_tokens: get_u64(u, "candidatesTokenCount"),
                cache_read_tokens: get_u64(u, "cachedContentTokenCount"),
                cache_write_tokens: 0,
                reasoning_tokens: 0,
            });
        }
    }

    fn finish(&mut self) {
        if self.queue.is_done() || self.queue.has_failed() {
            return;
        }
        if !self.calls.is_empty() {
            self.queue.push_event(StreamEvent::ToolCalls {
                calls: std::mem::take(&mut self.calls),
            });
        }
        self.queue.push_event(StreamEvent::Done {
            usage: self.usage.take(),
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

    fn client() -> GeminiClient {
        GeminiClient::new(
            "https://generativelanguage.googleapis.com",
            Some("AIza-test".to_string()),
            "gemini-2.5-flash",
            Duration::from_secs(120),
        )
        .unwrap()
    }

    #[test]
    fn build_body_converts_messages() {
        let c = client();
        let mut req = ChatRequest::new(
            "gemini-2.5-flash",
            vec![
                Message::system("Be helpful."),
                Message::user("Weather?"),
                Message::assistant("Checking..."),
                Message::tool_result("get_weather", "{\"temp\": 22}"),
            ],
        );
        req.max_tokens = Some(1024);
        req.temperature = Some(0.5);
        let body = c.build_body(&req);
        assert_eq!(
            body["systemInstruction"]["parts"][0]["text"],
            serde_json::json!("Be helpful.")
        );
        let contents = body["contents"].as_array().unwrap();
        // user, model, then tool_result merges into the user turn.
        assert_eq!(contents.len(), 3);
        assert_eq!(contents[0]["role"], serde_json::json!("user"));
        assert_eq!(contents[1]["role"], serde_json::json!("model"));
        assert_eq!(contents[2]["role"], serde_json::json!("user"));
        assert!(contents[2]["parts"][0].get("functionResponse").is_some());
        assert_eq!(
            contents[2]["parts"][0]["functionResponse"]["name"],
            serde_json::json!("get_weather")
        );
        assert_eq!(body["generationConfig"]["maxOutputTokens"], serde_json::json!(1024));
        assert_eq!(body["generationConfig"]["temperature"], serde_json::json!(0.5));
    }

    #[test]
    fn build_body_maps_tools_to_function_declarations() {
        let c = client();
        let mut req = ChatRequest::new("m", vec![Message::user("hi")]);
        req.tools = Some(vec![crate::providers::ToolSpec {
            name: "get_weather".to_string(),
            description: Some("Get weather".to_string()),
            parameters: serde_json::json!({"type": "object"}),
        }]);
        let body = c.build_body(&req);
        let decl = &body["tools"][0]["functionDeclarations"][0];
        assert_eq!(decl["name"], serde_json::json!("get_weather"));
        assert_eq!(
            decl["parameters"],
            serde_json::json!({"type": "object"})
        );
    }

    #[test]
    fn parse_response_reads_text_and_function_call() {
        let data = serde_json::json!({
            "candidates": [{
                "content": {
                    "parts": [
                        {"text": "The weather "},
                        {"functionCall": {"name": "get_weather", "args": {"city": "Hanoi"}}}
                    ],
                    "role": "model"
                },
                "finishReason": "STOP"
            }],
            "usageMetadata": {
                "promptTokenCount": 20,
                "candidatesTokenCount": 35,
                "totalTokenCount": 55,
                "cachedContentTokenCount": 4
            }
        });
        let resp = GeminiClient::parse_response(&data).unwrap();
        assert_eq!(resp.content, "The weather ");
        assert_eq!(resp.tool_calls.len(), 1);
        assert_eq!(resp.tool_calls[0].name, "get_weather");
        assert_eq!(
            resp.tool_calls[0].arguments,
            serde_json::json!({"city": "Hanoi"})
        );
        let u = resp.usage.unwrap();
        assert_eq!(u.input_tokens, 20);
        assert_eq!(u.output_tokens, 35);
        assert_eq!(u.cache_read_tokens, 4);
        assert_eq!(resp.finish_reason.as_deref(), Some("STOP"));
    }

    #[test]
    fn parse_response_blocked_prompt_is_api_error() {
        let data = serde_json::json!({
            "promptFeedback": {"blockReason": "SAFETY"}
        });
        match GeminiClient::parse_response(&data) {
            Err(ProviderError::Api { message, .. }) => assert!(message.contains("SAFETY")),
            other => panic!("expected Api error, got {other:?}"),
        }
    }

    #[test]
    fn parse_response_no_candidates_is_parse_error() {
        let data = serde_json::json!({});
        assert!(matches!(
            GeminiClient::parse_response(&data),
            Err(ProviderError::Parse(_))
        ));
    }

    fn feed_all(accum: &mut GeminiStreamAccum, payloads: &[&str]) -> Vec<String> {
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
    fn sse_accum_collects_text_and_function_calls() {
        let mut accum = GeminiStreamAccum::new();
        let out = feed_all(
            &mut accum,
            &[
                r#"{"candidates":[{"content":{"parts":[{"text":"The weather "}],"role":"model"}}]}"#,
                r#"{"candidates":[{"content":{"parts":[{"text":"in Hanoi."}],"role":"model"}}]}"#,
                r#"{"candidates":[{"content":{"parts":[{"functionCall":{"name":"get_weather","args":{"city":"Hanoi"}}}],"role":"model"},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":20,"candidatesTokenCount":35,"totalTokenCount":55}}"#,
            ],
        );
        assert_eq!(out.len(), 4);
        assert_eq!(
            StreamEvent::from_json(&out[0]).unwrap(),
            StreamEvent::Token { text: "The weather ".to_string() }
        );
        assert_eq!(
            StreamEvent::from_json(&out[1]).unwrap(),
            StreamEvent::Token { text: "in Hanoi.".to_string() }
        );
        match StreamEvent::from_json(&out[2]).unwrap() {
            StreamEvent::ToolCalls { calls } => {
                assert_eq!(calls.len(), 1);
                assert_eq!(calls[0].name, "get_weather");
                assert_eq!(calls[0].arguments, serde_json::json!({"city": "Hanoi"}));
            }
            other => panic!("expected ToolCalls, got {other:?}"),
        }
        match StreamEvent::from_json(&out[3]).unwrap() {
            StreamEvent::Done { usage, .. } => {
                let u = usage.unwrap();
                assert_eq!(u.input_tokens, 20);
                assert_eq!(u.output_tokens, 35);
            }
            other => panic!("expected Done, got {other:?}"),
        }
    }

    #[test]
    fn client_debug_redacts_key() {
        let dbg = format!("{:?}", client());
        assert!(dbg.contains("[redacted]"));
        assert!(!dbg.contains("AIza-test"));
    }

    #[tokio::test]
    async fn missing_key_fails_before_network() {
        let c = GeminiClient::new(
            "https://generativelanguage.googleapis.com",
            None,
            "gemini-2.5-flash",
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
