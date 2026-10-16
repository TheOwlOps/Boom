//! Agent execution harness: the think → act → observe machinery.
//!
//! Submodules mirror the Python sources they were ported from:
//!
//! | module | Python source |
//! |---|---|
//! | [`react`] | `agent/src/loop/react.py`, `tool_selector.py`, `smart_turns.py` |
//! | [`trajectory`] | `agent/src/loop/trajectory.py` |
//! | [`dag`] | `agent/src/orchestration/dag.py` |
//! | [`brain`] | `agent/src/orchestration/brain.py`, `roles.py` |
//! | [`reasoning`] | `agent/src/reasoning/cot.py`, `verify.py`, `self_consistency.py` |
//! | [`types`] | shared data types + the `LlmClient` / `ToolExecutor` seams |

pub mod brain;
pub mod dag;
pub mod react;
pub mod reasoning;
pub mod trajectory;
pub mod types;

pub use types::{AgentError, LlmClient, Message, ToolCall, ToolExecutor, ToolResult, TurnResult};

/// Top-level knobs for every harness component.
///
/// Mirrors the constructor arguments of the Python `ReActLoop`
/// (`max_turns`, `token_budget`, `parallel_tools`, `smart_exit`,
/// `system_prompt`, `channel`, `session_id`).
#[derive(Debug, Clone)]
pub struct HarnessConfig {
    /// Hard cap on model/tool iterations per turn (Python default: 6;
    /// coding tasks bump it to 10 — set explicitly by the caller).
    pub max_turns: usize,
    /// Optional spend ceiling for one turn, in input+output tokens.
    /// `None` means unbounded.
    pub token_budget: Option<u64>,
    /// Run one assistant turn's tool calls concurrently (`tokio::JoinSet`)
    /// instead of one at a time. Recording into the trajectory stays
    /// strictly sequential in the original tool-call order either way.
    pub parallel_tools: bool,
    /// Enable the [`react::TurnController`] smart early-exit
    /// (repeated calls / empty observations / repeated errors).
    pub smart_exit: bool,
    /// Fixed role prompt; replaces any environment-built system prompt.
    pub system_prompt: Option<String>,
    /// Platform channel the tone is tuned for (e.g. `"tui"`, `"telegram"`).
    pub channel: String,
    /// Session this harness instance is serving.
    pub session_id: String,
}

impl Default for HarnessConfig {
    fn default() -> Self {
        Self {
            max_turns: 6,
            token_budget: None,
            parallel_tools: true,
            smart_exit: true,
            system_prompt: None,
            channel: "tui".to_string(),
            session_id: "default".to_string(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_config_matches_python_defaults() {
        let cfg = HarnessConfig::default();
        assert_eq!(cfg.max_turns, 6);
        assert_eq!(cfg.token_budget, None);
        assert!(cfg.parallel_tools);
        assert!(cfg.smart_exit);
        assert_eq!(cfg.channel, "tui");
        assert_eq!(cfg.session_id, "default");
    }
}
