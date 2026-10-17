//! SQLite persistence layer for the Boom desktop core.
//!
//! A single [`Storage`] handle owns one SQLite connection guarded by a
//! [`Mutex`]. The sub-stores ([`SessionStore`], [`ProviderStore`],
//! [`BotStore`], [`KvStore`]) borrow that mutex and lock it once per
//! operation.
//!
//! The database file lives at `~/.boom/boom.db`. The `~/.boom` directory is
//! created (or tightened) with mode `0700` on Unix, and the database file
//! itself with mode `0600`, so API keys and chat history are never
//! world-readable.
//!
//! Migrations are intentionally simple: [`Storage::open`] runs `schema.sql`
//! (`CREATE TABLE IF NOT EXISTS` ...) on every open, so schema changes stay
//! additive and old databases keep working without a version table.

mod bots;
mod kv;
mod providers;
pub mod server_storage;
mod sessions;

pub use bots::{Bot, BotPatch, BotStore, NewBot};
pub use kv::KvStore;
pub use providers::{ProviderConfig, ProviderStore};
pub use server_storage::SqliteStorage;
pub use sessions::{Message, Session, SessionStore};

use rusqlite::Connection;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

/// Embedded schema, applied with `CREATE TABLE IF NOT EXISTS` on every open.
const SCHEMA_SQL: &str = include_str!("schema.sql");

/// Errors produced by the storage layer.
#[derive(Debug)]
pub enum StorageError {
    /// Underlying SQLite error.
    Sqlite(rusqlite::Error),
    /// JSON (de)serialization error for provider configs / kv JSON values.
    Json(serde_json::Error),
    /// Filesystem error (creating `~/.boom`, opening the DB file, ...).
    Io(io::Error),
    /// No home directory could be determined.
    HomeDirNotFound,
}

impl std::fmt::Display for StorageError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StorageError::Sqlite(e) => write!(f, "sqlite error: {e}"),
            StorageError::Json(e) => write!(f, "json error: {e}"),
            StorageError::Io(e) => write!(f, "io error: {e}"),
            StorageError::HomeDirNotFound => write!(f, "home directory not found"),
        }
    }
}

impl std::error::Error for StorageError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            StorageError::Sqlite(e) => Some(e),
            StorageError::Json(e) => Some(e),
            StorageError::Io(e) => Some(e),
            StorageError::HomeDirNotFound => None,
        }
    }
}

impl From<rusqlite::Error> for StorageError {
    fn from(e: rusqlite::Error) -> Self {
        StorageError::Sqlite(e)
    }
}

impl From<serde_json::Error> for StorageError {
    fn from(e: serde_json::Error) -> Self {
        StorageError::Json(e)
    }
}

impl From<io::Error> for StorageError {
    fn from(e: io::Error) -> Self {
        StorageError::Io(e)
    }
}

/// Result alias used across the storage layer.
pub type Result<T> = std::result::Result<T, StorageError>;

/// Current time as Unix epoch seconds.
pub(crate) fn now_unix() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// A thread-safe handle to the Boom SQLite database.
///
/// The single inner connection is serialized with a [`Mutex`]. SQLite
/// connections are `!Sync`, so sharing one connection behind a mutex is the
/// simplest correct way to serve multiple threads from an embedded database.
/// A pool (e.g. r2d2) would only add lock contention on the same file plus
/// another dependency, with no throughput gain for a local desktop DB.
/// WAL mode is enabled so readers never block writers.
pub struct Storage {
    conn: Mutex<Connection>,
}

impl Storage {
    /// Opens (creating if needed) the database at `path` and applies the schema.
    ///
    /// Parent directories are created with owner-only permissions on Unix.
    pub fn open(path: &Path) -> Result<Self> {
        if let Some(parent) = path.parent() {
            ensure_private_dir(parent)?;
        }
        let conn = Connection::open(path)?;
        // WAL: readers don't block writers; durable enough with synchronous=NORMAL
        // for a desktop app, and much faster than the default rollback journal.
        conn.execute_batch("PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL;")?;
        // Enforce foreign keys (e.g. messages.session_id -> sessions.id).
        conn.execute_batch("PRAGMA foreign_keys = ON;")?;
        // Additive migrations: safe to run on every open, on old DBs too.
        conn.execute_batch(SCHEMA_SQL)?;

        #[cfg(unix)]
        {
            // Belt and suspenders on top of the 0700 directory: the DB file
            // itself is owner-only as well.
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
        }

        Ok(Storage {
            conn: Mutex::new(conn),
        })
    }

    /// Resolves `$BOOM_HOME/boom.db` (or `~/.boom/boom.db` when unset),
    /// creating the dir with mode 0700 on Unix.
    pub fn default_path() -> Result<PathBuf> {
        let dir = std::env::var("BOOM_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|_| {
                dirs::home_dir()
                    .map(|h| h.join(".boom"))
                    .unwrap_or_else(|| PathBuf::from(".boom"))
            });
        ensure_private_dir(&dir)?;
        Ok(dir.join("boom.db"))
    }

    /// Opens the default database at `~/.boom/boom.db`.
    pub fn open_default() -> Result<Self> {
        let path = Self::default_path()?;
        Self::open(path.as_path())
    }

    /// Chat sessions + messages store.
    pub fn sessions(&self) -> SessionStore<'_> {
        SessionStore::new(&self.conn)
    }

    /// Provider (API key) configs store.
    pub fn providers(&self) -> ProviderStore<'_> {
        ProviderStore::new(&self.conn)
    }

    /// Custom bots store.
    pub fn bots(&self) -> BotStore<'_> {
        BotStore::new(&self.conn)
    }

    /// Generic key-value store (marketplace installs, group policy/mute, settings).
    pub fn kv(&self) -> KvStore<'_> {
        KvStore::new(&self.conn)
    }
}

/// Creates `dir` (and parents) and restricts it to owner-only access.
///
/// On Unix the directory is created with mode `0700` — the requested
/// `0600`-file equivalent applied at the directory level, so the DB file,
/// its WAL/SHM sidecars and any sibling secret files inherit the
/// protection. If the directory already exists, its mode is tightened to
/// `0700` as well.
fn ensure_private_dir(dir: &Path) -> Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
        let mut builder = std::fs::DirBuilder::new();
        builder.recursive(true).mode(0o700);
        builder.create(dir)?;
        std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))?;
    }
    #[cfg(not(unix))]
    {
        std::fs::create_dir_all(dir)?;
    }
    Ok(())
}

#[cfg(test)]
pub(crate) fn open_test_db() -> Storage {
    // In-memory databases are private to their connection and need no
    // cleanup, which keeps the unit tests hermetic and fast.
    let conn = Connection::open_in_memory().expect("open in-memory db");
    conn.execute_batch("PRAGMA foreign_keys = ON;")
        .expect("enable foreign keys");
    conn.execute_batch(SCHEMA_SQL).expect("apply schema");
    Storage {
        conn: Mutex::new(conn),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_path_points_at_boom_db() {
        let path = Storage::default_path().expect("default path");
        assert!(path.ends_with(".boom/boom.db") || path.to_string_lossy().ends_with(".boom/boom.db"));
    }

    #[test]
    fn open_creates_missing_parent_dirs() {
        let dir = tempfile::tempdir().expect("tempdir");
        let db_path = dir.path().join("nested").join("boom.db");
        let storage = Storage::open(&db_path).expect("open");
        assert!(db_path.exists());
        // Schema was applied: the tables exist and are usable.
        storage.kv().set("k", "v").expect("kv set");
        assert_eq!(storage.kv().get("k").expect("kv get").as_deref(), Some("v"));
    }

    #[test]
    #[cfg(unix)]
    fn open_restricts_permissions() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().expect("tempdir");
        let boom = dir.path().join(".boom");
        let db_path = boom.join("boom.db");
        let _ = Storage::open(&db_path).expect("open");

        let dir_mode = std::fs::metadata(&boom).expect("dir metadata").permissions().mode();
        assert_eq!(dir_mode & 0o777, 0o700, "directory must be owner-only");

        let db_mode = std::fs::metadata(&db_path)
            .expect("db metadata")
            .permissions()
            .mode();
        assert_eq!(db_mode & 0o777, 0o600, "db file must be owner-only");
    }

    #[test]
    fn open_is_idempotent_on_existing_db() {
        // Applying the schema twice (e.g. after an upgrade) must not fail
        // and must preserve existing data.
        let dir = tempfile::tempdir().expect("tempdir");
        let db_path = dir.path().join("boom.db");
        {
            let storage = Storage::open(&db_path).expect("first open");
            storage.kv().set("k", "v").expect("kv set");
        }
        {
            let storage = Storage::open(&db_path).expect("second open");
            assert_eq!(storage.kv().get("k").expect("kv get").as_deref(), Some("v"));
        }
    }

    #[test]
    fn concurrent_access_is_serialized() {
        use std::sync::Arc;
        use std::thread;

        let storage = Arc::new(open_test_db());
        let mut handles = Vec::new();
        for i in 0..8 {
            let s = Arc::clone(&storage);
            handles.push(thread::spawn(move || {
                for j in 0..25 {
                    s.kv().set(&format!("t{i}.k{j}"), "v").expect("set");
                    s.sessions()
                        .create_session(&format!("s{i}-{j}"), "t")
                        .expect("create session");
                }
            }));
        }
        for h in handles {
            h.join().expect("thread");
        }
        assert_eq!(storage.kv().list_prefix("t").expect("list").len(), 200);
        assert_eq!(storage.sessions().list_sessions().expect("sessions").len(), 200);
    }
}
