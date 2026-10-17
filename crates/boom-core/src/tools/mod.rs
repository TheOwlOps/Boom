//! Core tool infrastructure: the [`Tool`] trait, [`ToolError`],
//! [`ToolRegistry`], and shared safety helpers (SSRF protection, arg parsing).
//!
//! This is the Rust port of the registration/dispatch side of
//! `agent/src/tools/registry.py`.

use std::collections::HashMap;
use std::net::IpAddr;
use std::sync::Arc;

use async_trait::async_trait;
use serde_json::Value;

pub mod data;
pub mod fs;
pub mod http;
pub mod shell;
pub mod system;
pub mod web;

/// Errors a tool can return. Mirrors the failure envelopes the Python tools
/// produce (`{"error": ...}`) but as a typed enum.
#[derive(Debug, thiserror::Error)]
pub enum ToolError {
    /// Caller supplied bad or missing arguments.
    #[error("invalid arguments: {0}")]
    InvalidArgs(String),
    /// Filesystem / OS I/O failure.
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    /// Operation exceeded its deadline.
    #[error("timeout after {0} seconds")]
    Timeout(u64),
    /// HTTP request failed.
    #[error("http error: {0}")]
    Http(String),
    /// Referenced resource does not exist.
    #[error("not found: {0}")]
    NotFound(String),
    /// Request refused by a safety policy (SSRF, path traversal, blocklist…).
    #[error("blocked: {0}")]
    Blocked(String),
    /// Text/binary encoding problem.
    #[error("encoding error: {0}")]
    Encoding(String),
    /// Anything else.
    #[error("{0}")]
    Other(String),
}

/// Every tool the agent can call.
///
/// * `name` — the stable identifier the model uses (e.g. `"file_read"`).
/// * `description` — short human/model readable description.
/// * `schema` — JSON-Schema object describing the accepted `args`.
/// * `execute` — run the tool; returns a JSON value on success.
#[async_trait]
pub trait Tool: Send + Sync {
    fn name(&self) -> &str;
    fn description(&self) -> &str;
    fn schema(&self) -> Value;
    async fn execute(&self, args: Value) -> Result<Value, ToolError>;
}

/// Registry of available tools. Port of `ToolRegistry` in `registry.py`.
#[derive(Default)]
pub struct ToolRegistry {
    tools: HashMap<String, Arc<dyn Tool>>,
    toolsets: HashMap<String, Vec<String>>,
}

impl ToolRegistry {
    /// New registry pre-populated with every built-in tool.
    pub fn with_builtins() -> Self {
        let mut reg = Self::default();
        // Filesystem
        reg.register("file", fs::FileRead);
        reg.register("file", fs::FileWrite);
        reg.register("file", fs::FileEdit);
        reg.register("file", fs::FileAppend);
        reg.register("file", fs::FileInfo);
        reg.register("file", fs::FileDelete);
        reg.register("file", fs::FileCopy);
        reg.register("file", fs::FileMove);
        reg.register("file", fs::FileExists);
        reg.register("file", fs::DirList);
        reg.register("file", fs::Glob);
        reg.register("file", fs::Grep);
        // Shell
        reg.register("terminal", shell::ShellRun);
        reg.register("terminal", shell::ShellBg);
        reg.register("terminal", shell::ProcessList);
        reg.register("terminal", shell::ProcessKill);
        // HTTP
        reg.register("web", http::HttpGet);
        reg.register("web", http::HttpPost);
        // Web research
        reg.register("web", web::WebSearch);
        reg.register("web", web::WebFetch);
        // Data
        reg.register("data", data::JsonParse);
        reg.register("data", data::JsonQuery);
        reg.register("data", data::Base64Encode);
        reg.register("data", data::Base64Decode);
        reg.register("data", data::HashSha256);
        reg.register("data", data::HashMd5);
        // System
        reg.register("system", system::EnvGet);
        reg.register("system", system::EnvList);
        reg.register("system", system::DateNow);
        reg.register("system", system::UuidNew);
        reg
    }

    /// Register one tool under a toolset name.
    pub fn register<T: Tool + 'static>(&mut self, toolset: &str, tool: T) {
        let name = tool.name().to_string();
        self.tools.insert(name.clone(), Arc::new(tool));
        self.toolsets
            .entry(toolset.to_string())
            .or_default()
            .push(name);
    }

    /// Look up a tool by name.
    pub fn get(&self, name: &str) -> Option<Arc<dyn Tool>> {
        self.tools.get(name).cloned()
    }

    /// All registered tool names, sorted.
    pub fn tool_names(&self) -> Vec<String> {
        let mut names: Vec<String> = self.tools.keys().cloned().collect();
        names.sort();
        names
    }

    /// All registered toolset names, sorted.
    pub fn toolset_names(&self) -> Vec<String> {
        let mut names: Vec<String> = self.toolsets.keys().cloned().collect();
        names.sort();
        names
    }

    /// Tool names belonging to one toolset.
    pub fn tools_in_toolset(&self, toolset: &str) -> Vec<String> {
        self.toolsets.get(toolset).cloned().unwrap_or_default()
    }

    /// Dispatch a call by tool name. Returns the tool's JSON result, or an
    /// error envelope `{"error": ...}` for unknown tools — mirroring the
    /// Python registry's `dispatch`.
    pub async fn dispatch(&self, name: &str, args: Value) -> Value {
        match self.get(name) {
            Some(tool) => match tool.execute(args).await {
                Ok(v) => v,
                Err(e) => serde_json::json!({"error": e.to_string()}),
            },
            None => serde_json::json!({"error": format!("Tool '{name}' not found in registry.")}),
        }
    }

    /// Export every tool as an OpenAI-style function definition
    /// (`{name, description, parameters}`) for the model prompt.
    pub fn function_definitions(&self) -> Vec<Value> {
        let mut defs: Vec<Value> = self
            .tools
            .values()
            .map(|t| {
                serde_json::json!({
                    "name": t.name(),
                    "description": t.description(),
                    "parameters": t.schema(),
                })
            })
            .collect();
        defs.sort_by(|a, b| a["name"].as_str().cmp(&b["name"].as_str()));
        defs
    }
}

// ---------------------------------------------------------------------------
// Argument helpers
// ---------------------------------------------------------------------------

/// Extract a required string argument.
pub(crate) fn req_str(args: &Value, key: &str) -> Result<String, ToolError> {
    args.get(key)
        .and_then(Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| ToolError::InvalidArgs(format!("missing required string argument '{key}'")))
}

/// Extract an optional string argument with a default.
pub(crate) fn opt_str<'a>(args: &'a Value, key: &str, default: &'a str) -> &'a str {
    args.get(key).and_then(Value::as_str).unwrap_or(default)
}

/// Extract an optional u64 argument with a default.
pub(crate) fn opt_u64(args: &Value, key: &str, default: u64) -> u64 {
    args.get(key).and_then(Value::as_u64).unwrap_or(default)
}

/// Extract an optional bool argument with a default.
pub(crate) fn opt_bool(args: &Value, key: &str, default: bool) -> bool {
    args.get(key).and_then(Value::as_bool).unwrap_or(default)
}

/// Build a tiny JSON-Schema object from (name, type, description, required)
/// property tuples.
pub(crate) fn schema_of(props: &[(&str, &str, &str)], required: &[&str]) -> Value {
    let mut properties = serde_json::Map::new();
    for (name, ty, desc) in props {
        properties.insert(
            name.to_string(),
            serde_json::json!({"type": ty, "description": desc}),
        );
    }
    serde_json::json!({
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": false,
    })
}

// ---------------------------------------------------------------------------
// SSRF protection
// ---------------------------------------------------------------------------

/// IP ranges that must never be contacted by outbound HTTP tools.
/// Covers the task's list (127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12,
/// 192.168.0.0/16, ::1) plus link-local, multicast, unspecified and the
/// IPv6 unique-local range.
fn blocked_nets() -> Vec<ipnet::IpNet> {
    [
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16", // link-local
        "0.0.0.0/8",      // "this network"
        "224.0.0.0/4",    // multicast
        "::1/128",
        "fc00::/7",  // unique local
        "fe80::/10", // link local
        "ff00::/8",  // multicast
        "::/128",    // unspecified
    ]
    .iter()
    .map(|s| s.parse().expect("hardcoded net must parse"))
    .collect()
}

/// True if the IP must not be contacted.
pub(crate) fn ip_is_blocked(ip: &IpAddr) -> bool {
    blocked_nets().iter().any(|net| net.contains(ip))
}

/// Resolve `host` and reject the request if *any* resolved address is a
/// private/loopback/link-local address. Returns the port the URL implies.
///
/// This is the SSRF guard shared by `http_get`, `http_post` and `web_fetch`.
/// Callers must also re-check every redirect hop (see `http.rs`).
pub(crate) async fn ssrf_check_url(url: &str) -> Result<(String, u16), ToolError> {
    let parsed: reqwest::Url = url
        .parse()
        .map_err(|e| ToolError::InvalidArgs(format!("invalid URL '{url}': {e}")))?;
    match parsed.scheme() {
        "http" | "https" => {}
        other => {
            return Err(ToolError::Blocked(format!(
                "URL scheme '{other}' is not allowed (only http/https)"
            )))
        }
    }
    let host = parsed
        .host_str()
        .ok_or_else(|| ToolError::InvalidArgs("URL has no host".to_string()))?
        .to_string();
    // Fast path: literal IP in the URL.
    if let Ok(ip) = host.parse::<IpAddr>() {
        if ip_is_blocked(&ip) {
            return Err(ToolError::Blocked(format!(
                "refusing to contact blocked IP address {ip}"
            )));
        }
    } else {
        // DNS resolution: block if ANY resolved address is private etc.
        let port = parsed.port_or_known_default().unwrap_or(443);
        let addrs = tokio::net::lookup_host((host.as_str(), port))
            .await
            .map_err(|e| ToolError::Http(format!("DNS resolution failed for '{host}': {e}")))?;
        let mut any = false;
        for addr in addrs {
            any = true;
            if ip_is_blocked(&addr.ip()) {
                return Err(ToolError::Blocked(format!(
                    "host '{host}' resolves to blocked address {}",
                    addr.ip()
                )));
            }
        }
        if !any {
            return Err(ToolError::Http(format!("host '{host}' resolved to no addresses")));
        }
    }
    let port = parsed.port_or_known_default().unwrap_or(443);
    Ok((host, port))
}

// ---------------------------------------------------------------------------
// Test support (shared by module tests)
// ---------------------------------------------------------------------------

/// Test-only helpers. File and shell tests both consult `$BOOM_TOOL_ROOT`
/// (via `fs::tools_root`), which is process-global, so tests that touch it
/// must serialize on [`TestRoot`]'s lock.
#[cfg(test)]
pub(crate) mod test_support {
    use std::path::Path;
    use std::sync::{Mutex, MutexGuard};

    static LOCK: Mutex<()> = Mutex::new(());

    /// RAII guard: fresh temp dir as the tool root for the whole test.
    pub struct TestRoot {
        _guard: MutexGuard<'static, ()>,
        _dir: tempfile::TempDir,
    }

    impl TestRoot {
        pub fn new() -> Self {
            let guard = LOCK.lock().unwrap();
            let dir = tempfile::tempdir().unwrap();
            std::env::set_var("BOOM_TOOL_ROOT", dir.path());
            Self { _guard: guard, _dir: dir }
        }

        pub fn path(&self) -> &Path {
            self._dir.path()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn blocked_ranges_cover_task_list() {
        for ip in [
            "127.0.0.1",
            "127.9.9.9",
            "10.0.0.1",
            "10.255.255.255",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.0.1",
            "192.168.1.100",
            "::1",
        ] {
            let ip: IpAddr = ip.parse().unwrap();
            assert!(ip_is_blocked(&ip), "{ip} should be blocked");
        }
        for ip in ["8.8.8.8", "1.1.1.1", "93.184.216.34"] {
            let ip: IpAddr = ip.parse().unwrap();
            assert!(!ip_is_blocked(&ip), "{ip} should be allowed");
        }
        // 172.32.x.x is NOT in 172.16/12
        let ip: IpAddr = "172.32.0.1".parse().unwrap();
        assert!(!ip_is_blocked(&ip));
    }

    #[tokio::test]
    async fn ssrf_rejects_private_literal_and_scheme() {
        assert!(ssrf_check_url("http://127.0.0.1/x").await.is_err());
        assert!(ssrf_check_url("http://10.1.2.3/x").await.is_err());
        assert!(ssrf_check_url("http://192.168.1.1/x").await.is_err());
        assert!(ssrf_check_url("http://[::1]/x").await.is_err());
        assert!(ssrf_check_url("ftp://example.com/x").await.is_err());
        assert!(ssrf_check_url("file:///etc/passwd").await.is_err());
    }

    #[tokio::test]
    async fn registry_dispatch_unknown_tool_errors() {
        let reg = ToolRegistry::with_builtins();
        assert_eq!(reg.tool_names().len(), 30);
        let out = reg.dispatch("nope", serde_json::json!({})).await;
        assert!(out.get("error").is_some());
    }

    #[tokio::test]
    async fn function_definitions_cover_all_tools() {
        let reg = ToolRegistry::with_builtins();
        let defs = reg.function_definitions();
        assert_eq!(defs.len(), 30);
        assert!(defs.iter().all(|d| d.get("parameters").is_some()));
    }

    #[test]
    fn all_tools_have_valid_tool_info_and_schemas() {
        let reg = ToolRegistry::with_builtins();
        assert_eq!(reg.tool_names().len(), 30);
        for name in reg.tool_names() {
            let tool = reg.get(&name).unwrap_or_else(|| panic!("tool '{name}' missing"));
            assert!(!tool.name().is_empty(), "empty name");
            assert!(!tool.description().is_empty(), "empty description for {name}");
            // Schema must be a valid JSON-Schema object: type "object" with
            // a "properties" object, and every required key present in
            // properties.
            let schema = tool.schema();
            let obj = schema.as_object().unwrap_or_else(|| panic!("schema of {name} is not an object"));
            assert_eq!(
                obj.get("type").and_then(|t| t.as_str()),
                Some("object"),
                "schema of {name} must declare type: object"
            );
            let props = obj
                .get("properties")
                .and_then(|p| p.as_object())
                .unwrap_or_else(|| panic!("schema of {name} lacks properties object"));
            let required = obj
                .get("required")
                .and_then(|r| r.as_array())
                .cloned()
                .unwrap_or_default();
            // Empty properties is fine for arg-less tools (date_now,
            // uuid_new); otherwise every tool should document at least one
            // accepted argument.
            if props.is_empty() {
                assert!(
                    required.is_empty(),
                    "schema of {name} has no properties but requires args"
                );
            }
            for req in required {
                let key = req.as_str().unwrap_or_else(|| panic!("required entry of {name} not a string"));
                assert!(
                    props.contains_key(key),
                    "required arg '{key}' of {name} missing from properties"
                );
            }
        }
    }
}
