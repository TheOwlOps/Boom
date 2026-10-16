//! Shell tools — Rust port of `agent/src/tools/shell.py`.
//!
//! Safety model (mirrors the Python module):
//! * Commands are **never** run through a shell. A string command is
//!   tokenised with `shell-words` (shlex-style), so `;`, `|`, `&&`, `$(…​)`
//!   are passed as literal arguments, never interpreted.
//! * A blocklist rejects obviously destructive commands (`rm -rf /`,
//!   `mkfs`, `dd` on `/dev/*`, fork bombs, power commands).
//! * `cwd` is confined to the tool root so a call cannot escape the tree.
//! * Output is capped at 100 KiB per stream; `shell_run` kills the child
//!   after `timeout` seconds.

use std::collections::HashMap;
use std::path::Path;
use std::process::Stdio;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use serde_json::Value;
use tokio::io::AsyncReadExt;

use super::fs::tools_root;
use super::{opt_u64, req_str, schema_of, Tool, ToolError};

/// Output cap per stream, matching the Python module.
const MAX_OUTPUT_BYTES: usize = 100 * 1024;

/// Split a command into argv. Strings go through shell-words (never a shell).
fn split_cmd(args: &Value) -> Result<Vec<String>, ToolError> {
    match args.get("cmd") {
        Some(Value::Array(arr)) => arr
            .iter()
            .map(|v| {
                v.as_str()
                    .map(str::to_string)
                    .ok_or_else(|| ToolError::InvalidArgs("cmd array must contain strings".into()))
            })
            .collect(),
        Some(Value::String(s)) => shell_words::split(s)
            .map_err(|e| ToolError::InvalidArgs(format!("cannot parse command: {e}"))),
        _ => Err(ToolError::InvalidArgs(
            "missing required argument 'cmd' (string or array of strings)".into(),
        )),
    }
}

/// True when the raw command string looks like a fork bomb (`:(){ … };:`).
fn looks_like_fork_bomb(s: &str) -> bool {
    let mut idx = 0;
    while let Some(pos) = s[idx..].find(":()") {
        if s[idx + pos + 3..].trim_start().starts_with('{') {
            return true;
        }
        idx += pos + 3;
    }
    false
}

/// Reject dangerous commands. Port of `_is_blocked` in `shell.py`.
fn check_dangerous(argv: &[String]) -> Result<(), ToolError> {
    let Some(first) = argv.first() else {
        return Err(ToolError::InvalidArgs("empty command".into()));
    };
    let exe = Path::new(first)
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or(first)
        .to_string();

    if matches!(exe.as_str(), "shutdown" | "reboot" | "poweroff" | "halt" | "init") {
        return Err(ToolError::Blocked(format!("dangerous command '{exe}'")));
    }
    if exe == "mkfs" || exe.starts_with("mkfs.") {
        return Err(ToolError::Blocked(format!("dangerous command '{exe}'")));
    }
    if exe == "dd" {
        for arg in &argv[1..] {
            if arg.starts_with("if=/dev/") || arg.starts_with("of=/dev/") {
                return Err(ToolError::Blocked(
                    "dd touching raw devices is blocked".into(),
                ));
            }
        }
    }
    if exe == "rm" {
        if argv.iter().any(|a| a == "--no-preserve-root") {
            return Err(ToolError::Blocked("dangerous rm invocation".into()));
        }
        let mut flags = String::new();
        let mut targets = Vec::new();
        for a in &argv[1..] {
            if let Some(f) = a.strip_prefix('-') {
                if !f.starts_with('-') {
                    flags.push_str(f);
                }
            } else {
                targets.push(a.as_str());
            }
        }
        if flags.contains('r') && flags.contains('f') {
            for t in targets {
                let norm = t.trim_end_matches('/');
                if norm.is_empty() || norm == "/" || norm == "/*" {
                    return Err(ToolError::Blocked("rm -rf on filesystem root is blocked".into()));
                }
            }
        }
    }
    Ok(())
}

/// Resolve `cwd` (optional) and confine it to the tool root.
fn resolve_cwd(cwd: Option<&str>) -> Result<std::path::PathBuf, ToolError> {
    let root = tools_root().canonicalize().map_err(ToolError::Io)?;
    let target = match cwd {
        Some(c) => {
            let p = Path::new(c);
            let joined = if p.is_absolute() { p.to_path_buf() } else { root.join(p) };
            joined.canonicalize().map_err(|_| {
                ToolError::InvalidArgs(format!("cwd does not exist: '{c}'"))
            })?
        }
        None => root.clone(),
    };
    if !target.starts_with(&root) {
        return Err(ToolError::Blocked(format!(
            "cwd '{}' is outside the allowed root",
            target.to_string_lossy()
        )));
    }
    Ok(target)
}

/// Truncate a captured stream at 100 KiB, like the Python `_truncate`.
fn truncate_stream(data: &[u8]) -> String {
    let mut truncated = false;
    let mut slice = data;
    if slice.len() > MAX_OUTPUT_BYTES {
        slice = &slice[..MAX_OUTPUT_BYTES];
        truncated = true;
    }
    let mut s = String::from_utf8_lossy(slice).into_owned();
    if truncated {
        s.push_str("\n...[truncated at 100KB]...");
    }
    s
}

// ---------------------------------------------------------------------------
// shell_run
// ---------------------------------------------------------------------------

pub struct ShellRun;
#[async_trait]
impl Tool for ShellRun {
    fn name(&self) -> &str {
        "shell_run"
    }
    fn description(&self) -> &str {
        "Run a command synchronously (no shell), capturing stdout/stderr. \
         Kills the process after `timeout` seconds. Output capped at 100KB per stream."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("cmd", "string", "Command: a string (shlex-split, never a shell) or an argv array"),
                ("timeout", "integer", "Seconds before the process is killed (default 30)"),
                ("cwd", "string", "Working directory, confined to the tool root"),
            ],
            &["cmd"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let raw = args.get("cmd").and_then(Value::as_str).map(str::to_string);
        // Fork-bomb check on the raw string first: a bomb rarely survives
        // shlex-style splitting intact.
        if let Some(ref s) = raw {
            if looks_like_fork_bomb(s) {
                return Err(ToolError::Blocked(
                    "dangerous command (fork bomb pattern)".into(),
                ));
            }
        }
        let argv = split_cmd(&args)?;
        check_dangerous(&argv)?;
        let timeout_s = opt_u64(&args, "timeout", 30).clamp(1, 3600);
        let cwd = resolve_cwd(args.get("cwd").and_then(Value::as_str))?;

        let mut command = tokio::process::Command::new(&argv[0]);
        command
            .args(&argv[1..])
            .current_dir(&cwd)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let mut child = command.spawn().map_err(|e| {
            ToolError::Other(format!("failed to spawn '{}': {e}", argv[0]))
        })?;

        let mut stdout = child.stdout.take();
        let mut stderr = child.stderr.take();

        // Collect output, then wait — all under the timeout.
        let collect = async {
            let mut out = Vec::new();
            let mut err = Vec::new();
            if let Some(mut p) = stdout.take() {
                p.read_to_end(&mut out).await.map_err(ToolError::Io)?;
            }
            if let Some(mut p) = stderr.take() {
                p.read_to_end(&mut err).await.map_err(ToolError::Io)?;
            }
            let status = child.wait().await.map_err(ToolError::Io)?;
            Ok::<_, ToolError>((out, err, status))
        };

        match tokio::time::timeout(Duration::from_secs(timeout_s), collect).await {
            Ok(Ok((out, err, status))) => Ok(serde_json::json!({
                "exit_code": status.code(),
                "success": status.success(),
                "stdout": truncate_stream(&out),
                "stderr": truncate_stream(&err),
                "timed_out": false,
            })),
            Ok(Err(e)) => Err(e),
            Err(_) => {
                // Timeout: kill and reap so we never leak a zombie.
                let _ = child.kill().await;
                let _ = child.wait().await;
                Err(ToolError::Timeout(timeout_s))
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Background job registry for shell_bg / process_list / process_kill
// ---------------------------------------------------------------------------

struct BgJob {
    child: tokio::process::Child,
    cmd: String,
    pid: Option<u32>,
    started_unix: u64,
}

static JOBS: OnceLock<Arc<Mutex<HashMap<String, BgJob>>>> = OnceLock::new();

fn job_registry() -> Arc<Mutex<HashMap<String, BgJob>>> {
    JOBS.get_or_init(|| Arc::new(Mutex::new(HashMap::new())))
        .clone()
}

fn now_unix() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

// ---------------------------------------------------------------------------
// shell_bg
// ---------------------------------------------------------------------------

pub struct ShellBg;
#[async_trait]
impl Tool for ShellBg {
    fn name(&self) -> &str {
        "shell_bg"
    }
    fn description(&self) -> &str {
        "Start a command in the background (no shell). Returns a job id; \
         use process_list / process_kill to manage it. Stdio is detached."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("cmd", "string", "Command: a string (shlex-split, never a shell) or an argv array"),
                ("cwd", "string", "Working directory, confined to the tool root"),
            ],
            &["cmd"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let raw = args.get("cmd").and_then(Value::as_str).map(str::to_string);
        if let Some(ref s) = raw {
            if looks_like_fork_bomb(s) {
                return Err(ToolError::Blocked(
                    "dangerous command (fork bomb pattern)".into(),
                ));
            }
        }
        let argv = split_cmd(&args)?;
        check_dangerous(&argv)?;
        let cwd = resolve_cwd(args.get("cwd").and_then(Value::as_str))?;

        let mut command = tokio::process::Command::new(&argv[0]);
        command
            .args(&argv[1..])
            .current_dir(&cwd)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        let child = command.spawn().map_err(|e| {
            ToolError::Other(format!("failed to spawn '{}': {e}", argv[0]))
        })?;

        let job_id = uuid::Uuid::new_v4().to_string();
        let cmdline = argv.join(" ");
        let registry = job_registry();
        let pid = {
            let mut jobs = registry
                .lock()
                .map_err(|e| ToolError::Other(format!("job registry poisoned: {e}")))?;
            let pid = child.id();
            jobs.insert(
                job_id.clone(),
                BgJob {
                    pid,
                    child,
                    cmd: cmdline.clone(),
                    started_unix: now_unix(),
                },
            );
            pid
        };
        Ok(serde_json::json!({
            "job_id": job_id,
            "pid": pid,
            "cmd": cmdline,
        }))
    }
}

// ---------------------------------------------------------------------------
// process_list
// ---------------------------------------------------------------------------

pub struct ProcessList;
#[async_trait]
impl Tool for ProcessList {
    fn name(&self) -> &str {
        "process_list"
    }
    fn description(&self) -> &str {
        "List background jobs started by shell_bg, with running state and exit codes."
    }
    fn schema(&self) -> Value {
        schema_of(&[], &[])
    }
    async fn execute(&self, _args: Value) -> Result<Value, ToolError> {
        let registry = job_registry();
        let mut jobs = registry
            .lock()
            .map_err(|e| ToolError::Other(format!("job registry poisoned: {e}")))?;
        let mut list = Vec::new();
        for (id, job) in jobs.iter_mut() {
            let (running, exit_code) = match job.child.try_wait() {
                Ok(None) => (true, Value::Null),
                Ok(Some(status)) => (false, serde_json::json!(status.code())),
                Err(_) => (false, Value::Null),
            };
            list.push(serde_json::json!({
                "job_id": id,
                "cmd": job.cmd,
                "pid": job.pid,
                "running": running,
                "exit_code": exit_code,
                "started_unix": job.started_unix,
                "elapsed_secs": now_unix().saturating_sub(job.started_unix),
            }));
        }
        list.sort_by(|a, b| a["started_unix"].as_u64().cmp(&b["started_unix"].as_u64()));
        Ok(serde_json::json!({"jobs": list, "count": list.len()}))
    }
}

// ---------------------------------------------------------------------------
// process_kill
// ---------------------------------------------------------------------------

pub struct ProcessKill;
#[async_trait]
impl Tool for ProcessKill {
    fn name(&self) -> &str {
        "process_kill"
    }
    fn description(&self) -> &str {
        "Kill a background job started by shell_bg."
    }
    fn schema(&self) -> Value {
        schema_of(&[("job_id", "string", "Job id returned by shell_bg")], &["job_id"])
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let job_id = req_str(&args, "job_id")?;
        // Remove from the registry first, then kill — the lock must not be
        // held across `.await` (MutexGuard is not Send).
        let mut job = {
            let registry = job_registry();
            let mut jobs = registry
                .lock()
                .map_err(|e| ToolError::Other(format!("job registry poisoned: {e}")))?;
            jobs.remove(&job_id)
                .ok_or_else(|| ToolError::NotFound(format!("no such job: '{job_id}'")))?
        };
        // Best effort: kill, then reap.
        let _ = job.child.kill().await;
        let status = job.child.wait().await.ok();
        Ok(serde_json::json!({
            "ok": true,
            "job_id": job_id,
            "exit_code": status.and_then(|s| s.code()),
        }))
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::super::test_support::TestRoot;
    use super::*;
    use std::time::Instant;

    async fn run<T: Tool>(tool: T, args: Value) -> Result<Value, ToolError> {
        tool.execute(args).await
    }

    #[tokio::test]
    async fn run_echo_captures_output() {
        let _root = TestRoot::new();
        let out = run(
            ShellRun,
            serde_json::json!({"cmd": ["echo", "hello boom"]}),
        )
        .await
        .unwrap();
        assert_eq!(out["exit_code"], 0);
        assert_eq!(out["stdout"].as_str().unwrap().trim(), "hello boom");
        assert_eq!(out["timed_out"], false);
    }

    #[tokio::test]
    async fn run_string_command_is_split_not_shelled() {
        let _root = TestRoot::new();
        // `;` must be a literal argument, not a shell separator.
        let out = run(ShellRun, serde_json::json!({"cmd": "echo a;b"})).await.unwrap();
        assert_eq!(out["stdout"].as_str().unwrap().trim(), "a;b");
    }

    #[tokio::test]
    async fn run_timeout_kills_process() {
        let _root = TestRoot::new();
        let start = Instant::now();
        let err = run(
            ShellRun,
            serde_json::json!({"cmd": ["sleep", "60"], "timeout": 1}),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ToolError::Timeout(1)));
        assert!(start.elapsed() < Duration::from_secs(10));
    }

    #[tokio::test]
    async fn dangerous_commands_are_blocked() {
        let _root = TestRoot::new();
        for cmd in [
            serde_json::json!(["rm", "-rf", "/"]),
            serde_json::json!(["shutdown", "-h", "now"]),
            serde_json::json!(["mkfs.ext4", "/dev/sda1"]),
            serde_json::json!(["dd", "if=/dev/zero", "of=/dev/sda"]),
        ] {
            let err = run(ShellRun, serde_json::json!({"cmd": cmd}))
                .await
                .unwrap_err();
            assert!(err.to_string().contains("blocked"), "cmd={cmd}");
        }
        // fork bomb string
        let err = run(ShellRun, serde_json::json!({"cmd": ":(){ :|:& };:"}))
            .await
            .unwrap_err();
        assert!(err.to_string().contains("bomb"));
    }

    #[tokio::test]
    async fn cwd_cannot_escape_root() {
        let _root = TestRoot::new();
        let err = run(
            ShellRun,
            serde_json::json!({"cmd": ["echo", "x"], "cwd": "/etc"}),
        )
        .await
        .unwrap_err();
        assert!(err.to_string().contains("outside") || err.to_string().contains("blocked"));
    }

    #[tokio::test]
    async fn bg_list_kill_lifecycle() {
        let _root = TestRoot::new();
        let out = run(ShellBg, serde_json::json!({"cmd": ["sleep", "60"]}))
            .await
            .unwrap();
        let job_id = out["job_id"].as_str().unwrap().to_string();

        let out = run(ProcessList, serde_json::json!({})).await.unwrap();
        let job = out["jobs"]
            .as_array()
            .unwrap()
            .iter()
            .find(|j| j["job_id"] == job_id)
            .expect("job should be listed");
        assert_eq!(job["running"], true);

        let out = run(ProcessKill, serde_json::json!({"job_id": job_id}))
            .await
            .unwrap();
        assert_eq!(out["ok"], true);

        let out = run(ProcessList, serde_json::json!({})).await.unwrap();
        assert!(out["jobs"]
            .as_array()
            .unwrap()
            .iter()
            .all(|j| j["job_id"] != job_id));

        // killing twice -> not found
        let err = run(ProcessKill, serde_json::json!({"job_id": job_id}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::NotFound(_)));
    }
}
