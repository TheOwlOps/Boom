// boom-hash: SHA256 hasher binary for the Boom agent harness.
//
// Protocol:
//   - stdin:  {"files": ["/path/a", "/path/b"]}   -> stdout: {"hashes": {path: hex}, "errors": {path: msg}, "error": null}
//   - stdin:  {"text": "..."}                     -> stdout: {"hash": "<hex>", "error": null}
//   - stdin:  {} (or neither key)                  -> stdout: {"error": "<message>"}
//   - Files are hashed with a 64KB streaming buffer so large files never
//     sit fully in memory. Unreadable/missing files are reported per-path in
//     "errors" without failing the whole batch. Never panics; exit code 0.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::File;
use std::io::{self, BufReader, Read};

// Streamed in 64KB chunks so even multi-GB files stay memory-cheap.
const BUFFER_SIZE: usize = 64 * 1024;

#[derive(Debug, Deserialize)]
struct Request {
    #[serde(default)]
    files: Option<Vec<String>>,
    #[serde(default)]
    text: Option<String>,
}

#[derive(Debug, Serialize)]
struct FilesResponse {
    hashes: BTreeMap<String, String>,
    errors: BTreeMap<String, String>,
    error: Option<String>,
}

#[derive(Debug, Serialize)]
struct TextResponse {
    hash: String,
    error: Option<String>,
}

#[derive(Debug, Serialize)]
struct ErrorResponse {
    error: String,
}

/// Hash one file by streaming it through a 64KB buffered reader.
fn hash_file(path: &str) -> Result<String, String> {
    let file = File::open(path).map_err(|e| e.to_string())?;
    let mut reader = BufReader::with_capacity(BUFFER_SIZE, file);
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; BUFFER_SIZE];
    loop {
        let n = reader.read(&mut buf).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hex_encode(&hasher.finalize()))
}

/// Hash an in-memory string (used for the {"text": ...} request form).
fn hash_text(text: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(text.as_bytes());
    hex_encode(&hasher.finalize())
}

/// Lowercase hex encode without an extra dependency.
fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0x0f) as usize] as char);
    }
    out
}

fn main() {
    // Read the whole stdin payload; a JSON parse failure is reported as
    // an error response rather than a panic.
    let mut input = String::new();
    if let Err(e) = io::stdin().read_to_string(&mut input) {
        let resp = ErrorResponse {
            error: format!("failed to read stdin: {e}"),
        };
        println!("{}", serde_json::to_string(&resp).unwrap());
        return;
    }
    let req: Request = match serde_json::from_str(&input) {
        Ok(r) => r,
        Err(e) => {
            let resp = ErrorResponse {
                error: format!("invalid JSON on stdin: {e}"),
            };
            println!("{}", serde_json::to_string(&resp).unwrap());
            return;
        }
    };

    if let Some(files) = req.files {
        // Batch mode: per-file errors go into "errors", the batch still succeeds.
        let mut hashes = BTreeMap::new();
        let mut errors = BTreeMap::new();
        for path in files {
            match hash_file(&path) {
                Ok(hex) => {
                    hashes.insert(path, hex);
                }
                Err(msg) => {
                    errors.insert(path, msg);
                }
            }
        }
        let resp = FilesResponse {
            hashes,
            errors,
            error: None,
        };
        println!("{}", serde_json::to_string(&resp).unwrap());
    } else if let Some(text) = req.text {
        let resp = TextResponse {
            hash: hash_text(&text),
            error: None,
        };
        println!("{}", serde_json::to_string(&resp).unwrap());
    } else {
        let resp = ErrorResponse {
            error: "request must contain either 'files' or 'text'".to_string(),
        };
        println!("{}", serde_json::to_string(&resp).unwrap());
    }
}
