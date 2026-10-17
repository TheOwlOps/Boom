//! Channel operation tools: let bots interact with connected messaging
//! channels (Telegram, Discord, Messenger, Slack).
//!
//! Each tool reads the channel's stored credential from
//! `$BOOM_HOME/channels/<id>.json` via `FileChannelRegistry` (same files
//! the HTTP API uses), then calls the real platform API. Unconnected
//! channel → clear error telling the user to connect first.
//!
//! Tool naming: `<channel>_<operation>`, e.g. `telegram_send_photo`.

use async_trait::async_trait;
use serde_json::Value;

use super::{req_str, schema_of, Tool, ToolError};
use crate::server::channels::FileChannelRegistry;
use crate::server::state::ChannelRegistry;

/// Load the stored token + API base for a channel.
/// `Err` when the channel is not connected (clear guidance, no token).
fn channel_creds(id: &str) -> Result<(String, String), ToolError> {
    let reg = FileChannelRegistry::new();
    let token = reg.channel_token(id).map_err(|e| {
        ToolError::InvalidArgs(format!(
            "{id} chưa được kết nối — vào Tools → Channels để connect trước ({})",
            e.message
        ))
    })?;
    let api_base = reg.channel_api_base(id);
    Ok((api_base, token))
}

/// Map a channel API error string into a ToolError.
fn api_err(e: String) -> ToolError {
    ToolError::Other(e)
}

fn req_i64(args: &Value, key: &str) -> Result<i64, ToolError> {
    args.get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| ToolError::InvalidArgs(format!("{key} is required")))
}

fn opt_str(args: &Value, key: &str) -> Option<String> {
    args.get(key)
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty())
        .map(|s| s.trim().to_string())
}

// ---------------------------------------------------------------------------
// Telegram tools
// ---------------------------------------------------------------------------

struct TelegramSendPhoto;
#[async_trait]
impl Tool for TelegramSendPhoto {
    fn name(&self) -> &str {
        "telegram_send_photo"
    }
    fn description(&self) -> &str {
        "Send a photo via Telegram (URL or file_id). Requires the telegram channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("chat_id", "string", "Telegram chat id"),
                ("photo", "string", "Photo URL or file_id"),
                ("caption", "string", "Optional caption"),
            ],
            &["chat_id", "photo"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("telegram")?;
        let chat_id = req_str(&args, "chat_id")?;
        let photo = req_str(&args, "photo")?;
        let caption = opt_str(&args, "caption");
        let mid =
            crate::server::channels::telegram::send_photo(&api, &token, &chat_id, &photo, caption.as_deref())
                .await
                .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "message_id": mid }))
    }
}

struct TelegramSendDocument;
#[async_trait]
impl Tool for TelegramSendDocument {
    fn name(&self) -> &str {
        "telegram_send_document"
    }
    fn description(&self) -> &str {
        "Send a document/file via Telegram (URL or file_id). Requires the telegram channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("chat_id", "string", "Telegram chat id"),
                ("document", "string", "Document URL or file_id"),
                ("caption", "string", "Optional caption"),
            ],
            &["chat_id", "document"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("telegram")?;
        let chat_id = req_str(&args, "chat_id")?;
        let document = req_str(&args, "document")?;
        let caption = opt_str(&args, "caption");
        let mid = crate::server::channels::telegram::send_document(
            &api, &token, &chat_id, &document, caption.as_deref(),
        )
        .await
        .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "message_id": mid }))
    }
}

struct TelegramSendSticker;
#[async_trait]
impl Tool for TelegramSendSticker {
    fn name(&self) -> &str {
        "telegram_send_sticker"
    }
    fn description(&self) -> &str {
        "Send a sticker via Telegram (file_id). Requires the telegram channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("chat_id", "string", "Telegram chat id"),
                ("sticker", "string", "Sticker file_id"),
            ],
            &["chat_id", "sticker"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("telegram")?;
        let chat_id = req_str(&args, "chat_id")?;
        let sticker = req_str(&args, "sticker")?;
        let mid = crate::server::channels::telegram::send_sticker(&api, &token, &chat_id, &sticker)
            .await
            .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "message_id": mid }))
    }
}

struct TelegramEditMessage;
#[async_trait]
impl Tool for TelegramEditMessage {
    fn name(&self) -> &str {
        "telegram_edit_message"
    }
    fn description(&self) -> &str {
        "Edit a Telegram message's text. Requires the telegram channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("chat_id", "string", "Telegram chat id"),
                ("message_id", "integer", "Message id to edit"),
                ("text", "string", "New text"),
            ],
            &["chat_id", "message_id", "text"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("telegram")?;
        let chat_id = req_str(&args, "chat_id")?;
        let mid = req_i64(&args, "message_id")?;
        let text = req_str(&args, "text")?;
        let new_mid =
            crate::server::channels::telegram::edit_message(&api, &token, &chat_id, mid, &text)
                .await
                .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "message_id": new_mid }))
    }
}

struct TelegramDeleteMessage;
#[async_trait]
impl Tool for TelegramDeleteMessage {
    fn name(&self) -> &str {
        "telegram_delete_message"
    }
    fn description(&self) -> &str {
        "Delete a Telegram message. Requires the telegram channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("chat_id", "string", "Telegram chat id"),
                ("message_id", "integer", "Message id to delete"),
            ],
            &["chat_id", "message_id"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("telegram")?;
        let chat_id = req_str(&args, "chat_id")?;
        let mid = req_i64(&args, "message_id")?;
        crate::server::channels::telegram::delete_message(&api, &token, &chat_id, mid)
            .await
            .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "deleted": true }))
    }
}

struct TelegramGetChatInfo;
#[async_trait]
impl Tool for TelegramGetChatInfo {
    fn name(&self) -> &str {
        "telegram_get_chat_info"
    }
    fn description(&self) -> &str {
        "Get Telegram chat info (title, type). Requires the telegram channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(&[("chat_id", "string", "Telegram chat id")], &["chat_id"])
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("telegram")?;
        let chat_id = req_str(&args, "chat_id")?;
        let info = crate::server::channels::telegram::get_chat_info(&api, &token, &chat_id)
            .await
            .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "id": info.id, "title": info.title, "type": info.kind }))
    }
}

struct TelegramGetChatMembers;
#[async_trait]
impl Tool for TelegramGetChatMembers {
    fn name(&self) -> &str {
        "telegram_get_chat_members"
    }
    fn description(&self) -> &str {
        "List Telegram chat administrators/members. Requires the telegram channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(&[("chat_id", "string", "Telegram chat id")], &["chat_id"])
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("telegram")?;
        let chat_id = req_str(&args, "chat_id")?;
        let members = crate::server::channels::telegram::get_chat_members(&api, &token, &chat_id)
            .await
            .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "members": members.iter().map(|m| serde_json::json!({
            "user_id": m.user_id, "username": m.username,
            "first_name": m.first_name, "status": m.status,
        })).collect::<Vec<_>>() }))
    }
}

struct TelegramPinMessage;
#[async_trait]
impl Tool for TelegramPinMessage {
    fn name(&self) -> &str {
        "telegram_pin_message"
    }
    fn description(&self) -> &str {
        "Pin a Telegram message. Requires the telegram channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("chat_id", "string", "Telegram chat id"),
                ("message_id", "integer", "Message id to pin"),
            ],
            &["chat_id", "message_id"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("telegram")?;
        let chat_id = req_str(&args, "chat_id")?;
        let mid = req_i64(&args, "message_id")?;
        crate::server::channels::telegram::pin_message(&api, &token, &chat_id, mid)
            .await
            .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "pinned": true }))
    }
}

// ---------------------------------------------------------------------------
// Discord tools
// ---------------------------------------------------------------------------

struct DiscordSendEmbed;
#[async_trait]
impl Tool for DiscordSendEmbed {
    fn name(&self) -> &str {
        "discord_send_embed"
    }
    fn description(&self) -> &str {
        "Send a rich embed message to a Discord channel. Requires the discord channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("channel_id", "string", "Discord channel id"),
                ("title", "string", "Embed title"),
                ("description", "string", "Embed description"),
                ("color", "integer", "Embed color as decimal (e.g. 65280 for green)"),
            ],
            &["channel_id", "title", "description"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("discord")?;
        let channel = req_str(&args, "channel_id")?;
        let title = req_str(&args, "title")?;
        let desc = req_str(&args, "description")?;
        let color = args.get("color").and_then(Value::as_i64);
        let mid =
            crate::server::channels::discord::send_embed(&api, &token, &channel, &title, &desc, color)
                .await
                .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "message_id": mid }))
    }
}

struct DiscordEditMessage;
#[async_trait]
impl Tool for DiscordEditMessage {
    fn name(&self) -> &str {
        "discord_edit_message"
    }
    fn description(&self) -> &str {
        "Edit a Discord message. Requires the discord channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("channel_id", "string", "Discord channel id"),
                ("message_id", "string", "Message id to edit"),
                ("text", "string", "New content"),
            ],
            &["channel_id", "message_id", "text"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("discord")?;
        let channel = req_str(&args, "channel_id")?;
        let mid = req_str(&args, "message_id")?;
        let text = req_str(&args, "text")?;
        let new_mid =
            crate::server::channels::discord::edit_message(&api, &token, &channel, &mid, &text)
                .await
                .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "message_id": new_mid }))
    }
}

struct DiscordDeleteMessage;
#[async_trait]
impl Tool for DiscordDeleteMessage {
    fn name(&self) -> &str {
        "discord_delete_message"
    }
    fn description(&self) -> &str {
        "Delete a Discord message. Requires the discord channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("channel_id", "string", "Discord channel id"),
                ("message_id", "string", "Message id to delete"),
            ],
            &["channel_id", "message_id"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("discord")?;
        let channel = req_str(&args, "channel_id")?;
        let mid = req_str(&args, "message_id")?;
        crate::server::channels::discord::delete_message(&api, &token, &channel, &mid)
            .await
            .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "deleted": true }))
    }
}

struct DiscordAddReaction;
#[async_trait]
impl Tool for DiscordAddReaction {
    fn name(&self) -> &str {
        "discord_add_reaction"
    }
    fn description(&self) -> &str {
        "Add a reaction emoji to a Discord message. Requires the discord channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("channel_id", "string", "Discord channel id"),
                ("message_id", "string", "Message id"),
                ("emoji", "string", "Emoji (unicode or name:id for custom)"),
            ],
            &["channel_id", "message_id", "emoji"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("discord")?;
        let channel = req_str(&args, "channel_id")?;
        let mid = req_str(&args, "message_id")?;
        let emoji = req_str(&args, "emoji")?;
        crate::server::channels::discord::add_reaction(&api, &token, &channel, &mid, &emoji)
            .await
            .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "reacted": true }))
    }
}

struct DiscordGetChannelInfo;
#[async_trait]
impl Tool for DiscordGetChannelInfo {
    fn name(&self) -> &str {
        "discord_get_channel_info"
    }
    fn description(&self) -> &str {
        "Get Discord channel info. Requires the discord channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(&[("channel_id", "string", "Discord channel id")], &["channel_id"])
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("discord")?;
        let channel = req_str(&args, "channel_id")?;
        let info = crate::server::channels::discord::get_channel_info(&api, &token, &channel)
            .await
            .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "id": info.id, "name": info.name, "type": info.kind, "guild_id": info.guild_id }))
    }
}

struct DiscordGetGuildInfo;
#[async_trait]
impl Tool for DiscordGetGuildInfo {
    fn name(&self) -> &str {
        "discord_get_guild_info"
    }
    fn description(&self) -> &str {
        "Get Discord server (guild) info. Requires the discord channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(&[("guild_id", "string", "Discord guild id")], &["guild_id"])
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("discord")?;
        let guild = req_str(&args, "guild_id")?;
        let info = crate::server::channels::discord::get_guild_info(&api, &token, &guild)
            .await
            .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "id": info.id, "name": info.name, "member_count": info.member_count }))
    }
}

struct DiscordListMembers;
#[async_trait]
impl Tool for DiscordListMembers {
    fn name(&self) -> &str {
        "discord_list_members"
    }
    fn description(&self) -> &str {
        "List Discord guild members. Requires the discord channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(&[("guild_id", "string", "Discord guild id")], &["guild_id"])
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("discord")?;
        let guild = req_str(&args, "guild_id")?;
        let members = crate::server::channels::discord::list_members(&api, &token, &guild)
            .await
            .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "members": members.iter().map(|m| serde_json::json!({
            "user_id": m.user_id, "username": m.username, "nick": m.nick,
        })).collect::<Vec<_>>() }))
    }
}

// ---------------------------------------------------------------------------
// Messenger tools
// ---------------------------------------------------------------------------

/// Parse a JSON-array-of-{title,payload} string argument.
fn parse_buttons(args: &Value) -> Result<Vec<(String, String)>, ToolError> {
    let raw = req_str(args, "buttons")?;
    let parsed: Value = serde_json::from_str(&raw).map_err(|_| {
        ToolError::InvalidArgs("buttons must be a JSON array of {title, payload}".to_string())
    })?;
    let buttons: Vec<(String, String)> = parsed
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(|b| {
                    Some((
                        b.get("title")?.as_str()?.to_string(),
                        b.get("payload")?.as_str()?.to_string(),
                    ))
                })
                .collect()
        })
        .unwrap_or_default();
    if buttons.is_empty() {
        return Err(ToolError::InvalidArgs(
            "buttons must be a non-empty JSON array".to_string(),
        ));
    }
    Ok(buttons)
}

struct MessengerSendImage;
#[async_trait]
impl Tool for MessengerSendImage {
    fn name(&self) -> &str {
        "messenger_send_image"
    }
    fn description(&self) -> &str {
        "Send an image via Messenger (public HTTPS URL). Requires the messenger channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("psid", "string", "Recipient PSID"),
                ("image_url", "string", "Public HTTPS image URL"),
            ],
            &["psid", "image_url"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("messenger")?;
        let psid = req_str(&args, "psid")?;
        let url = req_str(&args, "image_url")?;
        let mid = crate::server::channels::messenger::send_image(&api, &token, &psid, &url)
            .await
            .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "message_id": mid }))
    }
}

struct MessengerSendQuickReplies;
#[async_trait]
impl Tool for MessengerSendQuickReplies {
    fn name(&self) -> &str {
        "messenger_send_quick_replies"
    }
    fn description(&self) -> &str {
        "Send a Messenger message with quick-reply buttons (up to 13). Requires the messenger channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("psid", "string", "Recipient PSID"),
                ("text", "string", "Message text"),
                (
                    "buttons",
                    "string",
                    "JSON array of {title, payload} — e.g. [{\"title\":\"Yes\",\"payload\":\"YES\"}]",
                ),
            ],
            &["psid", "text", "buttons"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("messenger")?;
        let psid = req_str(&args, "psid")?;
        let text = req_str(&args, "text")?;
        let buttons = parse_buttons(&args)?;
        let mid =
            crate::server::channels::messenger::send_quick_replies(&api, &token, &psid, &text, &buttons)
                .await
                .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "message_id": mid }))
    }
}

struct MessengerSendButtons;
#[async_trait]
impl Tool for MessengerSendButtons {
    fn name(&self) -> &str {
        "messenger_send_buttons"
    }
    fn description(&self) -> &str {
        "Send a Messenger button template (up to 3 postback buttons). Requires the messenger channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("psid", "string", "Recipient PSID"),
                ("title", "string", "Template title text"),
                (
                    "buttons",
                    "string",
                    "JSON array of {title, payload} — e.g. [{\"title\":\"Buy\",\"payload\":\"BUY\"}]",
                ),
            ],
            &["psid", "title", "buttons"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("messenger")?;
        let psid = req_str(&args, "psid")?;
        let title = req_str(&args, "title")?;
        let buttons = parse_buttons(&args)?;
        let mid =
            crate::server::channels::messenger::send_buttons(&api, &token, &psid, &title, &buttons)
                .await
                .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "message_id": mid }))
    }
}

struct MessengerGetUserProfile;
#[async_trait]
impl Tool for MessengerGetUserProfile {
    fn name(&self) -> &str {
        "messenger_get_user_profile"
    }
    fn description(&self) -> &str {
        "Get a Messenger user's profile (name, avatar). Requires the messenger channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(&[("psid", "string", "User PSID")], &["psid"])
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("messenger")?;
        let psid = req_str(&args, "psid")?;
        let p = crate::server::channels::messenger::get_user_profile(&api, &token, &psid)
            .await
            .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "psid": p.psid, "first_name": p.first_name, "last_name": p.last_name, "profile_pic": p.profile_pic }))
    }
}

struct MessengerSetTyping;
#[async_trait]
impl Tool for MessengerSetTyping {
    fn name(&self) -> &str {
        "messenger_set_typing"
    }
    fn description(&self) -> &str {
        "Show/hide the typing indicator in a Messenger chat. Requires the messenger channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("psid", "string", "Recipient PSID"),
                (
                    "action",
                    "string",
                    "typing_on, typing_off, or mark_seen (default typing_on)",
                ),
            ],
            &["psid"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("messenger")?;
        let psid = req_str(&args, "psid")?;
        let action = opt_str(&args, "action").unwrap_or_else(|| "typing_on".to_string());
        crate::server::channels::messenger::set_typing(&api, &token, &psid, &action)
            .await
            .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true }))
    }
}

// ---------------------------------------------------------------------------
// Slack tools
// ---------------------------------------------------------------------------

struct SlackSendBlocks;
#[async_trait]
impl Tool for SlackSendBlocks {
    fn name(&self) -> &str {
        "slack_send_blocks"
    }
    fn description(&self) -> &str {
        "Send a Slack message with Block Kit blocks. Requires the slack channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("channel", "string", "Slack channel id"),
                ("text", "string", "Fallback plain text"),
                ("blocks", "string", "JSON array of Block Kit blocks (optional)"),
            ],
            &["channel", "text"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("slack")?;
        let channel = req_str(&args, "channel")?;
        let text = req_str(&args, "text")?;
        let blocks: Option<Value> = opt_str(&args, "blocks")
            .and_then(|s| serde_json::from_str(&s).ok())
            .filter(|v: &Value| !v.is_null());
        let ts =
            crate::server::channels::slack::send_blocks(&api, &token, &channel, &text, blocks.as_ref())
                .await
                .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "ts": ts }))
    }
}

struct SlackUpdateMessage;
#[async_trait]
impl Tool for SlackUpdateMessage {
    fn name(&self) -> &str {
        "slack_update_message"
    }
    fn description(&self) -> &str {
        "Update a Slack message. Requires the slack channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("channel", "string", "Slack channel id"),
                ("ts", "string", "Message timestamp id"),
                ("text", "string", "New text"),
            ],
            &["channel", "ts", "text"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("slack")?;
        let channel = req_str(&args, "channel")?;
        let ts = req_str(&args, "ts")?;
        let text = req_str(&args, "text")?;
        let new_ts =
            crate::server::channels::slack::update_message(&api, &token, &channel, &ts, &text)
                .await
                .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "ts": new_ts }))
    }
}

struct SlackDeleteMessage;
#[async_trait]
impl Tool for SlackDeleteMessage {
    fn name(&self) -> &str {
        "slack_delete_message"
    }
    fn description(&self) -> &str {
        "Delete a Slack message. Requires the slack channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("channel", "string", "Slack channel id"),
                ("ts", "string", "Message timestamp id"),
            ],
            &["channel", "ts"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("slack")?;
        let channel = req_str(&args, "channel")?;
        let ts = req_str(&args, "ts")?;
        crate::server::channels::slack::delete_message(&api, &token, &channel, &ts)
            .await
            .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "deleted": true }))
    }
}

struct SlackListChannels;
#[async_trait]
impl Tool for SlackListChannels {
    fn name(&self) -> &str {
        "slack_list_channels"
    }
    fn description(&self) -> &str {
        "List Slack channels. Requires the slack channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(&[], &[])
    }
    async fn execute(&self, _args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("slack")?;
        let channels = crate::server::channels::slack::list_channels(&api, &token)
            .await
            .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "channels": channels.iter().map(|c| serde_json::json!({
            "id": c.id, "name": c.name, "is_private": c.is_private,
        })).collect::<Vec<_>>() }))
    }
}

struct SlackGetUserInfo;
#[async_trait]
impl Tool for SlackGetUserInfo {
    fn name(&self) -> &str {
        "slack_get_user_info"
    }
    fn description(&self) -> &str {
        "Get Slack user info. Requires the slack channel to be connected."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[("user_id", "string", "Slack user id (U...)")],
            &["user_id"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let (api, token) = channel_creds("slack")?;
        let user = req_str(&args, "user_id")?;
        let u = crate::server::channels::slack::get_user_info(&api, &token, &user)
            .await
            .map_err(api_err)?;
        Ok(serde_json::json!({ "ok": true, "id": u.id, "name": u.name, "real_name": u.real_name }))
    }
}

/// Register all channel tools on the registry.
pub fn register_all(reg: &mut super::ToolRegistry) {
    reg.register("channels", TelegramSendPhoto);
    reg.register("channels", TelegramSendDocument);
    reg.register("channels", TelegramSendSticker);
    reg.register("channels", TelegramEditMessage);
    reg.register("channels", TelegramDeleteMessage);
    reg.register("channels", TelegramGetChatInfo);
    reg.register("channels", TelegramGetChatMembers);
    reg.register("channels", TelegramPinMessage);
    reg.register("channels", DiscordSendEmbed);
    reg.register("channels", DiscordEditMessage);
    reg.register("channels", DiscordDeleteMessage);
    reg.register("channels", DiscordAddReaction);
    reg.register("channels", DiscordGetChannelInfo);
    reg.register("channels", DiscordGetGuildInfo);
    reg.register("channels", DiscordListMembers);
    reg.register("channels", MessengerSendImage);
    reg.register("channels", MessengerSendQuickReplies);
    reg.register("channels", MessengerSendButtons);
    reg.register("channels", MessengerGetUserProfile);
    reg.register("channels", MessengerSetTyping);
    reg.register("channels", SlackSendBlocks);
    reg.register("channels", SlackUpdateMessage);
    reg.register("channels", SlackDeleteMessage);
    reg.register("channels", SlackListChannels);
    reg.register("channels", SlackGetUserInfo);
}
