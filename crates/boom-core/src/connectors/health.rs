//! Health connector — activity metrics, sleep and workouts via Google Fit.
//!
//! **Honest scope.** Apple HealthKit exposes no public REST API and Android
//! Health Connect is an on-device store with no cloud REST API either — data
//! from those platforms can only be read by a natively paired app, which a
//! server-side connector cannot do. This connector therefore talks to the
//! **Google Fit REST API** when the user connects a Google account with
//! fitness scopes, and fails with a clear message otherwise.
//!
//! **Deprecation warning.** Google has deprecated the Fit APIs (no new
//! sign-ups since 2024-05-01) and supports them only **until the end of
//! 2026**; afterwards they stop working. Every tool response carries this
//! warning so nobody builds on it unknowingly. The recommended migration
//! path is on-device Health Connect pairing (out of scope for this
//! server-side connector).
//!
//! Auth is OAuth2 (authorization-code flow, same Google Cloud project style
//! as the `gcalendar` connector):
//!
//! * [`HealthConnector::connect_url`] builds the Google consent URL. The
//!   operator opens it in a browser; Google redirects to
//!   `http://localhost:18080/api/connectors/health/callback?code=…&state=…`.
//! * [`HealthConnector::exchange_code`] trades the `code` for tokens and
//!   persists them to `$BOOM_HOME/connectors/health.json` (0600).
//! * Access tokens are refreshed automatically — proactively when `expires_at`
//!   is near, and reactively on a `401` from the API.
//!
//! Client id/secret from `BOOM_HEALTH_CLIENT_ID` / `BOOM_HEALTH_CLIENT_SECRET`
//! (a Google Cloud OAuth client with the Fitness API scopes enabled).
//! Tokens are never logged, never embedded in error strings, and never
//! appear in `Debug` output.
//!
//! ## Merge note for the coordinator
//!
//! Drop this file at `crates/boom-core/src/connectors/health.rs`, declare
//! `pub mod health;` in `src/connectors/mod.rs`, and register
//! `HealthConnector::new()` in `ConnectorRegistry::new()` (also extend
//! `ALL_CONNECTOR_IDS`). It uses only the shared contract from the parent
//! module (`Connector`, `ConnectorToken`, `load_token`, `save_token`,
//! `delete_token`, `http_client`).

use std::fmt;
use std::time::Duration;

use async_trait::async_trait;
use serde_json::Value;

use super::{delete_token, http_client, load_token, save_token, Connector, ConnectorToken};
use crate::tools::{opt_str, schema_of, Tool, ToolError, ToolRegistry};

/// Stable connector id (also the token file name: `health.json`).
pub const CONNECTOR_ID: &str = "health";

const AUTH_URL: &str = "https://accounts.google.com/o/oauth2/v2/auth";
const TOKEN_URL: &str = "https://oauth2.googleapis.com/token";
const API_BASE: &str = "https://www.googleapis.com/fitness/v1";
const REDIRECT_URI: &str = "http://localhost:18080/api/connectors/health/callback";
const SCOPES: &str = "https://www.googleapis.com/auth/fitness.activity.read \
     https://www.googleapis.com/auth/fitness.body.read \
     https://www.googleapis.com/auth/fitness.sleep.read \
     https://www.googleapis.com/auth/fitness.location.read";
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);
/// Refresh the access token this far before it actually expires.
const REFRESH_SKEW_SECS: i64 = 300;

/// Deprecation notice attached to every tool response.
const DEPRECATION_NOTE: &str = "Google Fit REST is deprecated by Google and supported only until \
     the end of 2026. Apple HealthKit and Android Health Connect expose no cloud REST API — \
     reading them needs a natively paired app.";

const NOT_CONNECTED_MSG: &str =
    "health is not connected — connect a Google account with fitness scopes first \
     (Settings → Connectors → health). Apple HealthKit / Android Health Connect have no cloud \
     REST API, so on-device pairing is the only way to read them.";

const NO_CLIENT_MSG: &str = "Google OAuth is not configured: set BOOM_HEALTH_CLIENT_ID and \
     BOOM_HEALTH_CLIENT_SECRET (a Google Cloud OAuth client with the Fitness API scopes enabled), \
     then retry";

// ---------------------------------------------------------------------------
// Pure helpers (unit-testable, no I/O)
// ---------------------------------------------------------------------------

fn now_secs() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// True when the token should be refreshed before use.
fn token_needs_refresh(tok: &ConnectorToken) -> bool {
    match tok.expires_at {
        Some(exp) => exp <= now_secs() + REFRESH_SKEW_SECS,
        None => false,
    }
}

/// Build the Google OAuth2 consent URL. Pure: easy to test the exact shape.
fn build_auth_url(client_id: &str, state: &str) -> String {
    let mut url = reqwest::Url::parse(AUTH_URL).expect("hardcoded auth URL must parse");
    url.query_pairs_mut()
        .append_pair("client_id", client_id)
        .append_pair("redirect_uri", REDIRECT_URI)
        .append_pair("response_type", "code")
        .append_pair("scope", SCOPES)
        .append_pair("access_type", "offline")
        .append_pair("prompt", "consent")
        .append_pair("state", state);
    url.to_string()
}

/// OAuth client credentials from the environment (shared with the gcalendar
/// connector's Google Cloud project). Clear error when missing.
fn client_credentials() -> Result<(String, String), String> {
    let id = std::env::var("BOOM_HEALTH_CLIENT_ID")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());
    let secret = std::env::var("BOOM_HEALTH_CLIENT_SECRET")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());
    match (id, secret) {
        (Some(id), Some(secret)) => Ok((id, secret)),
        _ => Err(NO_CLIENT_MSG.to_string()),
    }
}

/// Human-readable error from a failed token-endpoint call.
/// Never includes tokens — only the status and Google's error detail.
fn token_endpoint_error(status: u16, data: &Value) -> String {
    let detail = match data.get("error") {
        Some(Value::String(s)) => {
            let desc = data
                .get("error_description")
                .and_then(Value::as_str)
                .unwrap_or("");
            if desc.is_empty() {
                s.clone()
            } else {
                format!("{s}: {desc}")
            }
        }
        _ => String::new(),
    };
    let detail: String = detail.chars().take(300).collect();
    format!(
        "google token request failed (HTTP {status}): {}",
        if detail.is_empty() {
            "no detail"
        } else {
            &detail
        }
    )
}

/// Parse a token-endpoint success body into a [`ConnectorToken`]. `old` is
/// the previous token (refresh token is preserved when the response omits a
/// new one).
fn parse_token_response(data: &Value, old: Option<&ConnectorToken>) -> Result<ConnectorToken, String> {
    let access = data
        .get("access_token")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "google token response did not contain an access_token".to_string())?;
    let refresh = data
        .get("refresh_token")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .or_else(|| {
            old.and_then(|o| o.refresh_token.clone())
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
        });
    let expires_at = data
        .get("expires_in")
        .and_then(Value::as_i64)
        .map(|s| now_secs() + s);
    Ok(ConnectorToken {
        access_token: access.to_string(),
        refresh_token: refresh,
        expires_at,
        extra: serde_json::json!({"scopes": SCOPES.replace('\n', " ")}),
    })
}

/// Validate a YYYY-MM-DD date arg.
fn validate_date(raw: &str, arg: &str) -> Result<chrono::NaiveDate, ToolError> {
    chrono::NaiveDate::parse_from_str(raw.trim(), "%Y-%m-%d").map_err(|_| {
        ToolError::InvalidArgs(format!("invalid {arg} '{raw}': expected YYYY-MM-DD"))
    })
}

/// Day boundaries in the server's local timezone, as epoch millis.
fn day_range_millis(date: chrono::NaiveDate) -> (i64, i64) {
    use chrono::TimeZone;
    let start = chrono::Local
        .from_local_datetime(&date.and_hms_opt(0, 0, 0).unwrap())
        .single()
        .map(|d| d.timestamp_millis())
        .unwrap_or_else(|| date.and_hms_opt(0, 0, 0).unwrap().and_utc().timestamp_millis());
    (start, start + 86_400_000)
}

/// Epoch nanos → RFC3339, empty string when missing/invalid.
fn nanos_to_rfc3339(v: &Value, key: &str) -> String {
    use chrono::TimeZone;
    v.get(key)
        .and_then(|x| x.as_str().and_then(|s| s.parse::<i64>().ok()).or_else(|| x.as_i64()))
        .and_then(|ns| chrono::Utc.timestamp_opt(ns / 1_000_000_000, (ns % 1_000_000_000) as u32).single())
        .map(|dt| dt.to_rfc3339())
        .unwrap_or_default()
}

/// Google Fit sleep-segment stage codes.
fn sleep_stage(code: i64) -> &'static str {
    match code {
        1 => "awake",
        2 => "sleep",
        3 => "out_of_bed",
        4 => "light",
        5 => "deep",
        6 => "rem",
        _ => "unknown",
    }
}

/// Common Google Fit activity-type codes (best effort; unknown codes are
/// returned as `activity_{n}`).
fn activity_name(code: i64) -> String {
    match code {
        1 => "biking",
        7 => "walking",
        8 => "running",
        9 => "aerobics",
        10 => "badminton",
        11 => "baseball",
        16 => "elliptical",
        25 => "dancing",
        35 => "hiking",
        56 => "rowing",
        57 => "sailing",
        66 => "swimming",
        72 => "tennis",
        82 => "workout",
        97 => "weightlifting",
        102 => "yoga",
        108 => "soccer",
        n => return format!("activity_{n}"),
    }
    .to_string()
}

/// Metric descriptor for `dataset:aggregate`.
struct MetricDef {
    key: &'static str,
    data_type: &'static str,
    unit: &'static str,
}

const METRICS: &[MetricDef] = &[
    MetricDef { key: "steps", data_type: "com.google.step_count.delta", unit: "steps" },
    MetricDef { key: "heart_rate", data_type: "com.google.heart_rate.bpm", unit: "bpm" },
    MetricDef { key: "calories", data_type: "com.google.calories.expended", unit: "kcal" },
    MetricDef { key: "distance", data_type: "com.google.distance.delta", unit: "m" },
    MetricDef { key: "active_minutes", data_type: "com.google.active_minutes", unit: "min" },
];

fn metric_def(key: &str) -> Option<&'static MetricDef> {
    METRICS.iter().find(|m| m.key == key)
}

/// Build the `dataset:aggregate` request body. Pure.
fn build_aggregate_body(metrics: &[&MetricDef], start_ms: i64, end_ms: i64) -> Value {
    let aggregate_by: Vec<Value> = metrics
        .iter()
        .map(|m| serde_json::json!({ "dataTypeName": m.data_type }))
        .collect();
    serde_json::json!({
        "aggregateBy": aggregate_by,
        "bucketByTime": { "durationMillis": 86_400_000 },
        "startTimeMillis": start_ms,
        "endTimeMillis": end_ms,
    })
}

/// Summarize one aggregate response bucket for a metric. Pure.
fn summarize_metric(def: &MetricDef, body: &Value) -> Value {
    let mut int_sum: i64 = 0;
    let mut fp_sum: f64 = 0.0;
    let mut fp_vals: Vec<f64> = Vec::new();
    let mut points = 0u64;
    if let Some(bucket) = body.get("bucket").and_then(Value::as_array).and_then(|b| b.first()) {
        if let Some(datasets) = bucket.get("dataset").and_then(Value::as_array) {
            for ds in datasets {
                let ds_id = ds
                    .get("dataSourceId")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                if !ds_id.contains(def.data_type) {
                    continue;
                }
                if let Some(arr) = ds.get("point").and_then(Value::as_array) {
                    for p in arr {
                        points += 1;
                        if let Some(vals) = p.get("value").and_then(Value::as_array) {
                            for val in vals {
                                if let Some(i) = val.get("intVal").and_then(Value::as_i64) {
                                    int_sum += i;
                                }
                                if let Some(f) = val.get("fpVal").and_then(Value::as_f64) {
                                    fp_sum += f;
                                    fp_vals.push(f);
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    let mut out = serde_json::json!({
        "metric": def.key,
        "unit": def.unit,
        "data_points": points,
    });
    match def.key {
        "heart_rate" => {
            // Fit aggregates bpm as [average, max, min] fpVals.
            out["avg"] = fp_vals.first().copied().map(Value::from).unwrap_or(Value::Null);
            out["max"] = fp_vals.get(1).copied().map(Value::from).unwrap_or(Value::Null);
            out["min"] = fp_vals.get(2).copied().map(Value::from).unwrap_or(Value::Null);
        }
        "steps" | "active_minutes" => {
            out["total"] = Value::from(int_sum);
        }
        _ => {
            out["total"] = Value::from((fp_sum * 100.0).round() / 100.0);
        }
    }
    out
}

/// Summarize a sleep-segment aggregate body into per-stage minutes plus the
/// raw segments. Pure.
fn summarize_sleep(body: &Value) -> Value {
    let mut minutes: std::collections::BTreeMap<String, f64> = std::collections::BTreeMap::new();
    let mut segments: Vec<Value> = Vec::new();
    if let Some(bucket) = body.get("bucket").and_then(Value::as_array).and_then(|b| b.first()) {
        if let Some(datasets) = bucket.get("dataset").and_then(Value::as_array) {
            for ds in datasets {
                let ds_id = ds.get("dataSourceId").and_then(Value::as_str).unwrap_or("");
                if !ds_id.contains("com.google.sleep.segment") {
                    continue;
                }
                if let Some(arr) = ds.get("point").and_then(Value::as_array) {
                    for p in arr {
                        let start_ns = p
                            .get("startTimeNanos")
                            .and_then(|x| x.as_str().and_then(|s| s.parse::<i64>().ok()).or_else(|| x.as_i64()))
                            .unwrap_or(0);
                        let end_ns = p
                            .get("endTimeNanos")
                            .and_then(|x| x.as_str().and_then(|s| s.parse::<i64>().ok()).or_else(|| x.as_i64()))
                            .unwrap_or(0);
                        let stage_code = p
                            .get("value")
                            .and_then(Value::as_array)
                            .and_then(|a| a.first())
                            .and_then(|v| v.get("intVal"))
                            .and_then(Value::as_i64)
                            .unwrap_or(0);
                        let mins = ((end_ns - start_ns).max(0) as f64) / 60_000_000_000.0;
                        let stage = sleep_stage(stage_code).to_string();
                        *minutes.entry(stage.clone()).or_insert(0.0) += mins;
                        if segments.len() < 200 {
                            segments.push(serde_json::json!({
                                "start": nanos_to_rfc3339(p, "startTimeNanos"),
                                "end": nanos_to_rfc3339(p, "endTimeNanos"),
                                "stage": stage,
                                "minutes": (mins * 10.0).round() / 10.0,
                            }));
                        }
                    }
                }
            }
        }
    }
    let minutes: std::collections::BTreeMap<String, Value> = minutes
        .into_iter()
        .map(|(k, v)| (k, Value::from((v * 10.0).round() / 10.0)))
        .collect();
    serde_json::json!({ "minutes_by_stage": minutes, "segments": segments })
}

/// One millis-since-epoch field (Fit returns these as strings or numbers).
fn ms_field(v: &Value, key: &str) -> i64 {
    v.get(key)
        .and_then(|x| {
            x.as_str()
                .and_then(|s| s.parse::<i64>().ok())
                .or_else(|| x.as_i64())
        })
        .unwrap_or(0)
}

/// Summarize a Fit `sessions/list` body. Pure.
fn summarize_sessions(body: &Value) -> Vec<Value> {
    body.get("session")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .map(|s| {
                    let start_ms = ms_field(s, "startTimeMillis");
                    let end_ms = ms_field(s, "endTimeMillis");
                    let activity = s.get("activityType").and_then(Value::as_i64).unwrap_or(-1);
                    serde_json::json!({
                        "name": s.get("name").and_then(Value::as_str).unwrap_or(""),
                        "activity": activity_name(activity),
                        "activity_type": activity,
                        "start": rfc3339_ms(start_ms),
                        "end": rfc3339_ms(end_ms),
                        "duration_min": ((end_ms - start_ms).max(0) as f64 / 60_000.0).round() as i64,
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

// ---------------------------------------------------------------------------
// Connector
// ---------------------------------------------------------------------------

/// Health connector (Google OAuth2 → Google Fit REST).
///
/// Holds no secret: the token file is the source of truth and is loaded
/// fresh on every call, so `exchange_code`/`disconnect` take effect
/// immediately for all registered tools.
#[derive(Clone)]
pub struct HealthConnector {
    api_base: String,
    token_url: String,
}

impl HealthConnector {
    /// Production constructor: real Google endpoints.
    pub fn new() -> Self {
        Self::with_endpoints(API_BASE, TOKEN_URL)
    }

    /// Constructor with explicit endpoints — tests drive local mock servers
    /// through this.
    pub fn with_endpoints(api_base: &str, token_url: &str) -> Self {
        Self {
            api_base: api_base.trim_end_matches('/').to_string(),
            token_url: token_url.to_string(),
        }
    }

    fn api_base(&self) -> &str {
        &self.api_base
    }
    fn token_url(&self) -> &str {
        &self.token_url
    }

    // -- token plumbing ----------------------------------------------------

    fn load_stored(&self) -> Option<ConnectorToken> {
        load_token(CONNECTOR_ID)
    }

    /// Exchange one refresh token for a new token pair. Returns `None` on
    /// any failure — callers fall back to a clear "reconnect" error.
    /// Persists on success.
    async fn refresh_now(&self) -> Option<ConnectorToken> {
        let old = self.load_stored()?;
        let rt = old
            .refresh_token
            .as_ref()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())?;
        let (client_id, client_secret) = client_credentials().ok()?;
        let resp = http_client(REQUEST_TIMEOUT)
            .post(self.token_url())
            .form(&[
                ("grant_type", "refresh_token"),
                ("refresh_token", rt.as_str()),
                ("client_id", client_id.as_str()),
                ("client_secret", client_secret.as_str()),
            ])
            .send()
            .await
            .ok()?;
        if !resp.status().is_success() {
            return None;
        }
        let data: Value = resp.json().await.ok()?;
        let tok = parse_token_response(&data, Some(&old)).ok()?;
        save_token(CONNECTOR_ID, &tok).ok()?;
        Some(tok)
    }

    /// A usable access token: stored one, refreshed proactively when near
    /// expiry. Fails honestly when not connected or when refresh fails.
    async fn ensure_token(&self) -> Result<ConnectorToken, ToolError> {
        let tok = self
            .load_stored()
            .filter(|t| t.is_present())
            .ok_or_else(|| ToolError::Other(NOT_CONNECTED_MSG.to_string()))?;
        if token_needs_refresh(&tok) {
            match self.refresh_now().await {
                Some(fresh) => Ok(fresh),
                None => Err(ToolError::Other(
                    "google access token expired and the refresh failed (the stored refresh \
                     token may be revoked) — reconnect the health connector"
                        .to_string(),
                )),
            }
        } else {
            Ok(tok)
        }
    }

    // -- authed HTTP ---------------------------------------------------------

    /// One completed Fit API round-trip.
    async fn raw_request(
        &self,
        method: &str,
        path: &str,
        access_token: &str,
        json: Option<&Value>,
    ) -> Result<ApiResp, ToolError> {
        let url = format!("{}{}", self.api_base(), path);
        let mut req = match method {
            "POST" => http_client(REQUEST_TIMEOUT).post(&url),
            _ => http_client(REQUEST_TIMEOUT).get(&url),
        };
        req = req.bearer_auth(access_token);
        if let Some(body) = json {
            req = req.json(body);
        }
        let resp = req.send().await.map_err(transport_error)?;
        let status = resp.status().as_u16();
        let text = resp
            .text()
            .await
            .map_err(|_| ToolError::Http("failed to read Google Fit response body".to_string()))?;
        let body = serde_json::from_str::<Value>(text.trim()).unwrap_or(Value::Null);
        Ok(ApiResp { status, body })
    }

    /// GET with bearer auth; on 401, refresh once and retry. Never logs the
    /// token.
    async fn authed_get(&self, path: &str) -> Result<ApiResp, ToolError> {
        let tok = self.ensure_token().await?;
        let resp = self.raw_request("GET", path, &tok.access_token, None).await?;
        if resp.status == 401 {
            if let Some(fresh) = self.refresh_now().await {
                return self.raw_request("GET", path, &fresh.access_token, None).await;
            }
        }
        Ok(resp)
    }

    /// POST with bearer auth; on 401, refresh once and retry.
    async fn authed_post(&self, path: &str, json: &Value) -> Result<ApiResp, ToolError> {
        let tok = self.ensure_token().await?;
        let resp = self
            .raw_request("POST", path, &tok.access_token, Some(json))
            .await?;
        if resp.status == 401 {
            if let Some(fresh) = self.refresh_now().await {
                return self
                    .raw_request("POST", path, &fresh.access_token, Some(json))
                    .await;
            }
        }
        Ok(resp)
    }
}

impl Default for HealthConnector {
    fn default() -> Self {
        Self::new()
    }
}

/// Never print tokens in logs or Debug output.
impl fmt::Debug for HealthConnector {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("HealthConnector")
            .field("api_base", &self.api_base)
            .field("connected", &self.is_connected())
            .field("tokens", &"<redacted>")
            .finish()
    }
}

#[async_trait]
impl Connector for HealthConnector {
    fn id(&self) -> &'static str {
        CONNECTOR_ID
    }
    fn name(&self) -> &'static str {
        "Health"
    }
    fn description(&self) -> &'static str {
        "Health data via the Google Fit REST API (steps, heart rate, \
         calories, distance, active minutes, sleep, workouts). NOTE: the Fit \
         API is deprecated by Google (supported until end of 2026); Apple \
         HealthKit and Android Health Connect expose no cloud REST API. \
         OAuth2 sign-in with a Google account."
    }
    fn auth_kind(&self) -> &'static str {
        "oauth2"
    }
    fn connect_url(&self) -> Result<String, String> {
        let (client_id, _secret) = client_credentials()?;
        let state = format!("boom-{}", now_secs());
        Ok(build_auth_url(&client_id, &state))
    }
    async fn exchange_code(&mut self, code: &str) -> Result<(), String> {
        let code = code.trim();
        if code.is_empty() {
            return Err("empty authorization code".to_string());
        }
        let (client_id, client_secret) = client_credentials()?;
        let resp = http_client(REQUEST_TIMEOUT)
            .post(self.token_url())
            .form(&[
                ("grant_type", "authorization_code"),
                ("code", code),
                ("redirect_uri", REDIRECT_URI),
                ("client_id", client_id.as_str()),
                ("client_secret", client_secret.as_str()),
            ])
            .send()
            .await
            .map_err(|e| {
                if e.is_timeout() {
                    "connection to Google timed out".to_string()
                } else {
                    "could not reach the Google token endpoint".to_string()
                }
            })?;
        let status = resp.status().as_u16();
        let data: Value = resp.json().await.unwrap_or(Value::Null);
        if !(200..300).contains(&status) {
            return Err(token_endpoint_error(status, &data));
        }
        let tok = parse_token_response(&data, None)?;
        save_token(CONNECTOR_ID, &tok)?;
        Ok(())
    }
    fn disconnect(&mut self) -> Result<(), String> {
        delete_token(CONNECTOR_ID)
    }
    fn register_tools(&self, reg: &mut ToolRegistry) {
        reg.register("health", MetricsTool::new(self.clone()));
        reg.register("health", SleepTool::new(self.clone()));
        reg.register("health", WorkoutsTool::new(self.clone()));
    }
}

// ---------------------------------------------------------------------------
// Shared API plumbing
// ---------------------------------------------------------------------------

/// One completed Google Fit API round-trip.
struct ApiResp {
    status: u16,
    body: Value,
}

/// Generic transport failure message. Deliberately generic: reqwest errors
/// can echo request details, and we never want tokens in an error.
fn transport_error(err: reqwest::Error) -> ToolError {
    if err.is_timeout() {
        ToolError::Timeout(REQUEST_TIMEOUT.as_secs())
    } else if err.is_connect() {
        ToolError::Http("could not reach the Google Fit API (connection failed)".to_string())
    } else {
        ToolError::Http("Google Fit request failed".to_string())
    }
}

/// Map a non-2xx Fit response to a `ToolError`. Nothing here ever contains a
/// token. A 410 Gone is Google's deprecation signal — surface it honestly.
fn check_status(resp: &ApiResp, what: &str) -> Result<(), ToolError> {
    if (200..300).contains(&resp.status) {
        return Ok(());
    }
    let msg = resp
        .body
        .get("error")
        .and_then(|e| e.get("message"))
        .and_then(Value::as_str)
        .map(|s| s.trim().chars().take(300).collect::<String>())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "no detail from Google Fit".to_string());
    match resp.status {
        401 => Err(ToolError::Other(format!(
            "Google Fit rejected the access token (401 Unauthorized): {msg} — reconnect the \
             health connector"
        ))),
        403 => Err(ToolError::Other(format!(
            "Google Fit refused the {what} request (403 Forbidden): {msg} — the Google account \
             may not have granted the fitness scopes"
        ))),
        404 | 410 => Err(ToolError::Other(format!(
            "Google Fit {what} failed (HTTP {}): {msg} — the Fit API is deprecated by Google \
             (supported until end of 2026); it may already be unavailable for this account",
            resp.status
        ))),
        s => Err(ToolError::Http(format!(
            "Google Fit {what} returned HTTP {s}: {msg}"
        ))),
    }
}

/// Attach the deprecation notice to a successful tool result.
fn with_note(mut v: Value) -> Value {
    if let Some(obj) = v.as_object_mut() {
        obj.insert("note".to_string(), Value::String(DEPRECATION_NOTE.to_string()));
    }
    v
}

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

/// `health_metrics` — `POST /users/me/dataset:aggregate`.
pub struct MetricsTool {
    conn: HealthConnector,
}

impl MetricsTool {
    pub fn new(conn: HealthConnector) -> Self {
        Self { conn }
    }
}

#[async_trait]
impl Tool for MetricsTool {
    fn name(&self) -> &str {
        "health_metrics"
    }
    fn description(&self) -> &str {
        "Daily health metrics from Google Fit: steps, heart_rate (avg/max/min \
         bpm), calories, distance, active_minutes. Day boundaries use the \
         server's local timezone."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                (
                    "date",
                    "string",
                    "Day YYYY-MM-DD (default: today)",
                ),
                (
                    "metrics",
                    "string",
                    "Comma-separated subset of: steps, heart_rate, calories, distance, \
                     active_minutes (default: steps,heart_rate)",
                ),
            ],
            &[],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let date = match opt_str(&args, "date", "").trim() {
            "" => chrono::Local::now().date_naive(),
            s => validate_date(s, "date")?,
        };
        let mut defs: Vec<&MetricDef> = Vec::new();
        for key in opt_str(&args, "metrics", "steps,heart_rate").split(',') {
            let key = key.trim().to_lowercase();
            if key.is_empty() {
                continue;
            }
            match metric_def(&key) {
                Some(d) => {
                    if !defs.iter().any(|e| e.key == d.key) {
                        defs.push(d);
                    }
                }
                None => {
                    return Err(ToolError::InvalidArgs(format!(
                        "invalid metric '{key}': expected one of {}",
                        METRICS.iter().map(|m| m.key).collect::<Vec<_>>().join(", ")
                    )))
                }
            }
        }
        if defs.is_empty() {
            return Err(ToolError::InvalidArgs(
                "metrics must name at least one metric".to_string(),
            ));
        }
        let (start_ms, end_ms) = day_range_millis(date);
        let body = build_aggregate_body(&defs, start_ms, end_ms);
        let resp = self
            .conn
            .authed_post("/users/me/dataset:aggregate", &body)
            .await?;
        check_status(&resp, "aggregate metrics")?;
        let metrics: Vec<Value> = defs.iter().map(|d| summarize_metric(d, &resp.body)).collect();
        Ok(with_note(serde_json::json!({
            "date": date.format("%Y-%m-%d").to_string(),
            "metrics": metrics,
        })))
    }
}

/// `health_sleep` — `POST /users/me/dataset:aggregate` (sleep segments).
pub struct SleepTool {
    conn: HealthConnector,
}

impl SleepTool {
    pub fn new(conn: HealthConnector) -> Self {
        Self { conn }
    }
}

#[async_trait]
impl Tool for SleepTool {
    fn name(&self) -> &str {
        "health_sleep"
    }
    fn description(&self) -> &str {
        "Sleep segments from Google Fit for a date range (minutes per stage: \
         awake, light, deep, rem, …). Defaults to last night."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                (
                    "start_date",
                    "string",
                    "Range start YYYY-MM-DD (default: yesterday)",
                ),
                (
                    "end_date",
                    "string",
                    "Range end YYYY-MM-DD (default: today)",
                ),
            ],
            &[],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let today = chrono::Local::now().date_naive();
        let end = match opt_str(&args, "end_date", "").trim() {
            "" => today,
            s => validate_date(s, "end_date")?,
        };
        let start = match opt_str(&args, "start_date", "").trim() {
            "" => end - chrono::Duration::days(1),
            s => validate_date(s, "start_date")?,
        };
        if start > end {
            return Err(ToolError::InvalidArgs(
                "start_date must not be after end_date".to_string(),
            ));
        }
        let (start_ms, _) = day_range_millis(start);
        let (_, end_ms) = day_range_millis(end);
        let body = serde_json::json!({
            "aggregateBy": [{ "dataTypeName": "com.google.sleep.segment" }],
            "bucketByTime": { "durationMillis": (end_ms - start_ms).max(86_400_000) },
            "startTimeMillis": start_ms,
            "endTimeMillis": end_ms,
        });
        let resp = self
            .conn
            .authed_post("/users/me/dataset:aggregate", &body)
            .await?;
        check_status(&resp, "aggregate sleep")?;
        let mut out = summarize_sleep(&resp.body);
        out["start_date"] = Value::String(start.format("%Y-%m-%d").to_string());
        out["end_date"] = Value::String(end.format("%Y-%m-%d").to_string());
        Ok(with_note(out))
    }
}

/// `health_workouts` — `GET /users/me/sessions`.
pub struct WorkoutsTool {
    conn: HealthConnector,
}

impl WorkoutsTool {
    pub fn new(conn: HealthConnector) -> Self {
        Self { conn }
    }
}

#[async_trait]
impl Tool for WorkoutsTool {
    fn name(&self) -> &str {
        "health_workouts"
    }
    fn description(&self) -> &str {
        "Workout sessions from Google Fit for a date range (name, activity, \
         start/end, duration). Defaults to the last 7 days."
    }
    fn schema(&self) -> Value {
        schema_of(
            &[
                (
                    "start_date",
                    "string",
                    "Range start YYYY-MM-DD (default: 7 days ago)",
                ),
                (
                    "end_date",
                    "string",
                    "Range end YYYY-MM-DD (default: today)",
                ),
            ],
            &[],
        )
    }
    async fn execute(&self, args: Value) -> Result<Value, ToolError> {
        let today = chrono::Local::now().date_naive();
        let end = match opt_str(&args, "end_date", "").trim() {
            "" => today,
            s => validate_date(s, "end_date")?,
        };
        let start = match opt_str(&args, "start_date", "").trim() {
            "" => end - chrono::Duration::days(7),
            s => validate_date(s, "start_date")?,
        };
        if start > end {
            return Err(ToolError::InvalidArgs(
                "start_date must not be after end_date".to_string(),
            ));
        }
        let (start_ms, _) = day_range_millis(start);
        let (_, end_ms) = day_range_millis(end);
        let path = format!(
            "/users/me/sessions?startTime={}&endTime={}",
            rfc3339_ms(start_ms),
            rfc3339_ms(end_ms)
        );
        let resp = self.conn.authed_get(&path).await?;
        check_status(&resp, "list sessions")?;
        let sessions = summarize_sessions(&resp.body);
        Ok(with_note(serde_json::json!({
            "start_date": start.format("%Y-%m-%d").to_string(),
            "end_date": end.format("%Y-%m-%d").to_string(),
            "sessions": sessions,
            "count": sessions.len(),
        })))
    }
}

/// Epoch millis → RFC3339 UTC.
fn rfc3339_ms(ms: i64) -> String {
    use chrono::TimeZone;
    chrono::Utc
        .timestamp_opt(ms / 1000, 0)
        .single()
        .map(|d| d.to_rfc3339())
        .unwrap_or_default()
}

// ---------------------------------------------------------------------------
// Tests (all offline — fixtures + validation + honest degradation + mock API)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    struct TestHome {
        _lock: tokio::sync::MutexGuard<'static, ()>,
        _dir: tempfile::TempDir,
        old: Option<String>,
        old_client_id: Option<String>,
        old_client_secret: Option<String>,
    }

    impl TestHome {
        async fn new() -> Self {
            let lock = crate::connectors::TEST_ENV_LOCK.lock().await;
            let dir = tempfile::tempdir().unwrap();
            let old = std::env::var("BOOM_HOME").ok();
            std::env::set_var("BOOM_HOME", dir.path());
            let old_client_id = std::env::var("BOOM_HEALTH_CLIENT_ID").ok();
            let old_client_secret = std::env::var("BOOM_HEALTH_CLIENT_SECRET").ok();
            std::env::set_var("BOOM_HEALTH_CLIENT_ID", "test-google-id");
            std::env::set_var("BOOM_HEALTH_CLIENT_SECRET", "test-google-secret");
            Self {
                _lock: lock,
                _dir: dir,
                old,
                old_client_id,
                old_client_secret,
            }
        }
    }

    impl Drop for TestHome {
        fn drop(&mut self) {
            match self.old.take() {
                Some(v) => std::env::set_var("BOOM_HOME", v),
                None => std::env::remove_var("BOOM_HOME"),
            }
            match self.old_client_id.take() {
                Some(v) => std::env::set_var("BOOM_HEALTH_CLIENT_ID", v),
                None => std::env::remove_var("BOOM_HEALTH_CLIENT_ID"),
            }
            match self.old_client_secret.take() {
                Some(v) => std::env::set_var("BOOM_HEALTH_CLIENT_SECRET", v),
                None => std::env::remove_var("BOOM_HEALTH_CLIENT_SECRET"),
            }
        }
    }

    /// Raw-TCP mock serving a sequence of canned responses, capturing raw
    /// requests.
    async fn mock_seq(
        seq: Vec<(u16, &'static str)>,
    ) -> (String, tokio::task::JoinHandle<Vec<String>>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind mock server");
        let addr = listener.local_addr().expect("local addr");
        let handle = tokio::spawn(async move {
            use tokio::io::{AsyncReadExt, AsyncWriteExt};
            let mut requests = Vec::new();
            for (status, body) in seq {
                let (mut sock, _) = listener.accept().await.expect("accept");
                let mut req: Vec<u8> = Vec::new();
                let mut buf = vec![0u8; 8192];
                let mut content_len = 0usize;
                loop {
                    let n = sock.read(&mut buf).await.expect("read");
                    if n == 0 {
                        break;
                    }
                    req.extend_from_slice(&buf[..n]);
                    if let Some(pos) = req.windows(4).position(|w| w == b"\r\n\r\n") {
                        let head = String::from_utf8_lossy(&req[..pos + 4]);
                        content_len = head
                            .lines()
                            .find_map(|l| {
                                let (k, v) = l.split_once(':')?;
                                k.trim()
                                    .eq_ignore_ascii_case("content-length")
                                    .then(|| v.trim().parse::<usize>().ok())
                                    .flatten()
                            })
                            .unwrap_or(0);
                        if req.len() - (pos + 4) >= content_len {
                            break;
                        }
                    }
                }
                let _ = content_len;
                requests.push(String::from_utf8_lossy(&req).into_owned());
                let reason = match status {
                    200 => "OK",
                    400 => "Bad Request",
                    401 => "Unauthorized",
                    403 => "Forbidden",
                    404 => "Not Found",
                    410 => "Gone",
                    _ => "Error",
                };
                let resp = format!(
                    "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n\
                     Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                sock.write_all(resp.as_bytes()).await.expect("write");
            }
            requests
        });
        (format!("http://{addr}"), handle)
    }

    async fn save_test_token() -> TestHome {
        let home = TestHome::new().await;
        save_token(
            CONNECTOR_ID,
            &ConnectorToken {
                access_token: "fit-access-1".to_string(),
                refresh_token: Some("fit-refresh-1".to_string()),
                expires_at: Some(now_secs() + 3600),
                extra: serde_json::json!({}),
            },
        )
        .unwrap();
        home
    }

    const TOKEN_FIXTURE: &str = r#"{"access_token":"fit-access-2","token_type":"Bearer",
        "expires_in":3600,"refresh_token":"fit-refresh-2"}"#;

    const AGG_STEPS_FIXTURE: &str = r#"{"bucket":[{"startTimeMillis":"1760000000000",
      "endTimeMillis":"1760086400000","dataset":[
      {"dataSourceId":"derived:com.google.step_count.delta:com.google.android.gms:estimated_steps",
       "point":[{"startTimeNanos":"1760000000000000000","endTimeNanos":"1760086400000000000",
                 "dataTypeName":"com.google.step_count.delta","value":[{"intVal":8432}]}]},
      {"dataSourceId":"derived:com.google.heart_rate.bpm:com.google.android.gms:resting",
       "point":[{"startTimeNanos":"1760000000000000000","endTimeNanos":"1760086400000000000",
                 "dataTypeName":"com.google.heart_rate.bpm",
                 "value":[{"fpVal":72.5},{"fpVal":121.0},{"fpVal":58.0}]}]}
    ]}]}"#;

    const AGG_SLEEP_FIXTURE: &str = r#"{"bucket":[{"startTimeMillis":"1760000000000",
      "endTimeMillis":"1760086400000","dataset":[
      {"dataSourceId":"derived:com.google.sleep.segment:com.google.android.gms:merged",
       "point":[
        {"startTimeNanos":"1760030000000000000","endTimeNanos":"1760033600000000000",
         "dataTypeName":"com.google.sleep.segment","value":[{"intVal":4}]},
        {"startTimeNanos":"1760033600000000000","endTimeNanos":"1760040800000000000",
         "dataTypeName":"com.google.sleep.segment","value":[{"intVal":5}]}
      ]}
    ]}]}"#;

    const SESSIONS_FIXTURE: &str = r#"{"session":[
      {"id":"s1","name":"Morning Run","activityType":8,
       "startTimeMillis":"1760000000000","endTimeMillis":"1760002400000"},
      {"id":"s2","name":"Evening Walk","activityType":7,
       "startTimeMillis":"1760040000000","endTimeMillis":"1760041800000"}
    ]}"#;

    #[test]
    fn connector_identity() {
        let c = HealthConnector::new();
        assert_eq!(c.id(), "health");
        assert_eq!(c.name(), "Health");
        assert_eq!(c.auth_kind(), "oauth2");
        assert!(c.description().contains("deprecated"), "got: {}", c.description());
    }

    #[test]
    fn auth_url_shape() {
        let url = build_auth_url("cid", "st");
        assert!(url.starts_with("https://accounts.google.com/o/oauth2/v2/auth?"));
        for part in [
            "client_id=cid",
            "redirect_uri=",
            "response_type=code",
            "scope=",
            "access_type=offline",
            "state=st",
        ] {
            assert!(url.contains(part), "missing {part} in {url}");
        }
        assert!(url.contains("fitness.activity.read"));
    }

    #[tokio::test]
    async fn connect_url_needs_client_config() {
        let _home = TestHome::new().await;
        let url = HealthConnector::new().connect_url().unwrap();
        assert!(url.contains("client_id=test-google-id"));
        assert!(!url.contains("test-google-secret"), "secret leaked: {url}");

        std::env::remove_var("BOOM_HEALTH_CLIENT_ID");
        let err = HealthConnector::new().connect_url().unwrap_err();
        assert!(err.contains("BOOM_HEALTH_CLIENT_ID"), "got: {err}");
    }

    #[test]
    fn schemas_are_valid() {
        let mut reg = ToolRegistry::default();
        HealthConnector::new().register_tools(&mut reg);
        for name in ["health_metrics", "health_sleep", "health_workouts"] {
            let tool = reg.get(name).unwrap_or_else(|| panic!("tool '{name}' missing"));
            assert!(!tool.description().is_empty());
            let schema = tool.schema();
            let obj = schema.as_object().expect("schema is object");
            assert_eq!(obj.get("type").and_then(|t| t.as_str()), Some("object"));
        }
        assert_eq!(reg.tools_in_toolset("health").len(), 3);
    }

    #[tokio::test]
    async fn token_roundtrip_and_0600() {
        let _home = TestHome::new().await;
        assert!(!HealthConnector::new().is_connected());
        let tok = ConnectorToken {
            access_token: "a".to_string(),
            refresh_token: Some("r".to_string()),
            expires_at: Some(now_secs() + 3600),
            extra: serde_json::json!({}),
        };
        save_token(CONNECTOR_ID, &tok).unwrap();
        assert!(HealthConnector::new().is_connected());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(super::super::token_path(CONNECTOR_ID))
                .unwrap()
                .permissions()
                .mode()
                & 0o777;
            assert_eq!(mode, 0o600, "token file mode = {mode:o}");
        }
        delete_token(CONNECTOR_ID).unwrap();
        assert!(!HealthConnector::new().is_connected());
        delete_token(CONNECTOR_ID).unwrap(); // idempotent
    }

    #[tokio::test]
    async fn exchange_code_ok_stores_tokens() {
        let _home = TestHome::new().await;
        let (token_base, handle) = mock_seq(vec![(200, TOKEN_FIXTURE)]).await;
        let mut c = HealthConnector::with_endpoints(
            "http://127.0.0.1:9",
            &format!("{token_base}/token"),
        );
        c.exchange_code("auth-code-xyz").await.unwrap();
        assert!(c.is_connected());
        let tok = load_token(CONNECTOR_ID).unwrap();
        assert_eq!(tok.access_token, "fit-access-2");
        assert_eq!(tok.refresh_token.as_deref(), Some("fit-refresh-2"));

        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("POST /token "), "request: {raw}");
        assert!(raw.contains("grant_type=authorization_code"), "body: {raw}");

        let dbg = format!("{:?}", c);
        assert!(!dbg.contains("fit-access-2"), "token leaked: {dbg}");
        assert!(dbg.contains("<redacted>"));
    }

    #[tokio::test]
    async fn exchange_code_rejects_bad_code() {
        let _home = TestHome::new().await;
        let (token_base, _h) = mock_seq(vec![(
            400,
            r#"{"error":"invalid_grant","error_description":"bad code"}"#,
        )])
        .await;
        let mut c = HealthConnector::with_endpoints(
            "http://127.0.0.1:9",
            &format!("{token_base}/token"),
        );
        let err = c.exchange_code("bad-code").await.unwrap_err();
        assert!(err.contains("invalid_grant"), "got: {err}");
        assert!(!c.is_connected());
    }

    #[tokio::test]
    async fn not_connected_is_honest() {
        let _home = TestHome::new().await;
        let tool = MetricsTool::new(HealthConnector::new());
        let err = tool.execute(serde_json::json!({})).await.unwrap_err();
        assert!(err.to_string().contains("not connected"), "got: {err}");
        assert!(err.to_string().contains("HealthKit"), "got: {err}");
    }

    #[tokio::test]
    async fn metrics_aggregates_steps_and_heart_rate() {
        let _home = save_test_token().await;
        let (api_base, handle) = mock_seq(vec![(200, AGG_STEPS_FIXTURE)]).await;
        let c = HealthConnector::with_endpoints(&api_base, "http://127.0.0.1:9");
        let tool = MetricsTool::new(c);
        let out = tool
            .execute(serde_json::json!({"date": "2026-10-09"}))
            .await
            .unwrap();
        assert_eq!(out["date"], "2026-10-09");
        let metrics = out["metrics"].as_array().unwrap();
        assert_eq!(metrics.len(), 2);
        let steps = &metrics[0];
        assert_eq!(steps["metric"], "steps");
        assert_eq!(steps["total"], 8432);
        assert_eq!(steps["unit"], "steps");
        let hr = &metrics[1];
        assert_eq!(hr["metric"], "heart_rate");
        assert_eq!(hr["avg"], 72.5);
        assert_eq!(hr["max"], 121.0);
        assert_eq!(hr["min"], 58.0);
        // Deprecation note is always attached.
        assert!(out["note"].as_str().unwrap().contains("deprecated"));
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("POST /users/me/dataset:aggregate "), "request: {raw}");
        assert!(raw.contains("com.google.step_count.delta"), "body: {raw}");
        assert!(raw.contains("com.google.heart_rate.bpm"), "body: {raw}");
    }

    #[tokio::test]
    async fn metrics_rejects_bad_metric_and_bad_date() {
        let _home = save_test_token().await;
        let c = HealthConnector::with_endpoints("http://127.0.0.1:9", "http://127.0.0.1:9");
        let tool = MetricsTool::new(c);
        let err = tool
            .execute(serde_json::json!({"metrics": "teleportation"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "got: {err}");
        let err = tool
            .execute(serde_json::json!({"date": "09-10-2026"}))
            .await
            .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)), "got: {err}");
    }

    #[tokio::test]
    async fn sleep_parses_stages() {
        let _home = save_test_token().await;
        let (api_base, handle) = mock_seq(vec![(200, AGG_SLEEP_FIXTURE)]).await;
        let c = HealthConnector::with_endpoints(&api_base, "http://127.0.0.1:9");
        let tool = SleepTool::new(c);
        let out = tool
            .execute(serde_json::json!({"start_date": "2026-10-08", "end_date": "2026-10-09"}))
            .await
            .unwrap();
        assert_eq!(out["minutes_by_stage"]["light"], 60.0);
        assert_eq!(out["minutes_by_stage"]["deep"], 120.0);
        assert_eq!(out["segments"].as_array().unwrap().len(), 2);
        assert_eq!(out["segments"][0]["stage"], "light");
        assert!(out["note"].as_str().unwrap().contains("deprecated"));
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("com.google.sleep.segment"), "body: {raw}");
    }

    #[tokio::test]
    async fn workouts_lists_sessions() {
        let _home = save_test_token().await;
        let (api_base, handle) = mock_seq(vec![(200, SESSIONS_FIXTURE)]).await;
        let c = HealthConnector::with_endpoints(&api_base, "http://127.0.0.1:9");
        let tool = WorkoutsTool::new(c);
        let out = tool
            .execute(serde_json::json!({"start_date": "2026-10-01", "end_date": "2026-10-09"}))
            .await
            .unwrap();
        assert_eq!(out["count"], 2);
        assert_eq!(out["sessions"][0]["activity"], "running");
        assert_eq!(out["sessions"][0]["duration_min"], 40);
        assert_eq!(out["sessions"][1]["activity"], "walking");
        assert_eq!(out["sessions"][1]["duration_min"], 30);
        assert!(out["sessions"][0]["start"].as_str().unwrap().starts_with("2025-"));
        let raw = &handle.await.unwrap()[0];
        assert!(raw.contains("GET /users/me/sessions?"), "request: {raw}");
        assert!(raw.contains("startTime="), "request: {raw}");
    }

    #[tokio::test]
    async fn reactive_refresh_on_401() {
        let _home = save_test_token().await;
        let (token_base, _h) = mock_seq(vec![(200, TOKEN_FIXTURE)]).await;
        let (api_base, api_h) = mock_seq(vec![
            (401, r#"{"error":{"message":"expired"}}"#),
            (200, AGG_STEPS_FIXTURE),
        ])
        .await;
        let c = HealthConnector::with_endpoints(
            &api_base,
            &format!("{token_base}/token"),
        );
        let tool = MetricsTool::new(c);
        let out = tool.execute(serde_json::json!({})).await.unwrap();
        assert_eq!(out["metrics"].as_array().unwrap().len(), 2);
        let raws = api_h.await.unwrap();
        assert_eq!(raws.len(), 2);
        assert!(
            raws[1].to_lowercase().contains("authorization: bearer fit-access-2"),
            "retry auth: {}",
            raws[1]
        );
    }

    #[tokio::test]
    async fn deprecation_gone_is_honest() {
        let _home = save_test_token().await;
        let (api_base, _h) = mock_seq(vec![(
            410,
            r#"{"error":{"message":"The Fit API is no longer available"}}"#,
        )])
        .await;
        let c = HealthConnector::with_endpoints(&api_base, "http://127.0.0.1:9");
        let err = MetricsTool::new(c)
            .execute(serde_json::json!({}))
            .await
            .unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("deprecated"), "got: {msg}");
        assert!(msg.contains("410"), "got: {msg}");
    }

    #[test]
    fn pure_helpers() {
        assert_eq!(sleep_stage(4), "light");
        assert_eq!(sleep_stage(6), "rem");
        assert_eq!(sleep_stage(99), "unknown");
        assert_eq!(activity_name(8), "running");
        assert_eq!(activity_name(999), "activity_999");

        assert!(metric_def("steps").is_some());
        assert!(metric_def("nope").is_none());

        let body = build_aggregate_body(
            &[metric_def("steps").unwrap()],
            1000,
            2000,
        );
        assert_eq!(body["startTimeMillis"], 1000);
        assert_eq!(body["aggregateBy"][0]["dataTypeName"], "com.google.step_count.delta");

        let agg: Value = serde_json::from_str(AGG_STEPS_FIXTURE).unwrap();
        let s = summarize_metric(metric_def("steps").unwrap(), &agg);
        assert_eq!(s["total"], 8432);
        assert_eq!(s["data_points"], 1);

        let sleep: Value = serde_json::from_str(AGG_SLEEP_FIXTURE).unwrap();
        let s = summarize_sleep(&sleep);
        assert_eq!(s["minutes_by_stage"]["deep"], 120.0);

        let sess: Value = serde_json::from_str(SESSIONS_FIXTURE).unwrap();
        let s = summarize_sessions(&sess);
        assert_eq!(s.len(), 2);
        assert_eq!(s[0]["activity"], "running");

        assert!(validate_date("2026-10-17", "d").is_ok());
        assert!(validate_date("x", "d").is_err());

        let (start, end) = day_range_millis(chrono::NaiveDate::from_ymd_opt(2026, 10, 17).unwrap());
        assert_eq!(end - start, 86_400_000);

        assert!(rfc3339_ms(1760000000000).starts_with("2025-10-09"));
        assert_eq!(ms_field(&serde_json::json!({"a": "42"}), "a"), 42);

        // Token-response parsing keeps an old refresh token when omitted.
        let old = ConnectorToken {
            access_token: "o".to_string(),
            refresh_token: Some("old-rt".to_string()),
            expires_at: None,
            extra: serde_json::json!({}),
        };
        let tok = parse_token_response(
            &serde_json::json!({"access_token": "n", "expires_in": 100}),
            Some(&old),
        )
        .unwrap();
        assert_eq!(tok.refresh_token.as_deref(), Some("old-rt"));
        assert!(parse_token_response(&serde_json::json!({}), None).is_err());
    }
}
