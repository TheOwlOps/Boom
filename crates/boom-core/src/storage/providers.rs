//! Provider configurations (API keys, models, base URLs).
//!
//! Each provider's full config is stored as one JSON blob, e.g.
//! `{"api_key": "...", "model": "gpt-4o", "base_url": "https://..."}`.
//!
//! Read paths that surface data to the UI should use [`ProviderStore::list_names`]
//! and [`ProviderStore::has_api_key`] instead of [`ProviderStore::get`], so
//! API keys never leave the storage layer unintentionally — mirroring the
//! gateway's "never returns API keys" rule.

use super::{now_unix, Result};
use rusqlite::{params, Connection};
use serde_json::Value;
use std::sync::Mutex;

/// A provider's stored configuration.
#[derive(Debug, Clone)]
pub struct ProviderConfig {
    /// Provider id, e.g. `"openai"`, `"anthropic"`.
    pub name: String,
    /// Full config blob; may contain secrets — keep inside the backend.
    pub config: Value,
    /// Last write time, Unix epoch seconds.
    pub updated_at: i64,
}

/// CRUD access to the `providers` table.
///
/// Borrows the shared [`Mutex<Connection>`] from [`super::Storage`].
pub struct ProviderStore<'a> {
    conn: &'a Mutex<Connection>,
}

impl<'a> ProviderStore<'a> {
    pub(crate) fn new(conn: &'a Mutex<Connection>) -> Self {
        ProviderStore { conn }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.conn.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// Inserts or replaces the config for `name`.
    pub fn set(&self, name: &str, config: &Value) -> Result<()> {
        let raw = serde_json::to_string(config)?;
        let conn = self.lock();
        conn.execute(
            "INSERT INTO providers (name, config_json, updated_at) VALUES (?1, ?2, ?3)
             ON CONFLICT(name) DO UPDATE SET
                config_json = excluded.config_json,
                updated_at = excluded.updated_at",
            params![name, raw, now_unix()],
        )?;
        Ok(())
    }

    /// Returns the full config, including secrets. Keep inside the backend;
    /// the UI must use [`ProviderStore::has_api_key`] instead.
    pub fn get(&self, name: &str) -> Result<Option<ProviderConfig>> {
        let conn = self.lock();
        let mut stmt =
            conn.prepare("SELECT name, config_json, updated_at FROM providers WHERE name = ?1")?;
        let mut rows = stmt.query(params![name])?;
        match rows.next()? {
            Some(row) => {
                let raw: String = row.get(1)?;
                let config: Value = serde_json::from_str(&raw)?;
                Ok(Some(ProviderConfig {
                    name: row.get(0)?,
                    config,
                    updated_at: row.get(2)?,
                }))
            }
            None => Ok(None),
        }
    }

    /// UI-safe: ids of all configured providers, no secrets.
    pub fn list_names(&self) -> Result<Vec<String>> {
        let conn = self.lock();
        let mut stmt = conn.prepare("SELECT name FROM providers ORDER BY name ASC")?;
        let names = stmt
            .query_map([], |row| row.get(0))?
            .collect::<rusqlite::Result<Vec<String>>>()?;
        Ok(names)
    }

    /// UI-safe: whether `name` has a non-empty `api_key` in its config.
    pub fn has_api_key(&self, name: &str) -> Result<bool> {
        match self.get(name)? {
            Some(cfg) => Ok(cfg
                .config
                .get("api_key")
                .and_then(Value::as_str)
                .map(|k| !k.is_empty())
                .unwrap_or(false)),
            None => Ok(false),
        }
    }

    /// Removes a provider config. Returns `false` when `name` is unknown.
    pub fn delete(&self, name: &str) -> Result<bool> {
        let conn = self.lock();
        let changed = conn.execute("DELETE FROM providers WHERE name = ?1", params![name])?;
        Ok(changed > 0)
    }
}

#[cfg(test)]
mod tests {
    use super::super::open_test_db;
    use super::*;
    use serde_json::json;

    fn sample_config() -> Value {
        json!({
            "api_key": "sk-test-123",
            "model": "gpt-4o",
            "base_url": "https://api.openai.com/v1",
        })
    }

    #[test]
    fn provider_crud_round_trip() {
        let db = open_test_db();
        let store = db.providers();

        assert!(store.get("openai").expect("get").is_none());
        assert!(!store.has_api_key("openai").expect("has_key"));

        store.set("openai", &sample_config()).expect("set");
        let cfg = store.get("openai").expect("get").expect("exists");
        assert_eq!(cfg.name, "openai");
        assert_eq!(cfg.config["api_key"], json!("sk-test-123"));
        assert_eq!(cfg.config["model"], json!("gpt-4o"));
        assert!(cfg.updated_at > 0);
        assert!(store.has_api_key("openai").expect("has_key"));

        // Upsert replaces the blob.
        let updated = json!({"api_key": "", "model": "gpt-4o-mini"});
        store.set("openai", &updated).expect("set again");
        let cfg = store.get("openai").expect("get").expect("exists");
        assert_eq!(cfg.config["model"], json!("gpt-4o-mini"));
        assert!(!store.has_api_key("openai").expect("has_key after clear"));

        assert_eq!(store.list_names().expect("list"), vec!["openai".to_string()]);

        assert!(store.delete("openai").expect("delete"));
        assert!(!store.delete("openai").expect("delete again"));
        assert!(store.list_names().expect("list").is_empty());
    }

    #[test]
    fn list_names_is_sorted_and_key_free() {
        let db = open_test_db();
        let store = db.providers();
        store.set("xai", &json!({"api_key": "x"})).expect("set");
        store.set("anthropic", &json!({"api_key": "y"})).expect("set");
        store.set("deepseek", &json!({})).expect("set");

        assert_eq!(
            store.list_names().expect("list"),
            vec!["anthropic".to_string(), "deepseek".to_string(), "xai".to_string()]
        );
        // has_api_key is false when the key field is missing entirely.
        assert!(!store.has_api_key("deepseek").expect("has_key"));
    }
}
