//! Generic key-value store.
//!
//! Namespaced by key prefix, by convention:
//!
//! - `marketplace.installed.<plugin_id>` — installed marketplace items
//! - `groups.policy.<group_id>` — group policy JSON
//! - `groups.muted.<group_id>` — group mute flag (`"1"` / `"0"`)
//! - `settings.*` — app settings
//! - `provider.active` — id of the active provider

use super::Result;
use rusqlite::{params, Connection};
use serde::{de::DeserializeOwned, Serialize};
use std::sync::Mutex;

/// CRUD access to the `kv` table, plus JSON helpers.
///
/// Borrows the shared [`Mutex<Connection>`] from [`super::Storage`].
pub struct KvStore<'a> {
    conn: &'a Mutex<Connection>,
}

impl<'a> KvStore<'a> {
    pub(crate) fn new(conn: &'a Mutex<Connection>) -> Self {
        KvStore { conn }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.conn.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// Reads a value, or `None` when the key is absent.
    pub fn get(&self, key: &str) -> Result<Option<String>> {
        let conn = self.lock();
        let mut stmt = conn.prepare("SELECT value FROM kv WHERE key = ?1")?;
        let mut rows = stmt.query(params![key])?;
        match rows.next()? {
            Some(row) => Ok(Some(row.get(0)?)),
            None => Ok(None),
        }
    }

    /// Inserts or replaces a value.
    pub fn set(&self, key: &str, value: &str) -> Result<()> {
        let conn = self.lock();
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?1, ?2)
             ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            params![key, value],
        )?;
        Ok(())
    }

    /// Deletes a key. Returns `false` when the key is absent.
    pub fn delete(&self, key: &str) -> Result<bool> {
        let conn = self.lock();
        let changed = conn.execute("DELETE FROM kv WHERE key = ?1", params![key])?;
        Ok(changed > 0)
    }

    /// Reads a JSON-encoded value, or `None` when the key is absent.
    pub fn get_json<T: DeserializeOwned>(&self, key: &str) -> Result<Option<T>> {
        match self.get(key)? {
            Some(raw) => Ok(Some(serde_json::from_str(&raw)?)),
            None => Ok(None),
        }
    }

    /// Writes a value serialized as JSON.
    pub fn set_json<T: Serialize>(&self, key: &str, value: &T) -> Result<()> {
        let raw = serde_json::to_string(value)?;
        self.set(key, &raw)
    }

    /// Lists all `(key, value)` pairs whose key starts with `prefix`,
    /// ordered by key. `LIKE` metacharacters in the prefix are escaped.
    pub fn list_prefix(&self, prefix: &str) -> Result<Vec<(String, String)>> {
        let conn = self.lock();
        let pattern = format!("{}%", escape_like(prefix));
        let mut stmt =
            conn.prepare("SELECT key, value FROM kv WHERE key LIKE ?1 ESCAPE '\\' ORDER BY key ASC")?;
        let pairs = stmt
            .query_map(params![pattern], |row| Ok((row.get(0)?, row.get(1)?)))?
            .collect::<rusqlite::Result<Vec<(String, String)>>>()?;
        Ok(pairs)
    }

    /// Deletes all keys starting with `prefix`. Returns the number removed.
    pub fn clear_prefix(&self, prefix: &str) -> Result<u64> {
        let conn = self.lock();
        let pattern = format!("{}%", escape_like(prefix));
        let changed = conn.execute(
            "DELETE FROM kv WHERE key LIKE ?1 ESCAPE '\\'",
            params![pattern],
        )?;
        Ok(changed as u64)
    }
}

/// Escapes `\`, `%` and `_` so a prefix is matched literally in a `LIKE`.
fn escape_like(prefix: &str) -> String {
    let mut out = String::with_capacity(prefix.len());
    for c in prefix.chars() {
        if c == '\\' || c == '%' || c == '_' {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::super::open_test_db;
    use serde::{Deserialize, Serialize};

    #[derive(Debug, Serialize, Deserialize, PartialEq)]
    struct Policy {
        allow: Vec<String>,
        max_messages: u32,
    }

    #[test]
    fn kv_crud_round_trip() {
        let db = open_test_db();
        let kv = db.kv();

        assert!(kv.get("settings.theme").expect("get").is_none());

        kv.set("settings.theme", "dark").expect("set");
        assert_eq!(kv.get("settings.theme").expect("get").as_deref(), Some("dark"));

        kv.set("settings.theme", "light").expect("overwrite");
        assert_eq!(kv.get("settings.theme").expect("get").as_deref(), Some("light"));

        assert!(kv.delete("settings.theme").expect("delete"));
        assert!(kv.get("settings.theme").expect("get").is_none());
        assert!(!kv.delete("settings.theme").expect("delete again"));
    }

    #[test]
    fn kv_json_round_trip() {
        let db = open_test_db();
        let kv = db.kv();

        let policy = Policy {
            allow: vec!["chat".to_string(), "voice".to_string()],
            max_messages: 100,
        };
        kv.set_json("groups.policy.123", &policy).expect("set_json");
        let back: Option<Policy> = kv.get_json("groups.policy.123").expect("get_json");
        assert_eq!(back, Some(policy));

        let missing: Option<Policy> = kv.get_json("groups.policy.999").expect("get_json");
        assert!(missing.is_none());
    }

    #[test]
    fn prefix_listing_and_clearing() {
        let db = open_test_db();
        let kv = db.kv();

        kv.set("marketplace.installed.a", "1").expect("set");
        kv.set("marketplace.installed.b", "1").expect("set");
        kv.set("groups.muted.1", "1").expect("set");
        kv.set("settings.x", "y").expect("set");

        let installed = kv.list_prefix("marketplace.installed.").expect("list");
        assert_eq!(
            installed,
            vec![
                ("marketplace.installed.a".to_string(), "1".to_string()),
                ("marketplace.installed.b".to_string(), "1".to_string()),
            ]
        );

        // LIKE metacharacters in a prefix are matched literally.
        kv.set("weird%key", "v").expect("set");
        assert_eq!(kv.list_prefix("weird%").expect("list").len(), 1);
        assert_eq!(kv.list_prefix("weird").expect("list").len(), 1);

        assert_eq!(kv.clear_prefix("marketplace.installed.").expect("clear"), 2);
        assert!(kv.list_prefix("marketplace.installed.").expect("list").is_empty());
        // Other namespaces are untouched.
        assert_eq!(kv.get("groups.muted.1").expect("get").as_deref(), Some("1"));
    }
}
