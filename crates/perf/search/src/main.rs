// boom-search: fast parallel regex search over a directory tree.
//
// Protocol: reads a single JSON object from stdin and writes a single JSON
// object to stdout. Never panics on user input; every failure is reported
// as {"results": [], "scanned": 0, "error": "<message>"} with exit code 0.
//
// stdin:  {"root": "/path", "pattern": "regex", "file_pattern": "*.py",
//          "max_results": 50}
// stdout: {"results": [{"file": "<abs>", "line": <1-based>, "text": "<trimmed>"}],
//          "scanned": <files scanned>, "error": null}

use regex::Regex;
use serde::{Deserialize, Serialize};
use std::fs;
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::sync::Arc;

// Directories skipped during recursive traversal.
const SKIP_DIRS: &[&str] = &[
    ".git",
    "node_modules",
    "__pycache__",
    "target",
    "dist",
    "build",
    ".venv",
    "venv",
];

// Number of leading bytes inspected for null bytes to detect binary files.
const BINARY_PROBE_BYTES: usize = 8192;

#[derive(Deserialize)]
struct Request {
    root: Option<String>,
    pattern: Option<String>,
    #[serde(default = "default_file_pattern")]
    file_pattern: String,
    #[serde(default = "default_max_results")]
    max_results: usize,
}

fn default_file_pattern() -> String {
    "*".to_string()
}

fn default_max_results() -> usize {
    50
}

#[derive(Serialize)]
struct Hit {
    file: String,
    line: usize,
    text: String,
}

#[derive(Serialize)]
struct Response {
    results: Vec<Hit>,
    scanned: usize,
    error: Option<String>,
}

fn err_response(msg: String) -> String {
    let resp = Response {
        results: Vec::new(),
        scanned: 0,
        error: Some(msg),
    };
    serde_json::to_string(&resp).unwrap_or_else(|_| {
        "{\"results\":[],\"scanned\":0,\"error\":\"serialization failed\"}".to_string()
    })
}

/// Simple glob matcher supporting `*` (any sequence) and `?` (one char).
/// The pattern is matched against the file name (basename), not the path.
fn glob_match(pattern: &str, name: &str) -> bool {
    let (mut pi, mut ni) = (0usize, 0usize);
    let (mut star, mut star_match) = (None, 0usize);
    let p: Vec<char> = pattern.chars().collect();
    let n: Vec<char> = name.chars().collect();

    while ni < n.len() {
        if pi < p.len() && (p[pi] == '?' || p[pi] == n[ni]) {
            pi += 1;
            ni += 1;
        } else if pi < p.len() && p[pi] == '*' {
            star = Some(pi);
            pi += 1;
            star_match = ni;
        } else if let Some(s) = star {
            pi = s + 1;
            star_match += 1;
            ni = star_match;
        } else {
            return false;
        }
    }
    while pi < p.len() && p[pi] == '*' {
        pi += 1;
    }
    pi == p.len()
}

/// Expand one level of `{a,b}` braces, e.g. "*.{py,rs}" -> ["*.py", "*.rs"].
/// Returns None if there is no (valid) brace expansion, in which case the
/// caller uses the original pattern as-is.
fn expand_braces(pattern: &str) -> Option<Vec<String>> {
    let open = pattern.find('{')?;
    let close = pattern[open..].find('}')? + open;
    let inner = &pattern[open + 1..close];
    if !inner.contains(',') {
        return None;
    }
    let prefix = &pattern[..open];
    let suffix = &pattern[close + 1..];
    let mut out = Vec::new();
    for alt in inner.split(',') {
        let mut s = String::with_capacity(pattern.len());
        s.push_str(prefix);
        s.push_str(alt);
        s.push_str(suffix);
        out.push(s);
    }
    Some(out)
}

fn file_matches_pattern(file_pattern: &str, file_name: &str) -> bool {
    match expand_braces(file_pattern) {
        Some(expanded) => expanded.iter().any(|p| glob_match(p, file_name)),
        None => glob_match(file_pattern, file_name),
    }
}

/// Recursively walk `dir`, collecting files whose names match `file_pattern`.
/// Directories in SKIP_DIRS are pruned. Unreadable directories are skipped
/// silently (their error is reported through nothing; the walk continues).
fn collect_files(dir: &Path, file_pattern: &str, out: &mut Vec<PathBuf>) {
    let entries = match fs::read_dir(dir) {
        Ok(e) => e,
        Err(_) => return,
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().into_owned();
        if path.is_dir() {
            if SKIP_DIRS.iter().any(|d| *d == name) {
                continue;
            }
            // Avoid following symlink loops by skipping symlinked dirs.
            if entry.file_type().map(|t| t.is_symlink()).unwrap_or(false) {
                continue;
            }
            collect_files(&path, file_pattern, out);
        } else if path.is_file() && file_matches_pattern(file_pattern, &name) {
            out.push(path);
        }
    }
}

/// Search one file for `re`. Returns (hits, was_scanned).
/// Files that cannot be read, are binary (null byte in the first 8KB), or are
/// not valid UTF-8 are skipped and reported as not scanned.
fn search_file(path: &Path, re: &Regex) -> (Vec<Hit>, bool) {
    let bytes = match fs::read(path) {
        Ok(b) => b,
        Err(_) => return (Vec::new(), false),
    };
    if bytes.iter().take(BINARY_PROBE_BYTES).any(|&b| b == 0) {
        return (Vec::new(), false);
    }
    let text = match String::from_utf8(bytes) {
        Ok(t) => t,
        Err(_) => return (Vec::new(), false),
    };

    let path_str = path.to_string_lossy().into_owned();
    let mut hits = Vec::new();
    for (idx, line) in text.lines().enumerate() {
        if re.is_match(line) {
            hits.push(Hit {
                file: path_str.clone(),
                line: idx + 1, // 1-based line numbers
                text: line.trim().to_string(),
            });
        }
    }
    (hits, true)
}

fn run() -> String {
    // 1. Read the whole stdin payload.
    let mut input = String::new();
    if let Err(e) = io::stdin().read_to_string(&mut input) {
        return err_response(format!("failed to read stdin: {e}"));
    }

    // 2. Parse the request.
    let req: Request = match serde_json::from_str(&input) {
        Ok(r) => r,
        Err(e) => return err_response(format!("invalid JSON: {e}")),
    };

    let root = match req.root {
        Some(r) if !r.trim().is_empty() => r,
        _ => return err_response("missing required field: root".to_string()),
    };
    let pattern = match req.pattern {
        Some(p) => p,
        None => return err_response("missing required field: pattern".to_string()),
    };

    // 3. Compile the regex before doing any IO.
    let re = match Regex::new(&pattern) {
        Ok(r) => r,
        Err(e) => return err_response(format!("invalid regex: {e}")),
    };

    // 4. Validate the root directory.
    let root_path = PathBuf::from(&root);
    let abs_root = match root_path.canonicalize() {
        Ok(p) if p.is_dir() => p,
        _ => return err_response(format!("root does not exist or is not a directory: {root}")),
    };

    // 5. Collect candidate files.
    let mut files: Vec<PathBuf> = Vec::new();
    collect_files(&abs_root, &req.file_pattern, &mut files);

    if files.is_empty() {
        return serde_json::to_string(&Response {
            results: Vec::new(),
            scanned: 0,
            error: None,
        })
        .unwrap();
    }

    // 6. Search in parallel: split the file list across N threads.
    let n_threads = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4)
        .max(1);
    let chunk_size = (files.len() + n_threads - 1) / n_threads;
    let re = Arc::new(re);

    let mut all_hits: Vec<Hit> = Vec::new();
    let mut scanned: usize = 0;

    std::thread::scope(|scope| {
        let mut handles = Vec::new();
        for chunk in files.chunks(chunk_size.max(1)) {
            let re = Arc::clone(&re);
            handles.push(scope.spawn(move || {
                let mut hits = Vec::new();
                let mut local_scanned: usize = 0;
                for path in chunk {
                    let (mut h, was_scanned) = search_file(path, &re);
                    hits.append(&mut h);
                    if was_scanned {
                        local_scanned += 1;
                    }
                }
                (hits, local_scanned)
            }));
        }
        for h in handles {
            match h.join() {
                Ok((hits, local_scanned)) => {
                    all_hits.extend(hits);
                    scanned += local_scanned;
                }
                Err(_) => {
                    // A worker panicked; keep going with what we have rather
                    // than crashing the whole process.
                }
            }
        }
    });

    // 7. Deterministic order: by file, then by line; respect max_results.
    all_hits.sort_by(|a, b| a.file.cmp(&b.file).then(a.line.cmp(&b.line)));
    all_hits.truncate(req.max_results);

    serde_json::to_string(&Response {
        results: all_hits,
        scanned,
        error: None,
    })
    .unwrap_or_else(|_| "{\"results\":[],\"scanned\":0,\"error\":\"serialization failed\"}".to_string())
}

fn main() {
    let out = run();
    println!("{out}");
    // Exit code is always 0; failures are reported in the JSON payload.
}
