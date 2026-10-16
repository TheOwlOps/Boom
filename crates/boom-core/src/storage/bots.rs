//! User-defined custom bots (Grok-style).
//!
//! Exactly one bot may be active at a time; [`BotStore::set_active`]
//! enforces that inside a single transaction.

use super::Result;
use rusqlite::{params, Connection};
use std::sync::Mutex;

/// A custom bot.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Bot {
    /// Unique bot id.
    pub id: String,
    pub name: String,
    pub system_prompt: String,
    pub model: String,
    pub provider: String,
    pub active: bool,
}

/// Fields required to create a bot.
#[derive(Debug, Clone)]
pub struct NewBot {
    pub id: String,
    pub name: String,
    pub system_prompt: String,
    pub model: String,
    pub provider: String,
}

/// Patch for [`BotStore::update`]: every `Some` field is written, `None`
/// fields are left untouched.
#[derive(Debug, Clone, Default)]
pub struct BotPatch {
    pub name: Option<String>,
    pub system_prompt: Option<String>,
    pub model: Option<String>,
    pub provider: Option<String>,
}

impl BotPatch {
    fn is_empty(&self) -> bool {
        self.name.is_none()
            && self.system_prompt.is_none()
            && self.model.is_none()
            && self.provider.is_none()
    }
}

/// CRUD access to the `bots` table.
///
/// Borrows the shared [`Mutex<Connection>`] from [`super::Storage`].
pub struct BotStore<'a> {
    conn: &'a Mutex<Connection>,
}

impl<'a> BotStore<'a> {
    pub(crate) fn new(conn: &'a Mutex<Connection>) -> Self {
        BotStore { conn }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.conn.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn bot_from_row(row: &rusqlite::Row) -> rusqlite::Result<Bot> {
        let active: i64 = row.get(5)?;
        Ok(Bot {
            id: row.get(0)?,
            name: row.get(1)?,
            system_prompt: row.get(2)?,
            model: row.get(3)?,
            provider: row.get(4)?,
            active: active != 0,
        })
    }

    fn exists(&self, conn: &Connection, id: &str) -> Result<bool> {
        let found: bool = conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM bots WHERE id = ?1)",
            params![id],
            |row| row.get(0),
        )?;
        Ok(found)
    }

    /// Creates a bot. New bots start inactive. Fails on duplicate id.
    pub fn create(&self, bot: &NewBot) -> Result<Bot> {
        let conn = self.lock();
        conn.execute(
            "INSERT INTO bots (id, name, system_prompt, model, provider, active)
             VALUES (?1, ?2, ?3, ?4, ?5, 0)",
            params![bot.id, bot.name, bot.system_prompt, bot.model, bot.provider],
        )?;
        Ok(Bot {
            id: bot.id.clone(),
            name: bot.name.clone(),
            system_prompt: bot.system_prompt.clone(),
            model: bot.model.clone(),
            provider: bot.provider.clone(),
            active: false,
        })
    }

    /// Fetches one bot by id, or `None` when it does not exist.
    pub fn get(&self, id: &str) -> Result<Option<Bot>> {
        let conn = self.lock();
        let mut stmt = conn.prepare(
            "SELECT id, name, system_prompt, model, provider, active FROM bots WHERE id = ?1",
        )?;
        let mut rows = stmt.query(params![id])?;
        match rows.next()? {
            Some(row) => Ok(Some(Self::bot_from_row(row)?)),
            None => Ok(None),
        }
    }

    /// Lists all bots, ordered by name.
    pub fn list(&self) -> Result<Vec<Bot>> {
        let conn = self.lock();
        let mut stmt = conn.prepare(
            "SELECT id, name, system_prompt, model, provider, active FROM bots ORDER BY name ASC, id ASC",
        )?;
        let bots = stmt
            .query_map([], Self::bot_from_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(bots)
    }

    /// Applies a [`BotPatch`]. Returns `false` when the id does not exist.
    pub fn update(&self, id: &str, patch: &BotPatch) -> Result<bool> {
        let mut conn = self.lock();
        if !self.exists(&conn, id)? {
            return Ok(false);
        }
        if patch.is_empty() {
            return Ok(true);
        }
        let tx = conn.transaction()?;
        if let Some(name) = &patch.name {
            tx.execute("UPDATE bots SET name = ?1 WHERE id = ?2", params![name, id])?;
        }
        if let Some(system_prompt) = &patch.system_prompt {
            tx.execute(
                "UPDATE bots SET system_prompt = ?1 WHERE id = ?2",
                params![system_prompt, id],
            )?;
        }
        if let Some(model) = &patch.model {
            tx.execute("UPDATE bots SET model = ?1 WHERE id = ?2", params![model, id])?;
        }
        if let Some(provider) = &patch.provider {
            tx.execute("UPDATE bots SET provider = ?1 WHERE id = ?2", params![provider, id])?;
        }
        tx.commit()?;
        Ok(true)
    }

    /// Deletes a bot. Returns `false` when the id does not exist.
    pub fn delete(&self, id: &str) -> Result<bool> {
        let conn = self.lock();
        let changed = conn.execute("DELETE FROM bots WHERE id = ?1", params![id])?;
        Ok(changed > 0)
    }

    /// Marks exactly one bot active, deactivating all others atomically.
    /// Returns `false` (changing nothing) when the id does not exist.
    pub fn set_active(&self, id: &str) -> Result<bool> {
        let mut conn = self.lock();
        if !self.exists(&conn, id)? {
            return Ok(false);
        }
        let tx = conn.transaction()?;
        tx.execute("UPDATE bots SET active = 0", [])?;
        tx.execute("UPDATE bots SET active = 1 WHERE id = ?1", params![id])?;
        tx.commit()?;
        Ok(true)
    }

    /// The currently active bot, if any.
    pub fn active(&self) -> Result<Option<Bot>> {
        let conn = self.lock();
        let mut stmt = conn.prepare(
            "SELECT id, name, system_prompt, model, provider, active FROM bots WHERE active != 0 LIMIT 1",
        )?;
        let mut rows = stmt.query([])?;
        match rows.next()? {
            Some(row) => Ok(Some(Self::bot_from_row(row)?)),
            None => Ok(None),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::super::open_test_db;
    use super::*;

    fn new_bot(id: &str, name: &str) -> NewBot {
        NewBot {
            id: id.to_string(),
            name: name.to_string(),
            system_prompt: "You are helpful.".to_string(),
            model: "gpt-4o".to_string(),
            provider: "openai".to_string(),
        }
    }

    #[test]
    fn bot_crud_round_trip() {
        let db = open_test_db();
        let store = db.bots();

        let created = store.create(&new_bot("b1", "Helper")).expect("create");
        assert!(!created.active);
        assert_eq!(created.system_prompt, "You are helpful.");

        let fetched = store.get("b1").expect("get").expect("exists");
        assert_eq!(fetched, created);
        assert!(store.get("nope").expect("get").is_none());

        let patch = BotPatch {
            name: Some("Super Helper".to_string()),
            model: Some("gpt-4o-mini".to_string()),
            ..Default::default()
        };
        assert!(store.update("b1", &patch).expect("update"));
        let updated = store.get("b1").expect("get").expect("exists");
        assert_eq!(updated.name, "Super Helper");
        assert_eq!(updated.model, "gpt-4o-mini");
        // Untouched fields keep their values.
        assert_eq!(updated.system_prompt, "You are helpful.");
        assert_eq!(updated.provider, "openai");

        assert!(!store.update("nope", &patch).expect("update missing"));

        assert!(store.delete("b1").expect("delete"));
        assert!(store.get("b1").expect("get").is_none());
        assert!(!store.delete("b1").expect("delete again"));
    }

    #[test]
    fn exactly_one_bot_is_active() {
        let db = open_test_db();
        let store = db.bots();

        store.create(&new_bot("b1", "One")).expect("create");
        store.create(&new_bot("b2", "Two")).expect("create");
        assert!(store.active().expect("active").is_none());

        assert!(store.set_active("b1").expect("set active"));
        assert_eq!(store.active().expect("active").expect("one").id, "b1");

        // Activating b2 deactivates b1 in the same transaction.
        assert!(store.set_active("b2").expect("set active"));
        let active = store.active().expect("active").expect("one");
        assert_eq!(active.id, "b2");
        assert!(!store.get("b1").expect("get").expect("exists").active);

        // Unknown id changes nothing.
        assert!(!store.set_active("ghost").expect("set active missing"));
        assert_eq!(store.active().expect("active").expect("one").id, "b2");

        // Deleting the active bot leaves no active bot.
        assert!(store.delete("b2").expect("delete"));
        assert!(store.active().expect("active").is_none());
    }

    #[test]
    fn list_orders_by_name() {
        let db = open_test_db();
        let store = db.bots();
        store.create(&new_bot("b2", "Zed")).expect("create");
        store.create(&new_bot("b1", "Alpha")).expect("create");
        let names: Vec<String> = store.list().expect("list").into_iter().map(|b| b.name).collect();
        assert_eq!(names, vec!["Alpha".to_string(), "Zed".to_string()]);
    }

    #[test]
    fn duplicate_bot_id_fails() {
        let db = open_test_db();
        let store = db.bots();
        store.create(&new_bot("b1", "One")).expect("create");
        assert!(store.create(&new_bot("b1", "Uno")).is_err());
    }
}
