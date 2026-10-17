// boom-text: text utilities for the Boom agent harness.
//
// Reads one JSON request from stdin, writes one JSON response to stdout.
// The process never panics on bad input and always exits with code 0.
// Character counts are Unicode scalar values (Rust `char`s), not bytes.
//
// Protocol:
//   stdin:  {"text": "...", "op": "stats"}
//           {"text": "...", "op": "keywords"}
//           {"text": "...", "op": "lines_around", "line_num": 5, "context": 3}
//   stdout: result object, or {"error": "<message>"} on failure.

use std::collections::HashMap;
use std::io::Read;

use serde_json::Value;

// Common English stopwords excluded from keyword extraction.
// Kept as a plain slice so the behavior is easy to audit and mirror
// in the pure-Python fallback.
const STOPWORDS: &[&str] = &[
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can", "had", "her",
    "was", "one", "our", "out", "get", "has", "him", "his", "how", "new", "now", "see",
    "two", "way", "who", "did", "its", "let", "put", "say", "she", "too", "use", "will",
    "with", "this", "that", "from",
];

/// Maximum keywords returned by the "keywords" op.
const MAX_KEYWORDS: usize = 10;

fn main() {
    // Read the whole stdin payload; report read errors as JSON, never panic.
    let mut input = String::new();
    let output = match std::io::stdin().read_to_string(&mut input) {
        Ok(_) => run(&input),
        Err(e) => error_json(&format!("failed to read stdin: {e}")),
    };
    println!("{output}");
    // Implicit exit code 0.
}

/// Dispatch one request body to the matching op.
fn run(input: &str) -> String {
    let req: Value = match serde_json::from_str(input) {
        Ok(v) => v,
        Err(e) => return error_json(&format!("invalid JSON request: {e}")),
    };
    let text = match req.get("text").and_then(Value::as_str) {
        Some(t) => t,
        None => return error_json("missing or invalid \"text\" field (expected a string)"),
    };
    let op = match req.get("op").and_then(Value::as_str) {
        Some(o) => o,
        None => return error_json("missing or invalid \"op\" field (expected a string)"),
    };
    match op {
        "stats" => op_stats(text),
        "keywords" => op_keywords(text),
        "lines_around" => op_lines_around(text, &req),
        _ => error_json(&format!("unknown op: \"{op}\"")),
    }
}

/// {"op": "stats"} -> line/word/char/token estimates.
fn op_stats(text: &str) -> String {
    let lines = text.lines().count();
    let words = text.split_whitespace().count();
    // Unicode scalar values, not bytes: "é".len() is 2 bytes but 1 char.
    let chars = text.chars().count();
    // Rough token estimate: ~4 chars per token, rounded up.
    let est_tokens = chars.div_ceil(4);
    serde_json::json!({
        "lines": lines,
        "words": words,
        "chars": chars,
        "est_tokens": est_tokens,
        "error": null,
    })
    .to_string()
}

/// {"op": "keywords"} -> top-10 frequent alphabetic words.
fn op_keywords(text: &str) -> String {
    let mut counts: HashMap<String, u64> = HashMap::new();
    for token in text.split_whitespace() {
        let word = token.to_lowercase();
        // Keep only purely alphabetic words of length >= 3.
        if word.chars().count() < 3 {
            continue;
        }
        if !word.chars().all(|c| c.is_alphabetic()) {
            continue;
        }
        if STOPWORDS.contains(&word.as_str()) {
            continue;
        }
        *counts.entry(word).or_insert(0) += 1;
    }
    let mut items: Vec<(String, u64)> = counts.into_iter().collect();
    // Deterministic order: count descending, then word ascending.
    items.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    items.truncate(MAX_KEYWORDS);
    let list: Vec<Value> = items
        .into_iter()
        .map(|(word, count)| serde_json::json!({"word": word, "count": count}))
        .collect();
    serde_json::json!({
        "keywords": list,
        "error": null,
    })
    .to_string()
}

/// {"op": "lines_around", "line_num": 5, "context": 3}
/// -> 1-based window of lines around the requested line; out-of-range
/// line numbers are clamped to the nearest valid line.
fn op_lines_around(text: &str, req: &Value) -> String {
    let total = text.lines().count() as i64;
    let line_num = req.get("line_num").and_then(Value::as_i64).unwrap_or(1);
    let context = req
        .get("context")
        .and_then(Value::as_i64)
        .unwrap_or(3)
        .max(0);

    let clamped = if total == 0 {
        1
    } else {
        line_num.clamp(1, total)
    };
    let start = (clamped - context).max(1);
    let end = (clamped + context).min(total);

    let mut lines_out: Vec<Value> = Vec::new();
    for (idx, line) in text.lines().enumerate() {
        let n = idx as i64 + 1; // 1-based line number
        if n >= start && n <= end {
            lines_out.push(serde_json::json!({"n": n, "text": line}));
        }
    }
    serde_json::json!({
        "lines": lines_out,
        "start_line": start,
        "end_line": end,
        "error": null,
    })
    .to_string()
}

/// Build a JSON error response object.
fn error_json(message: &str) -> String {
    serde_json::json!({"error": message}).to_string()
}
