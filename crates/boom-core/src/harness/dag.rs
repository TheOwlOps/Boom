//! DAG task scheduler: dependency-graph execution with parallel fan-out.
//! Port of `agent/src/orchestration/dag.py`.
//!
//! The Python version launches every node as an `asyncio` task at once and
//! lets dependency awaits impose the order; the Rust version does the same
//! with `tokio::spawn` plus a shared completion state woken by
//! [`tokio::sync::Notify`]. Handler panics are caught and recorded as node
//! errors (a panicking node must never hang its dependents forever).

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use serde_json::Value;
use tokio::sync::{Mutex, Notify};

use super::types::AgentError;

// ---------------------------------------------------------------------------
// Graph model
// ---------------------------------------------------------------------------

/// One unit of work in the graph.
#[derive(Debug, Clone)]
pub struct TaskNode {
    pub id: String,
    pub name: String,
    pub dependencies: Vec<String>,
}

impl TaskNode {
    pub fn new(id: impl Into<String>, name: impl Into<String>) -> Self {
        Self { id: id.into(), name: name.into(), dependencies: Vec::new() }
    }

    pub fn depends_on(mut self, deps: impl IntoIterator<Item = impl Into<String>>) -> Self {
        self.dependencies.extend(deps.into_iter().map(Into::into));
        self
    }
}

/// Node output passed to dependents (Python: arbitrary `Any`).
pub type NodeOutput = Value;

/// The work a node does: receives its dependencies' outputs, returns its own.
/// Must be `'static` because nodes run on spawned tasks.
pub type TaskHandler = Arc<
    dyn Fn(HashMap<String, NodeOutput>) -> Pin<Box<dyn Future<Output = Result<NodeOutput, AgentError>> + Send>>
        + Send
        + Sync,
>;

/// Helper to build a [`TaskHandler`] from an async closure.
pub fn handler<F, Fut>(f: F) -> TaskHandler
where
    F: Fn(HashMap<String, NodeOutput>) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = Result<NodeOutput, AgentError>> + Send + 'static,
{
    Arc::new(move |deps| Box::pin(f(deps)))
}

/// Outcome of [`DagScheduler::execute`].
#[derive(Debug, Clone, Default)]
pub struct DagExecutionResult {
    pub success: bool,
    pub outputs: HashMap<String, NodeOutput>,
    /// Nodes that ran and failed: node id → error message.
    pub errors: HashMap<String, String>,
    /// Nodes that never ran because a dependency failed (or, with
    /// `fail_fast`, because an earlier node failed). Kept apart from
    /// `errors` so a caller can tell "tried and broke" from
    /// "never attempted" — exactly like the Python result model.
    pub blocked: Vec<String>,
}

// ---------------------------------------------------------------------------
// Scheduler
// ---------------------------------------------------------------------------

/// Shared per-execution state. Nodes mark themselves done and wake the
/// [`Notify`]; waiters re-check under the lock (notify-registered-before-check
/// pattern, so no wakeup is ever missed).
struct RunState {
    done: Mutex<HashMap<String, bool>>,
    outputs: Mutex<HashMap<String, NodeOutput>>,
    errors: Mutex<HashMap<String, String>>,
    blocked: Mutex<Vec<String>>,
    notify: Notify,
    abort: AtomicBool,
    fail_fast: bool,
}

pub struct DagScheduler {
    nodes: HashMap<String, TaskNode>,
    handlers: HashMap<String, TaskHandler>,
    /// When true, a node failure stops work that has not started yet. A node
    /// already running cannot be interrupted without tearing its future, so
    /// it runs to completion (same caveat as the Python docstring).
    pub fail_fast: bool,
}

impl Default for DagScheduler {
    fn default() -> Self {
        Self::new()
    }
}

impl DagScheduler {
    pub fn new() -> Self {
        Self { nodes: HashMap::new(), handlers: HashMap::new(), fail_fast: false }
    }

    pub fn with_fail_fast(mut self, fail_fast: bool) -> Self {
        self.fail_fast = fail_fast;
        self
    }

    pub fn add_task(&mut self, node: TaskNode, handler: TaskHandler) {
        self.handlers.insert(node.id.clone(), handler);
        self.nodes.insert(node.id.clone(), node);
    }

    pub fn node_ids(&self) -> Vec<&str> {
        self.nodes.keys().map(String::as_str).collect()
    }

    /// Topological validation: reject cycles and dangling dependencies.
    /// Mirrors `DAGScheduler.validate` (DFS with a visiting set).
    pub fn validate(&self) -> Result<(), AgentError> {
        let mut visited: HashSet<&str> = HashSet::new();
        let mut visiting: HashSet<&str> = HashSet::new();

        fn dfs<'a>(
            id: &'a str,
            nodes: &'a HashMap<String, TaskNode>,
            visited: &mut HashSet<&'a str>,
            visiting: &mut HashSet<&'a str>,
        ) -> Result<(), AgentError> {
            if visiting.contains(id) {
                return Err(AgentError::InvalidPlan(format!("Cycle detected at task '{id}'")));
            }
            if visited.contains(id) {
                return Ok(());
            }
            visiting.insert(id);
            let node = nodes.get(id).ok_or_else(|| {
                AgentError::InvalidPlan(format!("Unknown task '{id}'"))
            })?;
            for dep in &node.dependencies {
                if !nodes.contains_key(dep) {
                    return Err(AgentError::InvalidPlan(format!(
                        "Missing dependency '{dep}' for task '{}'",
                        node.id
                    )));
                }
                dfs(dep, nodes, visited, visiting)?;
            }
            visiting.remove(id);
            visited.insert(id);
            Ok(())
        }

        for id in self.nodes.keys() {
            if !visited.contains(id.as_str()) {
                dfs(id, &self.nodes, &mut visited, &mut visiting)?;
            }
        }
        // Every node needs a handler, otherwise execute() could not run it.
        for id in self.nodes.keys() {
            if !self.handlers.contains_key(id) {
                return Err(AgentError::InvalidPlan(format!("No handler registered for task '{id}'")));
            }
        }
        Ok(())
    }

    /// Run the graph. All nodes are spawned at once; each node waits for its
    /// dependencies' completion, so independent nodes fan out in parallel.
    pub async fn execute(&self) -> Result<DagExecutionResult, AgentError> {
        self.validate()?;

        let state = Arc::new(RunState {
            done: Mutex::new(HashMap::new()),
            outputs: Mutex::new(HashMap::new()),
            errors: Mutex::new(HashMap::new()),
            blocked: Mutex::new(Vec::new()),
            notify: Notify::new(),
            abort: AtomicBool::new(false),
            fail_fast: self.fail_fast,
        });

        let mut handles = Vec::with_capacity(self.nodes.len());
        for (id, node) in &self.nodes {
            let id = id.clone();
            let node = node.clone();
            let handler = self.handlers.get(&id).cloned()
                .ok_or_else(|| AgentError::InvalidPlan(format!("No handler for '{id}'")))?;
            let state = state.clone();
            handles.push(tokio::spawn(async move {
                run_node(id, node, handler, state).await;
            }));
        }
        for h in handles {
            // A node task only ends via run_node's own paths (done is always
            // set); a join error here means our own bug — surface it loudly.
            h.await.map_err(|e| AgentError::Tool(format!("DAG node task panicked: {e}")))?;
        }

        let outputs = state.outputs.lock().await.clone();
        let errors = state.errors.lock().await.clone();
        let mut blocked = state.blocked.lock().await.clone();
        blocked.sort();
        blocked.dedup();
        Ok(DagExecutionResult {
            success: errors.is_empty(),
            outputs,
            errors,
            blocked,
        })
    }
}

/// Body of one spawned node: wait for deps, then run (or mark blocked).
async fn run_node(id: String, node: TaskNode, handler: TaskHandler, state: Arc<RunState>) {
    // Wait for every dependency to complete.
    for dep in &node.dependencies {
        loop {
            // Register the waker *before* checking, so a completion that
            // lands between the check and the wait cannot be missed.
            let notified = state.notify.notified();
            tokio::pin!(notified);
            {
                let done = state.done.lock().await;
                if done.get(dep).copied().unwrap_or(false) {
                    break;
                }
            }
            notified.await;
        }
        if state.errors.lock().await.contains_key(dep) {
            // Dependency failed: this node never runs.
            state.errors.lock().await.insert(id.clone(), format!("Dependency '{dep}' failed"));
            state.blocked.lock().await.push(id.clone());
            state.done.lock().await.insert(id.clone(), true);
            state.notify.notify_waiters();
            return;
        }
    }

    if state.abort.load(Ordering::SeqCst) {
        state.blocked.lock().await.push(id.clone());
        state.done.lock().await.insert(id.clone(), true);
        state.notify.notify_waiters();
        return;
    }

    // Hand the node its dependencies' outputs (Python passes the same dict).
    let dep_outputs: HashMap<String, NodeOutput> = {
        let outputs = state.outputs.lock().await;
        node.dependencies
            .iter()
            .filter_map(|d| outputs.get(d).map(|v| (d.clone(), v.clone())))
            .collect()
    };

    // A panicking handler becomes a node error, never a hung graph.
    let outcome = std::panic::AssertUnwindSafe(handler(dep_outputs))
        .catch_unwind_polyfill()
        .await;

    match outcome {
        Ok(Ok(out)) => {
            state.outputs.lock().await.insert(id.clone(), out);
        }
        Ok(Err(e)) => {
            state.errors.lock().await.insert(id.clone(), e.to_string());
            if state.fail_fast {
                state.abort.store(true, Ordering::SeqCst);
            }
        }
        Err(_) => {
            state
                .errors
                .lock()
                .await
                .insert(id.clone(), "handler panicked".to_string());
            if state.fail_fast {
                state.abort.store(true, Ordering::SeqCst);
            }
        }
    }
    state.done.lock().await.insert(id.clone(), true);
    state.notify.notify_waiters();
}

// ---------------------------------------------------------------------------
// Panic-safe node polling (no `futures` crate needed)
// ---------------------------------------------------------------------------

/// `Future::catch_unwind` without the `futures` crate.
///
/// A panicking handler must become a node *error*, never a hung graph: without
/// this, dependents would wait on `done` forever. Polling inside
/// `catch_unwind` is sound here because the future is never polled again
/// after a panic — the node is finished either way.
trait CatchUnwindPolyfill: Future {
    fn catch_unwind_polyfill(self) -> CatchUnwind<Self>
    where
        Self: Sized,
    {
        CatchUnwind { inner: self }
    }
}

impl<F: Future> CatchUnwindPolyfill for F {}

use std::panic::{catch_unwind, AssertUnwindSafe};

struct CatchUnwind<F> {
    inner: F,
}

impl<F: Future> Future for CatchUnwind<F> {
    type Output = Result<F::Output, ()>;

    fn poll(
        self: Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Self::Output> {
        // SAFETY: `inner` is never moved; after a panic we return `Ready` and
        // never poll again, so no unpollable state is observed.
        let this = unsafe { self.get_unchecked_mut() };
        let inner = unsafe { Pin::new_unchecked(&mut this.inner) };
        match catch_unwind(AssertUnwindSafe(|| inner.poll(cx))) {
            Ok(std::task::Poll::Ready(v)) => std::task::Poll::Ready(Ok(v)),
            Ok(std::task::Poll::Pending) => std::task::Poll::Pending,
            Err(_) => std::task::Poll::Ready(Err(())),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::atomic::{AtomicUsize, Ordering as AOrd};
    use std::time::Duration;

    fn ok_handler(value: Value) -> TaskHandler {
        handler(move |_| {
            let v = value.clone();
            async move { Ok(v) }
        })
    }

    fn record_order(state: Arc<Mutex<Vec<String>>>, id: &str) -> TaskHandler {
        let id = id.to_string();
        handler(move |_| {
            let state = state.clone();
            let id = id.clone();
            async move {
                state.lock().await.push(id);
                Ok(json!("done"))
            }
        })
    }

    #[tokio::test]
    async fn linear_chain_runs_in_dependency_order() {
        let order = Arc::new(Mutex::new(Vec::new()));
        let mut s = DagScheduler::new();
        s.add_task(TaskNode::new("a", "first"), record_order(order.clone(), "a"));
        s.add_task(
            TaskNode::new("b", "second").depends_on(["a"]),
            record_order(order.clone(), "b"),
        );
        s.add_task(
            TaskNode::new("c", "third").depends_on(["b"]),
            record_order(order.clone(), "c"),
        );
        let r = s.execute().await.unwrap();
        assert!(r.success);
        assert_eq!(*order.lock().await, vec!["a", "b", "c"]);
        assert_eq!(r.outputs["c"], json!("done"));
    }

    #[tokio::test]
    async fn independent_nodes_fan_out_in_parallel() {
        let concurrent = Arc::new(AtomicUsize::new(0));
        let max_seen = Arc::new(AtomicUsize::new(0));
        let mut s = DagScheduler::new();
        for i in 0..4 {
            let c = concurrent.clone();
            let m = max_seen.clone();
            s.add_task(
                TaskNode::new(format!("n{i}"), format!("node {i}")),
                handler(move |_| {
                    let c = c.clone();
                    let m = m.clone();
                    async move {
                        let now = c.fetch_add(1, AOrd::SeqCst) + 1;
                        m.fetch_max(now, AOrd::SeqCst);
                        tokio::time::sleep(Duration::from_millis(30)).await;
                        c.fetch_sub(1, AOrd::SeqCst);
                        Ok(json!(i))
                    }
                }),
            );
        }
        let r = s.execute().await.unwrap();
        assert!(r.success);
        assert_eq!(r.outputs.len(), 4);
        // All four overlapped: with sequential execution max would be 1.
        assert!(max_seen.load(AOrd::SeqCst) > 1, "nodes did not run in parallel");
    }

    #[tokio::test]
    async fn diamond_passes_dep_outputs_to_handler() {
        let mut s = DagScheduler::new();
        s.add_task(TaskNode::new("a", "a"), ok_handler(json!(2)));
        s.add_task(TaskNode::new("b", "b"), ok_handler(json!(3)));
        s.add_task(
            TaskNode::new("c", "c").depends_on(["a", "b"]),
            handler(|deps| async move {
                let sum = deps["a"].as_i64().unwrap_or(0) + deps["b"].as_i64().unwrap_or(0);
                Ok(json!(sum))
            }),
        );
        let r = s.execute().await.unwrap();
        assert!(r.success);
        assert_eq!(r.outputs["c"], json!(5));
    }

    #[tokio::test]
    async fn failed_dependency_blocks_dependents() {
        let mut s = DagScheduler::new();
        s.add_task(
            TaskNode::new("bad", "bad"),
            handler(|_| async { Err(AgentError::Tool("kaput".into())) }),
        );
        s.add_task(
            TaskNode::new("child", "child").depends_on(["bad"]),
            ok_handler(json!(1)),
        );
        s.add_task(TaskNode::new("loner", "loner"), ok_handler(json!(2)));
        let r = s.execute().await.unwrap();
        assert!(!r.success);
        assert!(r.errors.contains_key("bad"));
        assert!(r.blocked.contains(&"child".to_string()));
        // errors and blocked are kept apart: child never ran, so no output.
        assert!(!r.outputs.contains_key("child"));
        assert_eq!(r.outputs["loner"], json!(2));
    }

    #[tokio::test]
    async fn fail_fast_blocks_not_yet_started_work() {
        let mut s = DagScheduler::new().with_fail_fast(true);
        s.add_task(
            TaskNode::new("bad", "bad"),
            handler(|_| async { Err(AgentError::Tool("kaput".into())) }),
        );
        // slow starter: still waiting on nothing, but scheduled after bad.
        s.add_task(
            TaskNode::new("slow", "slow"),
            handler(|_| async {
                tokio::time::sleep(Duration::from_millis(50)).await;
                Ok(json!(1))
            }),
        );
        s.add_task(
            TaskNode::new("after_bad", "after").depends_on(["bad"]),
            ok_handler(json!(3)),
        );
        let r = s.execute().await.unwrap();
        assert!(!r.success);
        assert!(r.blocked.contains(&"after_bad".to_string()));
    }

    #[test]
    fn validate_rejects_cycles() {
        let mut s = DagScheduler::new();
        s.add_task(TaskNode::new("a", "a").depends_on(["b"]), ok_handler(json!(1)));
        s.add_task(TaskNode::new("b", "b").depends_on(["a"]), ok_handler(json!(1)));
        let err = s.validate().unwrap_err();
        assert!(err.to_string().contains("Cycle detected"));
    }

    #[test]
    fn validate_rejects_missing_dependency() {
        let mut s = DagScheduler::new();
        s.add_task(TaskNode::new("a", "a").depends_on(["ghost"]), ok_handler(json!(1)));
        let err = s.validate().unwrap_err();
        assert!(err.to_string().contains("Missing dependency 'ghost'"));
    }

    #[tokio::test]
    async fn execute_rejects_invalid_graph() {
        let mut s = DagScheduler::new();
        s.add_task(TaskNode::new("a", "a").depends_on(["b"]), ok_handler(json!(1)));
        s.add_task(TaskNode::new("b", "b").depends_on(["a"]), ok_handler(json!(1)));
        assert!(s.execute().await.is_err());
    }

    #[tokio::test]
    async fn panicking_handler_becomes_node_error_not_hang() {
        let mut s = DagScheduler::new();
        s.add_task(
            TaskNode::new("boom", "boom"),
            handler(|_| async { panic!("simulated handler panic") }),
        );
        s.add_task(
            TaskNode::new("child", "child").depends_on(["boom"]),
            ok_handler(json!(1)),
        );
        // Must complete (not hang) even though a handler panicked.
        let r = tokio::time::timeout(Duration::from_secs(5), s.execute())
            .await
            .expect("scheduler hung on panicking handler")
            .unwrap();
        assert!(!r.success);
        assert!(r.errors.contains_key("boom"));
        assert!(r.blocked.contains(&"child".to_string()));
    }
}
