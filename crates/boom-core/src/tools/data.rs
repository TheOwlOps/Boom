//! Data tools: JSON parsing/querying, base64, and hashing.
//! Pure-Rust equivalents of the small data helpers spread across the Python
//! toolset.

use async_trait::async_trait;
use base64::Engine;
use serde_json::Value;

use super::{opt_bool, req_str, schema_of, Tool, ToolError};

// ---------------------------------------------------------------------------
// json_parse
// ---------------------------------------------------------------------------

pub struct JsonParse;
#[async_trait]
impl Tool for JsonParse {
    fn name(&self) -> &str {
        "json_parse"
    }
    fn description(&self) -> &str {
        "Parse a JSON string into a JSON value (validates and pretty-prints)."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("text", "string", "JSON text to parse"),
                ("pretty", "boolean", "Return pretty-printed text too (default false)"),
            ],
            &["text"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let text = req_str(&args, "text")?;
        let value: Value = serde_json::from_str(&text)
            .map_err(|e| ToolError::InvalidArgs(format!("invalid JSON: {e}")))?;
        let mut out = serde_json::json!({"ok": true, "value": value});
        if opt_bool(&args, "pretty", false) {
            out["pretty"] = Value::String(
                serde_json::to_string_pretty(&value).unwrap_or_default(),
            );
        }
        Ok(out)
    }
}

// ---------------------------------------------------------------------------
// json_query — simple dotted-path query: "a.b.0.c"
// ---------------------------------------------------------------------------

pub struct JsonQuery;
#[async_trait]
impl Tool for JsonQuery {
    fn name(&self) -> &str {
        "json_query"
    }
    fn description(&self) -> &str {
        "Query a JSON value with a dotted path, e.g. \"user.addresses.0.city\". \
         `data` may be a JSON string or an already-parsed value."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("data", "string", "JSON text or value to query"),
                ("path", "string", "Dotted path; array indices are numeric segments"),
            ],
            &["data", "path"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let data = args.get("data").ok_or_else(|| {
            ToolError::InvalidArgs("missing required argument 'data'".into())
        })?;
        let path = req_str(&args, "path")?;

        let value: Value = match data {
            Value::String(s) => serde_json::from_str(s)
                .map_err(|e| ToolError::InvalidArgs(format!("invalid JSON: {e}")))?,
            other => other.clone(),
        };

        let mut current = &value;
        if !path.is_empty() {
            for seg in path.split('.') {
                current = match current {
                    Value::Object(map) => map.get(seg).ok_or_else(|| {
                        ToolError::NotFound(format!("no key '{seg}' at path segment"))
                    })?,
                    Value::Array(arr) => {
                        let idx: usize = seg.parse().map_err(|_| {
                            ToolError::InvalidArgs(format!(
                                "segment '{seg}' is not an array index"
                            ))
                        })?;
                        arr.get(idx).ok_or_else(|| {
                            ToolError::NotFound(format!("index {idx} out of bounds"))
                        })?
                    }
                    _ => {
                        return Err(ToolError::NotFound(format!(
                            "cannot descend into scalar at segment '{seg}'"
                        )))
                    }
                };
            }
        }
        Ok(serde_json::json!({"ok": true, "value": current}))
    }
}

// ---------------------------------------------------------------------------
// base64_encode / base64_decode
// ---------------------------------------------------------------------------

pub struct Base64Encode;
#[async_trait]
impl Tool for Base64Encode {
    fn name(&self) -> &str {
        "base64_encode"
    }
    fn description(&self) -> &str {
        "Base64-encode text."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("text", "string", "Text to encode (UTF-8)"),
                ("url_safe", "boolean", "Use the URL-safe alphabet (default false)"),
            ],
            &["text"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let text = req_str(&args, "text")?;
        let encoded = if opt_bool(&args, "url_safe", false) {
            base64::engine::general_purpose::URL_SAFE.encode(text.as_bytes())
        } else {
            base64::engine::general_purpose::STANDARD.encode(text.as_bytes())
        };
        Ok(serde_json::json!({"ok": true, "encoded": encoded}))
    }
}

pub struct Base64Decode;
#[async_trait]
impl Tool for Base64Decode {
    fn name(&self) -> &str {
        "base64_decode"
    }
    fn description(&self) -> &str {
        "Base64-decode to text."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("text", "string", "Base64 text to decode"),
                ("url_safe", "boolean", "Use the URL-safe alphabet (default false)"),
            ],
            &["text"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let text = req_str(&args, "text")?;
        let engine = if opt_bool(&args, "url_safe", false) {
            base64::engine::general_purpose::URL_SAFE
        } else {
            base64::engine::general_purpose::STANDARD
        };
        let bytes = engine
            .decode(text.trim())
            .map_err(|e| ToolError::InvalidArgs(format!("invalid base64: {e}")))?;
        let decoded =
            String::from_utf8(bytes).map_err(|e| ToolError::Encoding(e.to_string()))?;
        Ok(serde_json::json!({"ok": true, "decoded": decoded}))
    }
}

// ---------------------------------------------------------------------------
// hash_sha256 / hash_md5
// ---------------------------------------------------------------------------

pub struct HashSha256;
#[async_trait]
impl Tool for HashSha256 {
    fn name(&self) -> &str {
        "hash_sha256"
    }
    fn description(&self) -> &str {
        "Compute the SHA-256 hex digest of text."
    }
    fn schema(&self) -> Value {
        schema_of(&[("text", "string", "Text to hash (UTF-8)")], &["text"])
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        use sha2::Digest;
        let text = req_str(&args, "text")?;
        let mut hasher = sha2::Sha256::new();
        hasher.update(text.as_bytes());
        Ok(serde_json::json!({
            "ok": true,
            "algorithm": "sha256",
            "hex": format!("{:x}", hasher.finalize()),
        }))
    }
}

pub struct HashMd5;
#[async_trait]
impl Tool for HashMd5 {
    fn name(&self) -> &str {
        "hash_md5"
    }
    fn description(&self) -> &str {
        "Compute the MD5 hex digest of text (non-cryptographic use, e.g. cache keys)."
    }
    fn schema(&self) -> Value {
        schema_of(&[("text", "string", "Text to hash (UTF-8)")], &["text"])
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let text = req_str(&args, "text")?;
        let digest = md5::compute(text.as_bytes());
        Ok(serde_json::json!({
            "ok": true,
            "algorithm": "md5",
            "hex": format!("{digest:x}"),
        }))
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    async fn run<T: Tool>(tool: T, args: Value) -> Result<Value, ToolError> {
        tool.execute(args).await
    }

    #[tokio::test]
    async fn parse_valid_and_invalid() {
        let out = run(JsonParse, serde_json::json!({"text": r#"{"a": [1,2]}"#}))
            .await
            .unwrap();
        assert_eq!(out["value"]["a"][1], 2);
        let err = run(JsonParse, serde_json::json!({"text": "{bad"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)));
    }

    #[tokio::test]
    async fn query_dotted_path() {
        let data = serde_json::json!({"user": {"addresses": [{"city": "Hanoi"}]}});
        let out = run(
            JsonQuery,
            serde_json::json!({"data": data, "path": "user.addresses.0.city"}),
        )
        .await
        .unwrap();
        assert_eq!(out["value"], "Hanoi");

        // from a JSON string
        let out = run(
            JsonQuery,
            serde_json::json!({"data": r#"{"a": 1}"#, "path": "a"}),
        )
        .await
        .unwrap();
        assert_eq!(out["value"], 1);

        // missing key
        let err = run(
            JsonQuery,
            serde_json::json!({"data": {"a": 1}, "path": "b"}),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ToolError::NotFound(_)));
    }

    #[tokio::test]
    async fn base64_roundtrip() {
        let enc = run(
            Base64Encode,
            serde_json::json!({"text": "hello, boom!"}),
        )
        .await
        .unwrap();
        assert_eq!(enc["encoded"], "aGVsbG8sIGJvb20h");
        let dec = run(
            Base64Decode,
            serde_json::json!({"text": enc["encoded"]}),
        )
        .await
        .unwrap();
        assert_eq!(dec["decoded"], "hello, boom!");

        let err = run(Base64Decode, serde_json::json!({"text": "!!!"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)));
    }

    #[tokio::test]
    async fn hashes_match_known_vectors() {
        let out = run(HashSha256, serde_json::json!({"text": "abc"}))
            .await
            .unwrap();
        assert_eq!(
            out["hex"],
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        let out = run(HashMd5, serde_json::json!({"text": "abc"}))
            .await
            .unwrap();
        assert_eq!(out["hex"], "900150983cd24fb0d6963f7d28e17f72");
    }
}
