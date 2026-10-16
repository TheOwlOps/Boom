//! Real plugin registry: discovers plugins from `~/.boom/plugins/` on disk.
//!
//! Each plugin is a folder containing a `plugin.json` manifest — the same
//! format Python's `PluginManager` reads:
//!
//! ```json
//! {"name": "my-plugin", "description": "...", "icon": "🧩",
//!  "version": "1.0.0", "author": "someone"}
//! ```
//!
//! Broken manifests are skipped (never crash the server). Plugin names are
//! validated to block path traversal — only `[a-zA-Z0-9_-]` is accepted.

use std::path::PathBuf;

/// A plugin discovered on disk. `enabled` comes from storage, not the
/// manifest — this struct carries only what the folder contains.
#[derive(Clone, Debug)]
pub struct DiscoveredPlugin {
    pub name: String,
    pub description: String,
    pub icon: Option<String>,
    pub version: String,
    pub author: String,
}

/// Resolve the plugins directory: `$BOOM_HOME/plugins`, or
/// `~/.boom/plugins` when `BOOM_HOME` is unset. Mirrors
/// `crate::storage::DbStorage::default_path`.
pub fn plugins_dir() -> PathBuf {
    std::env::var("BOOM_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            dirs::home_dir()
                .map(|h| h.join(".boom"))
                .unwrap_or_else(|| PathBuf::from(".boom"))
        })
        .join("plugins")
}

/// Plugin names may only contain ASCII alphanumerics, `_` and `-`
/// (max 64 chars). Blocks `../` traversal and weird filesystem names.
pub fn is_valid_plugin_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 64
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

/// Scan the plugins directory and parse every `plugin.json` manifest.
/// Broken manifests and invalid names are skipped, never fatal.
pub fn discover_plugins() -> Vec<DiscoveredPlugin> {
    let dir = plugins_dir();
    let entries = match std::fs::read_dir(&dir) {
        Ok(entries) => entries,
        Err(_) => return Vec::new(),
    };
    let mut plugins = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        let manifest_path = path.join("plugin.json");
        let raw = match std::fs::read_to_string(&manifest_path) {
            Ok(raw) => raw,
            Err(_) => continue,
        };
        let manifest: serde_json::Value = match serde_json::from_str(&raw) {
            Ok(m) => m,
            Err(_) => continue, // broken manifest: skip, don't crash
        };
        let name = manifest
            .get("name")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        if !is_valid_plugin_name(&name) {
            continue;
        }
        // The manifest name wins, but a folder whose name disagrees with its
        // manifest is suspicious — accept it anyway (Python does the same via
        // manifest["name"]) while keeping the folder-derived path for safety.
        let _ = path; // folder name only used for the manifest lookup above
        let str_field = |key: &str| {
            manifest
                .get(key)
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string()
        };
        plugins.push(DiscoveredPlugin {
            name,
            description: str_field("description"),
            icon: manifest
                .get("icon")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string()),
            version: str_field("version"),
            author: str_field("author"),
        });
    }
    plugins.sort_by(|a, b| a.name.cmp(&b.name));
    plugins
}

/// Look up one plugin by name on disk.
pub fn find_plugin(name: &str) -> Option<DiscoveredPlugin> {
    if !is_valid_plugin_name(name) {
        return None;
    }
    discover_plugins().into_iter().find(|p| p.name == name)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn with_temp_plugins_dir(f: impl FnOnce(&PathBuf)) {
        let _guard = ENV_LOCK.lock().unwrap();
        let dir = std::env::temp_dir().join(format!(
            "boom-plugin-registry-test-{}",
            std::process::id()
        ));
        let plugins = dir.join("plugins");
        std::fs::create_dir_all(&plugins).unwrap();
        let prev = std::env::var("BOOM_HOME").ok();
        std::env::set_var("BOOM_HOME", &dir);
        f(&plugins);
        match prev {
            Some(v) => std::env::set_var("BOOM_HOME", v),
            None => std::env::remove_var("BOOM_HOME"),
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn discovers_valid_plugin() {
        with_temp_plugins_dir(|plugins| {
            let p = plugins.join("demo");
            std::fs::create_dir_all(&p).unwrap();
            std::fs::write(
                p.join("plugin.json"),
                r#"{"name":"demo","description":"Demo plugin","icon":"🧪","version":"1.2.3","author":"tester"}"#,
            )
            .unwrap();
            let found = discover_plugins();
            assert_eq!(found.len(), 1);
            assert_eq!(found[0].name, "demo");
            assert_eq!(found[0].version, "1.2.3");
            assert_eq!(found[0].author, "tester");
            assert_eq!(found[0].icon.as_deref(), Some("🧪"));
        });
    }

    #[test]
    fn skips_broken_and_missing_manifests() {
        with_temp_plugins_dir(|plugins| {
            let bad = plugins.join("bad");
            std::fs::create_dir_all(&bad).unwrap();
            std::fs::write(bad.join("plugin.json"), "{not json").unwrap();
            let empty = plugins.join("empty");
            std::fs::create_dir_all(&empty).unwrap();
            let good = plugins.join("good");
            std::fs::create_dir_all(&good).unwrap();
            std::fs::write(good.join("plugin.json"), r#"{"name":"good"}"#).unwrap();
            let found = discover_plugins();
            assert_eq!(found.len(), 1);
            assert_eq!(found[0].name, "good");
            // Missing optional fields default to empty.
            assert_eq!(found[0].description, "");
            assert_eq!(found[0].version, "");
        });
    }

    #[test]
    fn rejects_invalid_names_and_traversal() {
        assert!(is_valid_plugin_name("my-plugin_2"));
        assert!(!is_valid_plugin_name(""));
        assert!(!is_valid_plugin_name("../evil"));
        assert!(!is_valid_plugin_name("a/b"));
        assert!(!is_valid_plugin_name("sp ace"));
        assert!(!is_valid_plugin_name(&"x".repeat(65)));
        assert!(find_plugin("../evil").is_none());
    }

    #[test]
    fn empty_dir_returns_empty() {
        with_temp_plugins_dir(|_| {
            assert!(discover_plugins().is_empty());
        });
    }
}
