//! Chat sessions and their messages.

use super::{now_unix, Result};
use rusqlite::{params, Connection};
use std::sync::Mutex;

/// A chat session (conversation).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Session {
    /// Unique session id.
    pub id: String,
    /// Display title.
    pub title: String,
    /// Creation time, Unix epoch seconds.
    pub created_at: i64,
    /// Last activity time, Unix epoch seconds (bumped on new messages).
    pub updated_at: i64,
}

/// A single chat message inside a session.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Message {
    /// Row id, increasing in insertion order.
    pub id: i64,
    pub session_id: String,
    /// e.g. `"user"`, `"assistant"`, `"system"`, `"tool"`.
    pub role: String,
    pub content: String,
    /// Creation time, Unix epoch seconds.
    pub created_at: i64,
}

/// CRUD access to the `sessions` and `messages` tables.
///
/// Borrows the shared [`Mutex<Connection>`] from [`super::Storage`]; the
/// mutex is locked once per method call, so every method is atomic with
/// respect to other threads using the same `Storage`.
pub struct SessionStore<'a> {
    conn: &'a Mutex<Connection>,
}

impl<'a> SessionStore<'a> {
    pub(crate) fn new(conn: &'a Mutex<Connection>) -> Self {
        SessionStore { conn }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Connection> {
        // A poisoned mutex still guards a usable connection; recover instead
        // of panicking so one failed thread cannot take the DB down.
        self.conn.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn session_from_row(row: &rusqlite::Row) -> rusqlite::Result<Session> {
        Ok(Session {
            id: row.get(0)?,
            title: row.get(1)?,
            created_at: row.get(2)?,
            updated_at: row.get(3)?,
        })
    }

    fn message_from_row(row: &rusqlite::Row) -> rusqlite::Result<Message> {
        Ok(Message {
            id: row.get(0)?,
            session_id: row.get(1)?,
            role: row.get(2)?,
            content: row.get(3)?,
            created_at: row.get(4)?,
        })
    }

    /// Creates a new session. Fails if `id` already exists.
    pub fn create_session(&self, id: &str, title: &str) -> Result<Session> {
        let now = now_unix();
        let conn = self.lock();
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?1, ?2, ?3, ?3)",
            params![id, title, now],
        )?;
        Ok(Session {
            id: id.to_string(),
            title: title.to_string(),
            created_at: now,
            updated_at: now,
        })
    }

    /// Fetches one session by id, or `None` when it does not exist.
    pub fn get_session(&self, id: &str) -> Result<Option<Session>> {
        let conn = self.lock();
        let mut stmt =
            conn.prepare("SELECT id, title, created_at, updated_at FROM sessions WHERE id = ?1")?;
        let mut rows = stmt.query(params![id])?;
        match rows.next()? {
            Some(row) => Ok(Some(Self::session_from_row(row)?)),
            None => Ok(None),
        }
    }

    /// Create the session when missing, otherwise bump its `updated_at`.
    /// Idempotent — safe to call on every chat turn for the session id.
    /// The `title` is only used when the row is created.
    pub fn touch_session(&self, id: &str, title: &str) -> Result<Session> {
        if self.get_session(id)?.is_some() {
            let now = now_unix();
            self.lock().execute(
                "UPDATE sessions SET updated_at = ?1 WHERE id = ?2",
                params![now, id],
            )?;
            // The row existed a moment ago; re-fetch it after the bump.
            return self.get_session(id)?.ok_or_else(|| {
                crate::storage::StorageError::from(rusqlite::Error::QueryReturnedNoRows)
            });
        }
        self.create_session(id, title)
    }

    /// Lists all sessions, most recently active first.
    pub fn list_sessions(&self) -> Result<Vec<Session>> {
        let conn = self.lock();
        let mut stmt = conn.prepare(
            "SELECT id, title, created_at, updated_at FROM sessions ORDER BY updated_at DESC, id ASC",
        )?;
        let sessions = stmt
            .query_map([], Self::session_from_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(sessions)
    }

    /// Renames a session (and bumps `updated_at`). Returns `false` on unknown id.
    pub fn rename_session(&self, id: &str, title: &str) -> Result<bool> {
        let conn = self.lock();
        let changed = conn.execute(
            "UPDATE sessions SET title = ?1, updated_at = ?2 WHERE id = ?3",
            params![title, now_unix(), id],
        )?;
        Ok(changed > 0)
    }

    /// Deletes a session and all its messages in one transaction.
    /// Returns `false` when the id does not exist.
    pub fn delete_session(&self, id: &str) -> Result<bool> {
        let mut conn = self.lock();
        let tx = conn.transaction()?;
        // Explicit delete keeps this correct even if foreign_keys were off.
        tx.execute("DELETE FROM messages WHERE session_id = ?1", params![id])?;
        let changed = tx.execute("DELETE FROM sessions WHERE id = ?1", params![id])?;
        tx.commit()?;
        Ok(changed > 0)
    }

    /// Appends a message and bumps the session's `updated_at` atomically.
    /// Fails when the session does not exist (foreign key).
    pub fn append_message(&self, session_id: &str, role: &str, content: &str) -> Result<Message> {
        let mut conn = self.lock();
        let tx = conn.transaction()?;
        let now = now_unix();
        tx.execute(
            "INSERT INTO messages (session_id, role, content, created_at) VALUES (?1, ?2, ?3, ?4)",
            params![session_id, role, content, now],
        )?;
        let id = tx.last_insert_rowid();
        tx.execute(
            "UPDATE sessions SET updated_at = ?1 WHERE id = ?2",
            params![now, session_id],
        )?;
        tx.commit()?;
        Ok(Message {
            id,
            session_id: session_id.to_string(),
            role: role.to_string(),
            content: content.to_string(),
            created_at: now,
        })
    }

    /// Lists a session's messages in insertion order.
    pub fn list_messages(&self, session_id: &str) -> Result<Vec<Message>> {
        let conn = self.lock();
        let mut stmt = conn.prepare(
            "SELECT id, session_id, role, content, created_at FROM messages WHERE session_id = ?1 ORDER BY id ASC",
        )?;
        let messages = stmt
            .query_map(params![session_id], Self::message_from_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(messages)
    }

    /// Deletes one message by row id. Returns `false` when unknown.
    pub fn delete_message(&self, id: i64) -> Result<bool> {
        let conn = self.lock();
        let changed = conn.execute("DELETE FROM messages WHERE id = ?1", params![id])?;
        Ok(changed > 0)
    }

    /// Removes all messages of a session, keeping the session itself.
    /// Returns the number of messages removed.
    pub fn clear_messages(&self, session_id: &str) -> Result<u64> {
        let conn = self.lock();
        let changed =
            conn.execute("DELETE FROM messages WHERE session_id = ?1", params![session_id])?;
        Ok(changed as u64)
    }

    /// Number of messages in a session.
    pub fn message_count(&self, session_id: &str) -> Result<u64> {
        let conn = self.lock();
        let count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?1",
            params![session_id],
            |row| row.get(0),
        )?;
        Ok(count as u64)
    }
}

#[cfg(test)]
mod tests {
    use super::super::open_test_db;

    #[test]
    fn session_crud_round_trip() {
        let db = open_test_db();
        let store = db.sessions();

        let created = store.create_session("s1", "First chat").expect("create");
        assert_eq!(created.id, "s1");
        assert_eq!(created.title, "First chat");
        assert!(created.created_at > 0);
        assert_eq!(created.created_at, created.updated_at);

        let fetched = store.get_session("s1").expect("get").expect("exists");
        assert_eq!(fetched, created);

        assert!(store.get_session("nope").expect("get").is_none());

        assert!(store.rename_session("s1", "Renamed").expect("rename"));
        assert_eq!(store.get_session("s1").expect("get").expect("exists").title, "Renamed");
        assert!(!store.rename_session("nope", "x").expect("rename missing"));

        assert!(store.delete_session("s1").expect("delete"));
        assert!(store.get_session("s1").expect("get").is_none());
        assert!(!store.delete_session("s1").expect("delete again"));
    }

    #[test]
    fn list_sessions_orders_by_recent_activity() {
        let db = open_test_db();
        let store = db.sessions();

        store.create_session("old", "Old").expect("create old");
        store.create_session("new", "New").expect("create new");
        // Pin deterministic timestamps: second-resolution clocks make
        // wall-clock ordering flaky when both rows are created in the
        // same second.
        {
            let conn = store.lock();
            conn.execute("UPDATE sessions SET updated_at = 100 WHERE id = 'old'", [])
                .expect("backdate old");
            conn.execute("UPDATE sessions SET updated_at = 200 WHERE id = 'new'", [])
                .expect("backdate new");
        }

        let ids: Vec<String> = store
            .list_sessions()
            .expect("list")
            .into_iter()
            .map(|s| s.id)
            .collect();
        assert_eq!(ids, vec!["new".to_string(), "old".to_string()]);

        // A new message bumps updated_at to "now", moving "old" to the front.
        store.append_message("old", "user", "hello").expect("append");
        let ids: Vec<String> = store
            .list_sessions()
            .expect("list")
            .into_iter()
            .map(|s| s.id)
            .collect();
        assert_eq!(ids[0], "old");
    }

    #[test]
    fn message_crud_round_trip() {
        let db = open_test_db();
        let store = db.sessions();
        store.create_session("s1", "t").expect("create");

        let m1 = store.append_message("s1", "user", "hello").expect("append");
        let m2 = store.append_message("s1", "assistant", "hi there").expect("append");
        assert!(m2.id > m1.id);
        assert_eq!(m1.role, "user");
        assert_eq!(m2.content, "hi there");

        let messages = store.list_messages("s1").expect("list");
        assert_eq!(messages.len(), 2);
        assert_eq!(messages[0], m1);
        assert_eq!(messages[1], m2);
        assert_eq!(store.message_count("s1").expect("count"), 2);

        assert!(store.delete_message(m1.id).expect("delete message"));
        assert!(!store.delete_message(m1.id).expect("delete again"));
        assert_eq!(store.list_messages("s1").expect("list").len(), 1);

        assert_eq!(store.clear_messages("s1").expect("clear"), 1);
        assert!(store.list_messages("s1").expect("list").is_empty());
    }

    #[test]
    fn delete_session_removes_its_messages() {
        let db = open_test_db();
        let store = db.sessions();
        store.create_session("s1", "t").expect("create");
        store.create_session("s2", "t").expect("create");
        store.append_message("s1", "user", "a").expect("append");
        store.append_message("s2", "user", "b").expect("append");

        assert!(store.delete_session("s1").expect("delete"));
        assert!(store.list_messages("s1").expect("list").is_empty());
        // The other session is untouched.
        assert_eq!(store.list_messages("s2").expect("list").len(), 1);
    }

    #[test]
    fn append_to_missing_session_fails() {
        let db = open_test_db();
        let store = db.sessions();
        assert!(store.append_message("ghost", "user", "hi").is_err());
    }

    #[test]
    fn touch_session_creates_then_bumps() {
        let db = open_test_db();
        let store = db.sessions();

        let first = store.touch_session("s1", "First").expect("touch create");
        assert_eq!(first.id, "s1");
        assert_eq!(first.title, "First");
        assert_eq!(first.created_at, first.updated_at);

        // Second touch must not fail and must not change the title.
        let second = store.touch_session("s1", "Other").expect("touch again");
        assert_eq!(second.id, "s1");
        assert_eq!(second.title, "First");
        assert!(second.updated_at >= first.updated_at);

        // The session shows up in the listing.
        let ids: Vec<String> = store
            .list_sessions()
            .expect("list")
            .into_iter()
            .map(|s| s.id)
            .collect();
        assert_eq!(ids, vec!["s1".to_string()]);
    }

    #[test]
    fn duplicate_session_id_fails() {
        let db = open_test_db();
        let store = db.sessions();
        store.create_session("s1", "t").expect("create");
        assert!(store.create_session("s1", "t").is_err());
    }
}
