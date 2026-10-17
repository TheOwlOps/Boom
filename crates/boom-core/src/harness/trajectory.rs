//! Trajectory recording: fine-grained think → act → observe logs plus token
//! accounting. Port of `agent/src/loop/trajectory.py`.
//!
//! The Python `TrajectoryRecorder` writes one JSON file per session for evals
//! and dataset collection; the Rust version does the same (plus an in-memory
//! [`TokenLedger`] the ReAct loop uses for its per-turn budget).

use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::types::AgentError;

// ---------------------------------------------------------------------------
// Steps
// ---------------------------------------------------------------------------

/// One recorded reasoning step.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TrajectoryStep {
    pub step_id: usize,
    pub thought: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub action_tool: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub action_args: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub observation: Option<String>,
    pub timestamp: f64,
}

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Records an agent's reasoning trajectory for one session.
#[derive(Debug, Default)]
pub struct Trajectory {
    session_id: String,
    steps: Vec<TrajectoryStep>,
    counter: usize,
}

impl Trajectory {
    pub fn new(session_id: impl Into<String>) -> Self {
        Self { session_id: session_id.into(), steps: Vec::new(), counter: 0 }
    }

    pub fn session_id(&self) -> &str {
        &self.session_id
    }

    /// Append one step; ids are 1-based and strictly increasing.
    pub fn record_step(
        &mut self,
        thought: impl Into<String>,
        tool: Option<String>,
        args: Option<Value>,
        observation: Option<String>,
    ) -> &TrajectoryStep {
        self.counter += 1;
        self.steps.push(TrajectoryStep {
            step_id: self.counter,
            thought: thought.into(),
            action_tool: tool,
            action_args: args,
            observation,
            timestamp: now_secs(),
        });
        // Indexing is safe: we just pushed.
        &self.steps[self.steps.len() - 1]
    }

    pub fn steps(&self) -> &[TrajectoryStep] {
        &self.steps
    }

    pub fn len(&self) -> usize {
        self.steps.len()
    }

    pub fn is_empty(&self) -> bool {
        self.steps.is_empty()
    }

    /// Serialize the whole trajectory (mirrors the Python export dict).
    pub fn to_json_value(&self) -> Value {
        serde_json::json!({
            "session_id": self.session_id,
            "total_steps": self.steps.len(),
            "steps": self.steps,
        })
    }

    /// Write `trajectory_{session_id}_{unix_ts}.json` into `log_dir`
    /// (created if missing) and return its path.
    pub fn export_json(&self, log_dir: &Path) -> Result<PathBuf, AgentError> {
        std::fs::create_dir_all(log_dir)?;
        let safe_id: String = self
            .session_id
            .chars()
            .map(|c| if c.is_alphanumeric() || c == '-' || c == '_' { c } else { '_' })
            .collect();
        let out = log_dir.join(format!(
            "trajectory_{}_{}.json",
            safe_id,
            now_secs() as u64
        ));
        std::fs::write(&out, serde_json::to_string_pretty(&self.to_json_value()).unwrap_or_else(|_| "{}".into()))?;
        Ok(out)
    }
}

// ---------------------------------------------------------------------------
// Token accounting
// ---------------------------------------------------------------------------

/// Per-turn token ledger feeding the ReAct loop's spend ceiling
/// (`HarnessConfig::token_budget`) and session usage records.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct TokenLedger {
    pub input_tokens: u64,
    pub output_tokens: u64,
}

impl TokenLedger {
    pub fn record(&mut self, input: u64, output: u64) {
        self.input_tokens = self.input_tokens.saturating_add(input);
        self.output_tokens = self.output_tokens.saturating_add(output);
    }

    pub fn total(&self) -> u64 {
        self.input_tokens.saturating_add(self.output_tokens)
    }

    /// True once the ledger has reached or passed `budget`.
    pub fn budget_exceeded(&self, budget: u64) -> bool {
        self.total() >= budget
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn step_ids_increase_monotonically() {
        let mut t = Trajectory::new("s1");
        t.record_step("thinking", Some("read_file".into()), Some(json!({"path": "x"})), Some("ok".into()));
        t.record_step("done", None, None, None);
        assert_eq!(t.len(), 2);
        assert_eq!(t.steps()[0].step_id, 1);
        assert_eq!(t.steps()[1].step_id, 2);
        assert_eq!(t.steps()[0].action_tool.as_deref(), Some("read_file"));
        assert!(t.steps()[1].observation.is_none());
    }

    #[test]
    fn export_json_writes_a_parseable_file() {
        let mut t = Trajectory::new("sess/abc");
        t.record_step("thought", Some("shell_run".into()), None, Some("out".into()));
        let dir = std::env::temp_dir().join("boom_harness_test_traj");
        let path = t.export_json(&dir).expect("export works");
        let raw = std::fs::read_to_string(&path).expect("file written");
        let v: Value = serde_json::from_str(&raw).expect("valid json");
        assert_eq!(v["session_id"], json!("sess/abc"));
        assert_eq!(v["total_steps"], json!(1));
        assert_eq!(v["steps"][0]["action_tool"], json!("shell_run"));
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn ledger_tracks_budget() {
        let mut l = TokenLedger::default();
        assert!(!l.budget_exceeded(100));
        l.record(60, 30);
        assert_eq!(l.total(), 90);
        assert!(!l.budget_exceeded(100));
        l.record(5, 10);
        assert!(l.budget_exceeded(100));
    }
}
