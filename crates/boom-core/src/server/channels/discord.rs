//! Discord Bot API client: `GET /users/@me` verify, message send, and
//! channel-message polling for ingress.
//!
//! Uses the bot-token scheme: `Authorization: Bot <token>` header — the
//! token never appears in URLs, so transport errors cannot echo it.
//! Error messages are generic for the same reason.

use std::time::Duration;

use serde_json::Value;

use super::InboundMessage;

/// Discord API base (overridable in tests).
pub const API_BASE: &str = "https://discord.com/api/v10";
const TIMEOUT: Duration = Duration::from_secs(15);
/// Max messages per poll page.
const POLL_LIMIT: u8 = 50;

/// Bot identity returned by `GET /users/@me`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DiscordIdentity {
    pub id: String,
    pub username: String,
    pub display_name: Option<String>,
}

/// Parse a `GET /users/@me` response. `Err` never contains the token.
pub fn parse_verify(body: &Value) -> Result<DiscordIdentity, String> {
    let id = body
        .get("id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "Discord verify response missing bot id".to_string())?;
    let username = body
        .get("username")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let display_name = body
        .get("global_name")
        .and_then(Value::as_str)
        .map(|s| s.to_string());
    Ok(DiscordIdentity {
        id: id.to_string(),
        username,
        display_name,
    })
}

/// Parse a `POST /channels/{id}/messages` response. `Ok` → message id.
pub fn parse_send(body: &Value) -> Result<String, String> {
    body.get("id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .ok_or_else(|| describe_error(body, "refused to send"))
}

/// Parse a `GET /channels/{id}/messages` page into inbound messages.
/// `after` is applied server-side; this only normalizes the shape.
/// Messages sent by bots are kept — callers should skip their own bot id
/// to avoid echo loops.
pub fn parse_poll(body: &Value) -> Result<Vec<InboundMessage>, String> {
    let arr = body.as_array().ok_or_else(|| {
        // Error bodies are objects like {"message": "...", "code": 50001}.
        describe_error(body, "poll failed")
    })?;
    let mut out = Vec::new();
    for m in arr {
        let id = m
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let text = m
            .get("content")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if id.is_empty() || text.trim().is_empty() {
            continue;
        }
        let chat_id = m
            .get("channel_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let sender = m.get("author").and_then(|a| {
            a.get("global_name")
                .and_then(Value::as_str)
                .or_else(|| a.get("username").and_then(Value::as_str))
                .map(|s| format!("@{s}"))
        });
        let ts = m
            .get("timestamp")
            .and_then(Value::as_str)
            .map(|s| s.to_string());
        out.push(InboundMessage {
            id,
            chat_id,
            sender,
            text,
            ts,
        });
    }
    // Discord returns newest-first; normalize to oldest-first so `after`
    // cursors advance monotonically.
    out.reverse();
    Ok(out)
}

/// Render a Discord error body: `{"message": "...", "code": N}`.
fn describe_error(body: &Value, action: &str) -> String {
    let message = body
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or("unknown error")
        .trim();
    let code = body.get("code").and_then(Value::as_i64);
    match code {
        Some(c) => format!("Discord {action} (code {c}): {message}"),
        None => format!("Discord {action}: {message}"),
    }
}

/// Read a JSON response body; degrade non-JSON/empty to generic text.
async fn read_json_body(resp: reqwest::Response) -> Result<Value, String> {
    let status = resp.status();
    if !status.is_success() {
        // Error bodies are still JSON on Discord; parse for the message.
        let text = resp.text().await.unwrap_or_default();
        let body: Value = serde_json::from_str(text.trim()).unwrap_or(Value::Null);
        return Err(describe_error(&body, "request failed"));
    }
    let text = resp.text().await.map_err(|_| "request failed".to_string())?;
    let text = text.trim();
    if text.is_empty() {
        return Err(format!(
            "Discord returned HTTP {} with an empty body",
            status.as_u16()
        ));
    }
    serde_json::from_str::<Value>(text)
        .map_err(|_| format!("Discord returned HTTP {} with a non-JSON body", status.as_u16()))
}

/// Generic transport failure (token is header-only, but stay generic).
fn transport_error(err: reqwest::Error) -> String {
    if err.is_timeout() {
        "connection to Discord timed out".to_string()
    } else if err.is_connect() {
        "could not reach Discord (connection failed)".to_string()
    } else {
        "request to Discord failed".to_string()
    }
}

fn client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .timeout(TIMEOUT)
        .build()
        .map_err(|_| "failed to build HTTP client".to_string())
}

fn auth(token: &str) -> String {
    format!("Bot {token}")
}

/// Live verify: `GET /users/@me`. `Err` never contains the token.
pub async fn verify_token(api_base: &str, token: &str) -> Result<DiscordIdentity, String> {
    let url = format!("{}/users/@me", api_base.trim_end_matches('/'));
    let resp = client()?
        .get(&url)
        .header("Authorization", auth(token))
        .send()
        .await
        .map_err(transport_error)?;
    let body = read_json_body(resp).await?;
    parse_verify(&body)
}

/// Live send: `POST /channels/{channel_id}/messages`. `Ok` → message id.
pub async fn send_text(
    api_base: &str,
    token: &str,
    channel_id: &str,
    text: &str,
) -> Result<String, String> {
    let url = format!(
        "{}/channels/{}/messages",
        api_base.trim_end_matches('/'),
        channel_id.trim()
    );
    let resp = client()?
        .post(&url)
        .header("Authorization", auth(token))
        .json(&serde_json::json!({ "content": text }))
        .send()
        .await
        .map_err(transport_error)?;
    let body = read_json_body(resp).await?;
    parse_send(&body)
}

/// Poll recent channel messages for ingress. `after` is the last seen
/// message id (exclusive); `None` returns the latest page.
pub async fn poll_messages(
    api_base: &str,
    token: &str,
    channel_id: &str,
    after: Option<&str>,
) -> Result<Vec<InboundMessage>, String> {
    let mut url = format!(
        "{}/channels/{}/messages?limit={}",
        api_base.trim_end_matches('/'),
        channel_id.trim(),
        POLL_LIMIT
    );
    if let Some(a) = after.filter(|s| !s.trim().is_empty()) {
        url.push_str(&format!("&after={}", a.trim()));
    }
    let resp = client()?
        .get(&url)
        .header("Authorization", auth(token))
        .send()
        .await
        .map_err(transport_error)?;
    let body = read_json_body(resp).await?;
    parse_poll(&body)
}

// ---------------------------------------------------------------------------
// Extended operations: embeds, message management, reactions, guild info
// ---------------------------------------------------------------------------

/// Generic request helper: method + path + optional JSON body.
/// `Ok` → parsed body.
async fn request(
    api_base: &str,
    token: &str,
    method: &str,
    path: &str,
    payload: Option<&Value>,
) -> Result<Value, String> {
    let url = format!("{}{}", api_base.trim_end_matches('/'), path);
    let mut req = client()?
        .request(
            method.parse::<reqwest::Method>().map_err(|_| "bad http method".to_string())?,
            &url,
        )
        .header("Authorization", auth(token));
    if let Some(p) = payload {
        req = req.json(p);
    }
    let resp = req.send().await.map_err(transport_error)?;
    read_json_body(resp).await
}

/// Live send with a rich embed: `POST /channels/{id}/messages`.
/// `Ok` → message id.
pub async fn send_embed(
    api_base: &str,
    token: &str,
    channel_id: &str,
    title: &str,
    description: &str,
    color: Option<i64>,
) -> Result<String, String> {
    let mut embed = serde_json::json!({ "title": title, "description": description });
    if let Some(c) = color {
        embed["color"] = serde_json::json!(c);
    }
    let body = request(
        api_base,
        token,
        "POST",
        &format!("/channels/{}/messages", channel_id.trim()),
        Some(&serde_json::json!({ "embeds": [embed] })),
    )
    .await?;
    parse_send(&body)
}

/// Live edit: `PATCH /channels/{channel}/messages/{message}`.
/// `Ok` → message id.
pub async fn edit_message(
    api_base: &str,
    token: &str,
    channel_id: &str,
    message_id: &str,
    text: &str,
) -> Result<String, String> {
    let body = request(
        api_base,
        token,
        "PATCH",
        &format!(
            "/channels/{}/messages/{}",
            channel_id.trim(),
            message_id.trim()
        ),
        Some(&serde_json::json!({ "content": text })),
    )
    .await?;
    parse_send(&body)
}

/// Live delete: `DELETE /channels/{channel}/messages/{message}`.
/// Discord answers 204 with an empty body.
pub async fn delete_message(
    api_base: &str,
    token: &str,
    channel_id: &str,
    message_id: &str,
) -> Result<(), String> {
    let url = format!(
        "{}/channels/{}/messages/{}",
        api_base.trim_end_matches('/'),
        channel_id.trim(),
        message_id.trim()
    );
    let resp = client()?
        .delete(&url)
        .header("Authorization", auth(token))
        .send()
        .await
        .map_err(transport_error)?;
    let status = resp.status();
    if status == reqwest::StatusCode::NO_CONTENT || status.is_success() {
        return Ok(());
    }
    let text = resp.text().await.unwrap_or_default();
    let body: Value = serde_json::from_str(text.trim()).unwrap_or(Value::Null);
    Err(describe_error(&body, "refused to delete"))
}

/// Live add reaction: `PUT /channels/{c}/messages/{m}/reactions/{emoji}/@me`.
/// `emoji` is URL-encoded (e.g. `👍` → `%F0%9F%91%8D`).
pub async fn add_reaction(
    api_base: &str,
    token: &str,
    channel_id: &str,
    message_id: &str,
    emoji: &str,
) -> Result<(), String> {
    let encoded: String = emoji
        .bytes()
        .map(|b| {
            if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
                (b as char).to_string()
            } else {
                format!("%{b:02X}")
            }
        })
        .collect();
    let body = request(
        api_base,
        token,
        "PUT",
        &format!(
            "/channels/{}/messages/{}/reactions/{}/@me",
            channel_id.trim(),
            message_id.trim(),
            encoded
        ),
        None,
    )
    .await;
    match body {
        Ok(_) => Ok(()),
        Err(e) if e.contains("empty body") || e.contains("204") => Ok(()),
        Err(e) => Err(e),
    }
}

/// Live remove reaction: `DELETE .../reactions/{emoji}/@me`.
pub async fn remove_reaction(
    api_base: &str,
    token: &str,
    channel_id: &str,
    message_id: &str,
    emoji: &str,
) -> Result<(), String> {
    let encoded: String = emoji
        .bytes()
        .map(|b| {
            if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
                (b as char).to_string()
            } else {
                format!("%{b:02X}")
            }
        })
        .collect();
    let url = format!(
        "{}/channels/{}/messages/{}/reactions/{}/@me",
        api_base.trim_end_matches('/'),
        channel_id.trim(),
        message_id.trim(),
        encoded
    );
    let resp = client()?
        .delete(&url)
        .header("Authorization", auth(token))
        .send()
        .await
        .map_err(transport_error)?;
    let status = resp.status();
    if status == reqwest::StatusCode::NO_CONTENT || status.is_success() {
        return Ok(());
    }
    let text = resp.text().await.unwrap_or_default();
    let body: Value = serde_json::from_str(text.trim()).unwrap_or(Value::Null);
    Err(describe_error(&body, "refused to remove reaction"))
}

/// Channel info from `GET /channels/{id}`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChannelInfo {
    pub id: String,
    pub name: String,
    pub kind: i64,
    pub guild_id: Option<String>,
}

/// Parse a `GET /channels/{id}` response.
pub fn parse_channel_info(body: &Value) -> Result<ChannelInfo, String> {
    let id = body
        .get("id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "Discord channel response missing id".to_string())?;
    let name = body
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let kind = body.get("type").and_then(Value::as_i64).unwrap_or(0);
    let guild_id = body
        .get("guild_id")
        .and_then(Value::as_str)
        .map(|s| s.to_string());
    Ok(ChannelInfo {
        id: id.to_string(),
        name,
        kind,
        guild_id,
    })
}

/// Live `GET /channels/{id}`.
pub async fn get_channel_info(
    api_base: &str,
    token: &str,
    channel_id: &str,
) -> Result<ChannelInfo, String> {
    let body = request(
        api_base,
        token,
        "GET",
        &format!("/channels/{}", channel_id.trim()),
        None,
    )
    .await?;
    parse_channel_info(&body)
}

/// Guild info from `GET /guilds/{id}`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GuildInfo {
    pub id: String,
    pub name: String,
    pub member_count: Option<i64>,
}

/// Parse a `GET /guilds/{id}` response.
pub fn parse_guild_info(body: &Value) -> Result<GuildInfo, String> {
    if body.get("id").and_then(Value::as_str).is_none() {
        return Err(describe_error(body, "could not fetch guild"));
    }
    let id = body.get("id").and_then(Value::as_str).unwrap_or("").to_string();
    let name = body
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let member_count = body
        .get("approximate_member_count")
        .and_then(Value::as_i64);
    Ok(GuildInfo {
        id,
        name,
        member_count,
    })
}

/// Live `GET /guilds/{id}?with_counts=true`.
pub async fn get_guild_info(
    api_base: &str,
    token: &str,
    guild_id: &str,
) -> Result<GuildInfo, String> {
    let body = request(
        api_base,
        token,
        "GET",
        &format!("/guilds/{}?with_counts=true", guild_id.trim()),
        None,
    )
    .await?;
    parse_guild_info(&body)
}

/// One guild member from `GET /guilds/{id}/members`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GuildMember {
    pub user_id: String,
    pub username: String,
    pub nick: Option<String>,
}

/// Parse a `GET /guilds/{id}/members` response.
pub fn parse_members(body: &Value) -> Result<Vec<GuildMember>, String> {
    let arr = body.as_array().ok_or_else(|| describe_error(body, "could not list members"))?;
    let mut out = Vec::new();
    for m in arr {
        let user = m.get("user");
        let (Some(uid), Some(username)) = (
            user.and_then(|u| u.get("id")).and_then(Value::as_str),
            user.and_then(|u| u.get("username")).and_then(Value::as_str),
        ) else {
            continue;
        };
        out.push(GuildMember {
            user_id: uid.to_string(),
            username: username.to_string(),
            nick: m.get("nick").and_then(Value::as_str).map(|s| s.to_string()),
        });
    }
    Ok(out)
}

/// Live `GET /guilds/{id}/members?limit=100`.
pub async fn list_members(
    api_base: &str,
    token: &str,
    guild_id: &str,
) -> Result<Vec<GuildMember>, String> {
    let body = request(
        api_base,
        token,
        "GET",
        &format!("/guilds/{}/members?limit=100", guild_id.trim()),
        None,
    )
    .await?;
    parse_members(&body)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::server::channels::mock::{closed_port_url, mock_server, mock_server_n, GOOD_TOKEN};

    #[test]
    fn parse_verify_ok() {
        let body: Value = serde_json::from_str(
            r#"{"id":"123","username":"boomi","global_name":"Boomi Bot","bot":true}"#,
        )
        .unwrap();
        let id = parse_verify(&body).expect("must parse");
        assert_eq!(
            id,
            DiscordIdentity {
                id: "123".to_string(),
                username: "boomi".to_string(),
                display_name: Some("Boomi Bot".to_string()),
            }
        );
    }

    #[test]
    fn parse_verify_missing_id() {
        let body: Value = serde_json::from_str(r#"{"username":"x"}"#).unwrap();
        assert!(parse_verify(&body).is_err());
    }

    #[test]
    fn parse_send_ok() {
        let body: Value = serde_json::from_str(r#"{"id":"999","content":"hi"}"#).unwrap();
        assert_eq!(parse_send(&body).expect("must parse"), "999");
    }

    #[test]
    fn parse_poll_normalizes_newest_first() {
        let body: Value = serde_json::from_str(
            r#"[
                {"id":"3","channel_id":"7","author":{"username":"u","global_name":"U"},"content":"third","timestamp":"2026-10-17T07:00:02+00:00"},
                {"id":"2","channel_id":"7","author":{"username":"u"},"content":"second","timestamp":"2026-10-17T07:00:01+00:00"},
                {"id":"1","channel_id":"7","author":{"username":"u"},"content":"","timestamp":"2026-10-17T07:00:00+00:00"}
            ]"#,
        )
        .unwrap();
        let msgs = parse_poll(&body).expect("must parse");
        // Empty content skipped; order normalized oldest-first.
        assert_eq!(msgs.len(), 2);
        assert_eq!(msgs[0].id, "2");
        assert_eq!(msgs[0].text, "second");
        assert_eq!(msgs[0].sender.as_deref(), Some("@u"));
        assert_eq!(msgs[1].id, "3");
        assert_eq!(msgs[1].sender.as_deref(), Some("@U"));
        assert_eq!(msgs[1].ts.as_deref(), Some("2026-10-17T07:00:02+00:00"));
    }

    #[test]
    fn parse_poll_error_body() {
        let body: Value =
            serde_json::from_str(r#"{"message":"Missing Access","code":50001}"#).unwrap();
        let err = parse_poll(&body).unwrap_err();
        assert!(err.contains("50001"), "err: {err}");
        assert!(err.contains("Missing Access"), "err: {err}");
    }

    #[tokio::test]
    async fn verify_token_sends_bot_auth_header() {
        let (base, handle) = mock_server(
            200,
            r#"{"id":"123","username":"boomi","global_name":"Boomi"}"#,
        )
        .await;
        let id = verify_token(&base, GOOD_TOKEN).await.expect("verify");
        assert_eq!(id.id, "123");
        let reqs = handle.await.expect("server task");
        assert_eq!(reqs.len(), 1);
        assert!(reqs[0].starts_with("GET /users/@me "), "req: {}", reqs[0]);
        assert!(
            reqs[0].contains(&format!("authorization: Bot {GOOD_TOKEN}").to_lowercase())
                || reqs[0].to_lowercase().contains("authorization: bot "),
            "req: {}",
            reqs[0]
        );
        // Token must be header-only, never in the URL path.
        let request_line = reqs[0].lines().next().unwrap_or("");
        assert!(!request_line.contains("TESTTOKEN"), "req: {request_line}");
    }

    #[tokio::test]
    async fn verify_token_rejected_never_echoes_token() {
        let (base, _handle) = mock_server(
            401,
            r#"{"message":"401: Unauthorized","code":0}"#,
        )
        .await;
        let err = verify_token(&base, GOOD_TOKEN).await.unwrap_err();
        assert!(err.contains("Unauthorized"), "err: {err}");
        assert!(!err.contains("TESTTOKEN"), "err: {err}");
    }

    #[tokio::test]
    async fn verify_token_connection_refused_is_generic() {
        let base = closed_port_url().await;
        let err = verify_token(&base, GOOD_TOKEN).await.unwrap_err();
        assert!(!err.is_empty());
        assert!(!err.contains("TESTTOKEN"), "err: {err}");
    }

    #[tokio::test]
    async fn send_text_posts_content_json() {
        let (base, handle) = mock_server(200, r#"{"id":"555","content":"hello"}"#).await;
        let id = send_text(&base, GOOD_TOKEN, "777", "hello")
            .await
            .expect("send");
        assert_eq!(id, "555");
        let reqs = handle.await.expect("server task");
        assert!(
            reqs[0].starts_with("POST /channels/777/messages "),
            "req: {}",
            reqs[0]
        );
        assert!(reqs[0].contains(r#""content":"hello""#), "req: {}", reqs[0]);
    }

    #[tokio::test]
    async fn poll_messages_uses_after_cursor() {
        let (base, handle) = mock_server(
            200,
            r#"[{"id":"9","channel_id":"7","author":{"username":"u"},"content":"yo","timestamp":"t"}]"#,
        )
        .await;
        let msgs = poll_messages(&base, GOOD_TOKEN, "7", Some("5"))
            .await
            .expect("poll");
        assert_eq!(msgs.len(), 1);
        assert_eq!(msgs[0].id, "9");
        let reqs = handle.await.expect("server task");
        assert!(
            reqs[0].starts_with("GET /channels/7/messages?limit=50&after=5 "),
            "req: {}",
            reqs[0]
        );
    }

    // ---------------- Extended operations ----------------

    #[test]
    fn parse_channel_info_ok() {
        let body: Value = serde_json::from_str(
            r#"{"id":"7","name":"general","type":0,"guild_id":"99"}"#,
        )
        .unwrap();
        let c = parse_channel_info(&body).expect("must parse");
        assert_eq!(c.id, "7");
        assert_eq!(c.name, "general");
        assert_eq!(c.guild_id.as_deref(), Some("99"));
    }

    #[test]
    fn parse_guild_info_ok() {
        let body: Value = serde_json::from_str(
            r#"{"id":"99","name":"Boom Server","approximate_member_count":42}"#,
        )
        .unwrap();
        let g = parse_guild_info(&body).expect("must parse");
        assert_eq!(g.name, "Boom Server");
        assert_eq!(g.member_count, Some(42));
    }

    #[test]
    fn parse_members_ok() {
        let body: Value = serde_json::from_str(
            r#"[{"user":{"id":"1","username":"an"},"nick":"An"},
                {"user":{"id":"2","username":"bot"}}]"#,
        )
        .unwrap();
        let ms = parse_members(&body).expect("must parse");
        assert_eq!(ms.len(), 2);
        assert_eq!(ms[0].nick.as_deref(), Some("An"));
    }

    #[tokio::test]
    async fn send_embed_posts_embeds() {
        let (base, handle) = mock_server(200, r#"{"id":"555","content":""}"#).await;
        let id = send_embed(&base, GOOD_TOKEN, "777", "Title", "Desc", Some(0x00ff00))
            .await
            .expect("send");
        assert_eq!(id, "555");
        let reqs = handle.await.expect("server task");
        assert!(reqs[0].starts_with("POST /channels/777/messages "), "req: {}", reqs[0]);
        assert!(reqs[0].contains(r#""title":"Title""#), "req: {}", reqs[0]);
        assert!(reqs[0].contains(r#""embeds":"#) || reqs[0].contains("\"embeds\""), "req: {}", reqs[0]);
    }

    #[tokio::test]
    async fn edit_and_delete_message_flow() {
        let (base, handle) = mock_server_n(200, r#"{"id":"555"}"#, 1).await;
        let id = edit_message(&base, GOOD_TOKEN, "777", "555", "new")
            .await
            .expect("edit");
        assert_eq!(id, "555");
        let reqs = handle.await.expect("server task");
        assert!(reqs[0].starts_with("PATCH /channels/777/messages/555 "), "req: {}", reqs[0]);
        // DELETE answers 204 with an empty body.
        let (base2, handle2) = mock_server(204, "").await;
        delete_message(&base2, GOOD_TOKEN, "777", "555")
            .await
            .expect("delete");
        let reqs2 = handle2.await.expect("server task");
        assert!(reqs2[0].starts_with("DELETE /channels/777/messages/555 "), "req: {}", reqs2[0]);
    }
}
