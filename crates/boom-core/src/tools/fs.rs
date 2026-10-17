//! Filesystem tools — Rust port of `agent/src/tools/fileops.py` (and the
//! file helpers in `file_ops.py`).
//!
//! Safety: every path is resolved against an allowed root (the process
//! working directory, or `$BOOM_TOOL_ROOT` when set — mainly for tests) and
//! rejected unless the canonical path stays inside that root. This mirrors
//! the `_resolve_safe` guard in `fileops.py`.

use std::path::{Path, PathBuf};

use async_trait::async_trait;
use serde_json::Value;

use super::{opt_bool, opt_str, opt_u64, req_str, schema_of, Tool, ToolError};

/// Allowed root for all file operations.
pub(crate) fn tools_root() -> PathBuf {
    if let Ok(dir) = std::env::var("BOOM_TOOL_ROOT") {
        PathBuf::from(dir)
    } else {
        std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
    }
}

/// Resolve `path` against the allowed root and verify the result stays inside
/// it. Symlinks are resolved via canonicalization, so a symlink pointing
/// outside the root is rejected too.
pub(crate) fn resolve_safe(path: &str) -> Result<PathBuf, ToolError> {
    let root = tools_root().canonicalize().map_err(ToolError::Io)?;
    let p = Path::new(path);
    let joined: PathBuf = if p.is_absolute() {
        // Absolute paths are allowed only if they stay inside the root.
        p.to_path_buf()
    } else {
        // Strip any leading `/` confusion is unnecessary here: join handles it,
        // but an absolute `path` arg on the else branch cannot happen.
        root.join(p)
    };

    // Canonicalize the nearest existing ancestor, then re-append the rest.
    // This supports creating new files (whose parent may itself be missing).
    let mut probe = joined.as_path();
    let mut missing: Vec<std::ffi::OsString> = Vec::new();
    let existing: PathBuf = loop {
        match probe.canonicalize() {
            Ok(c) => break c,
            Err(_) => match probe.file_name() {
                Some(name) => {
                    missing.push(name.to_os_string());
                    probe = probe.parent().ok_or_else(|| {
                        ToolError::InvalidArgs(format!("invalid path '{path}'"))
                    })?;
                }
                None => {
                    return Err(ToolError::InvalidArgs(format!("invalid path '{path}'")));
                }
            },
        }
    };
    missing.reverse();
    let mut target = existing;
    for comp in &missing {
        if comp == ".." {
            // `..` in the not-yet-existing tail: pop textually.
            target.pop();
        } else if comp == "." {
            // no-op
        } else {
            target.push(comp);
        }
    }

    if !target.starts_with(&root) {
        return Err(ToolError::Blocked(format!(
            "path traversal blocked: '{path}' escapes the allowed root"
        )));
    }
    Ok(target)
}

/// True when the first 1 KiB of the file contains a NUL byte.
fn is_binary(path: &Path) -> bool {
    use std::io::Read;
    let mut fh = match std::fs::File::open(path) {
        Ok(f) => f,
        Err(_) => return false,
    };
    let mut buf = [0u8; 1024];
    match fh.read(&mut buf) {
        Ok(n) => buf[..n].contains(&0),
        Err(_) => false,
    }
}

/// Read at most this many bytes for `file_read`.
const MAX_READ_BYTES: u64 = 10 * 1024 * 1024;

// ---------------------------------------------------------------------------
// file_read
// ---------------------------------------------------------------------------

pub struct FileRead;
#[async_trait]
impl Tool for FileRead {
    fn name(&self) -> &str {
        "file_read"
    }
    fn description(&self) -> &str {
        "Read a UTF-8 text file. Supports line offset/limit pagination."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("path", "string", "File path, relative to the working directory"),
                ("offset", "integer", "First line to return (1-based, default 1)"),
                ("limit", "integer", "Max lines to return (default 2000)"),
            ],
            &["path"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let path = req_str(&args, "path")?;
        let target = resolve_safe(&path)?;
        if !target.is_file() {
            return Err(ToolError::NotFound(format!("file not found: '{path}'")));
        }
        let meta = target.metadata().map_err(ToolError::Io)?;
        if meta.len() > MAX_READ_BYTES {
            return Err(ToolError::InvalidArgs(format!(
                "file too large ({} bytes > 10 MiB)",
                meta.len()
            )));
        }
        let bytes = std::fs::read(&target).map_err(ToolError::Io)?;
        let content =
            String::from_utf8(bytes).map_err(|e| ToolError::Encoding(e.to_string()))?;
        let offset = opt_u64(&args, "offset", 1).max(1) as usize;
        let limit = opt_u64(&args, "limit", 2000).max(1) as usize;
        let lines: Vec<&str> = content.lines().collect();
        let total = lines.len();
        let start = (offset - 1).min(total);
        let end = (start + limit).min(total);
        Ok(serde_json::json!({
            "path": target.to_string_lossy(),
            "content": lines[start..end].join("\n"),
            "total_lines": total,
            "offset": offset,
            "truncated": end < total,
        }))
    }
}

// ---------------------------------------------------------------------------
// file_write
// ---------------------------------------------------------------------------

pub struct FileWrite;
#[async_trait]
impl Tool for FileWrite {
    fn name(&self) -> &str {
        "file_write"
    }
    fn description(&self) -> &str {
        "Write (create or overwrite) a UTF-8 text file, creating parent directories."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("path", "string", "File path, relative to the working directory"),
                ("content", "string", "Text content to write"),
            ],
            &["path", "content"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let path = req_str(&args, "path")?;
        let content = req_str(&args, "content")?;
        let target = resolve_safe(&path)?;
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent).map_err(ToolError::Io)?;
        }
        std::fs::write(&target, content.as_bytes()).map_err(ToolError::Io)?;
        Ok(serde_json::json!({
            "ok": true,
            "path": target.to_string_lossy(),
            "bytes_written": content.len(),
        }))
    }
}

// ---------------------------------------------------------------------------
// file_edit — strict single exact-match edit with .bak backup (port of
// edit_file in fileops.py)
// ---------------------------------------------------------------------------

pub struct FileEdit;
#[async_trait]
impl Tool for FileEdit {
    fn name(&self) -> &str {
        "file_edit"
    }
    fn description(&self) -> &str {
        "Replace exactly one occurrence of `old` with `new` in a text file. \
         Fails when there are 0 or >1 matches. Creates a .bak backup first."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("path", "string", "File path"),
                ("old", "string", "Exact text to find (must occur exactly once)"),
                ("new", "string", "Replacement text"),
            ],
            &["path", "old", "new"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let path = req_str(&args, "path")?;
        let old = req_str(&args, "old")?;
        let new = req_str(&args, "new")?;
        let target = resolve_safe(&path)?;
        if !target.is_file() {
            return Err(ToolError::NotFound(format!("file not found: '{path}'")));
        }
        if is_binary(&target) {
            return Err(ToolError::Blocked("binary file refused".to_string()));
        }
        if old.is_empty() {
            return Err(ToolError::InvalidArgs("old must be non-empty".to_string()));
        }
        let bytes = std::fs::read(&target).map_err(ToolError::Io)?;
        let content =
            String::from_utf8(bytes).map_err(|e| ToolError::Encoding(e.to_string()))?;
        let count = content.matches(&old).count();
        if count == 0 {
            return Err(ToolError::NotFound("old text not found".to_string()));
        }
        if count > 1 {
            return Err(ToolError::InvalidArgs(format!(
                "ambiguous: {count} matches, be more specific"
            )));
        }
        // Backup to `<name>.bak` before writing, like the Python port.
        let backup = {
            let mut s = target.as_os_str().to_os_string();
            s.push(".bak");
            PathBuf::from(s)
        };
        std::fs::copy(&target, &backup).map_err(ToolError::Io)?;
        std::fs::write(&target, content.replacen(&old, &new, 1)).map_err(ToolError::Io)?;
        Ok(serde_json::json!({
            "ok": true,
            "path": target.to_string_lossy(),
            "backup": backup.to_string_lossy(),
        }))
    }
}

// ---------------------------------------------------------------------------
// file_append (port of append_file in fileops.py)
// ---------------------------------------------------------------------------

pub struct FileAppend;
#[async_trait]
impl Tool for FileAppend {
    fn name(&self) -> &str {
        "file_append"
    }
    fn description(&self) -> &str {
        "Append text to a file, creating it when missing. Refuses binary files."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("path", "string", "File path"),
                ("text", "string", "Text to append"),
                ("newline", "boolean", "Start on a new line when the file does not end with one (default true)"),
            ],
            &["path", "text"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        use std::io::{Read, Seek, SeekFrom};
        let path = req_str(&args, "path")?;
        let text = req_str(&args, "text")?;
        let newline = opt_bool(&args, "newline", true);
        let target = resolve_safe(&path)?;
        if target.is_dir() {
            return Err(ToolError::InvalidArgs("path is a directory".to_string()));
        }
        if target.is_file() && is_binary(&target) {
            return Err(ToolError::Blocked("binary file refused".to_string()));
        }
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent).map_err(ToolError::Io)?;
        }
        let mut payload = text.clone();
        if newline && target.is_file() && target.metadata().map(|m| m.len()).unwrap_or(0) > 0 {
            let mut fh = std::fs::File::open(&target).map_err(ToolError::Io)?;
            fh.seek(SeekFrom::End(-1)).map_err(ToolError::Io)?;
            let mut last = [0u8; 1];
            fh.read_exact(&mut last).map_err(ToolError::Io)?;
            if last[0] != b'\n' && last[0] != b'\r' {
                payload = format!("\n{text}");
            }
        }
        use std::io::Write;
        let mut fh = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&target)
            .map_err(ToolError::Io)?;
        fh.write_all(payload.as_bytes()).map_err(ToolError::Io)?;
        Ok(serde_json::json!({
            "ok": true,
            "path": target.to_string_lossy(),
            "bytes_added": payload.len(),
        }))
    }
}

// ---------------------------------------------------------------------------
// file_info (port of file_info in fileops.py)
// ---------------------------------------------------------------------------

pub struct FileInfo;
#[async_trait]
impl Tool for FileInfo {
    fn name(&self) -> &str {
        "file_info"
    }
    fn description(&self) -> &str {
        "Return metadata for a path: exists, size, mtime, line count, type."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[("path", "string", "File or directory path")],
            &["path"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let path = req_str(&args, "path")?;
        let target = resolve_safe(&path)?;
        if !target.exists() {
            return Ok(serde_json::json!({"exists": false}));
        }
        let meta = target.symlink_metadata().map_err(ToolError::Io)?;
        let is_dir = meta.is_dir();
        let mtime = meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_secs());
        let mut out = serde_json::json!({
            "exists": true,
            "path": target.to_string_lossy(),
            "is_dir": is_dir,
            "is_file": meta.is_file(),
            "is_symlink": meta.file_type().is_symlink(),
            "size_bytes": meta.len(),
            "mtime_unix": mtime,
        });
        if meta.is_file() && !is_binary(&target) {
            if let Ok(bytes) = std::fs::read(&target) {
                if let Ok(text) = String::from_utf8(bytes) {
                    out["lines"] = serde_json::json!(text.lines().count());
                }
            }
        }
        Ok(out)
    }
}

// ---------------------------------------------------------------------------
// file_delete
// ---------------------------------------------------------------------------

pub struct FileDelete;
#[async_trait]
impl Tool for FileDelete {
    fn name(&self) -> &str {
        "file_delete"
    }
    fn description(&self) -> &str {
        "Delete a file. Directories require recursive=true."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("path", "string", "Path to delete"),
                ("recursive", "boolean", "Allow deleting directories recursively (default false)"),
            ],
            &["path"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let path = req_str(&args, "path")?;
        let recursive = opt_bool(&args, "recursive", false);
        let target = resolve_safe(&path)?;
        if !target.exists() {
            return Err(ToolError::NotFound(format!("not found: '{path}'")));
        }
        if target.is_dir() {
            if !recursive {
                return Err(ToolError::InvalidArgs(
                    "refusing to delete a directory without recursive=true".to_string(),
                ));
            }
            std::fs::remove_dir_all(&target).map_err(ToolError::Io)?;
        } else {
            std::fs::remove_file(&target).map_err(ToolError::Io)?;
        }
        Ok(serde_json::json!({"ok": true, "path": target.to_string_lossy()}))
    }
}

// ---------------------------------------------------------------------------
// file_copy
// ---------------------------------------------------------------------------

pub struct FileCopy;
#[async_trait]
impl Tool for FileCopy {
    fn name(&self) -> &str {
        "file_copy"
    }
    fn description(&self) -> &str {
        "Copy a file to a new path, creating parent directories."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("src", "string", "Source file path"),
                ("dst", "string", "Destination file path"),
            ],
            &["src", "dst"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let src = resolve_safe(&req_str(&args, "src")?)?;
        let dst = resolve_safe(&req_str(&args, "dst")?)?;
        if !src.is_file() {
            return Err(ToolError::NotFound("source file not found".to_string()));
        }
        if let Some(parent) = dst.parent() {
            std::fs::create_dir_all(parent).map_err(ToolError::Io)?;
        }
        let bytes = std::fs::copy(&src, &dst).map_err(ToolError::Io)?;
        Ok(serde_json::json!({
            "ok": true,
            "src": src.to_string_lossy(),
            "dst": dst.to_string_lossy(),
            "bytes": bytes,
        }))
    }
}

// ---------------------------------------------------------------------------
// file_move
// ---------------------------------------------------------------------------

pub struct FileMove;
#[async_trait]
impl Tool for FileMove {
    fn name(&self) -> &str {
        "file_move"
    }
    fn description(&self) -> &str {
        "Move/rename a file or directory, creating parent directories."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("src", "string", "Source path"),
                ("dst", "string", "Destination path"),
            ],
            &["src", "dst"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let src = resolve_safe(&req_str(&args, "src")?)?;
        let dst = resolve_safe(&req_str(&args, "dst")?)?;
        if !src.exists() {
            return Err(ToolError::NotFound("source not found".to_string()));
        }
        if let Some(parent) = dst.parent() {
            std::fs::create_dir_all(parent).map_err(ToolError::Io)?;
        }
        std::fs::rename(&src, &dst).map_err(ToolError::Io)?;
        Ok(serde_json::json!({
            "ok": true,
            "src": src.to_string_lossy(),
            "dst": dst.to_string_lossy(),
        }))
    }
}

// ---------------------------------------------------------------------------
// file_exists
// ---------------------------------------------------------------------------

pub struct FileExists;
#[async_trait]
impl Tool for FileExists {
    fn name(&self) -> &str {
        "file_exists"
    }
    fn description(&self) -> &str {
        "Check whether a path exists (and whether it is a file or directory)."
    }
    fn schema(&self) -> Value {
        schema_of(&[("path", "string", "Path to check")], &["path"])
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let target = resolve_safe(&req_str(&args, "path")?)?;
        Ok(serde_json::json!({
            "exists": target.exists(),
            "is_file": target.is_file(),
            "is_dir": target.is_dir(),
            "path": target.to_string_lossy(),
        }))
    }
}

// ---------------------------------------------------------------------------
// dir_list
// ---------------------------------------------------------------------------

pub struct DirList;
#[async_trait]
impl Tool for DirList {
    fn name(&self) -> &str {
        "dir_list"
    }
    fn description(&self) -> &str {
        "List entries of a directory, sorted by name."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("path", "string", "Directory path (default '.')"),
                ("show_hidden", "boolean", "Include dotfiles (default false)"),
            ],
            &[],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let target = resolve_safe(opt_str(&args, "path", "."))?;
        let show_hidden = opt_bool(&args, "show_hidden", false);
        if !target.is_dir() {
            return Err(ToolError::NotFound(format!(
                "directory not found: '{}'",
                target.to_string_lossy()
            )));
        }
        let mut entries = Vec::new();
        for entry in std::fs::read_dir(&target).map_err(ToolError::Io)? {
            let entry = entry.map_err(ToolError::Io)?;
            let name = entry.file_name().to_string_lossy().to_string();
            if !show_hidden && name.starts_with('.') {
                continue;
            }
            let ft = entry.file_type().map_err(ToolError::Io)?;
            let size = entry.metadata().map(|m| m.len()).unwrap_or(0);
            entries.push(serde_json::json!({
                "name": name,
                "is_dir": ft.is_dir(),
                "is_file": ft.is_file(),
                "size_bytes": size,
            }));
        }
        entries.sort_by(|a, b| a["name"].as_str().cmp(&b["name"].as_str()));
        Ok(serde_json::json!({
            "path": target.to_string_lossy(),
            "entries": entries,
            "count": entries.len(),
        }))
    }
}

// ---------------------------------------------------------------------------
// glob
// ---------------------------------------------------------------------------

pub struct Glob;
#[async_trait]
impl Tool for Glob {
    fn name(&self) -> &str {
        "glob"
    }
    fn description(&self) -> &str {
        "Find files matching a glob pattern relative to the working directory, e.g. \"**/*.rs\"."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("pattern", "string", "Glob pattern, e.g. \"src/**/*.rs\""),
                ("limit", "integer", "Max results (default 200)"),
            ],
            &["pattern"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let pattern = req_str(&args, "pattern")?;
        let limit = opt_u64(&args, "limit", 200) as usize;
        let root = tools_root()
            .canonicalize()
            .map_err(ToolError::Io)?;
        let full_pattern = root.join(&pattern).to_string_lossy().to_string();
        let mut files = Vec::new();
        for entry in glob::glob(&full_pattern)
            .map_err(|e| ToolError::InvalidArgs(format!("bad glob pattern: {e}")))?
        {
            let path = entry.map_err(|e| ToolError::Other(e.to_string()))?;
            // Re-verify containment: a crafted pattern could otherwise escape.
            let canon = path.canonicalize().unwrap_or(path.clone());
            if !canon.starts_with(&root) || !canon.is_file() {
                continue;
            }
            let rel = canon.strip_prefix(&root).unwrap_or(&canon);
            files.push(rel.to_string_lossy().to_string());
            if files.len() >= limit {
                break;
            }
        }
        files.sort();
        Ok(serde_json::json!({
            "pattern": pattern,
            "files": files,
            "count": files.len(),
        }))
    }
}

// ---------------------------------------------------------------------------
// grep — regex search over files
// ---------------------------------------------------------------------------

pub struct Grep;
#[async_trait]
impl Tool for Grep {
    fn name(&self) -> &str {
        "grep"
    }
    fn description(&self) -> &str {
        "Search file contents with a regex. `path` may be a file or a directory (searched recursively)."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("pattern", "string", "Regex pattern"),
                ("path", "string", "File or directory to search (default '.')"),
                ("include", "string", "Only search files matching this glob, e.g. \"*.rs\""),
                ("max_matches", "integer", "Max matches to return (default 100)"),
            ],
            &["pattern"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let pattern = req_str(&args, "pattern")?;
        let re = regex::Regex::new(&pattern)
            .map_err(|e| ToolError::InvalidArgs(format!("bad regex: {e}")))?;
        let base = resolve_safe(opt_str(&args, "path", "."))?;
        let include = args.get("include").and_then(Value::as_str);
        let max_matches = opt_u64(&args, "max_matches", 100) as usize;

        let mut files = Vec::new();
        collect_files(&base, include, &mut files).map_err(ToolError::Io)?;

        let mut matches = Vec::new();
        'outer: for file in files {
            if is_binary(&file) {
                continue;
            }
            let Ok(bytes) = std::fs::read(&file) else {
                continue;
            };
            let Ok(text) = String::from_utf8(bytes) else {
                continue;
            };
            for (i, line) in text.lines().enumerate() {
                if re.is_match(line) {
                    matches.push(serde_json::json!({
                        "file": file.to_string_lossy(),
                        "line": i + 1,
                        "text": line.chars().take(500).collect::<String>(),
                    }));
                    if matches.len() >= max_matches {
                        break 'outer;
                    }
                }
            }
        }
        Ok(serde_json::json!({
            "pattern": pattern,
            "matches": matches,
            "count": matches.len(),
        }))
    }
}

fn collect_files(
    base: &Path,
    include: Option<&str>,
    out: &mut Vec<PathBuf>,
) -> std::io::Result<()> {
    if base.is_file() {
        if include_matches(base, include) {
            out.push(base.to_path_buf());
        }
        return Ok(());
    }
    let mut stack = vec![base.to_path_buf()];
    while let Some(dir) = stack.pop() {
        for entry in std::fs::read_dir(&dir)? {
            let entry = entry?;
            let p = entry.path();
            let ft = entry.file_type()?;
            if ft.is_dir() {
                // Skip hidden dirs and common heavy dirs.
                if let Some(name) = p.file_name().and_then(|n| n.to_str()) {
                    if name.starts_with('.') || name == "target" || name == "node_modules" {
                        continue;
                    }
                }
                stack.push(p);
            } else if ft.is_file() && include_matches(&p, include) {
                out.push(p);
            }
        }
    }
    out.sort();
    Ok(())
}

fn include_matches(path: &Path, include: Option<&str>) -> bool {
    match include {
        None => true,
        Some(pat) => path
            .file_name()
            .and_then(|n| n.to_str())
            .map(|name| glob::Pattern::new(pat).map(|g| g.matches(name)).unwrap_or(false))
            .unwrap_or(false),
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::super::test_support::TestRoot;
    use super::*;

    async fn run<T: Tool>(tool: T, args: Value) -> Value {
        tool.execute(args).await.expect("tool failed")
    }

    #[tokio::test]
    async fn traversal_is_blocked() {
        let _t = TestRoot::new();
        assert!(resolve_safe("../../etc/passwd").is_err());
        assert!(resolve_safe("/etc/passwd").is_err());
        let err = resolve_safe("../outside.txt").unwrap_err();
        assert!(err.to_string().contains("blocked") || err.to_string().contains("traversal"));
    }

    #[tokio::test]
    async fn write_read_roundtrip() {
        let _t = TestRoot::new();
        let out = run(FileWrite, serde_json::json!({"path": "a/b.txt", "content": "hello\nworld\n"})).await;
        assert_eq!(out["ok"], true);
        let out = run(
            FileRead,
            serde_json::json!({"path": "a/b.txt", "offset": 2, "limit": 1}),
        )
        .await;
        assert_eq!(out["content"], "world");
        assert_eq!(out["total_lines"], 2);
    }

    #[tokio::test]
    async fn edit_single_match_with_backup() {
        let _t = TestRoot::new();
        run(FileWrite, serde_json::json!({"path": "e.txt", "content": "foo bar"})).await;
        let out = run(
            FileEdit,
            serde_json::json!({"path": "e.txt", "old": "bar", "new": "baz"}),
        )
        .await;
        assert_eq!(out["ok"], true);
        let out = run(FileRead, serde_json::json!({"path": "e.txt"})).await;
        assert_eq!(out["content"], "foo baz");
        // ambiguous
        run(FileWrite, serde_json::json!({"path": "e2.txt", "content": "x x"})).await;
        let err = FileEdit
            .execute(serde_json::json!({"path": "e2.txt", "old": "x", "new": "y"}))
            .await
            .unwrap_err();
        assert!(err.to_string().contains("ambiguous"));
        // not found
        let err = FileEdit
            .execute(serde_json::json!({"path": "e.txt", "old": "zzz", "new": "y"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::NotFound(_)));
    }

    #[tokio::test]
    async fn append_adds_newline_when_missing() {
        let _t = TestRoot::new();
        run(FileWrite, serde_json::json!({"path": "ap.txt", "content": "line1"})).await;
        run(FileAppend, serde_json::json!({"path": "ap.txt", "text": "line2"})).await;
        let out = run(FileRead, serde_json::json!({"path": "ap.txt"})).await;
        assert_eq!(out["content"], "line1\nline2");
    }

    #[tokio::test]
    async fn info_delete_copy_move_exists() {
        let _t = TestRoot::new();
        run(FileWrite, serde_json::json!({"path": "f.txt", "content": "abc"})).await;
        let out = run(FileInfo, serde_json::json!({"path": "f.txt"})).await;
        assert_eq!(out["exists"], true);
        assert_eq!(out["size_bytes"], 3);
        assert_eq!(out["lines"], 1);
        let out = run(FileExists, serde_json::json!({"path": "f.txt"})).await;
        assert_eq!(out["exists"], true);
        assert_eq!(out["is_file"], true);

        let out = run(FileCopy, serde_json::json!({"src": "f.txt", "dst": "g.txt"})).await;
        assert_eq!(out["bytes"], 3);
        let out = run(FileMove, serde_json::json!({"src": "g.txt", "dst": "sub/h.txt"})).await;
        assert_eq!(out["ok"], true);
        let out = run(FileExists, serde_json::json!({"path": "g.txt"})).await;
        assert_eq!(out["exists"], false);

        let out = run(FileDelete, serde_json::json!({"path": "sub", "recursive": true})).await;
        assert_eq!(out["ok"], true);
        let err = FileDelete
            .execute(serde_json::json!({"path": "nope.txt"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::NotFound(_)));
    }

    #[tokio::test]
    async fn dir_list_and_glob() {
        let _t = TestRoot::new();
        run(FileWrite, serde_json::json!({"path": "x/a.rs", "content": ""})).await;
        run(FileWrite, serde_json::json!({"path": "x/b.txt", "content": ""})).await;
        run(FileWrite, serde_json::json!({"path": "x/.hidden", "content": ""})).await;
        let out = run(DirList, serde_json::json!({"path": "x"})).await;
        assert_eq!(out["count"], 2); // hidden excluded
        let out = run(DirList, serde_json::json!({"path": "x", "show_hidden": true})).await;
        assert_eq!(out["count"], 3);
        let out = run(Glob, serde_json::json!({"pattern": "x/*.rs"})).await;
        assert_eq!(out["count"], 1);
        assert!(out["files"][0].as_str().unwrap().ends_with("a.rs"));
    }

    #[tokio::test]
    async fn grep_finds_regex_matches() {
        let _t = TestRoot::new();
        run(
            FileWrite,
            serde_json::json!({"path": "s/main.rs", "content": "fn main() {}\n// TODO fix\n"}),
        )
        .await;
        let out = run(
            Grep,
            serde_json::json!({"pattern": "TODO", "path": "s", "include": "*.rs"}),
        )
        .await;
        assert_eq!(out["count"], 1);
        assert_eq!(out["matches"][0]["line"], 2);
    }

    #[tokio::test]
    async fn binary_edit_refused() {
        let t = TestRoot::new();
        std::fs::write(t.path().join("bin.dat"), [0u8, 1, 2, 3]).unwrap();
        let err = FileEdit
            .execute(serde_json::json!({"path": "bin.dat", "old": "a", "new": "b"}))
            .await
            .unwrap_err();
        assert!(err.to_string().contains("binary"));
    }
}
