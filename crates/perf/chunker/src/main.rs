//! boom-chunk: Unicode-safe text chunker.
//!
//! Reads a JSON request from stdin:
//!   {"text": "...", "max_tokens": 500, "overlap": 50}
//! Token budget is estimated as ~4 chars per token, so
//!   max_chars = max_tokens * 4, overlap_chars = overlap * 4.
//!
//! Writes to stdout:
//!   {"chunks": ["...", ...], "count": N, "error": null}
//! On input errors:
//!   {"error": "<message>"}
//! Never panics, always exits with code 0.

use serde::{Deserialize, Serialize};
use std::io::Read;

/// JSON request read from stdin.
#[derive(Debug, Deserialize)]
struct ChunkRequest {
    text: Option<String>,
    max_tokens: Option<i64>,
    overlap: Option<i64>,
}

/// JSON response written to stdout.
#[derive(Debug, Serialize)]
struct ChunkResponse {
    #[serde(skip_serializing_if = "Option::is_none")]
    chunks: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    count: Option<usize>,
    error: Option<String>,
}

fn ok(chunks: Vec<String>) -> ChunkResponse {
    ChunkResponse {
        count: Some(chunks.len()),
        chunks: Some(chunks),
        error: None,
    }
}

fn err(message: impl Into<String>) -> ChunkResponse {
    ChunkResponse {
        chunks: None,
        count: None,
        error: Some(message.into()),
    }
}

/// Chunk text into pieces of at most `max_chars` chars, with `overlap_chars`
/// of overlap between consecutive chunks.
///
/// - Lines are grouped into chunks without breaking a line whenever possible.
/// - A single line longer than `max_chars` is hard-split by chars.
/// - Overlap: the next chunk starts `overlap_chars` before the end of the
///   previous chunk, snapped back to the nearest line boundary when one exists
///   inside the rewind zone.
/// - Unicode-safe: the text is processed as a `Vec<char>`, so no cut can land
///   in the middle of a Unicode scalar value.
fn chunk_text(text: &str, max_chars: usize, overlap_chars: usize) -> Vec<String> {
    if text.is_empty() {
        return Vec::new();
    }
    // Overlap can never exceed the chunk size; keep at least one char of progress.
    let overlap_chars = overlap_chars.min(max_chars.saturating_sub(1));

    let chars: Vec<char> = text.chars().collect();
    let total = chars.len();

    // Char offsets where each line begins (offset 0 is always a line start).
    let mut line_starts: Vec<usize> = vec![0];
    for (i, &c) in chars.iter().enumerate() {
        if c == '\n' {
            line_starts.push(i + 1);
        }
    }

    /// Largest line start `<= target`, or `None` if none exists.
    fn snap_line_start(line_starts: &[usize], target: usize) -> Option<usize> {
        let mut result = None;
        for &s in line_starts {
            if s > target {
                break;
            }
            result = Some(s);
        }
        result
    }

    let mut chunks: Vec<String> = Vec::new();
    let mut next: usize = 0; // char offset where the next chunk starts
    let mut guard: usize = 0;
    while next < total {
        guard += 1;
        if guard > total * 2 + 16 {
            break; // unreachable safety net against infinite loops
        }

        let limit = (next + max_chars).min(total);

        // Find the end of this chunk: the last '\n' at or before `limit`,
        // so lines are never broken. If there is none, this single line is
        // longer than max_chars and gets a hard cut at `limit`.
        let mut end = limit;
        let mut hard_cut = false;
        if limit < total {
            let mut last_nl: Option<usize> = None;
            for i in next..limit {
                if chars[i] == '\n' {
                    last_nl = Some(i);
                }
            }
            match last_nl {
                Some(p) => end = p, // chunk excludes the newline itself
                None => {
                    end = limit;
                    hard_cut = true;
                }
            }
        }

        if end == next {
            // Blank line (chars[next] == '\n'): advance past it, emit nothing.
            next += 1;
            continue;
        }

        let chunk: String = chars[next..end].iter().collect();
        chunks.push(chunk);

        // Raw end of the consumed region: skip the single '\n' that terminated
        // the chunk, so the next chunk starts at a line boundary by default.
        let end_raw = if end < total && chars[end] == '\n' {
            end + 1
        } else {
            end
        };

        if end_raw >= total {
            break; // everything consumed; no text left to overlap with
        }

        // Where the next chunk starts: rewind `overlap_chars` from the end,
        // snapped back to the nearest line boundary strictly after `next`
        // (progress is mandatory).
        let target = end_raw.saturating_sub(overlap_chars);
        let snapped = snap_line_start(&line_starts, target).filter(|&s| s > next && s <= end_raw);
        let mut new_next = match snapped {
            Some(s) => s,
            None => {
                if hard_cut {
                    // No line boundary inside a hard-cut region: rewind by chars.
                    target.max(next + 1)
                } else {
                    // No line boundary in the rewind zone: rewind by chars.
                    target.max(next + 1).min(end_raw)
                }
            }
        };
        if new_next > end_raw {
            new_next = end_raw;
        }
        // Always move forward (new_next > next is guaranteed above, but be safe).
        next = new_next.max(next + 1);
    }

    chunks
}

fn run(request: ChunkRequest) -> ChunkResponse {
    let text = match request.text {
        Some(t) => t,
        None => return err("missing required field: text"),
    };
    let max_tokens = match request.max_tokens {
        Some(m) if m > 0 => m as usize,
        _ => return err("max_tokens must be a positive integer"),
    };
    let overlap = request.overlap.unwrap_or(0).max(0) as usize;

    if text.is_empty() {
        return ok(Vec::new());
    }

    let max_chars = max_tokens.saturating_mul(4);
    let overlap_chars = overlap.saturating_mul(4);
    let chunks = chunk_text(&text, max_chars, overlap_chars);
    ok(chunks)
}

fn main() {
    // Read all of stdin; on any failure, answer with an error object (never panic).
    let mut input = String::new();
    let response = match std::io::stdin().read_to_string(&mut input) {
        Ok(_) => match serde_json::from_str::<ChunkRequest>(&input) {
            Ok(req) => run(req),
            Err(e) => err(format!("invalid JSON request: {e}")),
        },
        Err(e) => err(format!("failed to read stdin: {e}")),
    };

    // Serialize and print; stdout write failures are ignored (nothing left to do).
    if let Ok(out) = serde_json::to_string(&response) {
        println!("{out}");
    }
    // Exit code is always 0.
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_text_gives_no_chunks() {
        assert!(chunk_text("", 100, 10).is_empty());
    }

    #[test]
    fn single_short_text_is_one_chunk() {
        assert_eq!(chunk_text("hello world", 100, 10), vec!["hello world"]);
    }

    #[test]
    fn chunks_respect_limit_cover_everything_and_overlap() {
        let text = (0..20)
            .map(|i| format!("line number {}", i))
            .collect::<Vec<_>>()
            .join("\n");
        let chunks = chunk_text(&text, 40, 8);
        assert!(chunks.len() > 1);
        // every chunk within the char limit
        for c in &chunks {
            assert!(c.chars().count() <= 40, "chunk too long: {:?}", c);
        }
        // consecutive chunks overlap: the start of the next chunk appears
        // inside the previous chunk (or the previous chunk is tiny)
        for w in chunks.windows(2) {
            let (a, b) = (&w[0], &w[1]);
            let probe: String = b.chars().take(8).collect();
            assert!(
                a.contains(&probe) || a.chars().count() < 8,
                "no overlap between {:?} and {:?}",
                a,
                b
            );
        }
        // every line is covered by at least one chunk
        for i in 0..20 {
            let needle = format!("line number {}", i);
            assert!(
                chunks.iter().any(|c| c.contains(&needle)),
                "line {} missing from chunks",
                i
            );
        }
    }

    #[test]
    fn long_single_line_is_hard_split() {
        let line: String = "x".repeat(500);
        let chunks = chunk_text(&line, 100, 40);
        assert!(chunks.len() >= 5, "got {} chunks", chunks.len());
        for c in &chunks {
            assert!(c.chars().count() <= 100);
        }
        // hard-split pieces tile the line (with overlap, total >= source)
        let total: usize = chunks.iter().map(|c| c.chars().count()).sum();
        assert!(total >= 500);
    }

    #[test]
    fn unicode_is_never_split_mid_char() {
        let text = "Tiếng Việt có dấu: à á ả ã ạ\n".repeat(30);
        let chunks = chunk_text(&text, 60, 12);
        assert!(chunks.len() > 1);
        for c in &chunks {
            assert!(c.chars().count() <= 60, "chunk too long: {:?}", c);
            // valid char boundaries are guaranteed by construction; also every
            // chunk must decode cleanly (would panic on split UTF-8 otherwise)
            assert!(!c.is_empty());
        }
        let src_chars = text.chars().count();
        let total: usize = chunks.iter().map(|c| c.chars().count()).sum();
        assert!(total >= src_chars - chunks.len(), "lost too much text"); // minus newlines
    }

    #[test]
    fn request_validation() {
        let r = run(ChunkRequest {
            text: None,
            max_tokens: Some(10),
            overlap: None,
        });
        assert!(r.error.is_some());
        let r = run(ChunkRequest {
            text: Some("x".into()),
            max_tokens: Some(0),
            overlap: None,
        });
        assert!(r.error.is_some());
        let r = run(ChunkRequest {
            text: Some(String::new()),
            max_tokens: Some(10),
            overlap: None,
        });
        assert_eq!(r.count, Some(0));
        assert_eq!(r.chunks, Some(vec![]));
    }
}
