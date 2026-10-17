//! Shared request/response types for the LLM provider clients.
//!
//! These mirror the Pydantic models in `agent/src/providers/llm.py`
//! (`LLMResponse`, `LLMUsage`, `ToolCallSpec`) with two small, deliberate
//! extensions on [`Message`] — see its documentation.

use serde::{Deserialize, Serialize};

/// Chat message role, serialized lowercase on the wire.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    System,
    User,
    Assistant,
    Tool,
}

/// A single chat message.
///
/// `role` + `content` are the wire format shared by every provider. The two
/// optional fields exist so multi-turn tool-calling round-trips survive
/// re-sending history:
///
/// - `tool_calls`: tool calls the assistant made in this message
///   (OpenAI `tool_calls`, Anthropic `tool_use` blocks, Gemini `functionCall`
///   parts). Serialized in each provider's native shape by the client.
/// - `tool_call_id`: correlates a [`Role::Tool`] message with the call it
///   answers. Carries the provider's tool-call id (OpenAI / Anthropic) or the
///   function name (Gemini `functionResponse`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Message {
    pub role: Role,
    pub content: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub tool_calls: Vec<ToolCall>,
}

impl Message {
    pub fn new(role: Role, content: impl Into<String>) -> Self {
        Self {
            role,
            content: content.into(),
            tool_call_id: None,
            tool_calls: Vec::new(),
        }
    }

    pub fn system(content: impl Into<String>) -> Self {
        Self::new(Role::System, content)
    }

    pub fn user(content: impl Into<String>) -> Self {
        Self::new(Role::User, content)
    }

    pub fn assistant(content: impl Into<String>) -> Self {
        Self::new(Role::Assistant, content)
    }

    /// A tool-result message correlated with a previous tool call.
    pub fn tool_result(tool_call_id: impl Into<String>, content: impl Into<String>) -> Self {
        Self {
            role: Role::Tool,
            content: content.into(),
            tool_call_id: Some(tool_call_id.into()),
            tool_calls: Vec::new(),
        }
    }
}

/// A tool the model may call: name, optional description, and a JSON Schema
/// for its arguments object.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolSpec {
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    pub parameters: serde_json::Value,
}

/// One tool call requested by the model.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ToolCall {
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub arguments: serde_json::Value,
    /// Set when the arguments JSON could not be parsed; `arguments` is `{}` then.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub args_error: Option<String>,
}

/// Token usage for one turn. Mirrors `LLMUsage` in `llm.py`.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Usage {
    #[serde(default)]
    pub input_tokens: u64,
    #[serde(default)]
    pub output_tokens: u64,
    #[serde(default)]
    pub cache_read_tokens: u64,
    #[serde(default)]
    pub cache_write_tokens: u64,
    #[serde(default)]
    pub reasoning_tokens: u64,
}

/// A non-streaming chat completion request.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatRequest {
    /// Model id. When empty, the client falls back to its configured default.
    pub model: String,
    pub messages: Vec<Message>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_tokens: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub temperature: Option<f32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tools: Option<Vec<ToolSpec>>,
}

impl ChatRequest {
    pub fn new(model: impl Into<String>, messages: Vec<Message>) -> Self {
        Self {
            model: model.into(),
            messages,
            max_tokens: None,
            temperature: None,
            tools: None,
        }
    }
}

/// A non-streaming chat completion response. Mirrors `LLMResponse` in `llm.py`.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ChatResponse {
    #[serde(default)]
    pub content: String,
    /// Chain-of-thought extracted from `<thought>...</thought>` tags or a
    /// provider-native reasoning field. Never mixed into `content`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thought: Option<String>,
    #[serde(default)]
    pub tool_calls: Vec<ToolCall>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub usage: Option<Usage>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub finish_reason: Option<String>,
}

/// One streaming event, mirroring the event protocol of the Python client
/// (`{"type": "token" | "tool_calls" | "done", ...}`).
///
/// On the wire each event is a JSON string — that is what
/// [`LlmClient::stream`](crate::providers::LlmClient::stream) yields, so a
/// `Stream<Item = String>` can still carry tool calls, usage, and thought.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum StreamEvent {
    /// A slice of generated text.
    Token { text: String },
    /// A slice of reasoning text (kept out of `content`, like `thought`).
    Reasoning { text: String },
    /// Accumulated tool calls, emitted once at the end of the turn.
    ToolCalls { calls: Vec<ToolCall> },
    /// Terminal event: final token usage and any extracted thought.
    Done {
        usage: Option<Usage>,
        thought: Option<String>,
    },
}

impl StreamEvent {
    /// Serialize to the JSON string carried by the stream.
    pub fn to_json_string(&self) -> String {
        // Serializing our own enum cannot fail.
        serde_json::to_string(self).expect("StreamEvent serialization is infallible")
    }

    /// Parse a JSON string produced by [`StreamEvent::to_json_string`].
    pub fn from_json(s: &str) -> Result<Self, crate::providers::ProviderError> {
        serde_json::from_str(s)
            .map_err(|e| crate::providers::ProviderError::Parse(format!("invalid stream event: {e}")))
    }
}

/// Extract `<thought>...</thought>` blocks from model output.
///
/// Returns the first block's trimmed text (if any) plus the text with every
/// thought block removed. Mirrors `LLMProvider.scrub_thought` in `llm.py`.
pub(crate) fn scrub_thought(raw: &str) -> (Option<String>, String) {
    const OPEN: &str = "<thought>";
    const CLOSE: &str = "</thought>";

    let mut thought: Option<String> = None;
    let mut clean = String::with_capacity(raw.len());
    let mut rest = raw;

    while let Some(start) = rest.find(OPEN) {
        let after_open = start + OPEN.len();
        match rest[after_open..].find(CLOSE) {
            Some(rel_end) => {
                let end = after_open + rel_end;
                if thought.is_none() {
                    let t = rest[after_open..end].trim();
                    if !t.is_empty() {
                        thought = Some(t.to_string());
                    }
                }
                clean.push_str(&rest[..start]);
                rest = &rest[end + CLOSE.len()..];
            }
            None => break, // unterminated tag: leave the remainder untouched
        }
    }
    clean.push_str(rest);
    (thought, clean.trim().to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn role_serializes_lowercase() {
        assert_eq!(serde_json::to_string(&Role::System).unwrap(), "\"system\"");
        assert_eq!(serde_json::to_string(&Role::Assistant).unwrap(), "\"assistant\"");
        assert_eq!(serde_json::to_string(&Role::Tool).unwrap(), "\"tool\"");
        let r: Role = serde_json::from_str("\"user\"").unwrap();
        assert_eq!(r, Role::User);
    }

    #[test]
    fn message_omits_optional_fields_by_default() {
        let v = serde_json::to_value(Message::user("hi")).unwrap();
        assert_eq!(v, serde_json::json!({"role": "user", "content": "hi"}));
    }

    #[test]
    fn message_keeps_tool_correlation_fields() {
        let m = Message::tool_result("call_1", "{\"ok\":true}");
        let v = serde_json::to_value(&m).unwrap();
        assert_eq!(v["tool_call_id"], serde_json::json!("call_1"));
        assert_eq!(v["role"], serde_json::json!("tool"));
    }

    #[test]
    fn scrub_thought_extracts_first_and_strips_all() {
        let (thought, clean) = scrub_thought(
            "<thought>plan A</thought>Hello <thought>plan B</thought> world",
        );
        assert_eq!(thought.as_deref(), Some("plan A"));
        assert_eq!(clean, "Hello  world");
    }

    #[test]
    fn scrub_thought_without_tags_returns_input() {
        let (thought, clean) = scrub_thought("plain answer");
        assert_eq!(thought, None);
        assert_eq!(clean, "plain answer");
    }

    #[test]
    fn scrub_thought_ignores_unterminated_tag() {
        let (thought, clean) = scrub_thought("answer <thought>oops");
        assert_eq!(thought, None);
        assert_eq!(clean, "answer <thought>oops");
    }

    #[test]
    fn stream_event_json_roundtrip() {
        let ev = StreamEvent::Token {
            text: "hello".to_string(),
        };
        let s = ev.to_json_string();
        assert_eq!(s, r#"{"type":"token","text":"hello"}"#);
        assert_eq!(StreamEvent::from_json(&s).unwrap(), ev);

        let done = StreamEvent::Done {
            usage: Some(Usage {
                input_tokens: 10,
                output_tokens: 20,
                ..Usage::default()
            }),
            thought: Some("hmm".to_string()),
        };
        let s = done.to_json_string();
        assert_eq!(StreamEvent::from_json(&s).unwrap(), done);

        assert!(StreamEvent::from_json("not json").is_err());
    }
}
