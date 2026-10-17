//! Ticketmaster connector — **event discovery only, no ticket purchase**.
//!
//! Wraps the public Ticketmaster Discovery API v2
//! (`https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/`).
//! Auth is a plain API key passed as the `apikey` query parameter — there is
//! no OAuth flow, so `connect_url` returns instructions instead of a URL and
//! `exchange_code` treats the given "code" as the raw API key and verifies it
//! against the API.
//!
//! ## Merge note for the coordinator
//!
//! Drop this file at `crates/boom-core/src/connectors/tickets.rs`, declare
//! `pub mod tickets;` in `src/connectors/mod.rs`, and call
//! `TicketmasterConnector::new().register_tools(&mut reg)` from wherever
//! connectors are wired in. Everything the file needs (the `Connector` trait,
//! the token helpers) is defined here so it compiles standalone inside
//! `boom-core` without touching existing files.

use std::collections::HashMap;
use std::fmt;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use chrono::NaiveDate;
use serde_json::Value;

use crate::tools::http::fetch_url;
use super::Connector;
use crate::tools::{opt_u64, req_str, schema_of, Tool, ToolError, ToolRegistry};


// ---------------------------------------------------------------------------
// Token persistence helpers
// ---------------------------------------------------------------------------
//
// Contract: tokens live in `$BOOM_HOME/connectors/<id>.json` written with
// mode 0600. `$BOOM_HOME` defaults to `~/.boom`.

fn boom_home() -> std::path::PathBuf {
    if let Ok(dir) = std::env::var("BOOM_HOME") {
        return std::path::PathBuf::from(dir);
    }
    dirs::home_dir()
        .map(|h| h.join(".boom"))
        .unwrap_or_else(|| std::path::PathBuf::from(".boom"))
}

fn token_path(id: &str) -> std::path::PathBuf {
    boom_home().join("connectors").join(format!("{id}.json"))
}

/// Load the stored token for connector `id`. Returns `None` when the file is
/// absent or unreadable — never an error.
pub fn load_token(id: &str) -> Option<String> {
    let text = std::fs::read_to_string(token_path(id)).ok()?;
    let v: Value = serde_json::from_str(&text).ok()?;
    v.get("token")?.as_str().map(str::to_string)
}

/// Persist the token for connector `id` (file mode 0600 on Unix).
pub fn save_token(id: &str, token: &str) -> Result<(), String> {
    let path = token_path(id);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("cannot create connector dir: {e}"))?;
    }
    let body = serde_json::json!({ "token": token, "id": id }).to_string();
    #[cfg(unix)]
    {
        use std::io::Write;
        use std::os::unix::fs::OpenOptionsExt;
        let mut f = std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(&path)
            .map_err(|e| format!("cannot write token file: {e}"))?;
        f.write_all(body.as_bytes())
            .map_err(|e| format!("cannot write token file: {e}"))?;
    }
    #[cfg(not(unix))]
    {
        std::fs::write(&path, body.as_bytes())
            .map_err(|e| format!("cannot write token file: {e}"))?;
    }
    Ok(())
}

/// Delete the stored token for connector `id`. Missing file is fine.
pub fn delete_token(id: &str) -> Result<(), String> {
    match std::fs::remove_file(token_path(id)) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(format!("cannot delete token file: {e}")),
    }
}

// ---------------------------------------------------------------------------
// Ticketmaster connector
// ---------------------------------------------------------------------------

const CONNECTOR_ID: &str = "ticketmaster";
const API_BASE: &str = "https://app.ticketmaster.com/discovery/v2";
const REQUEST_TIMEOUT_S: u64 = 30;

const CONNECT_INSTRUCTIONS: &str = "Ticketmaster uses a plain API key (no OAuth). Get one free at \
https://developer.ticketmaster.com/ : sign up, create an app, copy the Consumer Key. Then in Boom \
run `connect ticketmaster <your-consumer-key>` (the key is verified against the Discovery API and \
stored in $BOOM_HOME/connectors/ticketmaster.json with mode 0600).";

/// Shared mutable key state: the connector and every tool it registers hold
/// the same `Arc`, so `exchange_code`/`disconnect` take effect immediately.
#[derive(Clone, Default)]
struct KeyState(Arc<Mutex<Option<String>>>);

impl KeyState {
    fn get(&self) -> Option<String> {
        self.0.lock().ok().and_then(|g| g.clone())
    }
    fn set(&self, key: Option<String>) {
        if let Ok(mut g) = self.0.lock() {
            *g = key;
        }
    }
}

pub struct TicketmasterConnector {
    key: KeyState,
}

impl TicketmasterConnector {
    /// New connector; restores a previously saved API key if one exists.
    pub fn new() -> Self {
        let state = KeyState::default();
        if let Some(tok) = load_token(CONNECTOR_ID) {
            if !tok.is_empty() {
                state.set(Some(tok));
            }
        }
        Self { key: state }
    }

    // (Debug never prints the key — see the manual `Debug` impl below.)
}

impl Default for TicketmasterConnector {
    fn default() -> Self {
        Self::new()
    }
}

/// Never print the API key in logs or Debug output.
impl fmt::Debug for TicketmasterConnector {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("TicketmasterConnector")
            .field("connected", &self.is_connected())
            .field("api_key", &"<redacted>")
            .finish()
    }
}

#[async_trait]
impl Connector for TicketmasterConnector {
    fn id(&self) -> &'static str {
        CONNECTOR_ID
    }
    fn name(&self) -> &'static str {
        "Ticketmaster"
    }
    fn description(&self) -> &'static str {
        "Ticketmaster Discovery API: search concerts, sports and other events worldwide. \
         Discovery only — no ticket purchase."
    }
    fn auth_kind(&self) -> &'static str {
        "api_key"
    }
    fn is_connected(&self) -> bool {
        self.key.get().is_some()
    }
    fn connect_url(&self) -> Result<String, String> {
        // No OAuth: the "code" step consumes the raw API key instead.
        Err(CONNECT_INSTRUCTIONS.to_string())
    }
    async fn exchange_code(&mut self, code: &str) -> Result<(), String> {
        let key = code.trim();
        if key.is_empty() {
            return Err("empty API key".to_string());
        }
        let url = verify_url(key);
        let headers = HashMap::new();
        let fetched = fetch_url("GET", &url, &headers, None, REQUEST_TIMEOUT_S)
            .await
            .map_err(|e| format!("verification request failed: {e}"))?;
        interpret_verify(fetched.status)?;
        // 200: the key works — store it.
        self.key.set(Some(key.to_string()));
        save_token(CONNECTOR_ID, key)
    }
    fn disconnect(&mut self) -> Result<(), String> {
        self.key.set(None);
        delete_token(CONNECTOR_ID)
    }
    fn register_tools(&self, reg: &mut ToolRegistry) {
        reg.register("tickets", EventsSearch::new(self.key.clone()));
        reg.register("tickets", EventDetails::new(self.key.clone()));
    }
}

/// GET /events.json?apikey=<key>&size=1 used to verify a candidate key.
/// The key is URL-encoded by `reqwest::Url`; it is never logged.
fn verify_url(key: &str) -> String {
    let mut u: reqwest::Url = format!("{API_BASE}/events.json")
        .parse()
        .expect("hardcoded API URL must parse");
    u.query_pairs_mut()
        .append_pair("apikey", key)
        .append_pair("size", "1");
    u.to_string()
}

/// Map the verification response status to a user-friendly outcome.
/// Kept pure so it can be unit-tested without network access.
fn interpret_verify(status: u16) -> Result<(), String> {
    match status {
        200..=299 => Ok(()),
        401 => Err("invalid Ticketmaster API key (HTTP 401). Check the Consumer Key \
                    in your developer.ticketmaster.com app settings."
            .to_string()),
        403 => Err("Ticketmaster rejected the key (HTTP 403 — quota exceeded or key \
                    disabled). Check your developer.ticketmaster.com app."
            .to_string()),
        429 => Err("Ticketmaster rate limit hit (HTTP 429). Wait a moment and try again."
            .to_string()),
        s => Err(format!(
            "Ticketmaster key verification failed with HTTP {s}. The key was NOT saved."
        )),
    }
}

// ---------------------------------------------------------------------------
// Argument validation
// ---------------------------------------------------------------------------

/// Validate `country_code`: exactly 2 ASCII letters, normalized to uppercase.
fn validate_country_code(raw: &str) -> Result<String, ToolError> {
    let cc = raw.trim().to_uppercase();
    if cc.len() == 2 && cc.chars().all(|c| c.is_ascii_alphabetic()) {
        Ok(cc)
    } else {
        Err(ToolError::InvalidArgs(format!(
            "country_code must be exactly 2 letters (e.g. \"VN\", \"US\"), got {raw:?}"
        )))
    }
}

/// Validate `start_date` as a real calendar date in YYYY-MM-DD form.
fn validate_date(raw: &str) -> Result<NaiveDate, ToolError> {
    NaiveDate::parse_from_str(raw.trim(), "%Y-%m-%d").map_err(|_| {
        ToolError::InvalidArgs(format!(
            "start_date must be a real date in YYYY-MM-DD format, got {raw:?}"
        ))
    })
}

const ALLOWED_SORTS: &[&str] = &["date,asc", "date,desc", "relevance"];

fn validate_sort(raw: &str) -> Result<String, ToolError> {
    let s = raw.trim();
    if ALLOWED_SORTS.contains(&s) {
        Ok(s.to_string())
    } else {
        Err(ToolError::InvalidArgs(format!(
            "sort must be one of {}, got {raw:?}",
            ALLOWED_SORTS.join(" | ")
        )))
    }
}

// ---------------------------------------------------------------------------
// Discovery response parsing (pure — tested with fixtures)
// ---------------------------------------------------------------------------

/// Compact summary of one event from `_embedded.events[]`.
fn event_summary(ev: &Value) -> Value {
    let id = ev.get("id").and_then(Value::as_str).unwrap_or("");
    let name = ev.get("name").and_then(Value::as_str).unwrap_or("");
    let date = ev
        .pointer("/dates/start/localDate")
        .and_then(Value::as_str)
        .unwrap_or("");
    let time = ev
        .pointer("/dates/start/localTime")
        .and_then(Value::as_str)
        .unwrap_or("");
    let venue = ev
        .pointer("/_embedded/venues/0/name")
        .and_then(Value::as_str)
        .unwrap_or("");
    let city = ev
        .pointer("/_embedded/venues/0/city/name")
        .and_then(Value::as_str)
        .unwrap_or("");
    let url = ev.get("url").and_then(Value::as_str).unwrap_or("");
    serde_json::json!({
        "id": id,
        "name": name,
        "date": date,
        "time": time,
        "venue": venue,
        "city": city,
        "url": url,
    })
}

/// Parse the search response body into `(total, Vec<summary>)`.
/// Missing `_embedded.events` (zero results) yields an empty list, not an error.
fn parse_search(body: &str) -> Result<(u64, Vec<Value>), ToolError> {
    let v: Value = serde_json::from_str(body)
        .map_err(|e| ToolError::Http(format!("invalid JSON from Ticketmaster: {e}")))?;
    let total = v
        .pointer("/page/totalElements")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let events = v
        .pointer("/_embedded/events")
        .and_then(Value::as_array)
        .map(|arr| arr.iter().map(event_summary).collect())
        .unwrap_or_default();
    Ok((total, events))
}

/// Parse a single event detail body.
fn parse_details(body: &str) -> Result<Value, ToolError> {
    let v: Value = serde_json::from_str(body)
        .map_err(|e| ToolError::Http(format!("invalid JSON from Ticketmaster: {e}")))?;
    let date = v
        .pointer("/dates/start/localDate")
        .and_then(Value::as_str)
        .unwrap_or("");
    let time = v
        .pointer("/dates/start/localTime")
        .and_then(Value::as_str)
        .unwrap_or("");
    let venue = v
        .pointer("/_embedded/venues/0/name")
        .and_then(Value::as_str)
        .unwrap_or("");
    let address = v
        .pointer("/_embedded/venues/0/address/line1")
        .and_then(Value::as_str)
        .unwrap_or("");
    let price_ranges: Vec<Value> = v
        .get("priceRanges")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .map(|p| {
                    serde_json::json!({
                        "type": p.get("type").and_then(Value::as_str).unwrap_or(""),
                        "currency": p.get("currency").and_then(Value::as_str).unwrap_or(""),
                        "min": p.get("min"),
                        "max": p.get("max"),
                    })
                })
                .collect()
        })
        .unwrap_or_default();
    let description = v
        .get("info")
        .or_else(|| v.get("pleaseNote"))
        .and_then(Value::as_str)
        .unwrap_or("");
    Ok(serde_json::json!({
        "id": v.get("id").and_then(Value::as_str).unwrap_or(""),
        "name": v.get("name").and_then(Value::as_str).unwrap_or(""),
        "date": date,
        "time": time,
        "venue": venue,
        "address": address,
        "price_ranges": price_ranges,
        "url": v.get("url").and_then(Value::as_str).unwrap_or(""),
        "description": description,
        "status": v.pointer("/dates/status/code").and_then(Value::as_str).unwrap_or(""),
        "categories": v.get("classifications").and_then(Value::as_array).map(|arr| {
            arr.iter().map(|c| serde_json::json!({
                "segment": c.pointer("/segment/name").and_then(Value::as_str).unwrap_or(""),
                "genre": c.pointer("/genre/name").and_then(Value::as_str).unwrap_or(""),
            })).collect::<Vec<_>>()
        }).unwrap_or_default(),
    }))
}

// ---------------------------------------------------------------------------
// HTTP helper shared by both tools
// ---------------------------------------------------------------------------

/// Build a Discovery API URL with the key (query param) and extra params,
/// run a GET with a 30 s timeout, and return the body on 2xx.
/// Non-2xx responses become `ToolError::Http` without ever exposing the key.
async fn discovery_get(key: &str, path: &str, params: &[(&str, String)]) -> Result<String, ToolError> {
    let mut u: reqwest::Url = format!("{API_BASE}{path}")
        .parse()
        .expect("hardcoded API URL must parse");
    {
        let mut q = u.query_pairs_mut();
        q.append_pair("apikey", key);
        for (k, v) in params {
            q.append_pair(k, v);
        }
    }
    let fetched = fetch_url("GET", u.as_str(), &HashMap::new(), None, REQUEST_TIMEOUT_S).await?;
    match fetched.status {
        200..=299 => Ok(fetched.body),
        401 => Err(ToolError::Http(
            "Ticketmaster returned 401 — the stored API key is invalid or revoked. \
             Re-connect with a fresh key from developer.ticketmaster.com."
                .to_string(),
        )),
        429 => Err(ToolError::Http(
            "Ticketmaster returned 429 (rate limit). Wait and retry.".to_string(),
        )),
        s => Err(ToolError::Http(format!(
            "Ticketmaster request failed with HTTP {s}"
        ))),
    }
}

// ---------------------------------------------------------------------------
// events_search
// ---------------------------------------------------------------------------

pub struct EventsSearch {
    key: KeyState,
}

impl EventsSearch {
    fn new(key: KeyState) -> Self {
        Self { key }
    }
}

#[async_trait]
impl Tool for EventsSearch {
    fn name(&self) -> &str {
        "events_search"
    }
    fn description(&self) -> &'static str {
        "Search Ticketmaster for events (concerts, sports, theater…) by keyword, city, \
         country and start date. Discovery only — this tool cannot buy tickets."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                ("keyword", "string", "Search keyword, e.g. an artist or event name"),
                ("city", "string", "City name, e.g. \"Hanoi\""),
                (
                    "country_code",
                    "string",
                    "ISO country code: exactly 2 letters, e.g. \"VN\", \"US\"",
                ),
                (
                    "start_date",
                    "string",
                    "Only events on/after this date, YYYY-MM-DD",
                ),
                (
                    "size",
                    "integer",
                    "Results per page (default 10, max 50)",
                ),
                (
                    "sort",
                    "string",
                    "One of: date,asc | date,desc | relevance (default)",
                ),
            ],
            &[],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let key = self.key.get().ok_or_else(|| {
            ToolError::Other(
                "Ticketmaster is not connected. Run `connect ticketmaster <your-api-key>` first — \
                 get a free key at https://developer.ticketmaster.com/."
                    .to_string(),
            )
        })?;

        let size = opt_u64(&args, "size", 10).clamp(1, 50);
        let mut params: Vec<(&str, String)> = vec![("size", size.to_string())];

        if let Some(kw) = args.get("keyword").and_then(Value::as_str) {
            if !kw.trim().is_empty() {
                params.push(("keyword", kw.trim().to_string()));
            }
        }
        if let Some(city) = args.get("city").and_then(Value::as_str) {
            if !city.trim().is_empty() {
                params.push(("city", city.trim().to_string()));
            }
        }
        if let Some(cc) = args.get("country_code").and_then(Value::as_str) {
            if !cc.trim().is_empty() {
                params.push(("countryCode", validate_country_code(cc)?));
            }
        }
        if let Some(d) = args.get("start_date").and_then(Value::as_str) {
            if !d.trim().is_empty() {
                let date = validate_date(d)?;
                params.push(("startDateTime", format!("{}T00:00:00Z", date.format("%Y-%m-%d"))));
            }
        }
        if let Some(s) = args.get("sort").and_then(Value::as_str) {
            if !s.trim().is_empty() {
                params.push(("sort", validate_sort(s)?));
            }
        }

        let body = discovery_get(&key, "/events.json", &params).await?;
        let (total, events) = parse_search(&body)?;
        Ok(serde_json::json!({
            "total": total,
            "count": events.len(),
            "events": events,
            "note": "Discovery only — no ticket purchase. Use the event url to buy tickets on ticketmaster.com.",
        }))
    }
}

// ---------------------------------------------------------------------------
// event_details
// ---------------------------------------------------------------------------

pub struct EventDetails {
    key: KeyState,
}

impl EventDetails {
    fn new(key: KeyState) -> Self {
        Self { key }
    }
}

#[async_trait]
impl Tool for EventDetails {
    fn name(&self) -> &str {
        "event_details"
    }
    fn description(&self) -> &'static str {
        "Get details for one Ticketmaster event by its ID (from events_search): \
         venue, address, price ranges, status and ticketmaster.com URL. Discovery only — \
         this tool cannot buy tickets."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                (
                    "event_id",
                    "string",
                    "Ticketmaster event ID (the \"id\" field from events_search)",
                ),
            ],
            &["event_id"],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let key = self.key.get().ok_or_else(|| {
            ToolError::Other(
                "Ticketmaster is not connected. Run `connect ticketmaster <your-api-key>` first — \
                 get a free key at https://developer.ticketmaster.com/."
                    .to_string(),
            )
        })?;
        let event_id = req_str(&args, "event_id")?;
        if event_id.trim().is_empty() {
            return Err(ToolError::InvalidArgs("event_id must not be empty".into()));
        }
        // The ID becomes part of the path; reject path separators and query chars.
        if event_id
            .chars()
            .any(|c| c == '/' || c == '?' || c == '#' || c == '\\' || c.is_control())
        {
            return Err(ToolError::InvalidArgs(
                "event_id contains invalid characters".into(),
            ));
        }
        let body = discovery_get(&key, &format!("/events/{event_id}.json"), &[]).await?;
        let mut details = parse_details(&body)?;
        details["note"] = serde_json::json!(
            "Discovery only — no ticket purchase. Use the url to buy tickets on ticketmaster.com."
        );
        Ok(details)
    }
}

// ---------------------------------------------------------------------------
// Tests (all offline — fixtures + validation + honest degradation)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tools::ToolRegistry;

    const SEARCH_FIXTURE: &str = r#"{
      "_embedded": { "events": [
        {
          "id": "Z9vAm1f2abc3",
          "name": "Taylor Swift | The Eras Tour",
          "url": "https://www.ticketmaster.com/event/abc",
          "dates": { "start": { "localDate": "2026-11-20", "localTime": "19:30:00" },
                     "status": { "code": "onsale" } },
          "_embedded": { "venues": [ {
            "name": "Singapore National Stadium",
            "city": { "name": "Singapore" },
            "address": { "line1": "1 Stadium Drive" }
          } ] }
        },
        {
          "id": "Z9vAm2d4efg5",
          "name": "Missing venue event",
          "dates": { "start": { "localDate": "2026-12-01" } }
        }
      ] },
      "page": { "size": 2, "totalElements": 128, "totalPages": 64, "number": 0 }
    }"#;

    const EMPTY_FIXTURE: &str = r#"{"page": {"size": 10, "totalElements": 0, "totalPages": 0, "number": 0}}"#;

    const DETAIL_FIXTURE: &str = r#"{
      "id": "Z9vAm1f2abc3",
      "name": "Taylor Swift | The Eras Tour",
      "url": "https://www.ticketmaster.com/event/abc",
      "info": "The Eras Tour comes to Singapore.",
      "dates": { "start": { "localDate": "2026-11-20", "localTime": "19:30:00" },
                 "status": { "code": "onsale" } },
      "priceRanges": [ { "type": "standard", "currency": "SGD", "min": 108.0, "max": 488.0 } ],
      "classifications": [ { "segment": { "name": "Music" }, "genre": { "name": "Pop" } } ],
      "_embedded": { "venues": [ {
        "name": "Singapore National Stadium",
        "city": { "name": "Singapore" },
        "address": { "line1": "1 Stadium Drive" }
      } ] }
    }"#;

    // -- validation ---------------------------------------------------------

    #[test]
    fn country_code_validates() {
        assert_eq!(validate_country_code("vn").unwrap(), "VN");
        assert_eq!(validate_country_code(" US ").unwrap(), "US");
        for bad in ["", "V", "VNM", "V1", "12", "U S"] {
            assert!(
                validate_country_code(bad).is_err(),
                "expected error for {bad:?}"
            );
        }
    }

    #[test]
    fn date_validates() {
        assert!(validate_date("2026-10-18").is_ok());
        for bad in ["18-10-2026", "2026-13-01", "2026-02-30", "tomorrow", ""] {
            assert!(validate_date(bad).is_err(), "expected error for {bad:?}");
        }
    }

    #[test]
    fn sort_validates() {
        for ok in ["date,asc", "date,desc", "relevance"] {
            assert!(validate_sort(ok).is_ok());
        }
        for bad in ["date", "asc", "random", ""] {
            assert!(validate_sort(bad).is_err(), "expected error for {bad:?}");
        }
    }

    // -- parsing ------------------------------------------------------------

    #[test]
    fn search_fixture_parses() {
        let (total, events) = parse_search(SEARCH_FIXTURE).unwrap();
        assert_eq!(total, 128);
        assert_eq!(events.len(), 2);
        let e0 = &events[0];
        assert_eq!(e0["id"], "Z9vAm1f2abc3");
        assert_eq!(e0["name"], "Taylor Swift | The Eras Tour");
        assert_eq!(e0["date"], "2026-11-20");
        assert_eq!(e0["time"], "19:30:00");
        assert_eq!(e0["venue"], "Singapore National Stadium");
        assert_eq!(e0["city"], "Singapore");
        assert_eq!(e0["url"], "https://www.ticketmaster.com/event/abc");
        // Event with no venue data degrades gracefully.
        assert_eq!(events[1]["venue"], "");
        assert_eq!(events[1]["city"], "");
    }

    #[test]
    fn empty_results_are_not_an_error() {
        let (total, events) = parse_search(EMPTY_FIXTURE).unwrap();
        assert_eq!(total, 0);
        assert!(events.is_empty());
    }

    #[test]
    fn detail_fixture_parses() {
        let d = parse_details(DETAIL_FIXTURE).unwrap();
        assert_eq!(d["name"], "Taylor Swift | The Eras Tour");
        assert_eq!(d["venue"], "Singapore National Stadium");
        assert_eq!(d["address"], "1 Stadium Drive");
        assert_eq!(d["date"], "2026-11-20");
        assert_eq!(d["status"], "onsale");
        assert_eq!(d["url"], "https://www.ticketmaster.com/event/abc");
        assert_eq!(d["description"], "The Eras Tour comes to Singapore.");
        assert_eq!(d["price_ranges"][0]["currency"], "SGD");
        assert_eq!(d["price_ranges"][0]["min"], 108.0);
        assert_eq!(d["categories"][0]["segment"], "Music");
    }

    #[test]
    fn malformed_json_is_http_error() {
        assert!(parse_search("not json").is_err());
        assert!(parse_details("{").is_err());
    }

    // -- verify logic -------------------------------------------------------

    #[test]
    fn interpret_verify_maps_statuses() {
        assert!(interpret_verify(200).is_ok());
        let e401 = interpret_verify(401).unwrap_err();
        assert!(e401.contains("invalid") && e401.contains("API key"));
        let e403 = interpret_verify(403).unwrap_err();
        assert!(e403.contains("403"));
        let e429 = interpret_verify(429).unwrap_err();
        assert!(e429.contains("429"));
        let e500 = interpret_verify(500).unwrap_err();
        assert!(e500.contains("500"));
    }

    #[test]
    fn verify_url_keeps_key_out_of_plain_sight_shape() {
        let u = verify_url("abc+def/ghi");
        assert!(u.starts_with("https://app.ticketmaster.com/discovery/v2/events.json?"));
        // reqwest percent-encodes the key in the query string.
        assert!(u.contains("apikey=abc%2Bdef%2Fghi"));
        assert!(u.contains("size=1"));
    }

    // -- honest degradation -------------------------------------------------

    #[tokio::test]
    async fn search_without_connection_is_honest() {
        let key = KeyState::default(); // no key set
        let err = EventsSearch::new(key)
            .execute(serde_json::json!({"keyword": "concert"}))
            .await
            .unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("not connected"), "got: {msg}");
        assert!(msg.contains("developer.ticketmaster.com"), "got: {msg}");
    }

    #[tokio::test]
    async fn details_without_connection_is_honest() {
        let err = EventDetails::new(KeyState::default())
            .execute(serde_json::json!({"event_id": "Z9vAm1f2abc3"}))
            .await
            .unwrap_err();
        assert!(err.to_string().contains("not connected"));
    }

    #[tokio::test]
    async fn invalid_args_are_rejected_before_network() {
        let c = TicketmasterConnector {
            key: {
                let k = KeyState::default();
                k.set(Some("DUMMY".into()));
                k
            },
        };
        let mut reg = ToolRegistry::default();
        c.register_tools(&mut reg);

        // Bad country code → InvalidArgs, no HTTP attempted.
        let r = reg.dispatch("events_search", serde_json::json!({"country_code": "VNM"}));
        let v = r.await;
        assert!(v["error"].as_str().unwrap().contains("country_code"), "got: {v}");

        // Bad date → InvalidArgs.
        let v = reg
            .dispatch("events_search", serde_json::json!({"start_date": "next friday"}))
            .await;
        assert!(v["error"].as_str().unwrap().contains("start_date"), "got: {v}");

        // Missing event_id → InvalidArgs.
        let v = reg.dispatch("event_details", serde_json::json!({})).await;
        assert!(v["error"].as_str().unwrap().contains("event_id"), "got: {v}");

        // Path-traversal-ish event_id → InvalidArgs.
        let v = reg
            .dispatch("event_details", serde_json::json!({"event_id": "../x"}))
            .await;
        assert!(v["error"].as_str().unwrap().contains("invalid"), "got: {v}");
    }

    #[test]
    fn both_tools_registered() {
        let c = TicketmasterConnector::new();
        let mut reg = ToolRegistry::default();
        c.register_tools(&mut reg);
        let names = reg.tool_names();
        assert!(names.contains(&"events_search".to_string()));
        assert!(names.contains(&"event_details".to_string()));
        assert!(reg.toolset_names().contains(&"tickets".to_string()));
    }

    // -- schema sanity ------------------------------------------------------

    #[test]
    fn schemas_are_valid() {
        let search = EventsSearch::new(KeyState::default()).schema();
        assert_eq!(search["type"], "object");
        let props = search["properties"].as_object().unwrap();
        for p in ["keyword", "city", "country_code", "start_date", "size", "sort"] {
            assert!(props.contains_key(p), "missing property {p}");
        }
        let details = EventDetails::new(KeyState::default()).schema();
        assert_eq!(
            details["required"].as_array().unwrap()[0],
            "event_id"
        );
    }

    // -- security -----------------------------------------------------------

    #[test]
    fn debug_redacts_key() {
        let c = TicketmasterConnector {
            key: {
                let k = KeyState::default();
                k.set(Some("SECRET_KEY_VALUE_123".into()));
                k
            },
        };
        let dbg = format!("{c:?}");
        assert!(dbg.contains("connected"), "got: {dbg}");
        assert!(
            !dbg.contains("SECRET_KEY_VALUE_123"),
            "key leaked in Debug: {dbg}"
        );
        assert!(dbg.contains("<redacted>"));
    }

    #[test]
    fn connect_url_gives_instructions() {
        let c = TicketmasterConnector::new();
        let err = c.connect_url().unwrap_err();
        assert!(err.contains("developer.ticketmaster.com"));
        assert!(err.contains("Consumer Key"));
        assert_eq!(c.auth_kind(), "api_key");
        assert_eq!(c.id(), "ticketmaster");
    }

    // -- token file round-trip (temp BOOM_HOME) ------------------------------

    #[test]
    fn token_helpers_roundtrip() {
        let tmp = tempfile::tempdir().unwrap();
        let old = std::env::var("BOOM_HOME").ok();
        std::env::set_var("BOOM_HOME", tmp.path());
        let id = "ticketmaster-test";

        assert!(load_token(id).is_none());
        save_token(id, "TESTKEY").unwrap();
        assert_eq!(load_token(id).as_deref(), Some("TESTKEY"));

        // Mode 0600 on unix.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(token_path(id))
                .unwrap()
                .permissions()
                .mode()
                & 0o777;
            assert_eq!(mode, 0o600, "token file mode = {mode:o}");
        }

        delete_token(id).unwrap();
        assert!(load_token(id).is_none());
        delete_token(id).unwrap(); // idempotent

        match old {
            Some(v) => std::env::set_var("BOOM_HOME", v),
            None => std::env::remove_var("BOOM_HOME"),
        }
    }
}
