//! SQLite-backed implementation of the server's [`Storage`](crate::server::state::Storage) trait.
//!
//! [`SqliteStorage`] wraps a [`super::Storage`] handle (one `Mutex<Connection>`,
//! already `Send + Sync`) and maps the trait's bots/groups/marketplace calls
//! onto the existing schema:
//!
//! - **bots**: the `bots` table keeps `id`/`name`/`system_prompt`/`active`
//!   (with `set_active` enforcing the exactly-one-active invariant in a
//!   transaction); the richer API fields (`description`, `avatar_emoji`,
//!   `allowed_tools`, `denied_tools`, `channel_bindings`, `created_at`,
//!   `updated_at`) live as a JSON blob in `kv` under `bots.meta.<id>`.
//!   Additive, no schema migration needed — rows written before this existed
//!   simply get default meta values.
//! - **groups**: group rows live in `kv` as JSON blobs under
//!   `groups.registered.<channel>/<group_id>` (the message pipeline registers
//!   them via [`SqliteStorage::register_group`]; the API never invents groups).
//! - **marketplace**: installed plugins live in `kv` as JSON blobs under
//!   `marketplace.installed.<name>`; disabled builtin tools live as a JSON
//!   array under `marketplace.disabled_tools`.
//!
//! Patch/validation semantics mirror [`crate::server::defaults::MemoryStorage`]
//! exactly (same error strings, same field rules), so swapping the backend
//! does not change the JSON API contract.

use std::collections::BTreeSet;
use std::sync::atomic::{AtomicU64, Ordering};

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use super::{Result as StoreResult, Storage as DbStorage, StorageError as DbError};
use crate::server::state::{
    normalize_str_list, now_epoch, now_iso, Bot as ApiBot, Group as ApiGroup,
    NewBot as ApiNewBot, PluginMeta as ApiPluginMeta, Storage as ApiStorage,
    StorageError as ApiError,
};

/// kv key prefix for per-bot metadata JSON.
const BOT_META_PREFIX: &str = "bots.meta.";
/// kv key prefix for registered group JSON blobs.
const GROUP_PREFIX: &str = "groups.registered.";
/// kv key prefix for installed plugin JSON blobs.
const PLUGIN_PREFIX: &str = "marketplace.installed.";
/// kv key holding the JSON array of disabled builtin tool names.
const DISABLED_TOOLS_KEY: &str = "marketplace.disabled_tools";

/// The API-facing bot fields that do not fit the `bots` table, kept as one
/// JSON blob in `kv`. Timestamps use the same `YYYY-MM-DDTHH:MM:SS` shape as
/// the rest of the API surface.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct BotMeta {
    #[serde(default)]
    description: String,
    #[serde(default = "default_avatar")]
    avatar_emoji: String,
    #[serde(default)]
    allowed_tools: Vec<String>,
    #[serde(default)]
    denied_tools: Vec<String>,
    #[serde(default)]
    channel_bindings: Vec<String>,
    #[serde(default)]
    created_at: String,
    #[serde(default)]
    updated_at: String,
}

fn default_avatar() -> String {
    "🤖".to_string()
}

fn api_error(e: DbError) -> ApiError {
    ApiError::Internal(e.to_string())
}

fn bot_meta_key(id: &str) -> String {
    format!("{BOT_META_PREFIX}{id}")
}

fn group_key(channel: &str, group_id: &str) -> String {
    format!("{GROUP_PREFIX}{channel}/{group_id}")
}

fn plugin_key(name: &str) -> String {
    format!("{PLUGIN_PREFIX}{name}")
}

/// SQLite-backed [`ApiStorage`].
///
/// `Send + Sync`: the inner [`DbStorage`] serializes its single connection
/// with a `Mutex`, and the id counter is an [`AtomicU64`].
pub struct SqliteStorage {
    db: DbStorage,
    next_bot: AtomicU64,
}

impl SqliteStorage {
    /// Wrap an already-open database handle.
    pub fn new(db: DbStorage) -> Self {
        Self {
            db,
            // Seed from the clock so ids stay unique across restarts even
            // when two processes create bots within the same second (the
            // process id is part of the id as well).
            next_bot: AtomicU64::new(
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.subsec_nanos() as u64)
                    .unwrap_or(0),
            ),
        }
    }

    /// Open `~/.boom/boom.db` (creating it, mode 0600) and wrap it.
    pub fn open_default() -> StoreResult<Self> {
        Ok(Self::new(DbStorage::open_default()?))
    }

    /// Borrow the underlying database handle (e.g. for session/provider
    /// stores that share the same file).
    pub fn inner(&self) -> &DbStorage {
        &self.db
    }

    /// Register (or refresh) a group row. The message pipeline calls this;
    /// the HTTP API never invents groups, mirroring the Python behaviour.
    pub fn register_group(&self, group: &ApiGroup) -> StoreResult<()> {
        self.db
            .kv()
            .set_json(&group_key(&group.channel, &group.group_id), group)
    }

    fn bot_meta(&self, id: &str) -> StoreResult<BotMeta> {
        let meta: Option<BotMeta> = self.db.kv().get_json(&bot_meta_key(id))?;
        Ok(meta.unwrap_or_default())
    }

    fn save_bot_meta(&self, id: &str, meta: &BotMeta) -> StoreResult<()> {
        self.db.kv().set_json(&bot_meta_key(id), meta)
    }

    /// Combine one `bots` table row with its kv metadata blob into the
    /// API-facing [`ApiBot`].
    fn to_api_bot(&self, row: super::Bot) -> StoreResult<ApiBot> {
        let meta = self.bot_meta(&row.id)?;
        Ok(ApiBot {
            id: row.id,
            name: row.name,
            description: meta.description,
            system_prompt: row.system_prompt,
            avatar_emoji: meta.avatar_emoji,
            allowed_tools: meta.allowed_tools,
            denied_tools: meta.denied_tools,
            channel_bindings: meta.channel_bindings,
            created_at: meta.created_at,
            updated_at: meta.updated_at,
            is_active: row.active,
        })
    }

    fn get_group_row(&self, channel: &str, group_id: &str) -> StoreResult<Option<ApiGroup>> {
        let group: Option<ApiGroup> = self.db.kv().get_json(&group_key(channel, group_id))?;
        Ok(group)
    }

    fn next_bot_id(&self) -> String {
        let n = self.next_bot.fetch_add(1, Ordering::Relaxed);
        format!("bot_{}_{}_{}", now_epoch(), std::process::id(), n)
    }
}

impl ApiStorage for SqliteStorage {
    // -- bots --

    fn list_bots(&self) -> Vec<ApiBot> {
        let rows = match self.db.bots().list() {
            Ok(rows) => rows,
            Err(_) => return Vec::new(),
        };
        let mut bots: Vec<ApiBot> = rows
            .into_iter()
            .filter_map(|row| self.to_api_bot(row).ok())
            .collect();
        // Active bot first, then by name — mirrors MemoryStorage.
        bots.sort_by(|a, b| {
            b.is_active
                .cmp(&a.is_active)
                .then_with(|| a.name.cmp(&b.name))
                .then_with(|| a.id.cmp(&b.id))
        });
        bots
    }

    fn create_bot(&self, input: ApiNewBot) -> Result<ApiBot, ApiError> {
        if input.name.trim().is_empty() {
            return Err(ApiError::Invalid("name is required".to_string()));
        }
        let id = self.next_bot_id();
        let now = now_iso();
        let name = input.name.trim().to_string();
        let row = self
            .db
            .bots()
            .create(&super::NewBot {
                id: id.clone(),
                name: name.clone(),
                system_prompt: input.system_prompt.clone(),
                model: String::new(),
                provider: String::new(),
            })
            .map_err(api_error)?;
        let meta = BotMeta {
            description: input.description,
            avatar_emoji: if input.avatar_emoji.is_empty() {
                default_avatar()
            } else {
                input.avatar_emoji
            },
            allowed_tools: input.allowed_tools.unwrap_or_default(),
            denied_tools: input.denied_tools.unwrap_or_default(),
            channel_bindings: input.channel_bindings.unwrap_or_default(),
            created_at: now.clone(),
            updated_at: now,
        };
        self.save_bot_meta(&id, &meta).map_err(api_error)?;
        self.to_api_bot(row).map_err(api_error)
    }

    fn get_bot(&self, id: &str) -> Option<ApiBot> {
        let row = self.db.bots().get(id).ok()??;
        self.to_api_bot(row).ok()
    }

    fn update_bot(&self, id: &str, patch: &Map<String, Value>) -> Result<ApiBot, ApiError> {
        if id.trim().is_empty() {
            return Err(ApiError::Invalid(
                "bot_id must be a non-empty string".to_string(),
            ));
        }
        let row = self
            .db
            .bots()
            .get(id)
            .map_err(api_error)?
            .ok_or_else(|| ApiError::NotFound(format!("bot not found: '{id}'")))?;

        // Split the patch into table fields (name/system_prompt) and kv
        // meta fields, with the exact validation rules MemoryStorage uses.
        let mut table_patch = super::BotPatch::default();
        let mut meta = self.bot_meta(id).map_err(api_error)?;
        let mut touched = false;

        for (key, value) in patch {
            match key.as_str() {
                "name" => {
                    let name = value.as_str().ok_or_else(|| {
                        ApiError::Invalid("name must be a non-empty string".to_string())
                    })?;
                    if name.trim().is_empty() {
                        return Err(ApiError::Invalid(
                            "name must be a non-empty string".to_string(),
                        ));
                    }
                    table_patch.name = Some(name.trim().to_string());
                    touched = true;
                }
                "description" | "system_prompt" => {
                    let s = value.as_str().ok_or_else(|| {
                        ApiError::Invalid(format!("{key} must be a string"))
                    })?;
                    if key == "description" {
                        meta.description = s.to_string();
                    } else {
                        table_patch.system_prompt = Some(s.to_string());
                    }
                    touched = true;
                }
                "avatar_emoji" => {
                    let s = value.as_str().ok_or_else(|| {
                        ApiError::Invalid("avatar_emoji must be a string".to_string())
                    })?;
                    meta.avatar_emoji = if s.is_empty() {
                        default_avatar()
                    } else {
                        s.to_string()
                    };
                    touched = true;
                }
                "allowed_tools" | "denied_tools" | "channel_bindings" => {
                    let list = normalize_str_list(value, key).map_err(ApiError::Invalid)?;
                    match key.as_str() {
                        "allowed_tools" => meta.allowed_tools = list,
                        "denied_tools" => meta.denied_tools = list,
                        _ => meta.channel_bindings = list,
                    }
                    touched = true;
                }
                // Unknown fields are silently ignored, like Python/MemoryStorage.
                _ => {}
            }
        }
        if !touched {
            return Err(ApiError::Invalid("no updatable fields given".to_string()));
        }

        self.db.bots().update(id, &table_patch).map_err(api_error)?;
        meta.updated_at = now_iso();
        self.save_bot_meta(id, &meta).map_err(api_error)?;

        let row = self
            .db
            .bots()
            .get(id)
            .map_err(api_error)?
            .unwrap_or(row);
        self.to_api_bot(row).map_err(api_error)
    }

    fn delete_bot(&self, id: &str) -> bool {
        if id.trim().is_empty() {
            return false;
        }
        let deleted = self.db.bots().delete(id).unwrap_or(false);
        if deleted {
            // Best-effort cleanup of the metadata blob.
            let _ = self.db.kv().delete(&bot_meta_key(id));
        }
        deleted
    }

    fn set_active_bot(&self, id: &str) -> bool {
        let id = id.trim();
        if id.is_empty() {
            return false;
        }
        self.db.bots().set_active(id).unwrap_or(false)
    }

    fn active_bot(&self) -> Option<ApiBot> {
        let row = self.db.bots().active().ok()??;
        self.to_api_bot(row).ok()
    }

    // -- groups --

    fn list_groups(&self) -> Vec<ApiGroup> {
        let pairs = match self.db.kv().list_prefix(GROUP_PREFIX) {
            Ok(pairs) => pairs,
            Err(_) => return Vec::new(),
        };
        let mut groups: Vec<ApiGroup> = pairs
            .into_iter()
            .filter_map(|(_, raw)| serde_json::from_str(&raw).ok())
            .collect();
        groups.sort_by(|a, b| {
            a.channel
                .cmp(&b.channel)
                .then_with(|| a.group_id.cmp(&b.group_id))
        });
        groups
    }

    fn get_group(&self, channel: &str, group_id: &str) -> Option<ApiGroup> {
        self.get_group_row(channel, group_id).ok()?
    }

    fn set_group_muted(
        &self,
        channel: &str,
        group_id: &str,
        muted: bool,
    ) -> Result<(), ApiError> {
        let mut group = self
            .get_group_row(channel, group_id)
            .map_err(api_error)?
            .ok_or_else(|| {
                ApiError::NotFound(format!("group not registered: {channel}/{group_id}"))
            })?;
        let mut settings = group.settings.as_object().cloned().unwrap_or_default();
        settings.insert("muted".to_string(), Value::Bool(muted));
        group.settings = Value::Object(settings);
        self.db
            .kv()
            .set_json(&group_key(channel, group_id), &group)
            .map_err(api_error)?;
        Ok(())
    }

    fn set_group_policy(
        &self,
        channel: &str,
        group_id: &str,
        policy: &Value,
    ) -> Result<Value, ApiError> {
        // Must be JSON-serializable (it always is as serde_json::Value).
        serde_json::to_string(policy)
            .map_err(|e| ApiError::Invalid(format!("policy is not JSON-serializable: {e}")))?;
        let mut group = self
            .get_group_row(channel, group_id)
            .map_err(api_error)?
            .ok_or_else(|| {
                ApiError::NotFound(format!(
                    "group not registered: channel='{channel}' group_id='{group_id}'"
                ))
            })?;
        group.policy = policy.clone();
        self.db
            .kv()
            .set_json(&group_key(channel, group_id), &group)
            .map_err(api_error)?;
        Ok(policy.clone())
    }

    // -- marketplace --

    fn plugins(&self) -> Vec<ApiPluginMeta> {
        let pairs = match self.db.kv().list_prefix(PLUGIN_PREFIX) {
            Ok(pairs) => pairs,
            Err(_) => return Vec::new(),
        };
        let mut plugins: Vec<ApiPluginMeta> = pairs
            .into_iter()
            .filter_map(|(_, raw)| serde_json::from_str(&raw).ok())
            .collect();
        plugins.sort_by(|a, b| a.name.cmp(&b.name));
        plugins
    }

    fn enable_plugin(&self, name: &str) -> Result<(), ApiError> {
        let key = plugin_key(name);
        let mut plugin: ApiPluginMeta = self
            .db
            .kv()
            .get_json(&key)
            .map_err(api_error)?
            .ok_or_else(|| ApiError::NotFound(format!("plugin '{name}' not found")))?;
        plugin.enabled = true;
        self.db.kv().set_json(&key, &plugin).map_err(api_error)?;
        Ok(())
    }

    fn install_plugin(&self, meta: &ApiPluginMeta) -> Result<(), ApiError> {
        self.db
            .kv()
            .set_json(&plugin_key(&meta.name), meta)
            .map_err(api_error)
    }

    fn uninstall_plugin(&self, name: &str) -> bool {
        self.db.kv().delete(&plugin_key(name)).unwrap_or(false)
    }

    fn disabled_tools(&self) -> BTreeSet<String> {
        let names: Option<Vec<String>> = self.db.kv().get_json(DISABLED_TOOLS_KEY).unwrap_or(None);
        names.unwrap_or_default().into_iter().collect()
    }

    fn set_tool_disabled(&self, name: &str, disabled: bool) {
        let mut set = self.disabled_tools();
        if disabled {
            set.insert(name.to_string());
        } else {
            set.remove(name);
        }
        let names: Vec<String> = set.into_iter().collect();
        let _ = self.db.kv().set_json(DISABLED_TOOLS_KEY, &names);
    }
}

#[cfg(test)]
mod tests {
    use super::super::open_test_db;
    use super::*;
    use crate::server::state::NewBot as ApiNewBot;

    fn test_storage() -> SqliteStorage {
        SqliteStorage::new(open_test_db())
    }

    fn new_bot_input(name: &str) -> ApiNewBot {
        ApiNewBot {
            name: name.to_string(),
            description: "test bot".to_string(),
            system_prompt: "You are helpful.".to_string(),
            avatar_emoji: "🧪".to_string(),
            allowed_tools: Some(vec!["shell_run".to_string()]),
            denied_tools: None,
            channel_bindings: Some(vec!["telegram".to_string()]),
        }
    }

    fn test_group(channel: &str, group_id: &str) -> ApiGroup {
        ApiGroup {
            channel: channel.to_string(),
            group_id: group_id.to_string(),
            name: "Test Group".to_string(),
            settings: serde_json::json!({}),
            message_count: 7,
            member_count: 3,
            policy: serde_json::json!({}),
            created_at: now_iso(),
            last_active: now_iso(),
        }
    }

    fn test_plugin(name: &str) -> ApiPluginMeta {
        ApiPluginMeta {
            name: name.to_string(),
            description: format!("{name} plugin"),
            icon: None,
            version: "1.0.0".to_string(),
            author: "tester".to_string(),
            enabled: false,
        }
    }

    #[test]
    fn storage_is_send_and_sync() {
        fn assert_send_sync<T: Send + Sync>() {}
        assert_send_sync::<SqliteStorage>();
    }

    // -- bots --

    #[test]
    fn bot_crud_round_trip() {
        let s = test_storage();

        let created = s.create_bot(new_bot_input("Helper")).expect("create");
        assert!(!created.is_active);
        assert_eq!(created.name, "Helper");
        assert_eq!(created.description, "test bot");
        assert_eq!(created.system_prompt, "You are helpful.");
        assert_eq!(created.avatar_emoji, "🧪");
        assert_eq!(created.allowed_tools, vec!["shell_run".to_string()]);
        assert!(created.denied_tools.is_empty());
        assert_eq!(created.channel_bindings, vec!["telegram".to_string()]);
        assert!(!created.created_at.is_empty());
        assert!(!created.updated_at.is_empty());

        let fetched = s.get_bot(&created.id).expect("get");
        assert_eq!(fetched.id, created.id);
        assert_eq!(fetched.name, "Helper");
        assert!(s.get_bot("nope").is_none());

        let list = s.list_bots();
        assert_eq!(list.len(), 1);

        // Update a mix of table fields and meta fields.
        let mut patch = Map::new();
        patch.insert("name".to_string(), Value::String("Super Helper".to_string()));
        patch.insert("avatar_emoji".to_string(), Value::String(String::new())); // -> default
        patch.insert(
            "denied_tools".to_string(),
            serde_json::json!(["dangerous_tool"]),
        );
        patch.insert("bogus".to_string(), Value::String("ignored".to_string()));
        let updated = s.update_bot(&created.id, &patch).expect("update");
        assert_eq!(updated.name, "Super Helper");
        assert_eq!(updated.avatar_emoji, "🤖");
        assert_eq!(updated.denied_tools, vec!["dangerous_tool".to_string()]);
        // Untouched fields keep their values.
        assert_eq!(updated.system_prompt, "You are helpful.");
        assert_eq!(updated.description, "test bot");
        assert_eq!(updated.allowed_tools, vec!["shell_run".to_string()]);

        assert!(s.set_active_bot(&created.id));
        assert_eq!(s.active_bot().expect("active").id, created.id);
        // Active bot sorts first.
        let second = s.create_bot(new_bot_input("Zed")).expect("create");
        let names: Vec<String> = s.list_bots().into_iter().map(|b| b.name).collect();
        assert_eq!(names, vec!["Super Helper".to_string(), "Zed".to_string()]);

        assert!(!s.set_active_bot("ghost"));
        assert!(!s.set_active_bot("   "));
        assert!(s.delete_bot(&second.id));
        assert!(s.get_bot(&second.id).is_none());
        assert!(!s.delete_bot(&second.id));
        assert!(!s.delete_bot(""));
    }

    #[test]
    fn bot_validation_errors_match_memory_storage() {
        let s = test_storage();

        // Empty name on create.
        let bad = new_bot_input("  ");
        match s.create_bot(bad) {
            Err(ApiError::Invalid(msg)) => assert_eq!(msg, "name is required"),
            other => panic!("expected Invalid, got {other:?}"),
        }

        // Empty id on update.
        let empty_patch = Map::new();
        match s.update_bot(" ", &empty_patch) {
            Err(ApiError::Invalid(msg)) => {
                assert_eq!(msg, "bot_id must be a non-empty string")
            }
            other => panic!("expected Invalid, got {other:?}"),
        }

        // Unknown id.
        let created = s.create_bot(new_bot_input("One")).expect("create");
        let mut patch = Map::new();
        patch.insert("name".to_string(), Value::String("X".to_string()));
        match s.update_bot("ghost", &patch) {
            Err(ApiError::NotFound(_)) => {}
            other => panic!("expected NotFound, got {other:?}"),
        }

        // No updatable fields (unknown keys only).
        let mut unknown_only = Map::new();
        unknown_only.insert("nope".to_string(), Value::Bool(true));
        match s.update_bot(&created.id, &unknown_only) {
            Err(ApiError::Invalid(msg)) => assert_eq!(msg, "no updatable fields given"),
            other => panic!("expected Invalid, got {other:?}"),
        }

        // Empty patch map.
        match s.update_bot(&created.id, &Map::new()) {
            Err(ApiError::Invalid(msg)) => assert_eq!(msg, "no updatable fields given"),
            other => panic!("expected Invalid, got {other:?}"),
        }

        // Bad field types.
        let mut bad_name = Map::new();
        bad_name.insert("name".to_string(), Value::Number(1.into()));
        assert!(matches!(
            s.update_bot(&created.id, &bad_name),
            Err(ApiError::Invalid(_))
        ));
        let mut bad_list = Map::new();
        bad_list.insert("allowed_tools".to_string(), Value::String("x".to_string()));
        assert!(matches!(
            s.update_bot(&created.id, &bad_list),
            Err(ApiError::Invalid(_))
        ));
    }

    #[test]
    fn bots_survive_reopen() {
        let dir = tempfile::tempdir().expect("tempdir");
        let db_path = dir.path().join("boom.db");
        let id = {
            let s = SqliteStorage::new(DbStorage::open(&db_path).expect("open"));
            let bot = s.create_bot(new_bot_input("Persistent")).expect("create");
            assert!(s.set_active_bot(&bot.id));
            bot.id
        };
        // New handle on the same file: everything is still there.
        let s2 = SqliteStorage::new(DbStorage::open(&db_path).expect("reopen"));
        let fetched = s2.get_bot(&id).expect("get after reopen");
        assert_eq!(fetched.name, "Persistent");
        assert_eq!(fetched.description, "test bot");
        assert!(fetched.is_active);
        assert_eq!(s2.active_bot().expect("active").id, id);
        assert_eq!(s2.list_bots().len(), 1);
    }

    #[test]
    fn concurrent_bot_creation_has_unique_ids() {
        use std::sync::Arc;
        use std::thread;

        let s = Arc::new(test_storage());
        let handles: Vec<_> = (0..8)
            .map(|i| {
                let s = Arc::clone(&s);
                thread::spawn(move || {
                    for _ in 0..10 {
                        let bot = s
                            .create_bot(new_bot_input(&format!("t{i}")))
                            .expect("create");
                        assert!(!bot.id.is_empty());
                    }
                })
            })
            .collect();
        for h in handles {
            h.join().expect("thread");
        }
        let bots = s.list_bots();
        assert_eq!(bots.len(), 80);
        let mut ids: Vec<String> = bots.into_iter().map(|b| b.id).collect();
        ids.sort();
        ids.dedup();
        assert_eq!(ids.len(), 80, "bot ids must be unique");
    }

    // -- groups --

    #[test]
    fn group_mute_and_policy() {
        let s = test_storage();
        assert!(s.list_groups().is_empty());
        assert!(s.get_group("telegram", "g1").is_none());

        s.register_group(&test_group("telegram", "g1")).expect("register");
        s.register_group(&test_group("discord", "g2")).expect("register");
        let groups = s.list_groups();
        assert_eq!(groups.len(), 2);
        // Ordered by (channel, group_id).
        assert_eq!(groups[0].channel, "discord");
        assert_eq!(groups[1].channel, "telegram");
        let g1 = s.get_group("telegram", "g1").expect("get");
        assert_eq!(g1.name, "Test Group");
        assert_eq!(g1.message_count, 7);

        s.set_group_muted("telegram", "g1", true).expect("mute");
        let muted = s.get_group("telegram", "g1").expect("get");
        assert_eq!(muted.settings.get("muted"), Some(&Value::Bool(true)));
        s.set_group_muted("telegram", "g1", false).expect("unmute");
        let unmuted = s.get_group("telegram", "g1").expect("get");
        assert_eq!(unmuted.settings.get("muted"), Some(&Value::Bool(false)));

        let policy = serde_json::json!({"allow": ["chat"], "max_messages": 100});
        let back = s
            .set_group_policy("telegram", "g1", &policy)
            .expect("policy");
        assert_eq!(back, policy);
        assert_eq!(s.get_group("telegram", "g1").expect("get").policy, policy);

        // Unknown groups.
        assert!(matches!(
            s.set_group_muted("telegram", "ghost", true),
            Err(ApiError::NotFound(_))
        ));
        assert!(matches!(
            s.set_group_policy("telegram", "ghost", &policy),
            Err(ApiError::NotFound(_))
        ));
    }

    #[test]
    fn groups_survive_reopen() {
        let dir = tempfile::tempdir().expect("tempdir");
        let db_path = dir.path().join("boom.db");
        {
            let s = SqliteStorage::new(DbStorage::open(&db_path).expect("open"));
            s.register_group(&test_group("telegram", "g1")).expect("register");
            s.set_group_muted("telegram", "g1", true).expect("mute");
        }
        let s2 = SqliteStorage::new(DbStorage::open(&db_path).expect("reopen"));
        let g = s2.get_group("telegram", "g1").expect("get after reopen");
        assert_eq!(g.message_count, 7);
        assert_eq!(g.settings.get("muted"), Some(&Value::Bool(true)));
    }

    // -- marketplace --

    #[test]
    fn plugin_enable_and_uninstall() {
        let s = test_storage();
        assert!(s.plugins().is_empty());

        s.install_plugin(&test_plugin("alpha")).expect("install");
        s.install_plugin(&test_plugin("beta")).expect("install");
        let names: Vec<String> =
            s.plugins().into_iter().map(|p| p.name).collect();
        assert_eq!(names, vec!["alpha".to_string(), "beta".to_string()]);

        // Enabling an unknown plugin is a 404.
        assert!(matches!(
            s.enable_plugin("ghost"),
            Err(ApiError::NotFound(_))
        ));

        s.enable_plugin("alpha").expect("enable");
        let alpha = s.plugins().into_iter().find(|p| p.name == "alpha").expect("alpha");
        assert!(alpha.enabled);
        assert_eq!(alpha.version, "1.0.0");
        assert_eq!(alpha.author, "tester");

        assert!(s.uninstall_plugin("beta"));
        assert_eq!(s.plugins().len(), 1);
        assert!(!s.uninstall_plugin("beta"));
    }

    #[test]
    fn disabled_tools_round_trip() {
        let s = test_storage();
        assert!(s.disabled_tools().is_empty());

        s.set_tool_disabled("shell_run", true);
        s.set_tool_disabled("web_search", true);
        let disabled = s.disabled_tools();
        assert!(disabled.contains("shell_run"));
        assert!(disabled.contains("web_search"));

        s.set_tool_disabled("shell_run", false);
        let disabled = s.disabled_tools();
        assert!(!disabled.contains("shell_run"));
        assert!(disabled.contains("web_search"));

        // Disabling an already-disabled tool is idempotent.
        s.set_tool_disabled("web_search", true);
        assert_eq!(s.disabled_tools().len(), 1);
    }

    #[test]
    fn marketplace_state_survives_reopen() {
        let dir = tempfile::tempdir().expect("tempdir");
        let db_path = dir.path().join("boom.db");
        {
            let s = SqliteStorage::new(DbStorage::open(&db_path).expect("open"));
            s.install_plugin(&test_plugin("alpha")).expect("install");
            s.enable_plugin("alpha").expect("enable");
            s.set_tool_disabled("shell_run", true);
        }
        let s2 = SqliteStorage::new(DbStorage::open(&db_path).expect("reopen"));
        let plugins = s2.plugins();
        assert_eq!(plugins.len(), 1);
        assert!(plugins[0].enabled);
        assert!(s2.disabled_tools().contains("shell_run"));
    }
}
