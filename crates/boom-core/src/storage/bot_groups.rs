//! Bot groups: multi-bot chat rooms with a persisted message log.
//!
//! Tables (see `schema.sql`):
//! - `bot_groups` (`id` TEXT PK, `name` TEXT, `bot_ids` TEXT as a JSON array,
//!   `created_at` INTEGER epoch seconds)
//! - `bot_group_messages` (`id` TEXT PK, `group_id` TEXT, `bot_id` TEXT,
//!   `bot_name` TEXT, `text` TEXT, `created_at` INTEGER)
//!
//! Deleting a group deletes its messages as well (explicitly, in the same
//! transaction — the `ON DELETE CASCADE` foreign key is a backstop).

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use rusqlite::{params, Connection};

use super::{now_unix, Result, StorageError};
use crate::server::state::{BotGroup, BotGroupMessage};

/// Process-wide id counters. Ids embed epoch seconds + process id + a
/// monotonic counter, so they are unique within a process; the `create` /
/// `add_message` retry loop covers the (near-impossible) cross-process
/// primary-key collision inside the same second.
static NEXT_GROUP: AtomicU64 = AtomicU64::new(0);
static NEXT_MESSAGE: AtomicU64 = AtomicU64::new(0);

/// Build a group id of the form `bgroup_<epoch>_<pid>_<counter>`.
fn group_id() -> String {
    let n = NEXT_GROUP.fetch_add(1, Ordering::Relaxed);
    format!("bgroup_{}_{}_{}", now_unix(), std::process::id(), n)
}

/// Build a message id of the form `bmsg_<epoch>_<pid>_<counter>`.
fn message_id() -> String {
    let n = NEXT_MESSAGE.fetch_add(1, Ordering::Relaxed);
    format!("bmsg_{}_{}_{}", now_unix(), std::process::id(), n)
}

fn is_constraint_violation(e: &rusqlite::Error) -> bool {
    matches!(
        e,
        rusqlite::Error::SqliteFailure(err, _)
            if err.code == rusqlite::ErrorCode::ConstraintViolation
    )
}

/// CRUD access to the `bot_groups` / `bot_group_messages` tables.
///
/// Borrows the shared [`Mutex<Connection>`] from [`super::Storage`].
/// Bot-existence validation lives one layer up (the server [`Storage`]
/// trait impls), so this store only deals with groups and messages.
pub struct BotGroupStore<'a> {
    conn: &'a Mutex<Connection>,
}

impl<'a> BotGroupStore<'a> {
    pub(crate) fn new(conn: &'a Mutex<Connection>) -> Self {
        BotGroupStore { conn }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.conn.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn group_from_row(row: &rusqlite::Row) -> rusqlite::Result<BotGroup> {
        let bot_ids_json: String = row.get(2)?;
        let bot_ids: Vec<String> = serde_json::from_str(&bot_ids_json).unwrap_or_default();
        Ok(BotGroup {
            id: row.get(0)?,
            name: row.get(1)?,
            bot_ids,
            // Column added later; old rows / old SELECTs may yield NULL.
            chief_bot_id: row.get::<_, Option<String>>(3)?,
            created_at: row.get(4)?,
        })
    }

    fn message_from_row(row: &rusqlite::Row) -> rusqlite::Result<BotGroupMessage> {
        Ok(BotGroupMessage {
            id: row.get(0)?,
            group_id: row.get(1)?,
            bot_id: row.get(2)?,
            bot_name: row.get(3)?,
            text: row.get(4)?,
            created_at: row.get(5)?,
        })
    }

    /// Creates a group. The id is generated; a primary-key collision
    /// (same second, reused process id) is retried with a fresh id.
    /// The chief defaults to the first bot in `bot_ids` (if any).
    pub fn create(&self, name: &str, bot_ids: &[String]) -> Result<BotGroup> {
        let bot_ids_json = serde_json::to_string(bot_ids)?;
        let created_at = now_unix();
        let chief_bot_id: Option<String> = bot_ids.first().cloned();
        let mut last_err: Option<rusqlite::Error> = None;
        for _ in 0..5 {
            let id = group_id();
            let conn = self.lock();
            match conn.execute(
                "INSERT INTO bot_groups (id, name, bot_ids, chief_bot_id, created_at) VALUES (?1, ?2, ?3, ?4, ?5)",
                params![id, name, bot_ids_json, chief_bot_id, created_at],
            ) {
                Ok(_) => {
                    return Ok(BotGroup {
                        id,
                        name: name.to_string(),
                        bot_ids: bot_ids.to_vec(),
                        chief_bot_id: chief_bot_id.clone(),
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

    /// Fetches one group by id, or `None` when it does not exist.
    pub fn get(&self, id: &str) -> Result<Option<BotGroup>> {
        let conn = self.lock();
        let mut stmt = conn.prepare(
            "SELECT id, name, bot_ids, chief_bot_id, created_at FROM bot_groups WHERE id = ?1",
        )?;
        let mut rows = stmt.query(params![id])?;
        match rows.next()? {
            Some(row) => Ok(Some(Self::group_from_row(row)?)),
            None => Ok(None),
        }
    }

    /// Lists all groups, oldest first.
    pub fn list(&self) -> Result<Vec<BotGroup>> {
        let conn = self.lock();
        let mut stmt = conn.prepare(
            "SELECT id, name, bot_ids, chief_bot_id, created_at FROM bot_groups ORDER BY created_at ASC, id ASC",
        )?;
        let groups = stmt
            .query_map([], Self::group_from_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(groups)
    }

    /// Deletes a group and all its messages. Returns `false` when the id
    /// does not exist.
    pub fn delete(&self, id: &str) -> Result<bool> {
        let mut conn = self.lock();
        let tx = conn.transaction()?;
        tx.execute("DELETE FROM bot_group_messages WHERE group_id = ?1", params![id])?;
        let changed = tx.execute("DELETE FROM bot_groups WHERE id = ?1", params![id])?;
        tx.commit()?;
        Ok(changed > 0)
    }

    /// Adds a bot to a group (idempotent — already a member is a no-op).
    /// Returns `None` when the group does not exist.
    /// The first bot ever added becomes chief when no chief is set.
    pub fn add_bot(&self, group_id: &str, bot_id: &str) -> Result<Option<BotGroup>> {
        let mut group = match self.get(group_id)? {
            Some(g) => g,
            None => return Ok(None),
        };
        if !group.bot_ids.iter().any(|b| b == bot_id) {
            group.bot_ids.push(bot_id.to_string());
            let json = serde_json::to_string(&group.bot_ids)?;
            // First member becomes chief when none is set.
            if group.chief_bot_id.is_none() && group.bot_ids.len() == 1 {
                group.chief_bot_id = Some(bot_id.to_string());
            }
            self.lock().execute(
                "UPDATE bot_groups SET bot_ids = ?1, chief_bot_id = ?2 WHERE id = ?3",
                params![json, group.chief_bot_id, group_id],
            )?;
        }
        Ok(Some(group))
    }

    /// Removes a bot from a group (not a member is a no-op).
    /// Returns `None` when the group does not exist.
    /// When the chief is removed, the first remaining member is promoted.
    pub fn remove_bot(&self, group_id: &str, bot_id: &str) -> Result<Option<BotGroup>> {
        let mut group = match self.get(group_id)? {
            Some(g) => g,
            None => return Ok(None),
        };
        if group.bot_ids.iter().any(|b| b == bot_id) {
            group.bot_ids.retain(|b| b != bot_id);
            // Chief left: promote the first remaining member (or clear).
            if group.chief_bot_id.as_deref() == Some(bot_id) {
                group.chief_bot_id = group.bot_ids.first().cloned();
            }
            let json = serde_json::to_string(&group.bot_ids)?;
            self.lock().execute(
                "UPDATE bot_groups SET bot_ids = ?1, chief_bot_id = ?2 WHERE id = ?3",
                params![json, group.chief_bot_id, group_id],
            )?;
        }
        Ok(Some(group))
    }

    /// Sets the chief-of-staff bot. The caller must validate that the bot
    /// is a group member. Returns `None` when the group does not exist.
    pub fn set_chief(&self, group_id: &str, bot_id: &str) -> Result<Option<BotGroup>> {
        let mut group = match self.get(group_id)? {
            Some(g) => g,
            None => return Ok(None),
        };
        group.chief_bot_id = Some(bot_id.to_string());
        self.lock().execute(
            "UPDATE bot_groups SET chief_bot_id = ?1 WHERE id = ?2",
            params![group.chief_bot_id, group_id],
        )?;
        Ok(Some(group))
    }

    /// The effective chief: the designated chief when still a member,
    /// otherwise the first member, otherwise `None`.
    pub fn effective_chief(group: &BotGroup) -> Option<String> {
        match &group.chief_bot_id {
            Some(id) if group.bot_ids.iter().any(|b| b == id) => Some(id.clone()),
            _ => group.bot_ids.first().cloned(),
        }
    }

    /// Appends a message to a group's log. The id is generated; a
    /// primary-key collision is retried with a fresh id.
    pub fn add_message(
        &self,
        group_id: &str,
        bot_id: &str,
        bot_name: &str,
        text: &str,
    ) -> Result<BotGroupMessage> {
        let created_at = now_unix();
        let mut last_err: Option<rusqlite::Error> = None;
        for _ in 0..5 {
            let id = message_id();
            let conn = self.lock();
            match conn.execute(
                "INSERT INTO bot_group_messages (id, group_id, bot_id, bot_name, text, created_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                params![id, group_id, bot_id, bot_name, text, created_at],
            ) {
                Ok(_) => {
                    return Ok(BotGroupMessage {
                        id,
                        group_id: group_id.to_string(),
                        bot_id: bot_id.to_string(),
                        bot_name: bot_name.to_string(),
                        text: text.to_string(),
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

    /// All messages in a group, oldest first.
    pub fn list_messages(&self, group_id: &str) -> Result<Vec<BotGroupMessage>> {
        let conn = self.lock();
        let mut stmt = conn.prepare(
            "SELECT id, group_id, bot_id, bot_name, text, created_at
             FROM bot_group_messages WHERE group_id = ?1
             ORDER BY created_at ASC, id ASC",
        )?;
        let messages = stmt
            .query_map(params![group_id], Self::message_from_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(messages)
    }
}

#[cfg(test)]
mod tests {
    use super::super::open_test_db;
    use super::BotGroupStore;

    #[test]
    fn group_crud_round_trip() {
        let db = open_test_db();
        let store = db.bot_groups();

        assert!(store.list().expect("list").is_empty());
        assert!(store.get("nope").expect("get").is_none());

        let created = store
            .create("Team", &["b1".to_string(), "b2".to_string()])
            .expect("create");
        assert!(!created.id.is_empty());
        assert_eq!(created.name, "Team");
        assert_eq!(created.bot_ids, vec!["b1".to_string(), "b2".to_string()]);
        // Chief defaults to the first member.
        assert_eq!(created.chief_bot_id, Some("b1".to_string()));
        assert!(created.created_at > 0);

        let fetched = store.get(&created.id).expect("get").expect("exists");
        assert_eq!(fetched.id, created.id);
        assert_eq!(fetched.name, "Team");

        let groups = store.list().expect("list");
        assert_eq!(groups.len(), 1);

        // Empty bot list is fine at the store level (validation is above).
        let empty = store.create("Solo", &[]).expect("create empty");
        assert!(empty.bot_ids.is_empty());
        assert_ne!(empty.id, created.id);
        assert_eq!(store.list().expect("list").len(), 2);

        assert!(store.delete(&created.id).expect("delete"));
        assert!(store.get(&created.id).expect("get").is_none());
        assert!(!store.delete(&created.id).expect("delete again"));
        assert_eq!(store.list().expect("list").len(), 1);
    }

    #[test]
    fn chief_assignment_and_reassignment() {
        let db = open_test_db();
        let store = db.bot_groups();

        // Empty group → no chief.
        let g = store.create("Solo", &[]).expect("create");
        assert_eq!(g.chief_bot_id, None);
        assert_eq!(BotGroupStore::effective_chief(&g), None);

        // First bot added becomes chief.
        let g = store.add_bot(&g.id, "b1").expect("add").expect("exists");
        assert_eq!(g.chief_bot_id, Some("b1".to_string()));

        // Second bot added does not steal chief.
        let g = store.add_bot(&g.id, "b2").expect("add").expect("exists");
        assert_eq!(g.chief_bot_id, Some("b1".to_string()));

        // Explicit reassignment.
        let g = store.set_chief(&g.id, "b2").expect("set").expect("exists");
        assert_eq!(g.chief_bot_id, Some("b2".to_string()));

        // Chief removed → first remaining member promoted.
        let g = store.remove_bot(&g.id, "b2").expect("remove").expect("exists");
        assert_eq!(g.chief_bot_id, Some("b1".to_string()));

        // Last member removed → chief cleared.
        let g = store.remove_bot(&g.id, "b1").expect("remove").expect("exists");
        assert_eq!(g.chief_bot_id, None);

        // Unknown group → None.
        assert!(store.set_chief("nope", "b1").expect("set missing").is_none());
    }

    #[test]
    fn effective_chief_falls_back_to_first_member() {
        let db = open_test_db();
        let store = db.bot_groups();
        let g = store.create("Team", &["b1".to_string(), "b2".to_string()]).expect("create");
        // Designated chief not a member (data inconsistency) → first member.
        let mut hacked = g.clone();
        hacked.chief_bot_id = Some("ghost".to_string());
        assert_eq!(
            BotGroupStore::effective_chief(&hacked),
            Some("b1".to_string())
        );
    }

    #[test]
    fn add_and_remove_bots() {
        let db = open_test_db();
        let store = db.bot_groups();
        let g = store.create("Team", &["b1".to_string()]).expect("create");

        // Adding is idempotent.
        let g = store.add_bot(&g.id, "b2").expect("add").expect("exists");
        assert_eq!(g.bot_ids, vec!["b1".to_string(), "b2".to_string()]);
        let g = store.add_bot(&g.id, "b2").expect("add dup").expect("exists");
        assert_eq!(g.bot_ids.len(), 2);

        // Unknown group → None.
        assert!(store.add_bot("nope", "b1").expect("add missing").is_none());
        assert!(store.remove_bot("nope", "b1").expect("remove missing").is_none());

        // Removing a non-member is a no-op.
        let g = store.remove_bot(&g.id, "ghost").expect("remove non-member").expect("exists");
        assert_eq!(g.bot_ids.len(), 2);

        let g = store.remove_bot(&g.id, "b1").expect("remove").expect("exists");
        assert_eq!(g.bot_ids, vec!["b2".to_string()]);
        let persisted = store.get(&g.id).expect("get").expect("exists");
        assert_eq!(persisted.bot_ids, vec!["b2".to_string()]);
    }

    #[test]
    fn messages_are_ordered_and_deleted_with_group() {
        let db = open_test_db();
        let store = db.bot_groups();
        let g = store.create("Team", &["b1".to_string()]).expect("create");

        assert!(store.list_messages(&g.id).expect("list").is_empty());

        let m1 = store.add_message(&g.id, "", "user", "hello").expect("msg");
        let m2 = store.add_message(&g.id, "b1", "Helper", "hi there").expect("msg");
        assert_ne!(m1.id, m2.id);
        assert_eq!(m1.group_id, g.id);
        assert_eq!(m1.bot_name, "user");
        assert_eq!(m2.bot_id, "b1");

        let messages = store.list_messages(&g.id).expect("list");
        assert_eq!(messages.len(), 2);
        // Oldest first.
        assert_eq!(messages[0].id, m1.id);
        assert_eq!(messages[1].id, m2.id);

        // Deleting the group deletes its messages too.
        assert!(store.delete(&g.id).expect("delete"));
        assert!(store.list_messages(&g.id).expect("list after delete").is_empty());
    }

    #[test]
    fn many_groups_get_unique_ids() {
        let db = open_test_db();
        let store = db.bot_groups();
        let mut ids: Vec<String> = (0..50)
            .map(|i| store.create(&format!("g{i}"), &[]).expect("create").id)
            .collect();
        ids.sort();
        ids.dedup();
        assert_eq!(ids.len(), 50, "group ids must be unique");
    }
}
