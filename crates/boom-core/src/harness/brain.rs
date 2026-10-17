//! Brain — one execution path combining ReAct + DAG + MASS.
//!
//! Port of `agent/src/orchestration/brain.py` and `roles.py`.
//!
//! Before this module, the pieces were disconnected: [`crate::harness::react`]
//! (the turn loop), [`crate::harness::dag`] (the DAG executor) and the
//! specialist roles (a registry with no parallel engine). Brain is the single
//! path a prompt flows through.
//!
//! Like the Python version, Brain owns no provider and makes no network calls
//! itself: every model call goes through `make_loop`, a factory the app layer
//! closes over the real environment + LLM. In tests the factory returns fakes.
//!
//! Strategies:
//! - `react` — one [`ReActLoop`](crate::harness::react::ReActLoop) turn.
//! - `dag` — a planner turn decomposes the goal into subtasks; the plan is
//!   parsed into [`TaskNode`](crate::harness::dag::TaskNode)s and the
//!   [`DagScheduler`](crate::harness::dag::DagScheduler) runs each node as
//!   its own ReAct loop with its dependencies' outputs in context.
//!   Unparseable/invalid plans fall back to `react` (recorded in `errors`,
//!   never raised).
//! - `mass` — every chosen specialist runs the same prompt in parallel, each
//!   with its role system prompt and only its allowed tools. v1 synthesis is
//!   an honest labelled join, not a second model call.
//!
//! Routing ([`Brain::choose_strategy`]) is a rule-based heuristic v1 — not an
//! AI classifier: an explicit strategy always wins; in `auto` mode dependency
//! keywords pick DAG, an explicit multi-item list picks MASS, else REACT.

use std::collections::HashMap;
use std::sync::Arc;

use serde_json::Value;
use tokio::sync::Semaphore;
use tokio::task::JoinSet;

use super::dag::{handler, DagScheduler, TaskHandler, TaskNode};
use super::react::ReActLoop;
use super::types::AgentError;

// ---------------------------------------------------------------------------
// Strategy + result
// ---------------------------------------------------------------------------

/// Execution strategy.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BrainStrategy {
    React,
    Dag,
    Mass,
}

impl BrainStrategy {
    pub fn as_str(self) -> &'static str {
        match self {
            BrainStrategy::React => "react",
            BrainStrategy::Dag => "dag",
            BrainStrategy::Mass => "mass",
        }
    }

    fn parse(s: &str) -> Option<Self> {
        match s.trim().to_lowercase().as_str() {
            "react" => Some(BrainStrategy::React),
            "dag" => Some(BrainStrategy::Dag),
            "mass" => Some(BrainStrategy::Mass),
            _ => None,
        }
    }
}

/// Outcome of [`Brain::run`]. Never-raising: every failure lands in `errors`.
#[derive(Debug, Clone, Default)]
pub struct BrainResult {
    pub strategy: String,
    pub text: String,
    /// DAG: output per node id.
    pub node_outputs: HashMap<String, Value>,
    /// MASS: output per role name.
    pub agent_outputs: HashMap<String, Value>,
    pub errors: HashMap<String, String>,
}

// ---------------------------------------------------------------------------
// Roles (port of roles.py)
// ---------------------------------------------------------------------------

/// One specialist role for MASS fan-out.
#[derive(Debug, Clone, Copy)]
pub struct AgentRole {
    pub name: &'static str,
    pub description: &'static str,
    pub system_prompt: &'static str,
    /// Tool names this role may use. IMPORTANT: every name must exist in the
    /// environment's toolset — the factory silently drops unknown names
    /// (never a crash), but a role left with no tools cannot do anything.
    pub tools: &'static [&'static str],
    pub when_to_use: &'static str,
}

/// The six specialist roles from `roles.py`.
pub static ROLES: &[AgentRole] = &[
    AgentRole {
        name: "coder",
        description: "Code engineer: write, fix and debug code until it works.",
        system_prompt: "You are a coder: write, fix and debug code until it actually runs correctly.\n\
            Always read the existing file before editing, and prefer surgical edits over \
            full rewrites: use patch_file for small changes, write_file for brand-new files.\n\
            Never break existing code: keep unrelated APIs, signatures and behavior intact, \
            and every change must have a clear reason.\n\
            After every change, re-run to verify: run tests via shell_run or execute_code, \
            read the real output, and only report done when the output proves it.\n\
            When unsure where the bug is, read context with read_file/ast_inspect first — \
            do not guess.",
        tools: &["edit_file", "write_file", "read_file", "shell_run", "execute_code", "patch_file"],
        when_to_use: "Spawn when the task requires writing new/fixing/debugging code, refactoring, \
            or adding a feature. Not for read-only review work (that's the validator's job).",
    },
    AgentRole {
        name: "researcher",
        description: "Research agent: search and synthesize information from the web.",
        system_prompt: "You are a researcher: find and synthesize accurate, up-to-date information.\n\
            Cross-check at least 2–3 independent sources before concluding, and cite each \
            key claim with source name and URL.\n\
            Prefer primary sources — official docs, repos, original reports — and distrust \
            unsourced claims.\n\
            Structure the answer: summary, details by section, and an 'uncertain' section \
            for points where sources conflict.\n\
            Never fabricate numbers, dates or quotes; say plainly what you could not find.",
        tools: &["web_search", "web_fetch", "web_extract", "news_search", "session_search", "research_brief"],
        when_to_use: "Spawn when you need to look up docs, compare technologies, find best \
            practices, or synthesize market/news information. Not when the answer is \
            already in the codebase (use coder/validator to read the files directly).",
    },
    AgentRole {
        name: "data_scientist",
        description: "Data analyst: analyze numerical data with Python.",
        system_prompt: "You are a data_scientist: analyze data with Python; results must be \
            reproducible from code.\n\
            Always compute with execute_code instead of mental math: load data via \
            read_file, process it in Python (statistics/pandas if available), and print \
            concrete numbers.\n\
            Present results clearly: tables with units plus interpretation — not raw dumps.\n\
            Check data assumptions before concluding (missing values, outliers, sample \
            size); if the data is insufficient, state the limits plainly instead of \
            over-interpreting.\n\
            When external context is needed (metric definitions, industry benchmarks), \
            look it up with web_fetch and cite the source.",
        tools: &["execute_code", "read_file", "web_fetch"],
        when_to_use: "Spawn when you need to analyze numbers, statistics, charting, or dataset \
            processing. Not for simply reading logs.",
    },
    AgentRole {
        name: "visual_analyst",
        description: "Vision analyst: describe images in detail and OCR text.",
        system_prompt: "You are a visual_analyst: analyze images and video, describing in detail \
            what you see.\n\
            When an image contains text (screenshots, scanned docs, signs), ALWAYS run OCR \
            (ocr_image / ocr_image_url) to extract it, including Vietnamese text.\n\
            Describe in structure: overall frame, key regions, OCR text (verbatim), and \
            notes. Separate what you see from what you infer.\n\
            If the image is blurry or unreadable, say which parts are uncertain instead \
            of guessing.\n\
            Use read_file to check image files/metadata on disk before analyzing.",
        tools: &["ocr_image", "ocr_image_url", "read_file"],
        when_to_use: "Spawn when the task involves screenshots, scanned documents, images with \
            text needing OCR, or describing image content. Not for images that already \
            have complete alt-text.",
    },
    AgentRole {
        name: "financial_modeler",
        description: "Financial modeler: build financial models with precise calculations.",
        system_prompt: "You are a financial_modeler: build financial models; every number must be \
            computed in code, never estimated in prose.\n\
            Run all calculations — NPV, IRR, cash flow, break-even, scenario analysis — \
            in execute_code with formulas visible so others can re-check.\n\
            State every input assumption (rates, growth, horizon, currency) up front in \
            the report; re-run the model when assumptions change.\n\
            Present results as scenario tables (base/best/worst) with units and sane \
            rounding; state the model's risks and limits.\n\
            When market data is needed (rates, FX, commodity prices), fetch it with \
            web_search/web_fetch, citing the source and the data timestamp.",
        tools: &["execute_code", "web_search", "web_fetch"],
        when_to_use: "Spawn when you need financial modeling, valuation, investment analysis, or \
            business scenario math. Not for simple arithmetic.",
    },
    AgentRole {
        name: "validator",
        description: "Independent validator: verify results, report defects bluntly.",
        system_prompt: "You are a validator: independently verify another agent's work — your job is \
            to FIND defects, not to praise. Assume the work is wrong until you prove it \
            right yourself.\n\
            Verify by doing: re-read files with read_file, re-run commands/tests with \
            shell_run, cross-check claims with web_search when needed — never trust the \
            report at face value.\n\
            Report defects concretely with evidence: `path:line — what is wrong — why it \
            matters`, plus the command/output that proves it.\n\
            Be blunt: log even small defects; if you truly find none, say `no defects` \
            rather than inventing findings.\n\
            A claim you could not verify counts against the report, not for it.",
        tools: &["read_file", "shell_run", "web_search"],
        when_to_use: "Spawn at the end of every important task for an independent check before \
            reporting done to the user. Don't use a validator to check its own task in \
            the same pass (that kills independence).",
    },
];

/// Get a role definition by name (case-insensitive, whitespace trimmed).
/// Returns `None` for unknown names — the orchestrator must not crash just
/// because a model mistyped a role name.
pub fn get_role(name: &str) -> Option<&'static AgentRole> {
    let key = name.trim().to_lowercase();
    ROLES.iter().find(|r| r.name == key)
}

/// Names of all available roles.
pub fn list_roles() -> Vec<&'static str> {
    ROLES.iter().map(|r| r.name).collect()
}

// ---------------------------------------------------------------------------
// Loop factory
// ---------------------------------------------------------------------------

/// What [`Brain`] asks the app layer for when it needs a turn loop.
///
/// Convention (mirrors the Python `make_loop`):
/// - `tool_names: None` → the full toolset,
/// - `tool_names: Some(list)` → only those tools (unknown names dropped).
#[derive(Debug, Clone)]
pub struct LoopSpec {
    pub system_prompt: Option<String>,
    pub session_id: String,
    pub tool_names: Option<Vec<String>>,
}

/// Options for [`Brain::run`].
#[derive(Debug, Clone, Default)]
pub struct RunOptions {
    /// `"auto"` (default), `"react"`, `"dag"` or `"mass"`.
    pub strategy: String,
    pub session_id: Option<String>,
    pub roles: Option<Vec<String>>,
    pub system_prompt: Option<String>,
    pub tool_names: Option<Vec<String>>,
}

// ---------------------------------------------------------------------------
// Brain
// ---------------------------------------------------------------------------

const MASS_CONCURRENCY: usize = 5;

const PLANNER_SYSTEM: &str = "You are a planner for an autonomous coding agent. You output only JSON-like numbered lists.";

const PLANNER_CONTRACT: &str = "Contract: reply with a numbered list of subtasks, one per line, in the form \
`1. <what this subtask produces>`. When a subtask must wait for another, append the dependency in \
parentheses, e.g. `2. Analyse the results (waits for 1)` or `3. Write the fix (depends on 1, 2)`.";

/// Single execution path over [`ReActLoop`], [`DagScheduler`] and the MASS
/// specialist roles.
pub struct Brain {
    make_loop: Arc<dyn Fn(&LoopSpec) -> ReActLoop + Send + Sync>,
}

impl Brain {
    pub fn new(make_loop: Arc<dyn Fn(&LoopSpec) -> ReActLoop + Send + Sync>) -> Self {
        Self { make_loop }
    }

    // -------------------------------------------------------------- router

    /// Pick a strategy. Returns `(strategy, note)`; `note` is `Some` when the
    /// requested strategy was unknown and we fell back to `react`.
    ///
    /// Heuristic v1, rule-based — not an AI classifier. An explicit strategy
    /// always wins; in `auto` mode dependency keywords pick DAG, an explicit
    /// multi-item list picks MASS, everything else is REACT.
    pub fn choose_strategy(prompt: &str, strategy: &str) -> (BrainStrategy, Option<String>) {
        let s = strategy.trim().to_lowercase();
        if s != "auto" {
            return match BrainStrategy::parse(&s) {
                Some(b) => (b, None),
                None => (
                    BrainStrategy::React,
                    Some(format!("unknown strategy '{strategy}'; using 'react'")),
                ),
            };
        }
        let text = prompt.to_lowercase();
        if has_dependency_hint(&text) {
            return (BrainStrategy::Dag, None);
        }
        if count_list_items(prompt) >= 2 {
            return (BrainStrategy::Mass, None);
        }
        (BrainStrategy::React, None)
    }

    // ---------------------------------------------------------------- run

    /// Run one prompt through the chosen strategy. Never fails at the top
    /// level: every failure is returned as `BrainResult { errors: ... }`.
    pub async fn run(&self, prompt: &str, opts: RunOptions) -> BrainResult {
        let (chosen, note) = Self::choose_strategy(prompt, &opts.strategy);
        let mut errors = HashMap::new();
        if let Some(n) = note {
            errors.insert("strategy".to_string(), n);
        }
        let mut result = match chosen {
            BrainStrategy::Dag => self.run_dag(prompt, opts.session_id.as_deref()).await,
            BrainStrategy::Mass => {
                self.run_mass(
                    prompt,
                    opts.session_id.as_deref(),
                    opts.roles.as_deref(),
                )
                .await
            }
            BrainStrategy::React => {
                self.run_react(
                    prompt,
                    opts.session_id.as_deref(),
                    opts.system_prompt.as_deref(),
                    opts.tool_names.as_deref(),
                )
                .await
            }
        };
        // Merge the strategy note without clobbering runner errors.
        for (k, v) in errors {
            result.errors.entry(k).or_insert(v);
        }
        result
    }

    // --------------------------------------------------------------- REACT

    /// One ReActLoop turn — identical behaviour to calling the loop directly,
    /// because that is literally what it does.
    pub async fn run_react(
        &self,
        prompt: &str,
        session_id: Option<&str>,
        system_prompt: Option<&str>,
        tool_names: Option<&[String]>,
    ) -> BrainResult {
        let spec = LoopSpec {
            system_prompt: system_prompt.map(str::to_string),
            session_id: session_id
                .map(str::to_string)
                .unwrap_or_else(|| new_session_id("brain")),
            tool_names: tool_names.map(|t| t.to_vec()),
        };
        let mut l = (self.make_loop)(&spec);
        match l.run_turn(prompt).await {
            Ok(res) => {
                let mut errors = HashMap::new();
                if let Some(e) = res.error {
                    errors.insert("react".to_string(), e);
                }
                BrainResult {
                    strategy: BrainStrategy::React.as_str().to_string(),
                    text: res.final_answer,
                    errors,
                    ..Default::default()
                }
            }
            Err(e) => BrainResult {
                strategy: BrainStrategy::React.as_str().to_string(),
                errors: [("react".to_string(), format!("{e:?}"))]
                    .into_iter()
                    .collect(),
                ..Default::default()
            },
        }
    }

    // ----------------------------------------------------------------- DAG

    /// Planner → parse → [`DagScheduler`]. Any planning failure falls back to
    /// REACT and says so in `errors`.
    pub async fn run_dag(&self, prompt: &str, session_id: Option<&str>) -> BrainResult {
        let sid = session_id
            .map(str::to_string)
            .unwrap_or_else(|| new_session_id("brain"));

        // Planner turn: one loop with the planner prompt and no tools.
        let planner_spec = LoopSpec {
            system_prompt: Some(format!("{PLANNER_SYSTEM}\n\n{PLANNER_CONTRACT}")),
            session_id: format!("{sid}/planner"),
            tool_names: Some(Vec::new()),
        };
        let plan_text = {
            let mut ploop = (self.make_loop)(&planner_spec);
            let instruction = format!(
                "{prompt}\n\nBreak the goal above into subtasks with dependencies. \
                 Follow your contract exactly: a numbered list; for each subtask \
                 say what it produces and which other subtask number (if any) \
                 it must wait for, e.g. '2. Analyse (waits for 1)'."
            );
            match ploop.run_turn(&instruction).await {
                Ok(res) => res.final_answer,
                Err(e) => {
                    return self
                        .dag_fallback(
                            prompt,
                            session_id,
                            format!("dag->react: planner turn failed: {e}"),
                            None,
                        )
                        .await
                }
            }
        };

        let nodes = match parse_plan(&plan_text) {
            Ok(nodes) => nodes,
            Err(e) => {
                return self
                    .dag_fallback(
                        prompt,
                        session_id,
                        format!("dag->react: planner output unparseable: {e}"),
                        None,
                    )
                    .await
            }
        };

        let mut sched = DagScheduler::new();
        for node in &nodes {
            sched.add_task(node.clone(), self.node_handler(node.clone(), sid.clone()));
        }
        let exec = match sched.execute().await {
            Ok(r) => r,
            Err(e) => {
                return self
                    .dag_fallback(
                        prompt,
                        session_id,
                        format!("dag->react: invalid plan: {e}"),
                        None,
                    )
                    .await
            }
        };

        let mut errors = exec.errors.clone();
        for b in &exec.blocked {
            errors
                .entry(format!("blocked:{b}"))
                .or_insert_with(|| "never ran: a dependency failed or was blocked".to_string());
        }

        // Synthesize from terminal nodes (nothing depends on them); if none
        // produced output, fall back to whatever outputs exist.
        let mut depended = std::collections::HashSet::new();
        for n in &nodes {
            depended.extend(n.dependencies.iter().cloned());
        }
        let terminals: Vec<&str> =
            nodes.iter().map(|n| n.id.as_str()).filter(|id| !depended.contains(*id)).collect();
        let mut parts: Vec<String> = terminals
            .iter()
            .filter_map(|id| exec.outputs.get(*id).map(|o| (id, o)))
            .map(|(id, o)| format!("[{id}] {}", render_output(o)))
            .collect();
        if parts.is_empty() {
            parts = exec
                .outputs
                .iter()
                .map(|(id, o)| format!("[{id}] {}", render_output(o)))
                .collect();
        }

        BrainResult {
            strategy: BrainStrategy::Dag.as_str().to_string(),
            text: parts.join("\n\n"),
            node_outputs: exec.outputs,
            errors,
            ..Default::default()
        }
    }

    /// One [`DagScheduler`] node: a fresh ReAct loop with the dependencies'
    /// outputs in context. A failed node returns `Err` so the scheduler marks
    /// dependents blocked instead of running them on garbage.
    fn node_handler(&self, node: TaskNode, session_id: String) -> TaskHandler {
        let make_loop = self.make_loop.clone();
        handler(move |dep_outputs| {
            let make_loop = make_loop.clone();
            let node = node.clone();
            let session_id = session_id.clone();
            async move {
                let mut ctx = format!("Subtask [{}]: {}", node.id, node.name);
                if !dep_outputs.is_empty() {
                    ctx.push_str("\n\nOutputs from prerequisite subtasks:\n");
                    ctx.push_str(
                        &serde_json::to_string_pretty(&dep_outputs).unwrap_or_default(),
                    );
                }
                let spec = LoopSpec {
                    system_prompt: None,
                    session_id: format!("{session_id}/dag/{}", node.id),
                    tool_names: None,
                };
                let mut l = make_loop(&spec);
                let res = l.run_turn(&ctx).await.map_err(|e| AgentError::Llm(e.to_string()))?;
                if let Some(err) = res.error {
                    return Err(AgentError::Tool(err));
                }
                Ok(Value::String(res.final_answer))
            }
        })
    }

    async fn dag_fallback(
        &self,
        prompt: &str,
        session_id: Option<&str>,
        reason: String,
        extra: Option<HashMap<String, String>>,
    ) -> BrainResult {
        let mut fb = self.run_react(prompt, session_id, None, None).await;
        let mut errors = extra.unwrap_or_default();
        errors.extend(fb.errors.drain());
        errors.insert("fallback".to_string(), reason);
        fb.errors = errors;
        fb
    }

    // ---------------------------------------------------------------- MASS

    /// Fan-out: every chosen specialist runs the prompt in parallel, each
    /// with its role prompt and only its allowed tools. One role failing
    /// never stops the others. v1 synthesis is a labelled join — honest, no
    /// extra model call.
    pub async fn run_mass(
        &self,
        prompt: &str,
        session_id: Option<&str>,
        roles: Option<&[String]>,
    ) -> BrainResult {
        let sid = session_id
            .map(str::to_string)
            .unwrap_or_else(|| new_session_id("brain"));
        let mut errors = HashMap::new();
        let mut specs: Vec<&'static AgentRole> = Vec::new();
        match roles {
            None => specs.extend(ROLES.iter()),
            Some(names) => {
                for name in names {
                    match get_role(name) {
                        Some(spec) => {
                            if !specs.iter().any(|s| s.name == spec.name) {
                                specs.push(spec);
                            }
                        }
                        None => {
                            errors.insert(
                                format!("role:{name}"),
                                format!("unknown role '{name}'"),
                            );
                        }
                    }
                }
            }
        }
        if specs.is_empty() {
            errors
                .entry("mass".to_string())
                .or_insert_with(|| "no valid roles to run".to_string());
            return BrainResult {
                strategy: BrainStrategy::Mass.as_str().to_string(),
                errors,
                ..Default::default()
            };
        }

        let sem = Arc::new(Semaphore::new(MASS_CONCURRENCY));
        let mut set: JoinSet<(String, String, String)> = JoinSet::new();
        for spec in specs {
            let sem = sem.clone();
            let make_loop = self.make_loop.clone();
            let prompt = prompt.to_string();
            let sid = sid.clone();
            set.spawn(async move {
                let _permit = sem.acquire().await;
                let loop_spec = LoopSpec {
                    system_prompt: Some(spec.system_prompt.to_string()),
                    session_id: format!("{sid}/mass/{}", spec.name),
                    tool_names: Some(spec.tools.iter().map(|s| s.to_string()).collect()),
                };
                let mut l = make_loop(&loop_spec);
                match l.run_turn(&prompt).await {
                    Ok(res) => match res.error {
                        Some(e) => (spec.name.to_string(), String::new(), e),
                        None => (spec.name.to_string(), res.final_answer, String::new()),
                    },
                    Err(e) => (spec.name.to_string(), String::new(), format!("{e}")),
                }
            });
        }

        let mut agent_outputs: HashMap<String, Value> = HashMap::new();
        let mut parts: Vec<(String, String)> = Vec::new();
        while let Some(joined) = set.join_next().await {
            // A specialist task only fails to join on our own bug; record it
            // under a synthetic key rather than crashing the fan-out.
            let (role_name, text, err) = joined.unwrap_or_else(|e| {
                ("_join".to_string(), String::new(), format!("specialist task failed: {e}"))
            });
            if err.is_empty() {
                agent_outputs.insert(
                    role_name.clone(),
                    Value::String(text.clone()),
                );
                parts.push((role_name, text));
            } else {
                errors.insert(role_name.clone(), err.clone());
                agent_outputs.insert(
                    role_name,
                    serde_json::json!({"error": err}),
                );
            }
        }
        // Deterministic synthesis order: sort by role name.
        parts.sort_by(|a, b| a.0.cmp(&b.0));
        let text = parts
            .iter()
            .map(|(name, t)| format!("[{name}]\n{t}"))
            .collect::<Vec<_>>()
            .join("\n\n");

        BrainResult {
            strategy: BrainStrategy::Mass.as_str().to_string(),
            text,
            agent_outputs,
            errors,
            ..Default::default()
        }
    }

    // ---------------------------------------------------------------- plan
    // Autonomous-controller support: turn a goal into tool-call steps.
    // Never fails: an LLM planning failure degrades to the heuristic
    // intent-based plan, and that degrades to [] (the controller's own
    // default then applies).

    /// Turn `goal` into a list of [`PlanStep`]s.
    ///
    /// Every returned step names a tool from `available_tools`, so a
    /// controller's `tool not in tools` guard never trips on our output.
    pub async fn plan(
        &self,
        goal: &str,
        history: &[Value],
        available_tools: Option<&[String]>,
    ) -> Vec<PlanStep> {
        let tool_names: Vec<String> = match available_tools {
            Some(names) => names
                .iter()
                .filter(|t| !t.trim().is_empty())
                .cloned()
                .collect(),
            None => Vec::new(),
        };
        if tool_names.is_empty() {
            return Vec::new();
        }
        let via_llm = self.plan_llm(goal, history, &tool_names).await;
        if !via_llm.is_empty() {
            return via_llm;
        }
        plan_heuristic(goal, &tool_names)
    }

    /// Ask the model for a plan; `[]` on any failure (bad JSON, unknown
    /// tools, transport error) so the heuristic fallback takes over.
    async fn plan_llm(
        &self,
        goal: &str,
        history: &[Value],
        tool_names: &[String],
    ) -> Vec<PlanStep> {
        let spec = LoopSpec {
            system_prompt: Some(PLANNER_SYSTEM.to_string()),
            session_id: format!("{}/plan", new_session_id("brain")),
            tool_names: Some(tool_names.to_vec()),
        };
        let mut l = (self.make_loop)(&spec);
        let prompt = plan_prompt(goal, history, tool_names);
        let text = match l.run_turn(&prompt).await {
            Ok(res) if res.error.is_none() => res.final_answer,
            _ => return Vec::new(),
        };
        parse_plan_json(&text, tool_names)
    }
}

// ---------------------------------------------------------------------------
// PlanStep
// ---------------------------------------------------------------------------

/// One planned tool call: `{"tool", "args", "desc"}`.
#[derive(Debug, Clone)]
pub struct PlanStep {
    pub tool: String,
    pub args: Value,
    pub desc: String,
}

// ---------------------------------------------------------------------------
// Plan parsing (planner numbered-list → TaskNodes)
// ---------------------------------------------------------------------------

/// Parse the planner's numbered list into [`TaskNode`]s.
///
/// Expected per the planner contract: `1. <what it produces> (waits for 2)`.
/// Returns `Err` when the plan is unusable — the caller falls back to REACT
/// instead of executing a hallucinated graph.
fn parse_plan(text: &str) -> Result<Vec<TaskNode>, AgentError> {
    let mut nodes: Vec<TaskNode> = Vec::new();
    for line in text.lines() {
        let (idx, rest) = match parse_numbered_line(line) {
            Some(v) => v,
            None => continue,
        };
        let deps = extract_dep_numbers(&rest, &idx);
        let name = strip_dep_paren(&rest);
        let name = if name.is_empty() { rest.trim().to_string() } else { name };
        nodes.push(TaskNode { id: idx, name, dependencies: deps });
    }
    if nodes.is_empty() {
        return Err(AgentError::InvalidPlan(
            "planner returned no numbered subtasks".to_string(),
        ));
    }
    let ids: Vec<&str> = nodes.iter().map(|n| n.id.as_str()).collect();
    let unique: std::collections::HashSet<&str> = ids.iter().copied().collect();
    if unique.len() != ids.len() {
        return Err(AgentError::InvalidPlan(
            "planner reused a subtask number".to_string(),
        ));
    }
    for n in &nodes {
        let unknown: Vec<&str> = n
            .dependencies
            .iter()
            .map(String::as_str)
            .filter(|d| !unique.contains(d))
            .collect();
        if !unknown.is_empty() {
            return Err(AgentError::InvalidPlan(format!(
                "subtask {} depends on unknown subtask(s): {}",
                n.id,
                unknown.join(", ")
            )));
        }
    }
    Ok(nodes)
}

/// `  2. Do the thing` → `Some(("2", "Do the thing"))`.
fn parse_numbered_line(line: &str) -> Option<(String, String)> {
    let t = line.trim_start();
    let digits: String = t.chars().take_while(|c| c.is_ascii_digit()).collect();
    if digits.is_empty() {
        return None;
    }
    let after = &t[digits.len()..];
    let mut chars = after.chars();
    match chars.next() {
        Some('.') | Some(')') | Some(']') => {}
        _ => return None,
    }
    let rest = chars.as_str().trim();
    if rest.is_empty() {
        return None;
    }
    Some((digits, rest.to_string()))
}

/// Dependency phrases the planner may use (English + Vietnamese).
const DEP_PHRASES: &[&str] = &[
    "depends on",
    "depend on",
    "waits for",
    "wait for",
    "after",
    "phụ thuộc",
    "sau bước",
    "sau",
];

/// Extract subtask numbers after a dependency phrase, excluding `self_idx`.
fn extract_dep_numbers(rest: &str, self_idx: &str) -> Vec<String> {
    let lower = rest.to_lowercase();
    let mut deps: Vec<String> = Vec::new();
    for phrase in DEP_PHRASES {
        let mut search_from = 0;
        while let Some(pos) = find_word(&lower[search_from..], phrase) {
            let abs = search_from + pos + phrase.len();
            let tail: String = lower[abs..]
                .chars()
                .take_while(|c| c.is_ascii_digit() || *c == ',' || c.is_whitespace())
                .collect();
            for num in tail.split(|c: char| !c.is_ascii_digit()).filter(|s| !s.is_empty()) {
                if num != self_idx && !deps.iter().any(|d| d == num) {
                    deps.push(num.to_string());
                }
            }
            search_from = abs + 1;
            if search_from >= lower.len() {
                break;
            }
        }
    }
    deps
}

/// Find `needle` in `haystack` at a word boundary (both haystack and needle
/// are already lowercased). Returns the byte index.
fn find_word(haystack: &str, needle: &str) -> Option<usize> {
    let mut start = 0;
    while let Some(pos) = haystack[start..].find(needle) {
        let abs = start + pos;
        let before_ok = abs == 0
            || !haystack[..abs].chars().next_back().map(is_word_char).unwrap_or(false);
        let after = abs + needle.len();
        let after_ok = after >= haystack.len()
            || !haystack[after..].chars().next().map(is_word_char).unwrap_or(false);
        if before_ok && after_ok {
            return Some(abs);
        }
        start = abs + 1;
    }
    None
}

fn is_word_char(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// Strip a trailing `(…depends on 1…)` / `[…sau bước 2…]` from a subtask name.
fn strip_dep_paren(rest: &str) -> String {
    let bytes = rest.as_bytes();
    for (i, &b) in bytes.iter().enumerate().rev() {
        if b == b'(' || b == b'[' {
            let tail = &rest[i..].to_lowercase();
            if DEP_PHRASES.iter().any(|p| tail.contains(p)) {
                return rest[..i].trim_end().to_string();
            }
            break; // last paren group is not a dep note; keep the name whole
        }
    }
    rest.trim().to_string()
}

// ---------------------------------------------------------------------------
// Strategy heuristics
// ---------------------------------------------------------------------------

/// Dependency hints for the auto router (port of `_DEP_WORDS`):
/// rồi / sau đó / trước tiên / bước N / step N / then / after that.
fn has_dependency_hint(lower_text: &str) -> bool {
    for phrase in ["sau đó", "trước tiên", "after that"] {
        if lower_text.contains(phrase) {
            return true;
        }
    }
    for word in ["rồi", "then"] {
        if find_word(lower_text, word).is_some() {
            return true;
        }
    }
    // "bước 3" / "step 2": the word followed by an optional space + digit.
    for word in ["bước", "step"] {
        let mut start = 0;
        while let Some(pos) = find_word(&lower_text[start..], word) {
            let abs = start + pos + word.len();
            let tail = lower_text[abs..].trim_start();
            if tail.chars().next().map(|c| c.is_ascii_digit()).unwrap_or(false) {
                return true;
            }
            start = abs + 1;
            if start >= lower_text.len() {
                break;
            }
        }
    }
    false
}

/// Count lines that look like list items: `1. x`, `2) x`, `- x`, `* x`, `• x`.
fn count_list_items(text: &str) -> usize {
    text.lines()
        .filter(|line| {
            let t = line.trim_start();
            if t.is_empty() {
                return false;
            }
            let numbered = {
                let digits: String = t.chars().take_while(|c| c.is_ascii_digit()).collect();
                !digits.is_empty()
                    && matches!(t[digits.len()..].chars().next(), Some('.') | Some(')') | Some(']'))
                    && !t[digits.len() + 1..].trim().is_empty()
            };
            let bulleted = matches!(t.chars().next(), Some('-') | Some('*') | Some('•'))
                && !t[1..].trim().is_empty();
            numbered || bulleted
        })
        .count()
}

// ---------------------------------------------------------------------------
// LLM plan prompt + JSON parsing
// ---------------------------------------------------------------------------

fn plan_prompt(goal: &str, history: &[Value], tool_names: &[String]) -> String {
    let mut lines = vec![
        "Break the GOAL below into a short ordered list of concrete tool calls.".to_string(),
        String::new(),
        format!("GOAL: {goal}"),
        String::new(),
        format!(
            "AVAILABLE TOOLS: {}",
            if tool_names.is_empty() { "(none)".to_string() } else { tool_names.join(", ") }
        ),
    ];
    if !history.is_empty() {
        lines.push(String::new());
        lines.push("HISTORY (previous iterations; do not repeat completed work):".to_string());
        for h in history.iter().take(10).rev() {
            let s = serde_json::to_string(h).unwrap_or_default();
            lines.push(format!("- {}", s.chars().take(200).collect::<String>()));
        }
    }
    lines.extend(
        [
            "",
            "RULES:",
            "- Reply with ONLY a JSON array, no prose, no markdown fences.",
            "- Each element: {\"tool\": \"<exact tool name>\", \"args\": {...}, \"desc\": \"<short description>\"}.",
            "- Use only tools from AVAILABLE TOOLS.",
            "- Keep it short (1-6 steps), ordered by dependency.",
            "- If the GOAL looks already done per HISTORY, return [].",
        ]
        .iter()
        .map(|s| s.to_string()),
    );
    lines.join("\n")
}

/// Parse the planner's JSON into canonical steps. Unknown tools are dropped;
/// anything unparseable yields `[]`. (Port of `_parse_plan_json`.)
fn parse_plan_json(text: &str, tool_names: &[String]) -> Vec<PlanStep> {
    let known: std::collections::HashSet<&str> =
        tool_names.iter().map(String::as_str).collect();
    let mut t = text.trim();
    if let Some(stripped) = t.strip_prefix("```") {
        t = stripped.trim_start_matches(|c: char| c.is_ascii_alphabetic()).trim();
        t = t.strip_suffix("```").unwrap_or(t).trim();
    }
    let (start, end) = match (t.find('['), t.rfind(']')) {
        (Some(s), Some(e)) if e > s => (s, e),
        _ => return Vec::new(),
    };
    let raw: Value = match serde_json::from_str(&t[start..=end]) {
        Ok(v) => v,
        Err(_) => return Vec::new(),
    };
    let items: &[Value] = match &raw {
        Value::Array(a) => a,
        Value::Object(m) => match m.get("steps").and_then(Value::as_array) {
            Some(a) => a,
            None => return Vec::new(),
        },
        _ => return Vec::new(),
    };
    let mut steps = Vec::new();
    for item in items {
        let obj = match item.as_object() {
            Some(o) => o,
            None => continue,
        };
        let tool = obj
            .get("tool")
            .or_else(|| obj.get("action"))
            .or_else(|| obj.get("name"))
            .and_then(Value::as_str)
            .unwrap_or("");
        let args = obj
            .get("args")
            .or_else(|| obj.get("arguments"))
            .or_else(|| obj.get("params"))
            .cloned()
            .unwrap_or(Value::Null);
        let desc = obj
            .get("desc")
            .or_else(|| obj.get("description"))
            .and_then(Value::as_str)
            .unwrap_or("");
        if known.contains(tool) && args.is_object() {
            steps.push(PlanStep {
                tool: tool.to_string(),
                args,
                desc: desc.to_string(),
            });
        }
    }
    steps
}

// ---------------------------------------------------------------------------
// Heuristic intent-based planner (no model needed)
// ---------------------------------------------------------------------------

/// Intent keywords, order matters (port of `_INTENT_PATTERNS`).
const INTENT_KEYWORDS: &[(&str, &[&str])] = &[
    ("test", &["pytest", "unittest", "test", "kiểm tra", "chạy test", "kiểm thử"]),
    ("fix", &["fix", "bug", "debug", "lỗi", "sửa lỗi", "khắc phục", "gặp lỗi", "bị lỗi"]),
    ("run", &["chạy", "run", "thực thi", "execute"]),
    ("read", &["đọc", "read", "xem", "mở"]),
    ("search", &["tìm", "search", "tìm kiếm", "grep"]),
    ("edit", &["sửa", "edit", "patch", "thay đổi", "cập nhật", "update"]),
    ("write", &["tạo", "create", "viết", "write"]),
];

const RUN_CANDIDATES: &[&str] = &["run_command", "shell", "execute", "exec", "bash"];
const READ_CANDIDATES: &[&str] = &["read_file", "file_read", "read", "cat"];
const TEST_CANDIDATES: &[&str] = &["pytest", "run_tests", "run_pytest", "npm_test", "test"];
const SEARCH_CANDIDATES: &[&str] = &["grep", "search", "search_files", "find", "rg"];

const FILE_EXTENSIONS: &[&str] = &[
    "py", "js", "ts", "tsx", "jsx", "md", "json", "yaml", "yml", "txt", "sh",
    "rs", "go", "java", "c", "cpp", "toml", "cfg", "ini",
];

fn classify_intent(goal: &str) -> &'static str {
    let lower = goal.to_lowercase();
    for (intent, keywords) in INTENT_KEYWORDS {
        for kw in *keywords {
            let hit = if kw.contains(' ') {
                lower.contains(kw)
            } else {
                find_word(&lower, kw).is_some()
            };
            if hit {
                return intent;
            }
        }
    }
    "generic"
}

/// Find file paths in free text (port of `_FILE_RE`).
fn extract_files(text: &str) -> Vec<String> {
    let mut found: Vec<String> = Vec::new();
    for token in text.split_whitespace() {
        let t = token
            .trim_matches(|c: char| "\"'`([{<".contains(c))
            .trim_matches(|c: char| "\"'`)]},>.;:!?".contains(c));
        if t.is_empty() || t.contains(' ') {
            continue;
        }
        let valid_chars = t
            .chars()
            .all(|c| c.is_alphanumeric() || "-_./".contains(c));
        if !valid_chars {
            continue;
        }
        if let Some(dot) = t.rfind('.') {
            let ext = t[dot + 1..].to_lowercase();
            if FILE_EXTENSIONS.contains(&ext.as_str()) && !found.iter().any(|f| f == t) {
                found.push(t.to_string());
            }
        }
    }
    found
}

/// Pick the first candidate that names a real tool: exact match, then
/// case-insensitive substring in either direction.
fn pick_tool<'a>(candidates: &[&str], tool_names: &'a [String]) -> Option<&'a str> {
    for c in candidates {
        if let Some(t) = tool_names.iter().find(|t| t.as_str() == *c) {
            return Some(t.as_str());
        }
    }
    for c in candidates {
        let cl = c.to_lowercase();
        if let Some(t) = tool_names.iter().find(|t| {
            let tl = t.to_lowercase();
            tl.contains(&cl) || cl.contains(&tl)
        }) {
            return Some(t.as_str());
        }
    }
    None
}

/// Strip a leading verb phrase (`chạy`, `run`, ...) plus separators.
fn strip_verb<'a>(text: &'a str, verbs: &[&str]) -> &'a str {
    let lower = text.to_lowercase();
    for v in verbs {
        if lower.starts_with(v) {
            let rest = text[v.len()..].trim_start_matches([' ', ':', '—', '-']);
            if !rest.trim().is_empty() {
                return rest.trim();
            }
        }
    }
    text.trim()
}

/// True when `text` is a real test invocation, not prose:
/// `pytest -x` / `pytest tests/a.py` → true (run verbatim, keep flags);
/// `pytest cho module x` → false ("cho module x" is Vietnamese prose —
/// running it verbatim would fail, so fall back to the test tool).
fn is_explicit_test_cmd(text: &str) -> bool {
    const PREFIXES: &[&str] = &[
        "python3 -m pytest",
        "python -m pytest",
        "pytest",
        "npm test",
        "npm run",
    ];
    let lower = text.trim().to_lowercase();
    let rest = match PREFIXES.iter().find(|p| lower.starts_with(*p)) {
        Some(p) => lower[p.len()..].trim().to_string(),
        None => return false,
    };
    if rest.is_empty() {
        return true;
    }
    for tok in rest.split_whitespace() {
        if tok.starts_with('-') {
            continue; // flag
        }
        if tok.contains('/') || tok.contains('\\') {
            continue; // path
        }
        if is_file_ext_token(tok) || is_dotted_module(tok) {
            continue;
        }
        return false;
    }
    true
}

fn is_file_ext_token(tok: &str) -> bool {
    match tok.rfind('.') {
        Some(dot) if dot > 0 && dot + 1 < tok.len() => {
            let (stem, ext) = (&tok[..dot], &tok[dot + 1..]);
            !stem.is_empty()
                && ext.chars().all(|c| c.is_alphanumeric())
                && stem.chars().all(|c| c.is_alphanumeric() || c == '_' || c == '-')
        }
        _ => false,
    }
}

fn is_dotted_module(tok: &str) -> bool {
    tok.contains('.')
        && !tok.starts_with('.')
        && !tok.ends_with('.')
        && !tok.contains("..")
        && tok.chars().all(|c| c.is_alphanumeric() || c == '_' || c == '.')
}

fn test_command(goal: &str, files: &[String]) -> String {
    let gl = goal.to_lowercase();
    if let Some(f) = files.first() {
        if f.ends_with(".py") {
            return format!("python3 -m pytest {f}");
        }
        if [".js", ".ts", ".tsx", ".jsx"].iter().any(|e| f.ends_with(e)) {
            return "npm test".to_string();
        }
    }
    if gl.contains("pytest") {
        return "python3 -m pytest".to_string();
    }
    if gl.contains("npm") {
        return "npm test".to_string();
    }
    "python3 -m pytest".to_string()
}

/// Intent-based plan without a model. Every step uses a real tool from
/// `tool_names`; returns `[]` only when no usable tool exists.
/// (Port of `Brain._heuristic_plan`.)
fn plan_heuristic(goal: &str, tool_names: &[String]) -> Vec<PlanStep> {
    let g = goal.trim();
    if g.is_empty() || tool_names.is_empty() {
        return Vec::new();
    }
    let intent = classify_intent(g);
    let files = extract_files(g);
    let run = pick_tool(RUN_CANDIDATES, tool_names);
    let read = pick_tool(READ_CANDIDATES, tool_names);
    let step = |tool: &str, args: Value, desc: String| PlanStep {
        tool: tool.to_string(),
        args,
        desc,
    };

    match intent {
        "test" => {
            // Goal already spells out the test command (with flags) → run it
            // verbatim so flags/paths are preserved.
            let cmd_candidate = strip_verb(g, &["chạy", "run", "thực thi", "execute"]);
            if let Some(run_tool) = run {
                if is_explicit_test_cmd(cmd_candidate) {
                    return vec![step(
                        run_tool,
                        serde_json::json!({"command": cmd_candidate}),
                        format!("run tests: {cmd_candidate}"),
                    )];
                }
            }
            if let Some(t) = pick_tool(TEST_CANDIDATES, tool_names) {
                let args = match files.first() {
                    Some(f) => serde_json::json!({"path": f}),
                    None => serde_json::json!({}),
                };
                return vec![step(t, args, format!("run tests: {g}"))];
            }
            if let Some(run_tool) = run {
                let cmd = test_command(g, &files);
                return vec![step(
                    run_tool,
                    serde_json::json!({"command": cmd}),
                    format!("run tests: {cmd}"),
                )];
            }
        }
        "fix" => {
            let mut steps = Vec::new();
            if let (Some(read_tool), Some(f)) = (read, files.first()) {
                steps.push(step(
                    read_tool,
                    serde_json::json!({"path": f}),
                    format!("read failing file: {f}"),
                ));
            }
            if let Some(t) = pick_tool(TEST_CANDIDATES, tool_names) {
                steps.push(step(t, serde_json::json!({}), "re-run tests to reproduce the error".into()));
            } else if let Some(run_tool) = run {
                steps.push(step(
                    run_tool,
                    serde_json::json!({"command": "python3 -m pytest"}),
                    "re-run to reproduce the error".into(),
                ));
            }
            if !steps.is_empty() {
                return steps;
            }
        }
        "run" => {
            if let Some(run_tool) = run {
                let cmd = strip_verb(g, &["chạy", "run", "thực thi", "execute"]);
                return vec![step(
                    run_tool,
                    serde_json::json!({"command": cmd}),
                    format!("execute: {cmd}"),
                )];
            }
        }
        "read" => {
            if let (Some(read_tool), Some(f)) = (read, files.first()) {
                return vec![step(
                    read_tool,
                    serde_json::json!({"path": f}),
                    format!("read file: {f}"),
                )];
            }
        }
        "search" => {
            if let Some(s) = pick_tool(SEARCH_CANDIDATES, tool_names) {
                let kw = strip_verb(g, &["tìm kiếm", "tìm", "search", "grep"]);
                return vec![step(
                    s,
                    serde_json::json!({"pattern": kw}),
                    format!("search: {kw}"),
                )];
            }
        }
        "edit" => {
            // Without a model we cannot invent the new content; the useful
            // first step is reading the file so the next iteration (with a
            // model, or a human) has context.
            if let (Some(read_tool), Some(f)) = (read, files.first()) {
                return vec![step(
                    read_tool,
                    serde_json::json!({"path": f}),
                    format!("read file before editing: {f}"),
                )];
            }
        }
        _ => {}
    }

    // generic / anything unhandled: run the goal as a command, mirroring the
    // autonomous controller's own default plan.
    if let Some(run_tool) = run {
        return vec![step(
            run_tool,
            serde_json::json!({"command": g}),
            format!("execute: {g}"),
        )];
    }
    Vec::new()
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn new_session_id(prefix: &str) -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("{prefix}_{nanos:x}")
}

fn render_output(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        _ => serde_json::to_string(v).unwrap_or_default(),
    }
}

// ---------------------------------------------------------------------------
// Tests (mock loop factory — no real model needed)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use async_trait::async_trait;
    use crate::harness::types::{LlmResponse, Message, ToolSchema, Usage};
    use serde_json::json;
    use std::collections::VecDeque;
    use std::sync::Mutex;

    // -- fakes ----------------------------------------------------------

    /// A fake ReActLoop-in-a-box: the factory ignores LoopSpec details and
    /// hands out loops whose scripted LLM replies drive the turn.
    struct ScriptLlm {
        replies: Mutex<VecDeque<LlmResponse>>,
        seen_system: Mutex<Vec<Option<String>>>,
    }

    #[async_trait]
    impl crate::harness::types::LlmClient for ScriptLlm {
        async fn complete(&self, _m: &[Message]) -> Result<String, AgentError> {
            Ok(String::new())
        }
        async fn complete_with_tools(
            &self,
            messages: &[Message],
            _t: &[ToolSchema],
        ) -> Result<LlmResponse, AgentError> {
            let sys = messages
                .iter()
                .find(|m| m.role == "system")
                .map(|m| m.content.clone());
            self.seen_system.lock().unwrap().push(sys);
            self.replies
                .lock()
                .unwrap()
                .pop_front()
                .ok_or_else(|| AgentError::Llm("script exhausted".into()))
        }
    }

    struct NoopTools;
    #[async_trait]
    impl crate::harness::types::ToolExecutor for NoopTools {
        async fn execute(
            &self,
            call: &crate::harness::types::ToolCall,
        ) -> Result<crate::harness::types::ToolResult, AgentError> {
            Ok(crate::harness::types::ToolResult {
                tool_call_id: call.id.clone(),
                name: call.name.clone(),
                output: "noop".into(),
                is_error: false,
            })
        }
    }

    fn answer(text: &str) -> LlmResponse {
        LlmResponse {
            content: text.into(),
            thought: None,
            tool_calls: vec![],
            usage: Some(Usage { input_tokens: 1, output_tokens: 1 }),
            finish_reason: "stop".into(),
        }
    }

    fn brain_with(replies: Vec<LlmResponse>) -> (Brain, Arc<ScriptLlm>) {
        let llm = Arc::new(ScriptLlm {
            replies: Mutex::new(replies.into()),
            seen_system: Mutex::new(Vec::new()),
        });
        let llm2 = llm.clone();
        let factory: Arc<dyn Fn(&LoopSpec) -> ReActLoop + Send + Sync> =
            Arc::new(move |spec: &LoopSpec| {
                let cfg = crate::harness::HarnessConfig {
                    system_prompt: spec.system_prompt.clone(),
                    session_id: spec.session_id.clone(),
                    ..Default::default()
                };
                ReActLoop::new(llm2.clone(), Arc::new(NoopTools), cfg, vec![])
            });
        (Brain::new(factory), llm)
    }

    fn default_opts() -> RunOptions {
        RunOptions { strategy: "auto".into(), ..Default::default() }
    }

    // -- router -----------------------------------------------------------

    #[test]
    fn explicit_strategy_wins() {
        assert_eq!(Brain::choose_strategy("anything", "dag").0, BrainStrategy::Dag);
        assert_eq!(Brain::choose_strategy("anything", "MASS").0, BrainStrategy::Mass);
        let (s, note) = Brain::choose_strategy("anything", "bogus");
        assert_eq!(s, BrainStrategy::React);
        assert!(note.unwrap().contains("unknown strategy"));
    }

    #[test]
    fn auto_routes_dependency_hints_to_dag() {
        assert_eq!(Brain::choose_strategy("làm bước 1 rồi bước 2", "auto").0, BrainStrategy::Dag);
        assert_eq!(Brain::choose_strategy("first do this, then that", "auto").0, BrainStrategy::Dag);
        assert_eq!(Brain::choose_strategy("step 1 then step 2", "auto").0, BrainStrategy::Dag);
    }

    #[test]
    fn auto_routes_lists_to_mass() {
        assert_eq!(
            Brain::choose_strategy("compare:\n1. speed\n2. price\n3. quality", "auto").0,
            BrainStrategy::Mass
        );
        assert_eq!(
            Brain::choose_strategy("- fast\n- cheap", "auto").0,
            BrainStrategy::Mass
        );
    }

    #[test]
    fn auto_defaults_to_react() {
        assert_eq!(Brain::choose_strategy("what is the weather?", "auto").0, BrainStrategy::React);
    }

    #[test]
    fn no_false_positive_on_then_inside_word() {
        // "strengthen" contains "then" but not as a word.
        assert_eq!(Brain::choose_strategy("strengthen the code", "auto").0, BrainStrategy::React);
    }

    // -- run_react ----------------------------------------------------------

    #[tokio::test]
    async fn run_react_returns_answer() {
        let (brain, _) = brain_with(vec![answer("hello from react")]);
        let r = brain.run("hi", default_opts()).await;
        assert_eq!(r.strategy, "react");
        assert_eq!(r.text, "hello from react");
        assert!(r.errors.is_empty());
    }

    // -- parse_plan ----------------------------------------------------------

    #[test]
    fn parse_plan_builds_nodes_with_deps() {
        let nodes = parse_plan(
            "1. Read the code\n2. Analyse the results (waits for 1)\n3. Write the fix (depends on 1, 2)",
        )
        .unwrap();
        assert_eq!(nodes.len(), 3);
        assert_eq!(nodes[0].dependencies, Vec::<String>::new());
        assert_eq!(nodes[1].dependencies, vec!["1"]);
        assert_eq!(nodes[2].dependencies, vec!["1", "2"]);
        assert_eq!(nodes[1].name, "Analyse the results");
    }

    #[test]
    fn parse_plan_rejects_empty_and_bad_deps() {
        assert!(parse_plan("no numbered lines here").is_err());
        assert!(parse_plan("1. Do x (depends on 9)").is_err());
        assert!(parse_plan("1. Do x\n1. Do y").is_err());
    }

    // -- run_dag ---------------------------------------------------------------

    #[tokio::test]
    async fn run_dag_executes_nodes_in_order() {
        // planner answer + one answer per node (single-node plans use the
        // same factory; node ids become sub-sessions).
        let (brain, _) = brain_with(vec![
            answer("1. First thing\n2. Second thing (waits for 1)"),
            answer("node1 done"),
            answer("node2 done"),
        ]);
        let r = brain
            .run("do two things in order", RunOptions { strategy: "dag".into(), ..Default::default() })
            .await;
        assert_eq!(r.strategy, "dag");
        // Synthesis covers terminal nodes only (node 1 is depended upon);
        // both nodes' outputs are still recorded individually.
        assert_eq!(r.text, "[2] node2 done", "got: {}", r.text);
        assert_eq!(r.node_outputs["1"], json!("node1 done"));
        assert_eq!(r.node_outputs["2"], json!("node2 done"));
    }

    #[tokio::test]
    async fn run_dag_falls_back_on_unparseable_plan() {
        let (brain, _) = brain_with(vec![
            answer("this is not a numbered list at all"),
            answer("fallback answer"),
        ]);
        let r = brain
            .run("do it", RunOptions { strategy: "dag".into(), ..Default::default() })
            .await;
        assert_eq!(r.strategy, "react");
        assert_eq!(r.text, "fallback answer");
        assert!(r.errors["fallback"].contains("unparseable"));
    }

    // -- run_mass ----------------------------------------------------------------

    #[tokio::test]
    async fn run_mass_fans_out_to_all_roles() {
        let replies: Vec<LlmResponse> = (0..6).map(|i| answer(&format!("out {i}"))).collect();
        let (brain, llm) = brain_with(replies);
        let r = brain
            .run(
                "compare:\n- a\n- b",
                RunOptions { strategy: "mass".into(), ..Default::default() },
            )
            .await;
        assert_eq!(r.strategy, "mass");
        assert_eq!(r.agent_outputs.len(), 6);
        assert!(r.text.contains("[coder]"));
        assert!(r.text.contains("[validator]"));
        // each specialist got its role system prompt
        let systems = llm.seen_system.lock().unwrap();
        assert!(systems.iter().any(|s| s.as_deref().unwrap_or("").contains("You are a coder")));
    }

    #[tokio::test]
    async fn run_mass_reports_unknown_roles() {
        let (brain, _) = brain_with(vec![answer("x")]);
        let r = brain
            .run(
                "x",
                RunOptions {
                    strategy: "mass".into(),
                    roles: Some(vec!["coder".into(), "nope".into()]),
                    ..Default::default()
                },
            )
            .await;
        assert!(r.errors.contains_key("role:nope"));
        assert!(r.agent_outputs.contains_key("coder"));
    }

    // -- roles ---------------------------------------------------------------------

    #[test]
    fn roles_lookup_is_case_insensitive() {
        assert_eq!(get_role("CODER").unwrap().name, "coder");
        assert_eq!(get_role("  validator ").unwrap().name, "validator");
        assert!(get_role("ghost").is_none());
        assert_eq!(list_roles().len(), 6);
    }

    // -- heuristic planner ------------------------------------------------------------

    #[test]
    fn heuristic_plan_test_intent_runs_command_verbatim() {
        let tools = ["shell_run".to_string(), "read_file".to_string()];
        let steps = plan_heuristic("chạy pytest -x tests/test_a.py", &tools);
        assert_eq!(steps.len(), 1);
        assert_eq!(steps[0].tool, "shell_run");
        assert_eq!(steps[0].args["command"], json!("pytest -x tests/test_a.py"));
    }

    #[test]
    fn heuristic_plan_fix_intent_reads_file_first() {
        let tools = ["read_file".to_string(), "shell_run".to_string()];
        let steps = plan_heuristic("fix bug in agent/src/loop.py", &tools);
        assert!(!steps.is_empty());
        assert_eq!(steps[0].tool, "read_file");
        assert_eq!(steps[0].args["path"], json!("agent/src/loop.py"));
    }

    #[test]
    fn heuristic_plan_returns_empty_without_usable_tools() {
        let tools = ["unrelated_tool".to_string()];
        assert!(plan_heuristic("do something", &tools).is_empty());
        assert!(plan_heuristic("", &["shell_run".to_string()]).is_empty());
    }

    #[test]
    fn parse_plan_json_filters_unknown_tools() {
        let tools = ["read_file".to_string()];
        let steps = parse_plan_json(
            r#"[{"tool": "read_file", "args": {"path": "x"}, "desc": "read"}, {"tool": "ghost", "args": {}, "desc": "nope"}]"#,
            &tools,
        );
        assert_eq!(steps.len(), 1);
        assert_eq!(steps[0].tool, "read_file");
    }

    #[test]
    fn extract_files_finds_paths() {
        let files = extract_files("fix bug in agent/src/loop.py and README.md");
        assert!(files.contains(&"agent/src/loop.py".to_string()));
        assert!(files.contains(&"README.md".to_string()));
    }

    #[test]
    fn is_explicit_test_cmd_distinguishes_prose() {
        assert!(is_explicit_test_cmd("pytest -x"));
        assert!(is_explicit_test_cmd("pytest tests/a.py"));
        assert!(!is_explicit_test_cmd("pytest cho module x"));
        assert!(!is_explicit_test_cmd("hãy test giúp anh"));
    }
}
