//! ReAct execution loop: think → act → observe.
//!
//! Ports:
//! - `agent/src/loop/react.py` — `ReActLoop` (tool-call loop, token budget,
//!   parallel tool execution, session transcript)
//! - `agent/src/loop/tool_selector.py` — keyword-based tool pruning so the
//!   model only sees a relevant subset of schemas each turn
//! - `agent/src/loop/smart_turns.py` — [`TurnController`] smart early exit
//!
//! Deliberately *not* ported (environment integrations, not loop logic):
//! streaming callbacks, `StateDB` transcript persistence, `prompt_builder`,
//! the compressor, checkpoint snapshots and the approval Telegram bridge.
//! Those belong to the app layer that wires the harness up.

use std::sync::Arc;

use async_trait::async_trait;
use serde_json::Value;
use tokio::task::JoinSet;

use super::trajectory::{TokenLedger, Trajectory};
use super::types::{
    AgentError, LlmClient, LlmResponse, Message, ParsedToolCall, ReactStep, ToolCall,
    ToolExecutor, ToolSchema, TurnResult,
};
use super::HarnessConfig;

// ---------------------------------------------------------------------------
// Tool policy (guardrail + approval gate seam)
// ---------------------------------------------------------------------------

/// Pre-execution verdict for a tool call. Mirrors `ReActLoop._refusal`:
/// the Python version screens shell-bound tools through a guardrail and
/// everything outside `SAFE_TOOLS` through an approval gate. `Ok(())`
/// means "may proceed"; `Err(reason)` becomes an error observation.
#[async_trait]
pub trait ToolPolicy: Send + Sync {
    async fn check(&self, call: &ToolCall) -> Result<(), String>;
}

/// Default policy: allow everything (the app layer installs the real one).
pub struct AllowAllPolicy;

#[async_trait]
impl ToolPolicy for AllowAllPolicy {
    async fn check(&self, _call: &ToolCall) -> Result<(), String> {
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// ToolSelector — keyword-based schema pruning
// ---------------------------------------------------------------------------

/// One capability group: keywords that trigger it, plus how to find its tools.
#[derive(Debug, Clone)]
pub struct ToolGroup {
    pub name: &'static str,
    pub keywords: &'static [&'static str],
    pub prefixes: &'static [&'static str],
    pub tools: &'static [&'static str],
}

/// Always-visible core: generic, safe, useful for almost any prompt.
/// `search_tools` is the escape hatch: if pruning ever hides a needed tool,
/// the model can still discover it by keyword at runtime.
const CORE_TOOLS: &[&str] = &[
    "search_tools",
    "list_toolsets",
    "memory",
    "session_search",
    "learn_from_conversation",
    "verify_text",
    "rewind",
    "delegate_task",
    "connector_status",
    "digest_preview",
];

/// Fallback when no keyword matches: core + the most commonly useful tools.
const POPULAR_TOOLS: &[&str] = &[
    "read_file", "write_file", "shell_run", "process_list", "process_start",
    "web_search", "web_fetch", "deep_search", "execute_code", "git_status",
    "code_review", "news_search", "weather_now", "calendar_list",
    "task_create", "task_list", "cron_list", "artifact_markdown",
    "goal_list", "db_query", "ocr_image", "drive_list", "shopee_search",
    "broadcast_send", "verify_code",
];

/// Capability groups (English + Vietnamese keywords, matched
/// accent-insensitively). Tool names come from the real tool catalog.
const TOOL_GROUPS: &[ToolGroup] = &[
    ToolGroup {
        name: "filesystem",
        keywords: &["file", "doc", "read", "write", "ghi", "patch", "edit",
                    "sua", "folder", "thu muc", "directory", "path",
                    "duong dan", "append", "delete file", "xoa file", "rename"],
        prefixes: &["fs_"],
        tools: &["read_file", "write_file", "patch_file", "edit_file",
                 "append_file", "file_info"],
    },
    ToolGroup {
        name: "terminal",
        keywords: &["shell", "terminal", "command", "lenh", "chay", "run",
                    "exec", "execute", "process", "tien trinh", "script",
                    "console", "bash", "background job", "dich vu"],
        prefixes: &["shell_"],
        tools: &["sandbox_exec", "execute_code", "process_list", "process_poll",
                 "process_start", "process_kill"],
    },
    ToolGroup {
        name: "git",
        keywords: &["git", "commit", "push", "pull", "branch", "merge", "repo",
                    "diff", "checkout", "stash", "nhanh"],
        prefixes: &["git_"],
        tools: &[],
    },
    ToolGroup {
        name: "web",
        keywords: &["web", "website", "trang web", "url", "link", "search",
                    "tim", "google", "internet", "mang", "scrape", "cao",
                    "crawl", "browser", "trinh duyet", "screenshot",
                    "chup man hinh", "tham khao"],
        prefixes: &["browser_"],
        tools: &["web_search", "web_fetch", "web_extract", "web_extract_many",
                 "deep_search", "deep_think", "crawl_site", "read_sitemap",
                 "scrape_basic", "scrape_list", "scrape_product",
                 "scrape_table", "watch_url"],
    },
    ToolGroup {
        name: "code",
        keywords: &["code", "review", "debug", "test", "refactor", "ast",
                    "syntax", "dependency", "phu thuoc", "skill", "plugin",
                    "function", "class", "module", "lap trinh"],
        prefixes: &[],
        tools: &["list_skills", "view_skill", "install_skill", "search_tools",
                 "list_toolsets", "delegate_task", "optimize_workflow",
                 "research_brief", "ast_parse", "ast_inspect", "fuzzy_patch",
                 "code_review", "dep_cycles", "dep_impact", "rewind",
                 "verify_code"],
    },
    ToolGroup {
        name: "media",
        keywords: &["image", "anh", "picture", "photo", "ocr", "ve", "draw",
                    "captcha", "nhan dang", "chup anh"],
        prefixes: &[],
        tools: &["ocr_image", "ocr_image_url", "ocr_image_queued",
                 "captcha_solve_image", "captcha_balance"],
    },
    ToolGroup {
        name: "gmail",
        keywords: &["mail", "gmail", "email", "thu", "inbox", "hop thu",
                    "calendar", "lich", "drive", "tai lieu drive"],
        prefixes: &[],
        tools: &["gmail_search", "gmail_read", "gmail_draft", "gmail_send",
                 "gmail_discard", "calendar_list", "calendar_create",
                 "calendar_delete", "drive_list", "drive_search",
                 "drive_download", "drive_upload"],
    },
    ToolGroup {
        name: "memory",
        keywords: &["memory", "nho", "remember", "quen", "learn", "hoc",
                    "recall", "person", "nguoi", "contact", "lien he",
                    "note", "ghi chu", "ban be"],
        prefixes: &[],
        tools: &["memory", "session_search", "learn_from_conversation",
                 "recall_learned", "forget_learning", "person_add",
                 "person_note", "person_get", "person_list", "person_search"],
    },
    ToolGroup {
        name: "cron",
        keywords: &["cron", "schedule", "lich", "hen gio", "hen", "trigger",
                    "remind", "nhac", "task", "job", "periodic", "dinh ky",
                    "moi ngay", "dinh gio"],
        prefixes: &[],
        tools: &["cron_add", "cron_list", "cron_remove", "cron_run",
                 "trigger_create", "trigger_list", "trigger_delete",
                 "trigger_toggle", "task_create", "task_list", "task_status",
                 "task_resume", "task_cancel"],
    },
    ToolGroup {
        name: "database",
        keywords: &["database", "sql", "db", "du lieu", "query", "truy van",
                    "table", "bang", "insert", "co so du lieu"],
        prefixes: &[],
        tools: &["db_query", "db_insert", "db_update"],
    },
    ToolGroup {
        name: "integrations",
        keywords: &["github", "issue", "pull request", "spotify", "music",
                    "am nhac", "weather", "thoi tiet", "news", "tin tuc",
                    "shopee", "tiki", "lazada", "mua sam", "shop", "gia",
                    "price", "san pham"],
        prefixes: &[],
        tools: &["github_list_issues", "github_get_issue", "github_list_prs",
                 "github_read_file", "github_search_code", "spotify_search",
                 "spotify_now_playing", "spotify_play", "spotify_pause",
                 "weather_now", "weather_daily", "news_headlines",
                 "news_search", "shopee_search", "shopee_product",
                 "tiki_search", "tiki_product", "lazada_search"],
    },
    ToolGroup {
        name: "security",
        keywords: &["secret", "key", "bao mat", "security", "auth", "audit",
                    "permission", "quyen", "sanitize", "rate limit",
                    "phan quyen"],
        prefixes: &[],
        tools: &["secret_scan", "sanitize_input", "check_rate_limit",
                 "audit_query", "audit_get", "authz_add_user",
                 "authz_assign_role", "authz_revoke_role", "authz_check",
                 "authz_list_users", "authz_create_role", "authz_list_roles"],
    },
    ToolGroup {
        name: "artifacts",
        keywords: &["artifact", "document", "tai lieu", "markdown", "html",
                    "csv", "json", "report", "bao cao", "goal", "muc tieu",
                    "digest", "tom tat", "xuat file"],
        prefixes: &[],
        tools: &["artifact_markdown", "artifact_html", "artifact_csv",
                 "artifact_json", "goal_add", "goal_list", "goal_log",
                 "goal_pause", "goal_complete", "goal_get", "digest_preview"],
    },
    ToolGroup {
        name: "broadcast",
        keywords: &["broadcast", "channel", "kenh", "send message", "nhan tin",
                    "gui tin", "telegram", "gui hang loat"],
        prefixes: &[],
        tools: &["broadcast_send", "broadcast_history"],
    },
];

/// Lowercase + strip Vietnamese diacritics so "đọc" matches "doc".
/// (Rust std has no `unicodedata`; this covers the Vietnamese precomposed
/// range plus combining marks, which is all the selector needs.)
fn normalize(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.to_lowercase().chars() {
        if ('\u{0300}'..='\u{036f}').contains(&c) {
            continue; // combining diacritical mark
        }
        out.push(match c {
            'à' | 'á' | 'ạ' | 'ả' | 'ã' | 'â' | 'ầ' | 'ấ' | 'ậ' | 'ẩ' | 'ẫ'
            | 'ă' | 'ằ' | 'ắ' | 'ặ' | 'ẳ' | 'ẵ' => 'a',
            'è' | 'é' | 'ẹ' | 'ẻ' | 'ẽ' | 'ê' | 'ề' | 'ế' | 'ệ' | 'ể' | 'ễ' => 'e',
            'ì' | 'í' | 'ị' | 'ỉ' | 'ĩ' => 'i',
            'ò' | 'ó' | 'ọ' | 'ỏ' | 'õ' | 'ô' | 'ồ' | 'ố' | 'ộ' | 'ổ' | 'ỗ'
            | 'ơ' | 'ờ' | 'ớ' | 'ợ' | 'ở' | 'ỡ' => 'o',
            'ù' | 'ú' | 'ụ' | 'ủ' | 'ũ' | 'ư' | 'ừ' | 'ứ' | 'ự' | 'ử' | 'ữ' => 'u',
            'ỳ' | 'ý' | 'ỵ' | 'ỷ' | 'ỹ' => 'y',
            'đ' => 'd',
            other => other,
        });
    }
    out
}

/// Select a small, prompt-relevant subset of tool schemas.
///
/// Keyword-based: fast (no model call), deterministic, easy to audit.
/// Always keeps core tools first; matched groups follow in definition order;
/// a keyword-less prompt falls back to core + popular tools.
#[derive(Debug, Clone)]
pub struct ToolSelector {
    core_tools: Vec<String>,
    groups: Vec<ToolGroup>,
    popular_tools: Vec<String>,
    /// Exclusive upper bound: `select` never returns more than
    /// `max_tools - 1` schemas (target: <40 per turn).
    max_tools: usize,
}

impl Default for ToolSelector {
    fn default() -> Self {
        Self::new()
    }
}

impl ToolSelector {
    pub fn new() -> Self {
        Self {
            core_tools: CORE_TOOLS.iter().map(|s| s.to_string()).collect(),
            groups: TOOL_GROUPS.to_vec(),
            popular_tools: POPULAR_TOOLS.iter().map(|s| s.to_string()).collect(),
            max_tools: 40,
        }
    }

    pub fn with_max_tools(mut self, max_tools: usize) -> Self {
        self.max_tools = max_tools.max(2);
        self
    }

    /// Groups whose keywords appear in the prompt.
    pub fn matched_groups(&self, prompt: &str) -> Vec<&ToolGroup> {
        let text = normalize(prompt);
        if text.is_empty() {
            return Vec::new();
        }
        self.groups
            .iter()
            .filter(|g| {
                g.keywords
                    .iter()
                    .any(|kw| !kw.is_empty() && text.contains(&normalize(kw)))
            })
            .collect()
    }

    /// Pick the schemas relevant to `prompt`.
    ///
    /// Unknown tool names are skipped, so a stale catalog never breaks
    /// selection. Result length is always `< max_tools`.
    pub fn select(&self, prompt: &str, all_schemas: &[ToolSchema]) -> Vec<ToolSchema> {
        let cap = self.max_tools.saturating_sub(1);
        if cap < 1 || all_schemas.is_empty() {
            return Vec::new();
        }
        // name -> schema; first occurrence wins, definition order preserved.
        let mut index: std::collections::HashMap<&str, &ToolSchema> = std::collections::HashMap::new();
        for schema in all_schemas {
            index.entry(schema.name.as_str()).or_insert(schema);
        }

        let mut candidates: Vec<&str> =
            self.core_tools.iter().map(String::as_str).collect();
        let matched = self.matched_groups(prompt);
        if matched.is_empty() {
            candidates.extend(self.popular_tools.iter().map(String::as_str));
        } else {
            for group in matched {
                candidates.extend(group.tools.iter().copied());
                for prefix in group.prefixes {
                    candidates.extend(
                        index.keys().filter(|name| name.starts_with(prefix)).copied(),
                    );
                }
            }
        }

        let mut selected = Vec::new();
        let mut seen = std::collections::HashSet::new();
        for name in candidates {
            if seen.insert(name) {
                if let Some(schema) = index.get(name) {
                    selected.push((*schema).clone());
                    if selected.len() >= cap {
                        break;
                    }
                }
            }
        }
        selected
    }

    /// Rough token estimate for a schema list: serialized chars / 4.
    pub fn estimate_tokens(schemas: &[ToolSchema]) -> usize {
        schemas
            .iter()
            .map(|s| serde_json::to_string(s).map(|j| j.len()).unwrap_or(0))
            .sum::<usize>()
            / 4
    }
}

/// Backward-compatible dispatch: with a selector return the pruned subset,
/// without one return the full schema list exactly as before.
pub fn select_or_all(
    selector: Option<&ToolSelector>,
    prompt: &str,
    all_schemas: &[ToolSchema],
) -> Vec<ToolSchema> {
    match selector {
        None => all_schemas.to_vec(),
        Some(sel) => sel.select(prompt, all_schemas),
    }
}

// ---------------------------------------------------------------------------
// TurnController — smart early exit (port of smart_turns.py)
// ---------------------------------------------------------------------------

/// Stable per-turn signature of a tool-call list: (name, canonical-args-json).
/// `serde_json::Map` is a `BTreeMap` by default, so `to_string` on a JSON
/// object already yields keys in sorted order — canonical like Python's
/// `json.dumps(args, sort_keys=True)`.
fn call_signature(tool_calls: &[(String, Value)]) -> Vec<(String, String)> {
    tool_calls
        .iter()
        .map(|(name, args)| {
            let args_key =
                serde_json::to_string(args).unwrap_or_else(|_| format!("{args:?}"));
            (name.clone(), args_key)
        })
        .collect()
}

fn is_blank(text: &str) -> bool {
    text.trim().is_empty()
}

/// Decides when a ReAct loop should stop early instead of burning `max_turns`
/// while stuck: repeating the exact same tool call, getting empty
/// observations, or hitting the same error over and over.
///
/// Keep one controller per prompt: call [`record_turn`](Self::record_turn)
/// after each turn, then [`should_continue`](Self::should_continue).
#[derive(Debug, Clone)]
pub struct TurnController {
    pub max_turns: usize,
    patience: u32,
    signatures: Vec<Vec<(String, String)>>,
    observations: Vec<String>,
    error_tool: String,
    error_streak: u32,
}

impl TurnController {
    pub fn new(max_turns: usize) -> Self {
        Self {
            max_turns,
            patience: 2,
            signatures: Vec::new(),
            observations: Vec::new(),
            error_tool: String::new(),
            error_streak: 0,
        }
    }

    /// How many turns have been recorded so far.
    pub fn turns_used(&self) -> usize {
        self.signatures.len()
    }

    /// Forget all history; reuse the controller for a fresh prompt.
    pub fn reset(&mut self) {
        self.signatures.clear();
        self.observations.clear();
        self.error_tool.clear();
        self.error_streak = 0;
    }

    /// Store this turn's calls and observations for future comparisons.
    pub fn record_turn(&mut self, tool_calls: &[(String, Value)], observations: &[String]) {
        self.signatures.push(call_signature(tool_calls));
        self.observations.extend(observations.iter().cloned());
    }

    /// Return `(continue?, reason)` with reason one of
    /// `"repeated_tool_calls"`, `"empty_observations"`, `"repeated_errors"`
    /// or `"ok"`.
    ///
    /// The caller records the turn first, so the current turn is already the
    /// last history entry. The turn bound itself is enforced by the loop's
    /// own `max_turns`, not here.
    pub fn should_continue(
        &mut self,
        tool_calls: &[(String, Value)],
        observations: &[String],
    ) -> (bool, &'static str) {
        let sig = call_signature(tool_calls);

        // 1) The model issued the exact same calls (names + arguments) as the
        //    previous turn: stuck in a loop, more turns will not help.
        if !sig.is_empty()
            && self.signatures.len() >= 2
            && self.signatures[self.signatures.len() - 1]
                == self.signatures[self.signatures.len() - 2]
        {
            return (false, "repeated_tool_calls");
        }

        // 2) Two observations in a row carry no information: the tools are
        //    returning nothing, the model has nothing new to work with.
        if self.observations.len() >= 2 {
            let n = self.observations.len();
            if is_blank(&self.observations[n - 1]) && is_blank(&self.observations[n - 2]) {
                return (false, "empty_observations");
            }
        }

        // 3) The same single tool errored `patience + 1` turns in a row.
        //    Also catches several erroring observations for one tool inside
        //    the current turn.
        let single_tool = if sig.len() == 1 { Some(sig[0].0.as_str()) } else { None };
        if let Some(tool) = single_tool {
            if !observations.is_empty() && observations.iter().all(|o| o.contains("Error")) {
                if tool == self.error_tool {
                    self.error_streak += 1;
                } else {
                    self.error_tool = tool.to_string();
                    self.error_streak = 1;
                }
            } else {
                self.error_tool.clear();
                self.error_streak = 0;
            }
        } else {
            self.error_tool.clear();
            self.error_streak = 0;
        }
        if self.error_streak > self.patience {
            return (false, "repeated_errors");
        }

        (true, "ok")
    }
}

// ---------------------------------------------------------------------------
// ReActLoop
// ---------------------------------------------------------------------------

/// Parts of the loop shared across concurrently-executed tool calls
/// (cloned `Arc`s, so `tokio::spawn` gets `'static` futures).
#[derive(Clone)]
struct Shared {
    llm: Arc<dyn LlmClient>,
    tools: Arc<dyn ToolExecutor>,
    config: HarnessConfig,
    selector: Option<ToolSelector>,
    policy: Option<Arc<dyn ToolPolicy>>,
    tool_schemas: Vec<ToolSchema>,
}

/// The ReAct loop: drives model → tool → observation turns until the model
/// answers without requesting a tool, the turn budget is hit, the smart
/// controller bails out, or `max_turns` is exhausted.
///
/// `run_turn` borrows `&mut self` because the live context (messages,
/// trajectory, token ledger) is per-turn mutable state — exactly like the
/// Python loop's `self.messages` / `self.history`.
pub struct ReActLoop {
    shared: Shared,
    messages: Vec<Message>,
    trajectory: Trajectory,
    controller: TurnController,
    ledger: TokenLedger,
    budget_exceeded: bool,
}

impl ReActLoop {
    pub fn new(
        llm: Arc<dyn LlmClient>,
        tools: Arc<dyn ToolExecutor>,
        config: HarnessConfig,
        tool_schemas: Vec<ToolSchema>,
    ) -> Self {
        let controller = TurnController::new(config.max_turns);
        let session_id = config.session_id.clone();
        Self {
            shared: Shared { llm, tools, config, selector: None, policy: None, tool_schemas },
            messages: Vec::new(),
            trajectory: Trajectory::new(session_id),
            controller,
            ledger: TokenLedger::default(),
            budget_exceeded: false,
        }
    }

    pub fn with_selector(mut self, selector: ToolSelector) -> Self {
        self.shared.selector = Some(selector);
        self
    }

    pub fn with_policy(mut self, policy: Arc<dyn ToolPolicy>) -> Self {
        self.shared.policy = Some(policy);
        self
    }

    pub fn trajectory(&self) -> &Trajectory {
        &self.trajectory
    }

    pub fn messages(&self) -> &[Message] {
        &self.messages
    }

    pub fn budget_exceeded(&self) -> bool {
        self.budget_exceeded
    }

    /// Start a fresh conversation without rebuilding the loop.
    pub fn reset(&mut self) {
        self.messages.clear();
        self.trajectory = Trajectory::new(self.shared.config.session_id.clone());
        self.controller.reset();
        self.ledger = TokenLedger::default();
        self.budget_exceeded = false;
    }

    /// Drive the model/tool loop until the model answers without requesting
    /// a tool. Mirrors `ReActLoop._run_live_turn`.
    pub async fn run_turn(&mut self, prompt: &str) -> Result<TurnResult, AgentError> {
        if let Some(system) = &self.shared.config.system_prompt {
            if !system.trim().is_empty() {
                self.messages.push(Message::system(system.clone()));
            }
        }
        self.messages.push(Message::user(prompt));

        self.controller.reset();
        self.controller.max_turns = self.shared.config.max_turns;
        self.ledger = TokenLedger::default();
        self.budget_exceeded = false;

        let max_turns = self.shared.config.max_turns;
        let mut trace: Vec<ReactStep> = Vec::new();
        let mut final_text = String::new();
        let mut error: Option<String> = None;
        let mut stop_reason = "ok".to_string();
        let mut broke = false;

        for _ in 0..max_turns {
            // Spend ceiling: stop spending, keep the last observation.
            if let Some(budget) = self.shared.config.token_budget {
                if self.ledger.budget_exceeded(budget) {
                    error = Some(format!(
                        "Token budget of {budget} exhausted after {} tokens.",
                        self.ledger.total()
                    ));
                    self.budget_exceeded = true;
                    final_text = trace.last().and_then(|s| s.observation.clone()).unwrap_or_default();
                    stop_reason = "budget_exhausted".to_string();
                    broke = true;
                    break;
                }
            }

            let schemas = select_or_all(self.shared.selector.as_ref(), prompt, &self.shared.tool_schemas);
            let resp: LlmResponse = self
                .shared
                .llm
                .complete_with_tools(&self.messages, &schemas)
                .await
                .map_err(|e| AgentError::Llm(e.to_string()))?;

            if let Some(usage) = &resp.usage {
                self.ledger.record(usage.input_tokens, usage.output_tokens);
            }

            if resp.tool_calls.is_empty() {
                final_text = resp.content.clone();
                self.messages.push(Message::assistant(&final_text));
                broke = true;
                break;
            }

            // Record the assistant turn (with its tool calls) before executing.
            self.messages.push(Message {
                role: "assistant".to_string(),
                content: resp.content.clone(),
                tool_calls: Some(
                    resp.tool_calls
                        .iter()
                        .map(|tc| ToolCall { id: tc.id.clone(), name: tc.name.clone(), arguments: tc.arguments.clone() })
                        .collect(),
                ),
                tool_call_id: None,
            });

            let thought = resp.thought.clone().unwrap_or_default();
            let outcomes = execute_calls(&self.shared, &resp.tool_calls).await;

            let sig: Vec<(String, Value)> = resp
                .tool_calls
                .iter()
                .map(|tc| (tc.name.clone(), tc.arguments.clone()))
                .collect();
            let mut turn_observations: Vec<String> = Vec::with_capacity(outcomes.len());

            // Recording stays strictly sequential in the original
            // tool_calls order, however the calls were executed.
            for (call, observation, _is_error) in outcomes {
                turn_observations.push(observation.clone());
                trace.push(ReactStep {
                    thought: thought.clone(),
                    action: Some(call.clone()),
                    observation: Some(observation.clone()),
                });
                self.trajectory.record_step(
                    thought.clone(),
                    Some(call.name.clone()),
                    Some(call.arguments.clone()),
                    Some(observation.clone()),
                );
                self.messages.push(Message::tool(&call.id, &observation));
            }

            // --- smart early exit -------------------------------------
            if self.shared.config.smart_exit {
                self.controller.record_turn(&sig, &turn_observations);
                let (keep_going, reason) = self.controller.should_continue(&sig, &turn_observations);
                if !keep_going {
                    // Deliberate early stop, not a failure: keep the last
                    // observation as the answer without flagging an error.
                    final_text = trace.last().and_then(|s| s.observation.clone()).unwrap_or_default();
                    stop_reason = format!("early_exit:{reason}");
                    broke = true;
                    break;
                }
            }
            // ----------------------------------------------------------
        }

        if !broke {
            error = Some(format!("Reached max_turns={max_turns} without a final answer."));
            final_text = trace.last().and_then(|s| s.observation.clone()).unwrap_or_default();
            stop_reason = "max_turns".to_string();
        }

        // The Python loop appends a per-prompt summary to self.history; the
        // trajectory already carries every step, so nothing extra is needed.
        Ok(TurnResult {
            success: error.is_none(),
            final_answer: if final_text.is_empty() { "No response.".to_string() } else { final_text },
            steps: trace,
            input_tokens: self.ledger.input_tokens,
            output_tokens: self.ledger.output_tokens,
            stop_reason,
            error,
        })
    }

    /// Execute a single tool programmatically (no model involved).
    /// Mirrors `ReActLoop.step(action=...)`.
    pub async fn run_single_tool(&mut self, thought: &str, call: ToolCall) -> ReactStep {
        let parsed = ParsedToolCall {
            id: call.id.clone(),
            name: call.name.clone(),
            arguments: call.arguments.clone(),
            args_error: None,
        };
        let (call, observation, _is_error) = execute_one(&self.shared, &parsed).await;
        let step = ReactStep {
            thought: thought.to_string(),
            action: Some(call.clone()),
            observation: Some(observation.clone()),
        };
        self.trajectory.record_step(
            thought,
            Some(call.name.clone()),
            Some(call.arguments.clone()),
            Some(observation),
        );
        step
    }
}

/// Execute one parsed call: malformed-args and unknown-tool cases never reach
/// a tool; the policy verdict maps to the Python `_refusal` error strings.
async fn execute_one(shared: &Shared, parsed: &ParsedToolCall) -> (ToolCall, String, bool) {
    let call = ToolCall {
        id: parsed.id.clone(),
        name: parsed.name.clone(),
        arguments: parsed.arguments.clone(),
    };
    if let Some(args_error) = &parsed.args_error {
        return (
            call,
            format!(
                "Error: tool '{}' was called with malformed JSON arguments ({}). Retry with valid JSON.",
                parsed.name, args_error
            ),
            true,
        );
    }
    if !shared.tool_schemas.iter().any(|s| s.name == parsed.name) {
        return (call, format!("Error: Tool '{}' not found", parsed.name), true);
    }
    if let Some(policy) = &shared.policy {
        if let Err(reason) = policy.check(&call).await {
            return (call, format!("Error: {reason}"), true);
        }
    }
    match shared.tools.execute(&call).await {
        Ok(result) => (call, result.output, result.is_error),
        Err(e) => (call, format!("Error executing '{}': {e}", parsed.name), true),
    }
}

/// Execute one assistant turn's tool calls, returning
/// `(call, observation, is_error)` in the original call order.
///
/// Parallel mode fans out via `tokio::spawn` (one slow or failing tool
/// cannot block the rest); a panicking task becomes an error observation,
/// never a broken turn.
async fn execute_calls(
    shared: &Shared,
    parsed_calls: &[ParsedToolCall],
) -> Vec<(ToolCall, String, bool)> {
    if !shared.config.parallel_tools || parsed_calls.len() < 2 {
        let mut out = Vec::with_capacity(parsed_calls.len());
        for pc in parsed_calls {
            out.push(execute_one(shared, pc).await);
        }
        return out;
    }

    let mut set: JoinSet<(usize, (ToolCall, String, bool))> = JoinSet::new();
    for (i, pc) in parsed_calls.iter().enumerate() {
        let shared = shared.clone();
        let pc = pc.clone();
        set.spawn(async move { (i, execute_one(&shared, &pc).await) });
    }
    let mut ordered: Vec<Option<(ToolCall, String, bool)>> =
        (0..parsed_calls.len()).map(|_| None).collect();
    while let Some(joined) = set.join_next().await {
        match joined {
            Ok((i, outcome)) => ordered[i] = Some(outcome),
            Err(e) => {
                // A task that failed to join at all (panic/cancel) still
                // yields an error observation in its original slot.
                let idx = ordered.iter().position(|o| o.is_none()).unwrap_or(0);
                let pc = &parsed_calls[idx.min(parsed_calls.len().saturating_sub(1))];
                ordered[idx] = Some((
                    ToolCall { id: pc.id.clone(), name: pc.name.clone(), arguments: pc.arguments.clone() },
                    format!("Error: tool task failed to complete: {e}"),
                    true,
                ));
            }
        }
    }
    ordered
        .into_iter()
        .map(|o| o.expect("every spawned tool call fills its slot"))
        .collect()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::harness::types::ToolResult;
    use serde_json::json;
    use std::collections::{HashMap, VecDeque};
    use std::sync::Mutex;
    use std::time::Duration;

    // -- fakes ----------------------------------------------------------

    struct ScriptLlm {
        script: Mutex<VecDeque<LlmResponse>>,
    }

    impl ScriptLlm {
        fn new(responses: Vec<LlmResponse>) -> Arc<Self> {
            Arc::new(Self { script: Mutex::new(responses.into()) })
        }
    }

    #[async_trait]
    impl LlmClient for ScriptLlm {
        async fn complete(&self, _messages: &[Message]) -> Result<String, AgentError> {
            Ok(String::new())
        }

        async fn complete_with_tools(
            &self,
            _messages: &[Message],
            _tools: &[ToolSchema],
        ) -> Result<LlmResponse, AgentError> {
            self.script
                .lock()
                .unwrap()
                .pop_front()
                .ok_or_else(|| AgentError::Llm("script exhausted".into()))
        }
    }

    struct MapTools {
        outputs: HashMap<String, String>,
        delay_ms: u64,
    }

    #[async_trait]
    impl ToolExecutor for MapTools {
        async fn execute(&self, call: &ToolCall) -> Result<ToolResult, AgentError> {
            if self.delay_ms > 0 {
                tokio::time::sleep(Duration::from_millis(self.delay_ms)).await;
            }
            let output = self
                .outputs
                .get(&call.name)
                .cloned()
                .unwrap_or_else(|| format!("ran {}", call.name));
            Ok(ToolResult {
                tool_call_id: call.id.clone(),
                name: call.name.clone(),
                output,
                is_error: false,
            })
        }
    }

    fn resp(content: &str, calls: Vec<ParsedToolCall>) -> LlmResponse {
        let finish_reason = if calls.is_empty() { "stop" } else { "tool_calls" }.to_string();
        LlmResponse {
            content: content.into(),
            thought: Some("thinking".into()),
            tool_calls: calls,
            usage: Some(super::super::types::Usage { input_tokens: 10, output_tokens: 5 }),
            finish_reason,
        }
    }

    fn tc(id: &str, name: &str, args: Value) -> ParsedToolCall {
        ParsedToolCall { id: id.into(), name: name.into(), arguments: args, args_error: None }
    }

    fn schema(name: &str) -> ToolSchema {
        ToolSchema { name: name.into(), description: format!("{name} tool"), parameters: json!({}) }
    }

    fn harness(script: Vec<LlmResponse>, tools: HashMap<String, String>) -> ReActLoop {
        ReActLoop::new(
            ScriptLlm::new(script),
            Arc::new(MapTools { outputs: tools, delay_ms: 0 }),
            HarnessConfig::default(),
            vec![schema("read_file"), schema("shell_run")],
        )
    }

    fn tools_map(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect()
    }

    // -- loop behaviour ---------------------------------------------------

    #[tokio::test]
    async fn turn_without_tool_calls_returns_answer() {
        let mut h = harness(vec![resp("hello there", vec![])], tools_map(&[]));
        let r = h.run_turn("hi").await.unwrap();
        assert!(r.success);
        assert_eq!(r.final_answer, "hello there");
        assert_eq!(r.stop_reason, "ok");
        assert!(r.steps.is_empty());
        assert_eq!(r.input_tokens, 10);
        assert_eq!(r.output_tokens, 5);
        // user + assistant messages recorded
        assert_eq!(h.messages().len(), 2);
    }

    #[tokio::test]
    async fn tool_call_loop_feeds_observation_back() {
        let mut h = harness(
            vec![
                resp("", vec![tc("c1", "read_file", json!({"path": "a.txt"}))]),
                resp("the file says hello", vec![]),
            ],
            tools_map(&[("read_file", "hello")]),
        );
        let r = h.run_turn("read a.txt").await.unwrap();
        assert!(r.success);
        assert_eq!(r.final_answer, "the file says hello");
        assert_eq!(r.steps.len(), 1);
        assert_eq!(r.steps[0].action.as_ref().unwrap().name, "read_file");
        assert_eq!(r.steps[0].observation.as_deref(), Some("hello"));
        assert_eq!(r.steps[0].thought, "thinking");
        // trajectory recorded the step
        assert_eq!(h.trajectory().len(), 1);
        // a tool message followed the assistant's tool-call message
        assert!(h.messages().iter().any(|m| m.role == "tool" && m.content == "hello"));
    }

    #[tokio::test]
    async fn unknown_tool_becomes_error_observation() {
        let mut h = harness(
            vec![
                resp("", vec![tc("c1", "nope_tool", json!({}))]),
                resp("got it", vec![]),
            ],
            tools_map(&[]),
        );
        let r = h.run_turn("do it").await.unwrap();
        assert!(r.success);
        assert!(r.steps[0].observation.as_deref().unwrap().contains("not found"));
    }

    #[tokio::test]
    async fn malformed_args_become_retry_observation_without_calling_tool() {
        let bad = ParsedToolCall {
            id: "c1".into(),
            name: "read_file".into(),
            arguments: json!({}),
            args_error: Some("unexpected token".into()),
        };
        let mut h = harness(vec![resp("", vec![bad]), resp("ok", vec![])], tools_map(&[("read_file", "SHOULD NOT RUN")]));
        let r = h.run_turn("x").await.unwrap();
        assert!(r.success);
        let obs = r.steps[0].observation.as_deref().unwrap();
        assert!(obs.contains("malformed JSON"));
        assert!(!obs.contains("SHOULD NOT RUN"));
    }

    #[tokio::test]
    async fn max_turns_exhaustion_reports_error() {
        // Model always wants another tool call; loop must give up at 6.
        // smart_exit is off here to isolate the max_turns path (with it on,
        // the identical repeated calls would — correctly — early-exit first).
        let script: Vec<LlmResponse> = (0..10)
            .map(|i| resp("", vec![tc(&format!("c{i}"), "shell_run", json!({"cmd": format!("cmd{i}")}))]))
            .collect();
        let mut h = harness(script, tools_map(&[("shell_run", "ok")]));
        h.shared.config.smart_exit = false;
        let r = h.run_turn("loop forever").await.unwrap();
        assert!(!r.success);
        assert!(r.error.as_deref().unwrap().contains("max_turns=6"));
        assert_eq!(r.stop_reason, "max_turns");
        assert_eq!(r.steps.len(), 6);
    }

    #[tokio::test]
    async fn token_budget_stops_the_turn() {
        let mut h = harness(
            vec![
                resp("", vec![tc("c1", "shell_run", json!({}))]),
                resp("never reached", vec![]),
            ],
            tools_map(&[("shell_run", "out")]),
        );
        h.shared.config.token_budget = Some(12); // one turn costs 15
        let r = h.run_turn("x").await.unwrap();
        assert!(!r.success);
        assert!(r.error.as_deref().unwrap().contains("Token budget of 12 exhausted"));
        assert_eq!(r.stop_reason, "budget_exhausted");
        assert!(h.budget_exceeded());
        // keeps the last observation as the answer
        assert_eq!(r.final_answer, "out");
    }

    #[tokio::test]
    async fn smart_exit_stops_repeated_tool_calls() {
        let same = || tc("c1", "shell_run", json!({"cmd": "true"}));
        let mut h = harness(
            vec![resp("", vec![same()]), resp("", vec![same()]), resp("never", vec![])],
            tools_map(&[("shell_run", "ok")]),
        );
        let r = h.run_turn("x").await.unwrap();
        assert!(r.success); // deliberate early stop, not a failure
        assert_eq!(r.stop_reason, "early_exit:repeated_tool_calls");
        assert_eq!(r.steps.len(), 2);
    }

    #[tokio::test]
    async fn smart_exit_stops_on_empty_observations() {
        let mk = |id: &str| tc(id, "shell_run", json!({"cmd": id}));
        let mut h = harness(
            vec![
                resp("", vec![mk("c1")]),
                resp("", vec![mk("c2")]),
                resp("never", vec![]),
            ],
            tools_map(&[("shell_run", "   ")]), // blank observations
        );
        let r = h.run_turn("x").await.unwrap();
        assert_eq!(r.stop_reason, "early_exit:empty_observations");
    }

    #[tokio::test]
    async fn parallel_execution_preserves_call_order() {
        struct SlowFirst;
        #[async_trait]
        impl ToolExecutor for SlowFirst {
            async fn execute(&self, call: &ToolCall) -> Result<ToolResult, AgentError> {
                // reverse completion order: first call is slowest
                let delay = if call.id == "c1" { 60 } else { 5 };
                tokio::time::sleep(Duration::from_millis(delay)).await;
                Ok(ToolResult {
                    tool_call_id: call.id.clone(),
                    name: call.name.clone(),
                    output: format!("out-{}", call.id),
                    is_error: false,
                })
            }
        }
        let mut h = ReActLoop::new(
            ScriptLlm::new(vec![
                resp("", vec![tc("c1", "read_file", json!({})), tc("c2", "shell_run", json!({}))]),
                resp("done", vec![]),
            ]),
            Arc::new(SlowFirst),
            HarnessConfig::default(),
            vec![schema("read_file"), schema("shell_run")],
        );
        let r = h.run_turn("x").await.unwrap();
        assert!(r.success);
        // recorded strictly in the original call order despite c2 finishing first
        assert_eq!(r.steps[0].action.as_ref().unwrap().id, "c1");
        assert_eq!(r.steps[1].action.as_ref().unwrap().id, "c2");
        assert_eq!(r.steps[0].observation.as_deref(), Some("out-c1"));
    }

    #[tokio::test]
    async fn policy_refusal_becomes_error_observation() {
        struct DenyAll;
        #[async_trait]
        impl ToolPolicy for DenyAll {
            async fn check(&self, call: &ToolCall) -> Result<(), String> {
                Err(format!("'{}' was denied at the approval prompt.", call.name))
            }
        }
        let mut h = harness(
            vec![resp("", vec![tc("c1", "shell_run", json!({}))]), resp("ok", vec![])],
            tools_map(&[("shell_run", "SHOULD NOT RUN")]),
        )
        .with_policy(Arc::new(DenyAll));
        let r = h.run_turn("x").await.unwrap();
        assert!(r.success);
        let obs = r.steps[0].observation.as_deref().unwrap();
        assert!(obs.contains("denied at the approval prompt"));
        assert!(!obs.contains("SHOULD NOT RUN"));
    }

    #[tokio::test]
    async fn run_single_tool_executes_without_model() {
        let mut h = harness(vec![], tools_map(&[("read_file", "content")]));
        let step = h
            .run_single_tool(
                "manual read",
                ToolCall { id: "m1".into(), name: "read_file".into(), arguments: json!({"path": "x"}) },
            )
            .await;
        assert_eq!(step.observation.as_deref(), Some("content"));
        assert_eq!(step.thought, "manual read");
        assert_eq!(h.trajectory().len(), 1);
    }

    #[tokio::test]
    async fn tool_selector_prunes_schemas_before_llm_call() {
        struct CaptureSchemas {
            seen: Mutex<Vec<usize>>,
        }
        #[async_trait]
        impl LlmClient for CaptureSchemas {
            async fn complete(&self, _m: &[Message]) -> Result<String, AgentError> {
                Ok(String::new())
            }
            async fn complete_with_tools(
                &self,
                _m: &[Message],
                tools: &[ToolSchema],
            ) -> Result<LlmResponse, AgentError> {
                self.seen.lock().unwrap().push(tools.len());
                Ok(resp("done", vec![]))
            }
        }
        let llm = Arc::new(CaptureSchemas { seen: Mutex::new(vec![]) });
        let mut h = ReActLoop::new(
            llm.clone(),
            Arc::new(MapTools { outputs: HashMap::new(), delay_ms: 0 }),
            HarnessConfig::default(),
            vec![schema("read_file"), schema("gmail_search"), schema("search_tools")],
        )
        .with_selector(ToolSelector::new());
        h.run_turn("đọc file báo cáo").await.unwrap();
        let seen = llm.seen.lock().unwrap();
        // filesystem group matched ("doc file" ~ "đọc file"): read_file + core search_tools kept
        assert!(seen[0] >= 2);
        assert!(seen[0] < 40);
    }

    // -- ToolSelector -----------------------------------------------------

    fn catalog() -> Vec<ToolSchema> {
        ["search_tools", "read_file", "write_file", "shell_run", "gmail_search", "cron_add", "db_query"]
            .iter()
            .map(|n| schema(n))
            .collect()
    }

    #[test]
    fn selector_always_keeps_core_tools() {
        let sel = ToolSelector::new();
        let picked = sel.select("completely unrelated zzz", &catalog());
        let names: Vec<&str> = picked.iter().map(|s| s.name.as_str()).collect();
        assert!(names.contains(&"search_tools"));
    }

    #[test]
    fn selector_matches_vietnamese_keywords_accent_insensitively() {
        let sel = ToolSelector::new();
        let picked = sel.select("đọc file này giúp anh", &catalog());
        let names: Vec<&str> = picked.iter().map(|s| s.name.as_str()).collect();
        assert!(names.contains(&"read_file"));
        assert!(names.contains(&"write_file"));
        assert!(!names.contains(&"gmail_search"));
    }

    #[test]
    fn selector_matches_english_keywords() {
        let sel = ToolSelector::new();
        let picked = sel.select("check my gmail inbox", &catalog());
        let names: Vec<&str> = picked.iter().map(|s| s.name.as_str()).collect();
        assert!(names.contains(&"gmail_search"));
    }

    #[test]
    fn selector_respects_max_tools_cap() {
        let sel = ToolSelector::new().with_max_tools(4);
        let picked = sel.select("read the file please", &catalog());
        assert!(picked.len() < 4);
    }

    #[test]
    fn select_or_all_without_selector_returns_everything() {
        let all = catalog();
        let picked = select_or_all(None, "anything", &all);
        assert_eq!(picked.len(), all.len());
    }

    #[test]
    fn estimate_tokens_scales_with_schema_size() {
        let small = vec![schema("a")];
        let big: Vec<ToolSchema> = (0..20).map(|i| schema(&format!("tool_{i}_with_a_long_name"))).collect();
        assert!(ToolSelector::estimate_tokens(&big) > ToolSelector::estimate_tokens(&small));
    }

    #[test]
    fn normalize_strips_vietnamese_diacritics() {
        assert_eq!(normalize("Đọc File"), "doc file");
        assert_eq!(normalize("ỮNG DỤNG"), "ung dung");
    }

    // -- TurnController -----------------------------------------------------

    #[test]
    fn controller_detects_repeated_tool_calls() {
        let mut c = TurnController::new(6);
        let calls = vec![("shell_run".to_string(), json!({"cmd": "true"}))];
        let obs = vec!["ok".to_string()];
        c.record_turn(&calls, &obs);
        c.record_turn(&calls, &obs);
        let (keep, reason) = c.should_continue(&calls, &obs);
        assert!(!keep);
        assert_eq!(reason, "repeated_tool_calls");
        assert_eq!(c.turns_used(), 2);
    }

    #[test]
    fn controller_detects_empty_observations() {
        let mut c = TurnController::new(6);
        c.record_turn(&[("a".to_string(), json!({}))], &["  ".to_string()]);
        c.record_turn(&[("b".to_string(), json!({}))], &["".to_string()]);
        let (keep, reason) = c.should_continue(&[("b".to_string(), json!({}))], &["".to_string()]);
        assert!(!keep);
        assert_eq!(reason, "empty_observations");
    }

    #[test]
    fn controller_detects_repeated_errors() {
        // Same tool erroring with *different* args each turn: the repeated
        // call signature check must not fire first (it only matches identical
        // name+arguments), so the error streak is what stops the loop.
        let mut c = TurnController::new(6);
        for i in 0..3 {
            let calls = vec![("shell_run".to_string(), json!({"cmd": format!("x{i}")}))];
            c.record_turn(&calls, &["Error: boom".to_string()]);
            let _ = c.should_continue(&calls, &["Error: boom".to_string()]);
        }
        let calls = vec![("shell_run".to_string(), json!({"cmd": "x3"}))];
        let (keep, reason) = c.should_continue(&calls, &["Error: boom".to_string()]);
        assert!(!keep);
        assert_eq!(reason, "repeated_errors");
    }

    #[test]
    fn controller_stays_ok_on_healthy_turns() {
        let mut c = TurnController::new(6);
        for i in 0..3 {
            let calls = vec![("tool".to_string(), json!({"i": i}))];
            let obs = vec![format!("result {i}")];
            c.record_turn(&calls, &obs);
            let (keep, reason) = c.should_continue(&calls, &obs);
            assert!(keep);
            assert_eq!(reason, "ok");
        }
    }
}
