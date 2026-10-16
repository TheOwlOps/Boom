//! Adapter layer: wires the three independently-built subsystems together.
//!
//! The crate's `harness`, `providers`, `tools` and `server` modules were
//! written against *different* seams, so they cannot be plugged into each
//! other directly:
//!
//! | seam | consumer | producer |
//! |---|---|---|
//! | LLM calls | [`harness::types::LlmClient`][harness_llm] (`&[Message] -> String` / native tool calls) | [`providers::LlmClient`][prov_llm] (`ChatRequest -> ChatResponse`) |
//! | tool execution | [`harness::types::ToolExecutor`][exec] (`ToolCall -> ToolResult`) | [`tools::ToolRegistry`][core_reg] (dispatch by name) |
//! | HTTP tools API | [`server::state::ToolRegistry`][srv_reg] (`list_tools` / `run_tool`) | [`tools::ToolRegistry`][core_reg] |
//!
//! This module provides the adapters that close those gaps, plus
//! [`WiredHarness`] — a [`server::state::Harness`] implementation that runs
//! the real [`harness::react::ReActLoop`] over a real provider client and the
//! real tool registry, so the server can replace its no-op harness default
//! ([`server::defaults::NoHarness`]) with a working backend. (The tool
//! registry already ships the real built-in tools by default via
//! [`ServerToolRegistry`]; [`server::defaults::EmptyToolRegistry`] remains
//! as an opt-in no-op for tests that need the honest-empty state.)
//!
//! [harness_llm]: crate::harness::types::LlmClient
//! [prov_llm]: crate::providers::LlmClient
//! [exec]: crate::harness::types::ToolExecutor
//! [core_reg]: crate::tools::ToolRegistry
//! [srv_reg]: crate::server::state::ToolRegistry
//!
//! ## Usage
//!
//! ```no_run
//! use std::sync::Arc;
//! use boom_core::{
//!     providers::{self, ProviderConfig},
//!     server::AppState,
//!     tools::ToolRegistry,
//!     wiring::{ProviderLlmAdapter, ServerToolRegistry, WiredHarness},
//! };
//!
//! let tools = Arc::new(ToolRegistry::with_builtins());
//! let mut state = AppState::with_defaults();
//! state.tools = Arc::new(ServerToolRegistry::new(tools.clone()));
//! // WiredHarness needs a factory that builds a provider client per turn:
//! state.harness = Arc::new(WiredHarness::new(
//!     Arc::new(|| {
//!         providers::from_config(
//!             &ProviderConfig::new("openai", "gpt-4o").with_api_key("sk-..."),
//!         )
//!     }),
//!     "gpt-4o",
//!     tools,
//! ));
//! ```

use std::sync::Arc;

use async_trait::async_trait;
use serde_json::{Map, Value};

use crate::harness::react::ReActLoop;
use crate::harness::types as htypes;
use crate::harness::HarnessConfig;
use crate::providers;
use crate::server::state as sstate;
use crate::tools;

// ---------------------------------------------------------------------------
// Shared conversion helpers
// ---------------------------------------------------------------------------

/// Map a harness message onto the provider wire format.
///
/// The harness stores roles as plain strings (`"system"`, `"user"`,
/// `"assistant"`, `"tool"`). Unknown strings degrade to `user` rather than
/// failing the whole turn — the model still sees the content.
fn to_provider_message(m: &htypes::Message) -> providers::Message {
    let role = match m.role.as_str() {
        "system" => providers::Role::System,
        "user" => providers::Role::User,
        "assistant" => providers::Role::Assistant,
        "tool" => providers::Role::Tool,
        _ => providers::Role::User,
    };
    providers::Message {
        role,
        content: m.content.clone(),
        tool_call_id: m.tool_call_id.clone(),
        tool_calls: m
            .tool_calls
            .as_deref()
            .unwrap_or(&[])
            .iter()
            .map(|tc| providers::ToolCall {
                id: tc.id.clone(),
                name: tc.name.clone(),
                arguments: tc.arguments.clone(),
                args_error: None,
            })
            .collect(),
    }
}

/// Map a harness tool schema onto a provider tool spec (native function
/// calling). `parameters` is passed through opaquely.
fn to_tool_spec(schema: &htypes::ToolSchema) -> providers::ToolSpec {
    providers::ToolSpec {
        name: schema.name.clone(),
        description: Some(schema.description.clone()),
        parameters: schema.parameters.clone(),
    }
}

/// Map a provider completion onto the harness's structured response.
fn to_llm_response(resp: providers::ChatResponse) -> htypes::LlmResponse {
    let tool_calls = resp
        .tool_calls
        .into_iter()
        .map(|tc| htypes::ParsedToolCall {
            id: tc.id,
            name: tc.name,
            arguments: tc.arguments,
            args_error: tc.args_error,
        })
        .collect::<Vec<_>>();
    let finish_reason = resp.finish_reason.unwrap_or_else(|| {
        if tool_calls.is_empty() {
            "stop".to_string()
        } else {
            "tool_calls".to_string()
        }
    });
    htypes::LlmResponse {
        content: resp.content,
        thought: resp.thought,
        tool_calls,
        usage: resp.usage.map(|u| htypes::Usage {
            input_tokens: u.input_tokens,
            output_tokens: u.output_tokens,
        }),
        finish_reason,
    }
}

/// Turn a registry dispatch result into model-facing text. String results
/// are used verbatim; anything else is compact JSON. `null` becomes empty.
fn stringify_output(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        _ => serde_json::to_string(v).unwrap_or_default(),
    }
}

/// True when the dispatch result is the registry's error envelope
/// (`{"error": ...}`) — i.e. an unknown tool or a tool-level failure.
fn is_error_envelope(v: &Value) -> bool {
    matches!(
        v,
        Value::Object(map) if map.get("error").is_some_and(|e| !e.is_null())
    )
}

/// Extract the message from an error envelope, for `Err` paths.
fn envelope_message(v: &Value) -> String {
    match v {
        Value::Object(map) => map
            .get("error")
            .map(stringify_output)
            .unwrap_or_else(|| "tool failed".to_string()),
        _ => stringify_output(v),
    }
}

// ---------------------------------------------------------------------------
// 1. providers::LlmClient -> harness::types::LlmClient
// ---------------------------------------------------------------------------

/// Adapts any provider client ([`providers::LlmClient`]) to the harness's
/// [`htypes::LlmClient`] seam.
///
/// The required `complete` is a plain text round-trip; `complete_with_tools`
/// is overridden to use the provider's **native** function calling, so the
/// ReAct loop gets real tool calls instead of the fenced-` ```toolcall `
/// text protocol. Provider errors become [`htypes::AgentError::Llm`]; API
/// keys are never included (provider error types already scrub them).
pub struct ProviderLlmAdapter {
    client: Box<dyn providers::LlmClient>,
    /// Model id placed on every [`providers::ChatRequest`].
    model: String,
}

impl ProviderLlmAdapter {
    pub fn new(client: Box<dyn providers::LlmClient>, model: impl Into<String>) -> Self {
        Self {
            client,
            model: model.into(),
        }
    }

    /// The wrapped provider client (e.g. for `provider_id` / `stream`).
    pub fn inner(&self) -> &dyn providers::LlmClient {
        &*self.client
    }

    fn request(
        &self,
        messages: &[htypes::Message],
        schemas: &[htypes::ToolSchema],
    ) -> providers::ChatRequest {
        providers::ChatRequest {
            model: self.model.clone(),
            messages: messages.iter().map(to_provider_message).collect(),
            max_tokens: None,
            temperature: None,
            tools: if schemas.is_empty() {
                None
            } else {
                Some(schemas.iter().map(to_tool_spec).collect())
            },
        }
    }
}

#[async_trait]
impl htypes::LlmClient for ProviderLlmAdapter {
    async fn complete(&self, messages: &[htypes::Message]) -> Result<String, htypes::AgentError> {
        let req = self.request(messages, &[]);
        let resp = self
            .client
            .complete(&req)
            .await
            .map_err(|e| htypes::AgentError::Llm(e.to_string()))?;
        Ok(resp.content)
    }

    async fn complete_with_tools(
        &self,
        messages: &[htypes::Message],
        tools: &[htypes::ToolSchema],
    ) -> Result<htypes::LlmResponse, htypes::AgentError> {
        let req = self.request(messages, tools);
        let resp = self
            .client
            .complete(&req)
            .await
            .map_err(|e| htypes::AgentError::Llm(e.to_string()))?;
        Ok(to_llm_response(resp))
    }
}

// ---------------------------------------------------------------------------
// 2. tools::ToolRegistry -> harness::types::ToolExecutor
// ---------------------------------------------------------------------------

/// Adapts the core [`tools::ToolRegistry`] to the harness's
/// [`htypes::ToolExecutor`] seam, so [`ReActLoop`][crate::harness::react::ReActLoop]
/// can drive the real built-in tools.
///
/// Tool-level failures (including unknown tools) are returned as
/// [`htypes::ToolResult`] with `is_error: true` — the harness contract
/// reserves `Err` for the executor itself breaking, which a pure in-memory
/// dispatch cannot do.
#[derive(Clone)]
pub struct RegistryToolExecutor {
    registry: Arc<tools::ToolRegistry>,
}

impl RegistryToolExecutor {
    pub fn new(registry: Arc<tools::ToolRegistry>) -> Self {
        Self { registry }
    }
}

#[async_trait]
impl htypes::ToolExecutor for RegistryToolExecutor {
    async fn execute(&self, call: &htypes::ToolCall) -> Result<htypes::ToolResult, htypes::AgentError> {
        let out = self
            .registry
            .dispatch(&call.name, call.arguments.clone())
            .await;
        Ok(htypes::ToolResult {
            tool_call_id: call.id.clone(),
            name: call.name.clone(),
            output: stringify_output(&out),
            is_error: is_error_envelope(&out),
        })
    }
}

// ---------------------------------------------------------------------------
// 3. tools::ToolRegistry -> server::state::ToolRegistry
// ---------------------------------------------------------------------------

/// Adapts the core [`tools::ToolRegistry`] to the server's
/// [`sstate::ToolRegistry`] trait, so `/api/tools` and `/api/tools/run`
/// expose the real built-in tools instead of the empty default.
///
/// The `group` in [`sstate::ToolInfo`] is the toolset name the tool was
/// registered under (`"file"`, `"terminal"`, `"web"`, `"data"`, `"system"`,
/// or `""` for ad-hoc registrations).
#[derive(Clone)]
pub struct ServerToolRegistry {
    registry: Arc<tools::ToolRegistry>,
    /// tool name -> toolset name, resolved once at construction.
    groups: Arc<std::collections::HashMap<String, String>>,
}

impl ServerToolRegistry {
    pub fn new(registry: Arc<tools::ToolRegistry>) -> Self {
        let mut groups = std::collections::HashMap::new();
        for toolset in registry.toolset_names() {
            for name in registry.tools_in_toolset(&toolset) {
                groups.entry(name).or_insert(toolset.clone());
            }
        }
        Self {
            registry,
            groups: Arc::new(groups),
        }
    }

    /// The underlying core registry.
    pub fn inner(&self) -> &tools::ToolRegistry {
        &self.registry
    }
}

impl sstate::ToolRegistry for ServerToolRegistry {
    fn list_tools(&self) -> Vec<sstate::ToolInfo> {
        self.registry
            .tool_names()
            .into_iter()
            .filter_map(|name| {
                let tool = self.registry.get(&name)?;
                Some(sstate::ToolInfo {
                    group: self.groups.get(&name).cloned().unwrap_or_default(),
                    name,
                    description: tool.description().to_string(),
                })
            })
            .collect()
    }

    fn run_tool(
        &self,
        name: &str,
        args: &Map<String, Value>,
    ) -> sstate::BoxFuture<'_, Result<String, String>> {
        let registry = self.registry.clone();
        let name = name.to_string();
        let args = Value::Object(args.clone());
        Box::pin(async move {
            let out = registry.dispatch(&name, args).await;
            if is_error_envelope(&out) {
                Err(envelope_message(&out))
            } else {
                Ok(stringify_output(&out))
            }
        })
    }
}

// ---------------------------------------------------------------------------
// 4. WiredHarness: server::state::Harness over the real loop + tools
// ---------------------------------------------------------------------------

/// Build the tool schemas for one turn, honoring the bot's tool filter
/// ([`sstate::BotTools`]) — the server-side equivalent of Python's
/// `_resolve_bot_context` tool pruning.
fn schemas_for_registry(
    registry: &tools::ToolRegistry,
    filter: &sstate::BotTools,
) -> Vec<htypes::ToolSchema> {
    registry
        .tool_names()
        .into_iter()
        .filter_map(|name| {
            let tool = registry.get(&name)?;
            let allowed = match filter {
                sstate::BotTools::All => true,
                sstate::BotTools::Allow(list) => list.iter().any(|t| t == &name),
                sstate::BotTools::DenyOnly(list) => !list.iter().any(|t| t == &name),
            };
            if !allowed {
                return None;
            }
            Some(htypes::ToolSchema {
                name,
                description: tool.description().to_string(),
                parameters: tool.schema(),
            })
        })
        .collect()
}

/// A [`sstate::Harness`] that runs the real [`ReActLoop`] over a real
/// provider client and the real tool registry.
///
/// The provider client is built per turn by `make_client`, so API keys and
/// model selection stay the app layer's business (e.g. read from the
/// server's [`sstate::ProviderRegistry`]); this module never touches
/// config files or environment variables.
///
/// Notes / deliberate limits:
/// - Every strategy (`auto`, `react`, `dag`, `mass`) currently runs the
///   ReAct loop; the result is labelled `"react"` honestly. DAG/MASS wiring
///   is future work (see [`crate::harness::dag`]).
/// - Turns are stateless across calls: a fresh [`ReActLoop`] is built per
///   turn (the server already serializes turns with its turn lock, and
///   session restore is the storage worker's job).
/// - A missing/broken client factory maps to [`sstate::TurnError::Failed`];
///   the route answers 500 `"chat turn failed"` exactly like Python's
///   brain-error branch.
pub struct WiredHarness {
    make_client: Arc<
        dyn Fn() -> Result<Box<dyn providers::LlmClient>, providers::ProviderError>
            + Send
            + Sync,
    >,
    model: String,
    registry: Arc<tools::ToolRegistry>,
    max_turns: usize,
}

impl WiredHarness {
    pub fn new(
        make_client: Arc<
            dyn Fn() -> Result<Box<dyn providers::LlmClient>, providers::ProviderError>
                + Send
                + Sync,
        >,
        model: impl Into<String>,
        registry: Arc<tools::ToolRegistry>,
    ) -> Self {
        Self {
            make_client,
            model: model.into(),
            registry,
            max_turns: 6,
        }
    }

    /// Override the ReAct turn cap (default 6, mirroring the Python loop).
    pub fn with_max_turns(mut self, max_turns: usize) -> Self {
        self.max_turns = max_turns.max(1);
        self
    }

    /// Run one turn and map the harness result onto the server's contract.
    async fn run_react_turn(
        &self,
        req: sstate::TurnRequest,
    ) -> Result<sstate::TurnResult, sstate::TurnError> {
        let client = (self.make_client)()
            .map_err(|e| sstate::TurnError::Failed(format!("LLM provider unavailable: {e}")))?;
        let llm: Arc<dyn htypes::LlmClient> =
            Arc::new(ProviderLlmAdapter::new(client, self.model.clone()));
        let executor: Arc<dyn htypes::ToolExecutor> =
            Arc::new(RegistryToolExecutor::new(self.registry.clone()));

        let config = HarnessConfig {
            max_turns: self.max_turns,
            session_id: req.session_id.clone(),
            system_prompt: req.system_prompt.clone(),
            ..HarnessConfig::default()
        };

        let schemas = schemas_for_registry(&self.registry, &req.tools);
        let mut react_loop = ReActLoop::new(llm, executor, config, schemas);
        match react_loop.run_turn(&req.prompt).await {
            Ok(turn) => Ok(sstate::TurnResult {
                reply: turn.final_answer,
                strategy: "react".to_string(),
                errors: Map::new(),
            }),
            Err(e) => Err(sstate::TurnError::Failed(e.to_string())),
        }
    }
}

impl sstate::Harness for WiredHarness {
    fn is_configured(&self) -> bool {
        true
    }

    fn model_name(&self) -> Option<String> {
        Some(self.model.clone())
    }

    fn run_turn(&self, req: sstate::TurnRequest) -> sstate::BoxFuture<'_, Result<sstate::TurnResult, sstate::TurnError>> {
        Box::pin(async move { self.run_react_turn(req).await })
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::harness::types::LlmClient as _;
    use crate::harness::types::ToolExecutor as _;
    use crate::server::state::Harness as _;
    use crate::server::state::ToolRegistry as _;
    use futures_util::stream;

    // -- Fake provider client (providers::LlmClient) -----------------------

    struct ScriptProvider {
        content: String,
        tool_calls: Vec<providers::ToolCall>,
        fail: Option<String>,
    }

    #[async_trait]
    impl providers::LlmClient for ScriptProvider {
        async fn complete(
            &self,
            _req: &providers::ChatRequest,
        ) -> Result<providers::ChatResponse, providers::ProviderError> {
            if let Some(msg) = &self.fail {
                return Err(providers::ProviderError::Api {
                    status: 500,
                    message: msg.clone(),
                });
            }
            Ok(providers::ChatResponse {
                content: self.content.clone(),
                thought: None,
                tool_calls: self.tool_calls.clone(),
                usage: Some(providers::Usage {
                    input_tokens: 3,
                    output_tokens: 7,
                    ..providers::Usage::default()
                }),
                finish_reason: None,
            })
        }

        async fn stream(
            &self,
            _req: &providers::ChatRequest,
        ) -> Result<providers::EventStream, providers::ProviderError> {
            Ok(Box::pin(stream::empty()))
        }

        fn provider_id(&self) -> &'static str {
            "test"
        }

        fn default_model(&self) -> &str {
            "test-model"
        }
    }

    fn harness_messages() -> Vec<htypes::Message> {
        vec![
            htypes::Message::system("be helpful"),
            htypes::Message::user("hi"),
            htypes::Message::tool("call_1", "{\"ok\":true}"),
        ]
    }

    // -- 1. ProviderLlmAdapter --------------------------------------------

    #[tokio::test]
    async fn adapter_complete_returns_text() {
        let adapter = ProviderLlmAdapter::new(
            Box::new(ScriptProvider {
                content: "hello there".into(),
                tool_calls: vec![],
                fail: None,
            }),
            "test-model",
        );
        let out = adapter.complete(&harness_messages()).await.unwrap();
        assert_eq!(out, "hello there");
    }

    #[tokio::test]
    async fn adapter_complete_with_tools_uses_native_calls() {
        let adapter = ProviderLlmAdapter::new(
            Box::new(ScriptProvider {
                content: "running tool".into(),
                tool_calls: vec![providers::ToolCall {
                    id: "call_9".into(),
                    name: "date_now".into(),
                    arguments: serde_json::json!({}),
                    args_error: None,
                }],
                fail: None,
            }),
            "test-model",
        );
        let schemas = vec![htypes::ToolSchema {
            name: "date_now".into(),
            description: "current time".into(),
            parameters: serde_json::json!({"type": "object"}),
        }];
        let resp = adapter
            .complete_with_tools(&harness_messages(), &schemas)
            .await
            .unwrap();
        assert_eq!(resp.content, "running tool");
        assert_eq!(resp.finish_reason, "tool_calls");
        assert_eq!(resp.tool_calls.len(), 1);
        assert_eq!(resp.tool_calls[0].id, "call_9");
        assert_eq!(resp.tool_calls[0].name, "date_now");
        assert!(resp.tool_calls[0].args_error.is_none());
        let usage = resp.usage.unwrap();
        assert_eq!((usage.input_tokens, usage.output_tokens), (3, 7));
    }

    #[tokio::test]
    async fn adapter_maps_provider_errors_to_agent_error() {
        let adapter = ProviderLlmAdapter::new(
            Box::new(ScriptProvider {
                content: String::new(),
                tool_calls: vec![],
                fail: Some("boom".into()),
            }),
            "test-model",
        );
        let err = adapter.complete(&harness_messages()).await.unwrap_err();
        assert!(matches!(err, htypes::AgentError::Llm(_)));
        assert!(err.to_string().contains("boom"));
    }

    #[test]
    fn role_mapping_covers_all_harness_roles() {
        let cases = [
            ("system", providers::Role::System),
            ("user", providers::Role::User),
            ("assistant", providers::Role::Assistant),
            ("tool", providers::Role::Tool),
            ("weird", providers::Role::User), // unknown degrades, keeps content
        ];
        for (role, expect) in cases {
            let m = htypes::Message {
                role: role.into(),
                content: "c".into(),
                tool_calls: None,
                tool_call_id: None,
            };
            let pm = to_provider_message(&m);
            assert_eq!(pm.role, expect, "role {role}");
            assert_eq!(pm.content, "c");
        }
    }

    // -- 2. RegistryToolExecutor ------------------------------------------

    #[tokio::test]
    async fn executor_runs_real_tool() {
        let reg = Arc::new(tools::ToolRegistry::with_builtins());
        let exec = RegistryToolExecutor::new(reg);
        let call = htypes::ToolCall {
            id: "c1".into(),
            name: "uuid_new".into(),
            arguments: serde_json::json!({}),
        };
        let result = exec.execute(&call).await.unwrap();
        assert_eq!(result.tool_call_id, "c1");
        assert_eq!(result.name, "uuid_new");
        assert!(!result.is_error);
        // uuid_new returns {"uuid": "<36-char v4>"} as compact JSON.
        let parsed: Value = serde_json::from_str(&result.output).unwrap();
        assert_eq!(
            parsed["uuid"].as_str().map(str::len),
            Some(36),
            "output: {}",
            result.output
        );
    }

    #[tokio::test]
    async fn executor_unknown_tool_is_error_observation() {
        let reg = Arc::new(tools::ToolRegistry::with_builtins());
        let exec = RegistryToolExecutor::new(reg);
        let call = htypes::ToolCall {
            id: "c2".into(),
            name: "nope".into(),
            arguments: serde_json::json!({}),
        };
        let result = exec.execute(&call).await.unwrap();
        assert!(result.is_error);
        assert!(result.output.contains("not found"));
    }

    #[tokio::test]
    async fn executor_tool_failure_is_error_observation() {
        let reg = Arc::new(tools::ToolRegistry::with_builtins());
        let exec = RegistryToolExecutor::new(reg);
        // file_read on a missing path -> ToolError -> error envelope.
        let call = htypes::ToolCall {
            id: "c3".into(),
            name: "file_read".into(),
            arguments: serde_json::json!({"path": "/definitely/not/here/xyz.txt"}),
        };
        let result = exec.execute(&call).await.unwrap();
        assert!(result.is_error);
    }

    // -- 3. ServerToolRegistry --------------------------------------------

    #[test]
    fn server_registry_lists_real_tools_with_groups() {
        let reg = Arc::new(tools::ToolRegistry::with_builtins());
        let srv = ServerToolRegistry::new(reg);
        let infos = srv.list_tools();
        assert_eq!(infos.len(), 30);
        let by_name: std::collections::HashMap<_, _> =
            infos.iter().map(|i| (i.name.as_str(), i)).collect();
        // Every tool registered via with_builtins() must resolve to its
        // toolset group; spot-check the known mappings.
        for i in &infos {
            assert!(!i.name.is_empty());
            assert!(!i.description.is_empty(), "description empty for {}", i.name);
            assert!(!i.group.is_empty(), "group unresolved for {}", i.name);
        }
        assert_eq!(by_name["file_read"].group, "file");
        assert_eq!(by_name["shell_run"].group, "terminal");
        assert_eq!(by_name["http_get"].group, "web");
        assert_eq!(by_name["web_search"].group, "web");
        assert_eq!(by_name["json_parse"].group, "data");
        assert_eq!(by_name["date_now"].group, "system");
    }

    #[tokio::test]
    async fn server_registry_run_tool_roundtrip() {
        let reg = Arc::new(tools::ToolRegistry::with_builtins());
        let srv = ServerToolRegistry::new(reg);
        let ok = srv.run_tool("uuid_new", &Map::new()).await.unwrap();
        let parsed: Value = serde_json::from_str(&ok).unwrap();
        assert_eq!(parsed["uuid"].as_str().map(str::len), Some(36));
        let err = srv.run_tool("nope", &Map::new()).await.unwrap_err();
        assert!(err.contains("not found"));
    }

    // -- 4. WiredHarness ---------------------------------------------------

    fn script_factory(
        content: &str,
    ) -> Arc<
        dyn Fn() -> Result<Box<dyn providers::LlmClient>, providers::ProviderError>
            + Send
            + Sync,
    > {
        let content = content.to_string();
        Arc::new(move || {
            Ok(Box::new(ScriptProvider {
                content: content.clone(),
                tool_calls: vec![],
                fail: None,
            }) as Box<dyn providers::LlmClient>)
        })
    }

    fn turn_request() -> sstate::TurnRequest {
        sstate::TurnRequest {
            prompt: "say hi".into(),
            strategy: "auto".into(),
            session_id: "s1".into(),
            system_prompt: None,
            tools: sstate::BotTools::All,
        }
    }

    #[tokio::test]
    async fn wired_harness_runs_react_turn() {
        let harness = WiredHarness::new(
            script_factory("final answer here"),
            "test-model",
            Arc::new(tools::ToolRegistry::with_builtins()),
        );
        assert!(harness.is_configured());
        assert_eq!(harness.model_name().as_deref(), Some("test-model"));
        let result = harness.run_turn(turn_request()).await.unwrap();
        assert_eq!(result.reply, "final answer here");
        assert_eq!(result.strategy, "react");
    }

    #[tokio::test]
    async fn wired_harness_honors_bot_tool_filter() {
        // The model asks for a tool that is denied: the loop must not offer
        // it, so the turn ends without the tool ever running.
        let harness = WiredHarness::new(
            script_factory("no tools for you"),
            "test-model",
            Arc::new(tools::ToolRegistry::with_builtins()),
        )
        .with_max_turns(2);
        let mut req = turn_request();
        req.tools = sstate::BotTools::Allow(vec!["date_now".into()]);
        let schemas = schemas_for_registry(
            &tools::ToolRegistry::with_builtins(),
            &req.tools,
        );
        assert_eq!(schemas.len(), 1);
        assert_eq!(schemas[0].name, "date_now");
        let result = harness.run_turn(req).await.unwrap();
        assert_eq!(result.reply, "no tools for you");

        let mut req = turn_request();
        req.tools = sstate::BotTools::DenyOnly(vec!["date_now".into()]);
        let schemas = schemas_for_registry(
            &tools::ToolRegistry::with_builtins(),
            &req.tools,
        );
        assert_eq!(schemas.len(), 29);
        assert!(!schemas.iter().any(|s| s.name == "date_now"));
    }

    #[tokio::test]
    async fn wired_harness_factory_failure_is_turn_failed() {
        let factory: Arc<
            dyn Fn() -> Result<Box<dyn providers::LlmClient>, providers::ProviderError>
                + Send
                + Sync,
        > = Arc::new(|| {
            Err(providers::ProviderError::MissingKey(
                "no key configured".into(),
            ))
        });
        let harness = WiredHarness::new(
            factory,
            "test-model",
            Arc::new(tools::ToolRegistry::with_builtins()),
        );
        let err = harness.run_turn(turn_request()).await.unwrap_err();
        assert!(matches!(err, sstate::TurnError::Failed(_)));
    }

    #[tokio::test]
    async fn wired_harness_end_to_end_tool_loop() {
        // The fake model emits ONE tool call on the first pass and a final
        // answer on the second, driving the real tool through the loop.
        struct TwoStep {
            step: std::sync::Mutex<u32>,
        }
        #[async_trait]
        impl providers::LlmClient for TwoStep {
            async fn complete(
                &self,
                _req: &providers::ChatRequest,
            ) -> Result<providers::ChatResponse, providers::ProviderError> {
                let mut step = self.step.lock().unwrap();
                *step += 1;
                if *step == 1 {
                    Ok(providers::ChatResponse {
                        content: String::new(),
                        thought: None,
                        tool_calls: vec![providers::ToolCall {
                            id: "call_1".into(),
                            name: "date_now".into(),
                            arguments: serde_json::json!({}),
                            args_error: None,
                        }],
                        usage: None,
                        finish_reason: Some("tool_calls".into()),
                    })
                } else {
                    Ok(providers::ChatResponse {
                        content: "the date tool ran".into(),
                        thought: None,
                        tool_calls: vec![],
                        usage: None,
                        finish_reason: Some("stop".into()),
                    })
                }
            }
            async fn stream(
                &self,
                _req: &providers::ChatRequest,
            ) -> Result<providers::EventStream, providers::ProviderError> {
                Ok(Box::pin(stream::empty()))
            }
            fn provider_id(&self) -> &'static str {
                "test"
            }
            fn default_model(&self) -> &str {
                "test-model"
            }
        }
        let factory: Arc<
            dyn Fn() -> Result<Box<dyn providers::LlmClient>, providers::ProviderError>
                + Send
                + Sync,
        > = Arc::new(|| {
            Ok(Box::new(TwoStep {
                step: std::sync::Mutex::new(0),
            }) as Box<dyn providers::LlmClient>)
        });
        let harness = WiredHarness::new(
            factory,
            "test-model",
            Arc::new(tools::ToolRegistry::with_builtins()),
        );
        let result = harness.run_turn(turn_request()).await.unwrap();
        assert_eq!(result.reply, "the date tool ran");
    }
}
