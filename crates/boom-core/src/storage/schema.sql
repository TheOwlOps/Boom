-- Boom desktop core — SQLite schema.
--
-- Applied with CREATE TABLE IF NOT EXISTS on every Storage::open, so
-- migrations are additive and opening an older database never fails.
-- All timestamps are Unix epoch seconds (i64).

-- Chat sessions (conversations).
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- Messages belonging to a session, in insertion order.
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

-- Provider configurations (API keys, model, base URL) as JSON blobs.
CREATE TABLE IF NOT EXISTS providers (
    name        TEXT PRIMARY KEY,
    config_json TEXT NOT NULL DEFAULT '{}',
    updated_at  INTEGER NOT NULL
);

-- User-defined custom bots (Grok-style). Exactly one row may be active;
-- BotStore::set_active enforces that in a transaction.
CREATE TABLE IF NOT EXISTS bots (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    system_prompt TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT '',
    provider      TEXT NOT NULL DEFAULT '',
    active        INTEGER NOT NULL DEFAULT 0
);

-- Generic key-value store. Namespaced by key prefix, by convention:
--   marketplace.installed.<plugin_id>  installed marketplace items
--   groups.policy.<group_id>           group policy JSON
--   groups.muted.<group_id>            group mute flag ("1"/"0")
--   settings.*                         app settings
--   provider.active                    id of the active provider
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- Bot groups: multi-bot chat rooms. `bot_ids` is a JSON array of bot ids.
-- `chief_bot_id` is the Chief-of-Staff bot that triages incoming user
-- messages and delegates to specialists (NULL = first member acts as chief).
CREATE TABLE IF NOT EXISTS bot_groups (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    bot_ids      TEXT NOT NULL DEFAULT '[]',
    chief_bot_id TEXT,
    created_at   INTEGER NOT NULL
);

-- Persisted message log for bot groups (incoming messages and bot replies).
CREATE TABLE IF NOT EXISTS bot_group_messages (
    id         TEXT PRIMARY KEY,
    group_id   TEXT NOT NULL,
    bot_id     TEXT NOT NULL DEFAULT '',
    bot_name   TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    FOREIGN KEY (group_id) REFERENCES bot_groups(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_bot_group_messages_group
    ON bot_group_messages(group_id, created_at, id);

-- Per-bot isolated memories: long-term facts/notes scoped to one bot id,
-- never shared across bots. Sessions (`botmsg_<bot_id>`) already isolate
-- conversation turns; this table is for durable per-bot memory entries.
CREATE TABLE IF NOT EXISTS bot_memories (
    id         TEXT PRIMARY KEY,
    bot_id     TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'fact',
    content    TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bot_memories_bot
    ON bot_memories(bot_id, created_at, id);
