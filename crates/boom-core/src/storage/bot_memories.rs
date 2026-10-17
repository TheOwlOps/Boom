//! Per-bot isolated memories: durable facts/notes scoped to one `bot_id`.
//!
//! Table (see `schema.sql`):
//! - `bot_memories` (`id` TEXT PK, `bot_id` TEXT, `role` TEXT, `content` TEXT,
//!   `created_at` INTEGER epoch seconds)
//!
//! Conversation turns are already isolated per bot via sessions
//! (`botmsg_<bot_id>`); this table is for durable per-bot memory entries
//! (facts, preferences, learnings) that must never leak across bots.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use rusqlite::{params, Connection};

use super::{now_unix, Result, StorageError};

static NEXT_MEMORY: AtomicU64 = AtomicU64::new(0);

/// Build a memory id of the form `bmem_<epoch>_<pid>_<counter>`.
fn memory_id() -> String {
    let n = NEXT_MEMORY.fetch_add(1, Ordering::Relaxed);
    format!("bmem_{}_{}_{}", now_unix(), std::process::id(), n)
}

/// One durable memory entry belonging to a single bot.
#[derive(Clone, Debug)]
pub struct BotMemory {
    pub id: String,
    pub bot_id: String,
    pub role: String,
    pub content: String,
    pub created_at: i64,
}

fn is_constraint_violation(e: &rusqlite::Error) -> bool {
    matches!(
        e,
        rusqlite::Error::SqliteFailure(err, _)
            if err.code == rusqlite::ErrorCode::ConstraintViolation
    )
}

/// CRUD access to the `bot_memories` table.
///
/// Borrows the shared [`Mutex<Connection>`] from [`super::Storage`].
pub struct BotMemoryStore<'a> {
    conn: &'a Mutex<Connection>,
}

impl<'a> BotMemoryStore<'a> {
    pub(crate) fn new(conn: &'a Mutex<Connection>) -> Self {
        BotMemoryStore { conn }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.conn.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn memory_from_row(row: &rusqlite::Row) -> rusqlite::Result<BotMemory> {
        Ok(BotMemory {
            id: row.get(0)?,
            bot_id: row.get(1)?,
            role: row.get(2)?,
            content: row.get(3)?,
            created_at: row.get(4)?,
        })
    }

    /// Records a memory for a bot. `role` is a free-form tag
    /// (`"fact"`, `"preference"`, `"learning"` …). Callers should not
    /// store blank content (no validation at this layer).
    pub fn add(&self, bot_id: &str, role: &str, content: &str) -> Result<BotMemory> {
        let content = content.trim();
        let role = if role.trim().is_empty() { "fact" } else { role.trim() };
        let created_at = now_unix();
        let mut last_err: Option<rusqlite::Error> = None;
        for _ in 0..5 {
            let id = memory_id();
            let conn = self.lock();
            match conn.execute(
                "INSERT INTO bot_memories (id, bot_id, role, content, created_at)
                 VALUES (?1, ?2, ?3, ?4, ?5)",
                params![id, bot_id, role, content, created_at],
            ) {
                Ok(_) => {
                    return Ok(BotMemory {
                        id,
                        bot_id: bot_id.to_string(),
                        role: role.to_string(),
                        content: content.to_string(),
                        created_at,
                    })
                }
                Err(e) if is_constraint_violation(&e) => last_err = Some(e),
                Err(e) => return Err(e.into()),
            }
        }
        Err(last_err
            .map(StorageError::from)
            .unwrap_or_else(|| StorageError::Sqlite(rusqlite::Error::InvalidQuery)))
    }

    /// All memories for one bot, oldest first. Never returns another bot's
    /// memories — isolation is enforced by the `WHERE bot_id` clause.
    pub fn list(&self, bot_id: &str, limit: Option<i64>) -> Result<Vec<BotMemory>> {
        let conn = self.lock();
        let limit = limit.unwrap_or(200).clamp(1, 1000);
        let mut stmt = conn.prepare(
            "SELECT id, bot_id, role, content, created_at FROM bot_memories
             WHERE bot_id = ?1 ORDER BY created_at ASC, id ASC LIMIT ?2",
        )?;
        let memories = stmt
            .query_map(params![bot_id, limit], Self::memory_from_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(memories)
    }

    /// Deletes one memory by id. Returns `false` when it does not exist.
    pub fn delete(&self, id: &str) -> Result<bool> {
        let changed = self
            .lock()
            .execute("DELETE FROM bot_memories WHERE id = ?1", params![id])?;
        Ok(changed > 0)
    }

    /// Deletes all memories of one bot. Returns the number removed.
    pub fn clear(&self, bot_id: &str) -> Result<usize> {
        let changed = self
            .lock()
            .execute("DELETE FROM bot_memories WHERE bot_id = ?1", params![bot_id])?;
        Ok(changed)
    }
}

#[cfg(test)]
mod tests {
    use super::super::open_test_db;

    #[test]
    fn memories_are_isolated_per_bot() {
        let db = open_test_db();
        let store = db.bot_memories();

        let m1 = store.add("b1", "fact", "likes coffee").expect("add");
        store.add("b1", "preference", "speaks Vietnamese").expect("add");
        store.add("b2", "fact", "likes tea").expect("add");

        assert!(!m1.id.is_empty());
        assert_eq!(m1.bot_id, "b1");
        assert_eq!(m1.role, "fact");

        // Each bot only sees its own memories.
        let b1_mems = store.list("b1", None).expect("list");
        assert_eq!(b1_mems.len(), 2);
        assert!(b1_mems.iter().all(|m| m.bot_id == "b1"));

        let b2_mems = store.list("b2", None).expect("list");
        assert_eq!(b2_mems.len(), 1);
        assert_eq!(b2_mems[0].content, "likes tea");

        // Unknown bot → empty, not an error.
        assert!(store.list("ghost", None).expect("list").is_empty());
    }

    #[test]
    fn add_defaults_role() {
        let db = open_test_db();
        let store = db.bot_memories();
        let m = store.add("b1", "", "hello").expect("add");
        assert_eq!(m.role, "fact");
    }

    #[test]
    fn delete_and_clear() {
        let db = open_test_db();
        let store = db.bot_memories();
        let m = store.add("b1", "fact", "x").expect("add");
        store.add("b1", "fact", "y").expect("add");
        store.add("b2", "fact", "z").expect("add");

        assert!(store.delete(&m.id).expect("delete"));
        assert!(!store.delete(&m.id).expect("delete again"));
        assert_eq!(store.list("b1", None).expect("list").len(), 1);

        // Clear only affects the target bot.
        assert_eq!(store.clear("b1").expect("clear"), 1);
        assert!(store.list("b1", None).expect("list").is_empty());
        assert_eq!(store.list("b2", None).expect("list").len(), 1);
    }

    #[test]
    fn list_respects_limit() {
        let db = open_test_db();
        let store = db.bot_memories();
        for i in 0..5 {
            store.add("b1", "fact", &format!("fact {i}")).expect("add");
        }
        assert_eq!(store.list("b1", Some(3)).expect("list").len(), 3);
        // Oldest first.
        let mems = store.list("b1", Some(2)).expect("list");
        assert_eq!(mems[0].content, "fact 0");
    }
}
