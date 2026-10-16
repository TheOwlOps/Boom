//! Shared data types for the harness, plus the two seams the rest of the
//! core implements later:
//!
//! - [`LlmClient`] — model access (core-providers). The required method is
//!   exactly `complete(&self, messages) -> String`; a provided
//!   `complete_with_tools` layers structured tool-call support on top so a
//!   plain text-only provider still drives the ReAct tool loop, while a
//!   provider with native function calling can override it.
//! - [`ToolExecutor`] — tool execution (core-tools).

use std::collections::HashMap;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use thiserror::Error;

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/// Every failure the harness can report. No panics cross a harness boundary:
///
/// - tool failures become error *observations* inside the turn,
/// - turn-level failures (`max_turns`, token budget) become
///   [`TurnResult`] with `success: false`,
/// - transport failures (the model or executor itself blew up) are returned
///   as `Err(AgentError)` so the caller (`Brain`) can record them honestly.
#[derive(Debug, Error)]
pub enum AgentError {
    /// The model call itself failed (network, auth, provider error).
    #[error("LLM call failed: {0}")]
    Llm(String),
    /// The tool executor itself failed (not a tool-level error observation).
    #[error("tool execution failed: {0}")]
    Tool(String),
    /// A plan / graph was structurally unusable (cycle, unknown dependency,
    /// unparseable planner output).
    #[error("invalid plan: {0}")]
    InvalidPlan(String),
    /// Filesystem or other I/O failure (trajectory export, ...).
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
}

// ---------------------------------------------------------------------------
// Messages
// ---------------------------------------------------------------------------

/// One chat message in the live context.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Message {
    /// `"system"`, `"user"`, `"assistant"` or `"tool"`.
    pub role: String,
    pub content: String,
    /// Set on assistant messages that request tool calls.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub tool_calls: Option<Vec<ToolCall>>,
    /// Set on tool messages: the id of the call being answered.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub tool_call_id: Option<String>,
}

impl Message {
    pub fn system(content: impl Into<String>) -> Self {
        Self { role: "system".into(), content: content.into(), tool_calls: None, tool_call_id: None }
    }
    pub fn user(content: impl Into<String>) -> Self {
        Self { role: "user".into(), content: content.into(), tool_calls: None, tool_call_id: None }
    }
    pub fn assistant(content: impl Into<String>) -> Self {
        Self { role: "assistant".into(), content: content.into(), tool_calls: None, tool_call_id: None }
    }
    pub fn tool(tool_call_id: impl Into<String>, content: impl Into<String>) -> Self {
        Self {
            role: "tool".into(),
            content: content.into(),
            tool_calls: None,
            tool_call_id: Some(tool_call_id.into()),
        }
    }
}

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

/// A single tool invocation requested by the model.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolCall {
    pub id: String,
    pub name: String,
    /// JSON object of arguments (Python: `Dict[str, Any]`).
    pub arguments: Value,
}

/// The observation produced by executing a [`ToolCall`].
#[derive(Debug, Clone)]
pub struct ToolResult {
    pub tool_call_id: String,
    pub name: String,
    /// Observation text fed back to the model.
    pub output: String,
    /// True when the observation is an error (unknown tool, refusal,
    /// malformed arguments, tool raised).
    pub is_error: bool,
}

/// A tool call as parsed out of one assistant turn.
///
/// `args_error` is `Some` when the model emitted malformed JSON arguments:
/// such a call never reaches a tool; the loop turns it into an error
/// observation telling the model to retry with valid JSON (mirrors the
/// Python `args_error` path).
#[derive(Debug, Clone)]
pub struct ParsedToolCall {
    pub id: String,
    pub name: String,
    pub arguments: Value,
    pub args_error: Option<String>,
}

/// A tool's schema as handed to the model.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolSchema {
    pub name: String,
    pub description: String,
    /// JSON-Schema-ish parameter spec (opaque to the harness).
    pub parameters: Value,
}

// ---------------------------------------------------------------------------
// Model responses
// ---------------------------------------------------------------------------

/// Token usage reported by one model call.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Usage {
    pub input_tokens: u64,
    pub output_tokens: u64,
}

/// Structured model response: the Python `LLMResponse` shape
/// (`content`, `thought`, `tool_calls`, `usage`, `finish_reason`).
#[derive(Debug, Clone)]
pub struct LlmResponse {
    pub content: String,
    /// The model's private reasoning, if the provider separates it
    /// (Python strips `<thought>...</thought>` blocks into this).
    pub thought: Option<String>,
    pub tool_calls: Vec<ParsedToolCall>,
    pub usage: Option<Usage>,
    pub finish_reason: String,
}

// ---------------------------------------------------------------------------
// Turn results
// ---------------------------------------------------------------------------

/// One think → act → observe step of a turn.
#[derive(Debug, Clone)]
pub struct ReactStep {
    pub thought: String,
    pub action: Option<ToolCall>,
    pub observation: Option<String>,
}

/// Outcome of one [`crate::harness::react::ReActLoop::run_turn`].
#[derive(Debug, Clone)]
pub struct TurnResult {
    pub success: bool,
    pub final_answer: String,
    pub steps: Vec<ReactStep>,
    pub input_tokens: u64,
    pub output_tokens: u64,
    /// `"ok"`, `"max_turns"`, `"budget_exhausted"` or `"early_exit:<reason>"`.
    pub stop_reason: String,
    /// `Some` when the turn stopped without a final answer.
    pub error: Option<String>,
}

// ---------------------------------------------------------------------------
// Seams
// ---------------------------------------------------------------------------

/// Model access. Implemented later by core-providers.
///
/// The one required method returns raw text, exactly as specified. The
/// provided `complete_with_tools` method is what the ReAct loop actually
/// calls: its default implementation renders the tool catalog into the
/// prompt and parses tool calls back out of fenced ` ```toolcall ` JSON
/// blocks, so even a text-only provider can drive the tool loop. A
/// provider with native function calling should override it and return
/// [`LlmResponse`] built from the native tool-call payload.
#[async_trait]
pub trait LlmClient: Send + Sync {
    /// Raw text completion over the message history.
    async fn complete(&self, messages: &[Message]) -> Result<String, AgentError>;

    /// Structured completion with tool-call support.
    ///
    /// Default: text round-trip through [`parse_toolcall_fences`].
    /// Override for native function calling.
    async fn complete_with_tools(
        &self,
        messages: &[Message],
        tools: &[ToolSchema],
    ) -> Result<LlmResponse, AgentError> {
        let mut outgoing: Vec<Message> = messages.to_vec();
        if !tools.is_empty() {
            outgoing.push(Message::system(render_tool_catalog(tools)));
        }
        let text = self.complete(&outgoing).await?;
        let (content, tool_calls) = parse_toolcall_fences(&text);
        Ok(LlmResponse {
            finish_reason: if tool_calls.is_empty() { "stop" } else { "tool_calls" }.to_string(),
            content,
            thought: None,
            tool_calls,
            usage: None,
        })
    }
}

/// Tool execution. Implemented later by core-tools.
#[async_trait]
pub trait ToolExecutor: Send + Sync {
    /// Execute one validated tool call and return its observation.
    /// A tool-level failure should be returned as an `Ok(ToolResult)` with
    /// `is_error: true`; `Err` is reserved for the executor itself breaking.
    async fn execute(&self, call: &ToolCall) -> Result<ToolResult, AgentError>;
}

// ---------------------------------------------------------------------------
// Text tool-call protocol (default `complete_with_tools` implementation)
// ---------------------------------------------------------------------------

/// Render the tool catalog as prompt text for text-only providers.
///
/// Each tool becomes one line `name: description`, followed by a short
/// protocol description. Kept compact: the [`crate::harness::react`]
/// `ToolSelector` already prunes this list before it reaches the model.
fn render_tool_catalog(tools: &[ToolSchema]) -> String {
    let mut out = String::from(
        "You have access to these tools. To call one or more tools, emit one fenced block per call:\n\
         ```toolcall\n{\"name\": \"tool_name\", \"arguments\": {\"arg\": \"value\"}}\n```\n\
         Anything outside toolcall fences is treated as your reply text.\n\nTOOLS:\n",
    );
    for t in tools {
        out.push_str(&format!("- {}: {}\n", t.name, t.description));
    }
    out
}

/// Split raw model text into reply content + parsed tool calls.
///
/// Tool calls are written as Markdown-style fenced blocks whose info string
/// is `toolcall` and whose body is a single JSON object with a string field
/// `name` and an object field `arguments`. Unparseable payloads (or a
/// missing `name`) become [`ParsedToolCall`]s with `args_error` set — the
/// loop turns those into "retry with valid JSON" observations instead of
/// calling a tool.
pub fn parse_toolcall_fences(text: &str) -> (String, Vec<ParsedToolCall>) {
    let mut calls: Vec<ParsedToolCall> = Vec::new();
    let mut content = String::new();
    let mut rest = text;
    let mut n = 0usize;

    while let Some(start) = rest.find("```toolcall") {
        content.push_str(&rest[..start]);
        let after_open = &rest[start + "```toolcall".len()..];
        let (payload, tail) = match after_open.find("```") {
            Some(end) => (&after_open[..end], &after_open[end + 3..]),
            None => (after_open, ""),
        };
        n += 1;
        let payload = payload.trim();
        match serde_json::from_str::<Value>(payload) {
            Ok(Value::Object(map)) => {
                let name = map.get("name").and_then(Value::as_str).unwrap_or("").to_string();
                let arguments = map.get("arguments").cloned().unwrap_or(Value::Null);
                if name.is_empty() || !arguments.is_object() {
                    calls.push(ParsedToolCall {
                        id: format!("call_{n}"),
                        name,
                        arguments: Value::Object(Default::default()),
                        args_error: Some(format!("malformed toolcall payload: {payload}")),
                    });
                } else {
                    calls.push(ParsedToolCall { id: format!("call_{n}"), name, arguments, args_error: None });
                }
            }
            _ => calls.push(ParsedToolCall {
                id: format!("call_{n}"),
                name: String::new(),
                arguments: Value::Object(Default::default()),
                args_error: Some(format!("malformed toolcall payload: {payload}")),
            }),
        }
        rest = tail;
    }
    content.push_str(rest);
    (content.trim().to_string(), calls)
}

/// Convenience: name → schema index over a schema slice (first wins).
pub fn schema_index(schemas: &[ToolSchema]) -> HashMap<&str, &ToolSchema> {
    let mut index = HashMap::new();
    for s in schemas {
        index.entry(s.name.as_str()).or_insert(s);
    }
    index
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parse_fences_extracts_calls_and_keeps_prose() {
        let text = "Let me read that file.\n```toolcall\n{\"name\": \"read_file\", \"arguments\": {\"path\": \"a.rs\"}}\n```\nDone.";
        let (content, calls) = parse_toolcall_fences(text);
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].name, "read_file");
        assert_eq!(calls[0].arguments, json!({"path": "a.rs"}));
        assert!(calls[0].args_error.is_none());
        assert!(content.contains("Let me read that file."));
        assert!(content.contains("Done."));
        assert!(!content.contains("toolcall"));
    }

    #[test]
    fn parse_fences_marks_malformed_payload() {
        let text = "```toolcall\n{\"name\": \"read_file\", \"arguments\": {oops}\n```";
        let (_, calls) = parse_toolcall_fences(text);
        assert_eq!(calls.len(), 1);
        assert!(calls[0].args_error.is_some());
    }

    #[test]
    fn parse_fences_empty_text_yields_no_calls() {
        let (content, calls) = parse_toolcall_fences("Just a plain answer.");
        assert!(calls.is_empty());
        assert_eq!(content, "Just a plain answer.");
    }

    #[test]
    fn render_catalog_lists_every_tool() {
        let schemas = vec![
            ToolSchema { name: "a".into(), description: "does a".into(), parameters: json!({}) },
            ToolSchema { name: "b".into(), description: "does b".into(), parameters: json!({}) },
        ];
        let catalog = render_tool_catalog(&schemas);
        assert!(catalog.contains("- a: does a"));
        assert!(catalog.contains("- b: does b"));
        assert!(catalog.contains("```toolcall"));
    }
}
