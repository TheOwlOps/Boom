//! Shared workspace tools for multi-agent groups.
//!
//! All bots in a group share `$BOOM_HOME/workspace/` (or `~/.boom/workspace`
//! when `BOOM_HOME` is unset). Instead of piping large payloads through chat
//! messages (which loses context and burns tokens), a bot writes its output
//! to a file and hands the next bot the filename:
//!
//! ```text
//! 📁 Saved to /workspace/at_risk_users.csv
//! ```
//!
//! Safety: filenames are restricted to a single path segment — no `/`,
//! no `..`, ASCII alphanumerics plus `._-` only (max 128 chars). The file is
//! always resolved inside the workspace dir, so traversal is impossible by
//! construction.

use std::path::PathBuf;

use async_trait::async_trait;
use serde_json::Value;

use super::{opt_u64, req_str, schema_of, Tool, ToolError};

/// Resolve `$BOOM_HOME/workspace`, creating it on first use.
pub fn workspace_dir() -> Result<PathBuf, ToolError> {
    let base = std::env::var("BOOM_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            dirs::home_dir()
                .map(|h| h.join(".boom"))
                .unwrap_or_else(|| PathBuf::from(".boom"))
        });
    let dir = base.join("workspace");
    std::fs::create_dir_all(&dir).map_err(ToolError::Io)?;
    Ok(dir)
}

/// A shared-workspace filename: one segment, safe characters only.
fn valid_filename(name: &str) -> bool {
    let n = name.trim();
    !n.is_empty()
        && n.len() <= 128
        && !n.contains('/')
        && !n.contains('\\')
        && n != "."
        && n != ".."
        && n.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-' ))
}

fn resolve_shared(filename: &str) -> Result<PathBuf, ToolError> {
    let name = filename.trim();
    if !valid_filename(name) {
        return Err(ToolError::InvalidArgs(format!(
            "invalid shared filename '{filename}': use a single file name with letters, digits, '.', '_' or '-' (max 128 chars)"
        )));
    }
    Ok(workspace_dir()?.join(name))
}

const MAX_SHARED_BYTES: u64 = 10 * 1024 * 1024; // 10 MiB, mirrors file_read.

// ---------------------------------------------------------------------------
// shared_write
// ---------------------------------------------------------------------------

pub struct SharedWrite;
#[async_trait]
impl Tool for SharedWrite {
    fn name(&self) -> &str {
        "shared_write"
    }
    fn description(&self) -> &str {
        "Write a UTF-8 text file to the group's shared /workspace/ so other bots can read it directly (avoids context loss from pasting large data in chat). After writing, tell the group: 📁 Saved to /workspace/<filename>"
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("filename", "string", "File name inside /workspace/ (no directories)"),
                ("content", "string", "Text content to write"),
            ],
            &["filename", "content"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let filename = req_str(&args, "filename")?;
        let content = req_str(&args, "content")?;
        let target = resolve_shared(&filename)?;
        std::fs::write(&target, content.as_bytes()).map_err(ToolError::Io)?;
        Ok(serde_json::json!({
            "ok": true,
            "path": format!("/workspace/{}", filename.trim()),
            "bytes": content.len(),
        }))
    }
}

// ---------------------------------------------------------------------------
// shared_read
// ---------------------------------------------------------------------------

pub struct SharedRead;
#[async_trait]
impl Tool for SharedRead {
    fn name(&self) -> &str {
        "shared_read"
    }
    fn description(&self) -> &str {
        "Read a UTF-8 text file from the group's shared /workspace/ (written by you or a teammate bot with shared_write)."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("filename", "string", "File name inside /workspace/ (no directories)"),
                ("offset", "u64", "First line to return (1-based, default 1)"),
                ("limit", "u64", "Max lines to return (default 2000)"),
            ],
            &["filename"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let filename = req_str(&args, "filename")?;
        let target = resolve_shared(&filename)?;
        if !target.is_file() {
            return Err(ToolError::NotFound(format!(
                "shared file not found: '/workspace/{}'",
                filename.trim()
            )));
        }
        let meta = target.metadata().map_err(ToolError::Io)?;
        if meta.len() > MAX_SHARED_BYTES {
            return Err(ToolError::InvalidArgs(format!(
                "file too large ({} bytes > 10 MiB)",
                meta.len()
            )));
        }
        let bytes = std::fs::read(&target).map_err(ToolError::Io)?;
        let content = String::from_utf8(bytes).map_err(|e| ToolError::Encoding(e.to_string()))?;
        let offset = opt_u64(&args, "offset", 1).max(1) as usize;
        let limit = opt_u64(&args, "limit", 2000).max(1) as usize;
        let lines: Vec<&str> = content.lines().collect();
        let total = lines.len();
        let start = (offset - 1).min(total);
        let end = (start + limit).min(total);
        Ok(serde_json::json!({
            "path": format!("/workspace/{}", filename.trim()),
            "content": lines[start..end].join("\n"),
            "total_lines": total,
            "offset": offset,
            "truncated": end < total,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::connectors::TEST_ENV_LOCK;

    struct TempHome {
        prev: Option<String>,
        dir: PathBuf,
    }

    impl TempHome {
        async fn set() -> (tokio::sync::MutexGuard<'static, ()>, Self) {
            let guard = TEST_ENV_LOCK.lock().await;
            let dir =
                std::env::temp_dir().join(format!("boom-shared-test-{}", std::process::id()));
            let prev = std::env::var("BOOM_HOME").ok();
            std::env::set_var("BOOM_HOME", &dir);
            (guard, Self { prev, dir })
        }
    }

    impl Drop for TempHome {
        fn drop(&mut self) {
            match &self.prev {
                Some(v) => std::env::set_var("BOOM_HOME", v),
                None => std::env::remove_var("BOOM_HOME"),
            }
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }

    #[test]
    fn valid_filename_rules() {
        assert!(valid_filename("report.csv"));
        assert!(valid_filename("a-b_c.d"));
        assert!(!valid_filename(""));
        assert!(!valid_filename("   "));
        assert!(!valid_filename("../evil"));
        assert!(!valid_filename("sub/dir.csv"));
        assert!(!valid_filename("a/b"));
        assert!(!valid_filename("."));
        assert!(!valid_filename(".."));
        assert!(!valid_filename("sp ace.csv"));
        assert!(!valid_filename(&"x".repeat(129)));
        assert!(valid_filename(&"x".repeat(128)));
    }

    #[tokio::test]
    async fn write_then_read_round_trip() {
        let (_guard, _home) = TempHome::set().await;
        let w = SharedWrite;
        let out = w
            .execute(serde_json::json!({"filename": "data.csv", "content": "a,b\n1,2\n"}))
            .await
            .expect("write");
        assert_eq!(out["ok"], serde_json::json!(true));
        assert_eq!(out["path"], serde_json::json!("/workspace/data.csv"));

        let r = SharedRead;
        let out = r
            .execute(serde_json::json!({"filename": "data.csv"}))
            .await
            .expect("read");
        assert_eq!(out["content"], serde_json::json!("a,b\n1,2"));
        assert_eq!(out["total_lines"], serde_json::json!(2));
    }

    #[tokio::test]
    async fn rejects_traversal_and_missing() {
        let (_guard, _home) = TempHome::set().await;
        let w = SharedWrite;
        let err = w
            .execute(serde_json::json!({"filename": "../evil.txt", "content": "x"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)));

        let r = SharedRead;
        let err = r
            .execute(serde_json::json!({"filename": "nope.txt"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::NotFound(_)));
    }

    #[tokio::test]
    async fn workspace_dir_is_created() {
        let (_guard, _home) = TempHome::set().await;
        let dir = workspace_dir().expect("workspace dir");
        assert!(dir.is_dir());
        assert!(dir.ends_with("workspace"));
    }
}
