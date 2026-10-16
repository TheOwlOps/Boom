//! System tools: environment variables, current time, UUID generation.

use async_trait::async_trait;
use serde_json::Value;

use super::{opt_str, req_str, schema_of, Tool, ToolError};

// ---------------------------------------------------------------------------
// env_get
// ---------------------------------------------------------------------------

pub struct EnvGet;
#[async_trait]
impl Tool for EnvGet {
    fn name(&self) -> &str {
        "env_get"
    }
    fn description(&self) -> &str {
        "Read one environment variable. Returns `present: false` when unset."
    }
    fn schema(&self) -> Value {
        schema_of(&[("name", "string", "Variable name")], &["name"])
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let name = req_str(&args, "name")?;
        match std::env::var(&name) {
            Ok(v) => Ok(serde_json::json!({"present": true, "name": name, "value": v})),
            Err(_) => Ok(serde_json::json!({"present": false, "name": name})),
        }
    }
}

// ---------------------------------------------------------------------------
// env_list
// ---------------------------------------------------------------------------

pub struct EnvList;
#[async_trait]
impl Tool for EnvList {
    fn name(&self) -> &str {
        "env_list"
    }
    fn description(&self) -> &str {
        "List environment variable names, optionally filtered by a prefix."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("prefix", "string", "Only list variables starting with this prefix"),
                (
                    "include_values",
                    "boolean",
                    "Include values too (default false — names only)",
                ),
            ],
            &[],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let prefix = opt_str(&args, "prefix", "");
        let include_values = args
            .get("include_values")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let mut names: Vec<String> = std::env::vars()
            .map(|(k, _)| k)
            .filter(|k| k.starts_with(prefix))
            .collect();
        names.sort();
        let mut out = serde_json::json!({
            "names": names,
            "count": names.len(),
        });
        if include_values {
            let mut map = serde_json::Map::new();
            for n in &names {
                if let Ok(v) = std::env::var(n) {
                    map.insert(n.clone(), Value::String(v));
                }
            }
            out["values"] = Value::Object(map);
        }
        Ok(out)
    }
}

// ---------------------------------------------------------------------------
// date_now
// ---------------------------------------------------------------------------

pub struct DateNow;
#[async_trait]
impl Tool for DateNow {
    fn name(&self) -> &str {
        "date_now"
    }
    fn description(&self) -> &str {
        "Current date/time: Unix timestamp, UTC ISO-8601, and local RFC-2822."
    }
    fn schema(&self) -> Value {
        schema_of(&[], &[])
    }
    async fn execute(&self, _args: Value) -> Result<Value, ToolError> {
        let now = chrono::Utc::now();
        let local = chrono::Local::now();
        Ok(serde_json::json!({
            "unix": now.timestamp(),
            "unix_ms": now.timestamp_millis(),
            "utc_iso8601": now.to_rfc3339_opts(chrono::SecondsFormat::Secs, true),
            "local": local.to_rfc2822(),
        }))
    }
}

// ---------------------------------------------------------------------------
// uuid_new
// ---------------------------------------------------------------------------

pub struct UuidNew;
#[async_trait]
impl Tool for UuidNew {
    fn name(&self) -> &str {
        "uuid_new"
    }
    fn description(&self) -> &str {
        "Generate a random UUID v4."
    }
    fn schema(&self) -> Value {
        schema_of(&[], &[])
    }
    async fn execute(&self, _args: Value) -> Result<Value, ToolError> {
        Ok(serde_json::json!({
            "uuid": uuid::Uuid::new_v4().to_string(),
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
    async fn env_roundtrip() {
        std::env::set_var("BOOM_TEST_VAR_XYZ", "123");
        let out = run(EnvGet, serde_json::json!({"name": "BOOM_TEST_VAR_XYZ"}))
            .await
            .unwrap();
        assert_eq!(out["present"], true);
        assert_eq!(out["value"], "123");

        let out = run(EnvGet, serde_json::json!({"name": "BOOM_DEFINITELY_MISSING_XYZ"}))
            .await
            .unwrap();
        assert_eq!(out["present"], false);

        let out = run(EnvList, serde_json::json!({"prefix": "BOOM_TEST_VAR_"}))
            .await
            .unwrap();
        assert!(out["names"]
            .as_array()
            .unwrap()
            .iter()
            .any(|n| n == "BOOM_TEST_VAR_XYZ"));
        std::env::remove_var("BOOM_TEST_VAR_XYZ");
    }

    #[tokio::test]
    async fn date_is_sane() {
        let out = run(DateNow, serde_json::json!({})).await.unwrap();
        let unix = out["unix"].as_i64().unwrap();
        assert!(unix > 1_700_000_000, "timestamp looks wrong: {unix}");
        assert!(out["utc_iso8601"].as_str().unwrap().ends_with('Z'));
    }

    #[tokio::test]
    async fn uuid_is_v4_shaped() {
        let out = run(UuidNew, serde_json::json!({})).await.unwrap();
        let s = out["uuid"].as_str().unwrap();
        let parsed: uuid::Uuid = s.parse().expect("must parse as UUID");
        assert_eq!(parsed.get_version(), Some(uuid::Version::Random));
        assert_eq!(s.len(), 36);
    }
}
